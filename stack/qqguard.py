#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 掉线看护：断线自动恢复 + 记录 + 恢复后如实告知。

背景（2026-10-24 实际发生）：
    07:25 QQ 服务端下发 `[KickedOffLine] 你的账号当前登录已失效`，NapCat 内部
    重启了 worker —— 但**内部重启不走 entrypoint，丢掉了 `-q <QQ号>`**，于是
    退回扫码模式。结果从 07:27 到 19:18 整整 12 小时，机器人一句话都回不了，
    没有任何人知道。

两个动作：
    1. 看到**新的** KickedOffLine 事件 → `docker restart napcat`
       容器重启才会走 entrypoint，带上 `-q`，有机会用保存的 token 快速登录。
       token 还有效就完全自愈，不用人扫。
    2. 恢复后记一笔，并注入一条消息让她如实告诉主人"刚才掉线多久"。

为什么不能靠 QQ 消息告警：
    QQ 掉线时出站通道也断了，发不出去。所以告警写文件 + 恢复后补一句。

为什么"只认新事件"：
    掉线后 NapCat 会一直停在扫码界面等人工。如果每轮都看到旧的 KickedOffLine
    就重启，会把主人正在扫的二维码刷掉，永远扫不上。所以只用 checkpoint
    之后的新事件做判断，扫码期间保持安静。
"""
import json
import os
import re
import subprocess
import time
import urllib.request

BASE = "/opt/dsh"
STATE = os.path.join(BASE, "memory/qqguard.json")
INCIDENTS = os.path.join(BASE, "memory/qq-incidents.log")
INJECT = os.path.join(BASE, "media/onebot/qq-inject.jsonl")
CONF = os.path.join(BASE, "profiles/web/cordis.patch.yml")
CONTAINER = "napcat"
ONEBOB_CFG = "/opt/qqbot/data/config/onebot11_3600398389.json"

MAX_ATTEMPTS_H = 3        # 一小时内最多重启几次（防止和风控死磕）
MIN_OFFLINE_S = 120       # 断线多久才动手（避免网络抖动误伤）
RECOVER_NOTICE_AFTER = 300  # 掉线超过 5 分钟才值得告诉她"刚才掉线了"

# 自动重启容器：默认关闭。
# 2026-10-25 实测：这台机器每次重启都要求重新扫码（整晚 4 次，0 次成功），
# 重启不但救不回来，还会把人工正在扫的那张二维码刷掉。所以只告警、不动容器。
# 换到「重启能快速登录」的环境时，设环境变量 QQG_AUTO_RESTART=1 打开。
AUTO_RESTART = os.environ.get("QQG_AUTO_RESTART", "0").lower() not in ("0", "", "false", "no")

# NapCat 日志里出现这些，说明 QQ 侧已不在登录状态（而 WS 可能仍连着）
LOGOUT_MARKERS = (
    "登录态已失效",
    "用户身份已失效",
    "请扫描下面的二维码",
    "需要验证码",
    "Login Error,ErrType",
)
QR_FILE = "/app/napcat/cache/qrcode.png"
QR_FRESH_S = 600          # 二维码 10 分钟内算"待扫"


def log(msg):
    line = "%s %s" % (time.strftime("%F %T"), msg)
    try:
        with open(INCIDENTS, "a", encoding="utf-8") as f:
            f.write(line + chr(10))
    except Exception:
        pass
    print(line)


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {"checkpoint": int(time.time()), "attempts": [],
            "offline_since": 0, "reported_for": 0,
            "logout_since": 0, "alerted_logout_for": 0}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def _docker(*args, timeout=60):
    try:
        r = subprocess.run(["docker"] + list(args), capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    except Exception as e:
        return 1, "", str(e)


def _napcat_conf():
    """读出 NapCat 的 HTTP 端点：容器 IP + 端口 + token。拿不到就返回 None。

    为什么直连容器 IP 而不是 127.0.0.1：HTTP 服务在容器内 0.0.0.0:3000，
    容器网络是 docker 内网（qqbot_default），宿主机可达，公网碰不到。
    """
    try:
        ip = ""
        r = subprocess.run(["docker", "inspect", CONTAINER, "--format",
                            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
                           capture_output=True, timeout=20)
        if r.returncode == 0:
            ip = r.stdout.decode("utf-8", "replace").strip()
        if not ip:
            return None
        cfg = json.load(open(ONEBOB_CFG, encoding="utf-8"))
        servers = (cfg.get("network") or {}).get("httpServers") or []
        for s in servers:
            if s.get("enable") and s.get("port"):
                return {"ip": ip, "port": int(s["port"]),
                        "token": s.get("token") or ""}
    except Exception as e:
        log("读 NapCat HTTP 配置失败: %s" % e)
    return None


def send_direct(text, user_id=None):
    """直接用 OneBot HTTP 接口发私聊。返回 (成功?, 说明)。

    这是 QQ 掉线/退登时唯一可靠的通道——不经过模型、不经过插件自动回发。
    """
    conf = _napcat_conf()
    if not conf:
        return False, "拿不到 NapCat HTTP 配置（端口没开？）"
    uid = user_id
    if not uid:
        _, uid = _owner_and_bot()
    if not uid:
        return False, "读不到主人 QQ 号"
    url = "http://%s:%d/send_private_msg" % (conf["ip"], conf["port"])
    body = json.dumps({"user_id": int(uid), "message": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if conf["token"]:
        req.add_header("Authorization", "Bearer " + conf["token"])
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("status") == "ok" and d.get("retcode") == 0:
            return True, "已发到 %s（message_id %s）" % (
                uid, (d.get("data") or {}).get("message_id"))
        return False, "接口返回异常: %s" % json.dumps(d, ensure_ascii=False)[:160]
    except Exception as e:
        return False, "发送失败: %s" % e


def online_confirmed():
    """用 OneBot HTTP 问一句"你到底在不在线"，比看 WS 和日志都准。"""
    conf = _napcat_conf()
    if not conf:
        return None
    url = "http://%s:%d/get_status" % (conf["ip"], conf["port"])
    req = urllib.request.Request(url)
    if conf["token"]:
        req.add_header("Authorization", "Bearer " + conf["token"])
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        data = d.get("data") or {}
        return bool(data.get("online")) and bool(data.get("good"))
    except Exception:
        return None


def onebot_connected():
    """OneBot 反向 WS 是否还在（NapCat 连到宿主机 8643）。"""
    try:
        r = subprocess.run(["ss", "-tn"], capture_output=True, timeout=20)
        out = r.stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            if ":8643" in line and "ESTAB" in line:
                return True
        return False
    except Exception:
        return False


def kicked_since(ts):
    """ts 之后有没有新的 KickedOffLine。"""
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
    rc, out, _ = _docker("logs", "--since", since, CONTAINER)
    for line in out.splitlines():
        if "KickedOffLine" in line or "登录已失效" in line:
            return True
    return False


def qr_ready():
    """容器里当前有没有可供人工扫的二维码。"""
    rc, out, _ = _docker("exec", CONTAINER, "ls", "-1", "/app/napcat/cache/qrcode.png",
                         timeout=30)
    return rc == 0


def classify_log(lines):
    """从日志行里判断 QQ 侧是不是已经没登录了。返回命中的关键字，否则 None。

    抽成纯函数是为了能单测：NapCat 日志格式一变，这里要能第一时间红。
    """
    for line in lines:
        for marker in LOGOUT_MARKERS:
            if marker in line:
                return marker
    return None


def logout_since(ts):
    """ts 之后有没有"登录态失效"类的日志。返回命中的关键字或 None。"""
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
    rc, out, _ = _docker("logs", "--since", since, CONTAINER)
    if rc != 0:
        return None
    return classify_log(out.splitlines())


def qr_mtime():
    """容器里二维码文件的修改时间（拿不到返回 0）。"""
    rc, out, _ = _docker("exec", CONTAINER, "stat", "-c", "%Y", QR_FILE, timeout=20)
    if rc != 0:
        return 0
    try:
        return int(out.strip())
    except Exception:
        return 0


def qr_fresh(max_age=QR_FRESH_S):
    """二维码是不是刚生成的（说明此刻正等人扫）。"""
    m = qr_mtime()
    return m > 0 and (time.time() - m) <= max_age


def recent_relevant_lines(ts, limit=8):
    """取最近的失效相关日志行，给告警邮件做证据。"""
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
    rc, out, _ = _docker("logs", "--since", since, CONTAINER)
    if rc != 0:
        return []
    hits = []
    for line in out.splitlines():
        if classify_log([line]):
            hits.append(line)
    return hits[-limit:]



def _pull_qr():
    """把容器里的二维码拷到宿主机，返回路径。"""
    dst = os.path.join(BASE, "memory/last_qr.png")
    rc, _, _ = _docker("cp", "%s:/app/napcat/cache/qrcode.png" % CONTAINER, dst, timeout=60)
    return dst if rc == 0 and os.path.exists(dst) else None


def _qr_url():
    """从容器日志里捞最新的二维码解码 URL（手机上可以直接打开跳转 QQ）。"""
    rc, out, _ = _docker("logs", "--tail", "600", CONTAINER, timeout=60)
    url = ""
    for line in out.splitlines():
        if "二维码解码URL" in line:
            url = line.split("二维码解码URL:")[-1].strip()
    return url


def _mail_alert(offline_since, attempts, kind="offline", reason=""):
    """需要人工介入时发一封邮件。这是 QQ 发不出去时唯一还通的外网通道。

    kind="offline"  OneBot 断开（重启没救回来）
    kind="logout"   WS 连着但 QQ 已退登（假在线，1 个基于 WS 的看护都发现不了）
    """
    try:
        import mailer
    except Exception as e:
        return False, "mailer 导入失败: %s" % e
    mins = max(1, (int(time.time()) - offline_since) // 60)
    qr = _pull_qr()
    url = _qr_url()

    if kind == "logout":
        subject = "【鲸鲸】QQ 登录态失效：她现在回不了话"
        head = [
            "OneBot 的 WebSocket 还连着，但 QQ 侧已经不在登录状态。",
            "也就是说：群里 @ 她、私聊她都不会有反应，",
            "而「服务还在线」这类看板看上去是正常的。",
            "",
            "检测到：%s" % (reason or "登录态失效"),
            "",
        ]
        tail_note = "这个状态不会自愈。看护没去重启容器，" \
                    "因为重启通常只是换一张新二维码，" \
                    "还会把你正在扫的那张刷掉。"
    else:
        subject = "【鲸鲸掉线】需要重新扫码登录"
        head = ["鲸鲸的 QQ 掉线了，需要你扫一次码。", ""]
        tail_note = "自动重启已经试过还是不行，说明保存的 token 已失效。"

    lines = head + [
        "发现时间：%s" % time.strftime("%F %T", time.localtime(offline_since)),
        "已持续：%d 分钟" % mins,
        "自动重启尝试：%d 次" % attempts,
        "",
    ]
    if url:
        lines += ["在手机上直接打开下面这个链接，会跳到 QQ 授权：",
                  url, ""]
    if qr:
        lines += ["附件是二维码图片，可以扫码。", ""]
    lines += [
        tail_note,
        "",
        "服务器上也可以手动看状态：",
        "  python3 /opt/dsh/qqguard.py --status",
        "",
        "—— 这是自动发出的告警，不需要回复。",
    ]
    ok, why = mailer.send(subject, chr(10).join(lines), [qr] if qr else [])
    log("邮件告警: %s" % why)
    return ok, why


def _owner_and_bot():
    bot = owner = None
    try:
        txt = open(CONF, encoding="utf-8").read()
        m = re.search(r'botQQ:\s*"?(\d{5,15})', txt)
        if m:
            bot = int(m.group(1))
        m = re.search(r'allowFrom:[ \t]*\r?\n((?:[ \t]*-[^\n]*\n){1,6})', txt)
        if m:
            mm = re.search(r'(\d{5,15})', m.group(1))
            if mm:
                owner = int(mm.group(1))
    except Exception:
        pass
    return bot, owner


def inject(text):
    bot, owner = _owner_and_bot()
    if not bot or not owner:
        log("恢复通知没发：读不到 botQQ/主人QQ")
        return False
    now = int(time.time())
    ev = {"post_type": "message", "message_type": "private", "sub_type": "friend",
          "self_id": bot, "user_id": owner, "time": now, "message_id": now,
          "raw_message": text,
          "message": [{"type": "text", "data": {"text": text}}],
          "sender": {"user_id": owner, "nickname": "主人"}}
    try:
        os.makedirs(os.path.dirname(INJECT), exist_ok=True)
        with open(INJECT, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + chr(10))
        return True
    except Exception as e:
        log("写注入失败: %s" % e)
        return False


def check(apply_=True):
    st = load_state()
    now = int(time.time())
    conn = onebot_connected()

    # ---------- 连着，但 QQ 可能已经退登（假在线）----------
    if conn:
        window = int(st.get("logout_since") or st.get("checkpoint") or 0)
        # 先直接问 NapCat 在不在线：这是最准的判据。
        # 二维码文件会残留在盘上，只看它会把"已登录"误判成"等人扫"。
        live = online_confirmed()
        mark = logout_since(window) if live is not True else None
        waiting_scan = qr_fresh() if live is not True else False
        if mark or waiting_scan:
            reason = mark or "二维码已生成，等人扫"
            first = not st.get("logout_since")
            if first:
                st["logout_since"] = now
                log("WS 连着但 QQ 已退登（%s）——这种假在线原来的看护发现不了" % reason)
            mins = max(1, (now - int(st["logout_since"])) // 60)
            if st.get("alerted_logout_for") != st["logout_since"]:
                if not apply_:
                    log("dry-run：本应发登录失效告警（%s）" % reason)
                else:
                    ok, why = _mail_alert(int(st["logout_since"]), 0,
                                          kind="logout", reason=reason)
                    if ok:
                        st["alerted_logout_for"] = st["logout_since"]
                    else:
                        log("登录失效告警未发出：%s" % why)
            save_state(st)
            return "连着但登录态失效 %d 分钟（%s）" % (mins, reason)

        if st.get("logout_since"):
            log("登录态已恢复（之前失效 %d 分钟）"
                % max(1, (now - int(st["logout_since"])) // 60))
        st["logout_since"] = 0
        st["alerted_logout_for"] = 0

    # ---------- 已恢复 ----------
    if conn:
        down = 0
        if st.get("offline_since"):
            down = now - int(st["offline_since"])
            if down >= RECOVER_NOTICE_AFTER and st.get("reported_for") != st["offline_since"]:
                mins = max(1, down // 60)
                text = ("主人，我刚才掉线了差不多 %d 分钟，期间的消息可能没回。现在")
                text = (text % mins) + "已经连回来了。"
                # 优先直连发送：不经过她，也不依赖插件自动回发。
                # （2026-10-24 教训：注入通道的消息不会发到 QQ，日志却写"已告知"）
                done = False
                if apply_:
                    ok, why = send_direct(text)
                    if ok:
                        log("已恢复，掉线 %d 分钟，已直接发到 QQ（%s）" % (mins, why))
                        done = True
                    else:
                        log("直连发送失败（%s），改用注入通道" % why)
                        if inject(text):
                            log("已恢复，掉线 %d 分钟，已注入（不保证真的发到了 QQ）" % mins)
                            done = True
                if done:
                    st["reported_for"] = st["offline_since"]
                else:
                    log("已恢复，掉线 %d 分钟，但通知未发出" % mins)
            else:
                log("已恢复，掉线 %d 秒" % down)
        st["offline_since"] = 0
        st["attempts"] = []
        st["checkpoint"] = now
        save_state(st)
        return "在线（已检查）"

    # ---------- 离线 ----------
    if not st.get("offline_since"):
        st["offline_since"] = now
        log("检测到 OneBot 断线")

    # 只对 checkpoint 之后的新踢线事件动手，避免刷掉人工正在扫的码
    fresh_kick = kicked_since(int(st.get("checkpoint") or 0))
    # 清理一小时前的尝试记录
    st["attempts"] = [a for a in (st.get("attempts") or []) if now - a < 3600]

    if fresh_kick and len(st["attempts"]) < MAX_ATTEMPTS_H:
        if not apply_:
            return "离线，检测到新踢线（dry-run，不重启）"
        if not AUTO_RESTART:
            # 重启救不回来，只会把人工正在扫的码刷掉 —— 只记录，不动容器
            log("检测到 KickedOffLine；AUTO_RESTART=false，不重启容器，只发告警")
            st["attempts"].append(now)
            save_state(st)
            return "离线，已抑制自动重启（等人工扫码）"
        log("检测到新的 KickedOffLine，重启容器尝试快速登录（第 %d 次）"
            % (len(st["attempts"]) + 1))
        rc, _, err = _docker("restart", CONTAINER, timeout=180)
        st["attempts"].append(now)
        st["checkpoint"] = now
        save_state(st)
        if rc != 0:
            log("重启失败: %s" % err[:120])
            return "离线，重启失败"
        time.sleep(12)
        if onebot_connected():
            log("重启后已自动恢复（快速登录成功，不需要扫码）")
            return "重启后已恢复"
        if qr_ready():
            log("重启后仍需扫码，二维码已生成，等待人工")
            return "仍需扫码（二维码已备好）"
        return "重启后仍未上线"

    # ---------- 需要人工扫码 → 发邮件 ----------
    # QQ 掉线时出站消息发不出去，邮箱是唯一还通的路。
    offline_s = now - int(st["offline_since"])
    if (apply_ and offline_s >= MIN_OFFLINE_S and qr_ready()
            and st.get("mailed_for") != st["offline_since"]):
        ok, why = _mail_alert(int(st["offline_since"]), len(st.get("attempts") or []))
        if ok:
            st["mailed_for"] = st["offline_since"]
            save_state(st)
            return "离线，已发邮件告警（含二维码）"
        # 没配置邮箱是正常状态，不刷日志
        if "未配置" not in why:
            log("邮件告警没发出去: %s" % why)

    if len(st["attempts"]) >= MAX_ATTEMPTS_H:
        return "离线，本小时已重启 %d 次，暂停自动恢复，等人工扫码" % len(st["attempts"])

    save_state(st)
    return "离线（%d 秒），等待新踢线事件" % (now - int(st["offline_since"]))


def status():
    st = load_state()
    conn = onebot_connected()
    lines = ["OneBot: %s" % ("已连接" if conn else "断开")]
    if not conn and st.get("offline_since"):
        lines.append("已断开: %d 分钟" % max(0, (int(time.time()) - int(st["offline_since"])) // 60))
    lines.append("本小时重启次数: %d / %d" % (len(st.get("attempts") or []), MAX_ATTEMPTS_H))
    lines.append("二维码可用: %s" % ("是" if qr_ready() else "否"))
    try:
        tail = open(INCIDENTS, encoding="utf-8").read().strip().splitlines()[-5:]
    except Exception:
        tail = []
    if tail:
        lines.append("最近事件:")
        lines.extend("  " + t for t in tail)
    return chr(10).join(lines)


if __name__ == "__main__":
    import sys
    if "--status" in sys.argv:
        print(status())
    else:
        print(check(apply_="--dry-run" not in sys.argv))
