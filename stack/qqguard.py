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

BASE = "/opt/dsh"
STATE = os.path.join(BASE, "memory/qqguard.json")
INCIDENTS = os.path.join(BASE, "memory/qq-incidents.log")
INJECT = os.path.join(BASE, "media/onebot/qq-inject.jsonl")
CONF = os.path.join(BASE, "profiles/web/cordis.patch.yml")
CONTAINER = "napcat"

MAX_ATTEMPTS_H = 3        # 一小时内最多重启几次（防止和风控死磕）
MIN_OFFLINE_S = 120       # 断线多久才动手（避免网络抖动误伤）
RECOVER_NOTICE_AFTER = 300  # 掉线超过 5 分钟才值得告诉她"刚才掉线了"


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
            "offline_since": 0, "reported_for": 0}


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

    # ---------- 已恢复 ----------
    if conn:
        down = 0
        if st.get("offline_since"):
            down = now - int(st["offline_since"])
            if down >= RECOVER_NOTICE_AFTER and st.get("reported_for") != st["offline_since"]:
                mins = max(1, down // 60)
                text = ("主人，我刚才掉线了差不多 %d 分钟，期间的消息可能没回。现在")
                text = (text % mins) + "已经连回来了。"
                if apply_ and inject(text):
                    log("已恢复，掉线 %d 分钟，已告知主人" % mins)
                    st["reported_for"] = st["offline_since"]
                else:
                    log("已恢复，掉线 %d 分钟（未发通知）" % mins)
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
