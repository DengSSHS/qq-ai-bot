#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站浏览时间窗：什么时候她可以自己去刷，什么时候不行。

三条规则，硬执行（不是靠人设文字）：

  1. 低谷时段（可配，默认 23:30-08:00）→ 允许自己刷，但受每日配额限制
  2. 峰值时段（其余时间）→ 不许，除非有手动授权
  3. 手动授权 → 他说「给你玩 30 分钟」，开一个到点自动关的窗口

怎么"硬"执行：不跟她讲道理，直接开关 Tulpa 的 MCP。
    MCP 关掉时她的 bili 工具直接不可用（表现为"B 站没开"），
    比在 prompt 里写"不要去看"可靠得多——后者已经证明拦不住。

为什么手动授权要有上限：
    授权动作在对话里由她发起（他说一句，她调 bili_open）。
    这理论上能被她自己骗过去，所以加两道锁：
      每天最多 2 次手动开、单次最长 60 分钟，并且全部记账。
    要彻底防住只能由主人在服务器上敲命令——那就失去了"对话里说一句"的便利。
    取舍记在这里：**时间窗是硬的，手动授权是软的但可审计、有上限。**

用法：
    python3 bili_window.py --status
    python3 bili_window.py --open 30 --by "主人说要玩"
    python3 bili_window.py --close
    python3 bili_window.py --apply        # 定时器调：按状态开/关 MCP
"""
import io
import json
import os
import subprocess
import sys
import time

BASE = "/opt/dsh"
STATE = os.path.join(BASE, "memory/bili-window.json")
LOG = os.path.join(BASE, "memory/bili-window.log")
TULPA = "/opt/tulpa"

# 低谷时段：跨零点写成 ["18:00", "08:00"]（2026-10-25 从 23:30 提前到 18:00）
DEFAULTS = {
    "allowed_windows": [["18:00", "08:00"]],
    "daily_auto_opens": 6,      # 低谷期每天最多自动开几次
    "night_max_auto": 2,       # 其中 00:00-08:00 最多几次（给晚上留额度）      # 低谷期每天最多自动开几次（每次都受下面时长限制）
    "auto_minutes": 25,         # 自动开窗时长
    "manual_per_day": 2,        # 每天最多手动开几次
    "manual_max_minutes": 60,
    # 累积随机：低谷期每多待一小时，"叫醒她"的概率抬高一档，封顶 ramp_cap
    "ramp_per_hour": 0.25,
    "ramp_cap": 0.9,
}


def log(msg):
    line = "%s %s" % (time.strftime("%F %T"), msg)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write(line + chr(10))
    except Exception:
        pass
    print(line)


def conf():
    try:
        d = json.load(io.open(os.path.join(BASE, "bili_window.conf.json"), encoding="utf-8"))
        if isinstance(d, dict):
            out = dict(DEFAULTS)
            out.update(d)
            return out
    except Exception:
        pass
    return dict(DEFAULTS)


def load():
    try:
        d = json.load(io.open(STATE, encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {"day": "", "auto_opens": 0, "manual_opens": 0,
            "window_until": 0, "opened_by": ""}


def save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_low_valley(cfg, now=None):
    now = now or time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    for a, b in cfg["allowed_windows"]:
        s, e = _hm(a), _hm(b)
        if s <= e:                      # 同日
            if s <= cur < e:
                return True
        else:                            # 跨零点
            if cur >= s or cur < e:
                return True
    return False


def reset_day(st):
    today = time.strftime("%F")
    if st.get("day") != today:
        st["day"] = today
        st["auto_opens"] = 0
        st["manual_opens"] = 0
    st["night_opens"] = 0
    return st


def allowed(cfg=None, st=None):
    """返回 (能不能刷, 原因, 剩余秒数)。"""
    cfg = cfg or conf()
    st = reset_day(st or load())
    now = time.time()
    if st.get("window_until", 0) > now:
        return True, "手动/自动窗口进行中（%s）" % (st.get("opened_by") or "?"), \
               int(st["window_until"] - now)
    if not in_low_valley(cfg):
        return False, "现在是高峰时段，不开", 0
    if st.get("auto_opens", 0) >= cfg["daily_auto_opens"]:
        return False, "低谷期今天的自动额度用完了（%d/%d）" % (
            st["auto_opens"], cfg["daily_auto_opens"]), 0
    return True, "低谷时段", cfg["auto_minutes"] * 60


def open_window(minutes, by="", manual=True):
    cfg = conf()
    st = reset_day(load())
    minutes = max(1, min(int(minutes), cfg["manual_max_minutes"]))
    if manual and st.get("manual_opens", 0) >= cfg["manual_per_day"]:
        return False, "今天手动开窗次数用完了（%d/%d）" % (
            st["manual_opens"], cfg["manual_per_day"])
    st["window_until"] = int(time.time() + minutes * 60)
    st["opened_by"] = by or ("manual" if manual else "auto")
    if manual:
        st["manual_opens"] = st.get("manual_opens", 0) + 1
    else:
        st["auto_opens"] = st.get("auto_opens", 0) + 1
    if int(time.strftime("%H")) < 8:
        st["night_opens"] = int(st.get("night_opens", 0)) + 1
    save(st)
    log("window opened %d min by=%s (%s)" % (minutes, st["opened_by"],
                                             "manual" if manual else "auto"))
    return True, "开了 %d 分钟（%s）" % (minutes, st["opened_by"])


def close_window(reason=""):
    st = reset_day(load())
    st["window_until"] = 0
    st["opened_by"] = ""
    save(st)
    log("window closed %s" % reason)
    return True, "已关闭"


# ---------------------------------------------------------------- 开关 Tulpa MCP
def _tulpa_api():
    base = json.load(io.open(os.path.join(TULPA, ".tmp/cli-endpoint.json"),
                             encoding="utf-8"))["url"]
    tok = io.open(os.path.join(TULPA, "data/terminal-admin.token"),
                  encoding="utf-8").read().strip()
    import urllib.request
    return base, tok, urllib.request


def mcp_enabled():
    base, tok, urllib = _tulpa_api()
    req = urllib.Request(base + "/api/mcp",
                         headers={"Authorization": "Bearer " + tok,
                                  "x-chatweave-ui": "1"})
    d = json.loads(urllib.urlopen(req, timeout=15).read().decode())
    return bool(d.get("enabled"))


def set_mcp(enabled):
    base, tok, urllib = _tulpa_api()
    body = json.dumps({"enabled": bool(enabled), "port": 18777}).encode()
    req = urllib.Request(base + "/api/mcp", data=body, method="PUT",
                         headers={"Authorization": "Bearer " + tok,
                                  "x-chatweave-ui": "1",
                                  "Content-Type": "application/json"})
    d = json.loads(urllib.urlopen(req, timeout=25).read().decode())
    return bool(d.get("enabled"))


def _notify_ids():
    """从插件配置里读 botQQ 和主人 QQ（注入用）。不用正则，避免转义问题。"""
    try:
        txt = io.open(os.path.join(BASE, "profiles/web/cordis.patch.yml"), encoding="utf-8").read()
        lines = txt.splitlines()
        bot = own = None
        for line in lines:
            t = line.strip()
            if bot is None and t.startswith("botQQ:"):
                d = "".join(ch for ch in t.split(":", 1)[1] if ch.isdigit())
                if d:
                    bot = int(d)
        for i, line in enumerate(lines):
            if line.strip().startswith("allowFrom:"):
                for nxt in lines[i + 1:i + 7]:
                    d = "".join(ch for ch in nxt if ch.isdigit())
                    if len(d) >= 5:
                        own = int(d)
                        break
                break
        return bot, own
    except Exception:
        return None, None


def _nudge(text):
    """往注入队列塞一条「主人」消息，等于叫醒她去刷。"""
    bot, own = _notify_ids()
    if not bot or not own:
        return False
    ev = {"post_type": "message", "message_type": "private", "sub_type": "friend",
          "self_id": bot, "user_id": own, "time": int(time.time()),
          "message_id": int(time.time()) * 10 + 3,
          "raw_message": text,
          "message": [{"type": "text", "data": {"text": text}}],
          "sender": {"user_id": own, "nickname": "主人"}}
    try:
        path = os.path.join(BASE, "media/onebot/qq-inject.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + chr(10))
        return True
    except Exception:
        return False


def _roll(rid, day):
    """同一小时内只掷一次（按小时桶哈希），换小时才重掷。"""
    import hashlib
    bucket = "%s-%s" % (day, time.strftime("%Y-%m-%dT%H"))
    h = int(hashlib.sha256((bucket + rid).encode()).hexdigest()[:8], 16)
    return (h % 1000) / 1000.0


def apply():
    """低谷期按累积随机叫醒她；不开关 Tulpa 的 MCP。

    为什么不开关 MCP（2026-10-24 实测）：dsh 的 MCP 连接是启动时建立的，
    关掉再开不会重连，工具名还在但调用全失败。所以 Tulpa 一直开着，
    「能不能刷」由 bili_hub 代理按时间窗判断，这里只管"什么时候叫她"。
    """
    cfg = conf()
    st = reset_day(load())
    if not in_low_valley(cfg):
        return "高峰时段，不叫"
    if st.get("auto_opens", 0) >= cfg["daily_auto_opens"]:
        return "今天自动叫醒额度用完了（%d/%d）" % (st["auto_opens"], cfg["daily_auto_opens"])
    # 凌晨别把额度吃光：00:00-08:00 最多叫 cfg["night_max_auto"] 次，
    # 否则晚上（窗口真正有价值的时段）反而不能刷。
    night_max = int(cfg.get("night_max_auto", 0) or 0)
    if night_max > 0 and int(time.strftime("%H")) < 8:
        night_used = int(st.get("night_opens", 0))
        if night_used >= night_max:
            return "凌晨叫醒已用 %d/%d（给晚上留额度）" % (night_used, night_max)
    last = st.get("last_auto_ts", 0)
    hours = max(0.0, (time.time() - last) / 3600.0) if last else 1.0
    prob = min(float(cfg["ramp_cap"]), float(cfg["ramp_per_hour"]) * (1 + hours))
    roll = _roll("bili_night", time.strftime("%F"))
    if roll >= prob:
        return "低谷期，掷出 %.2f >= %.2f，这小时先不叫" % (roll, prob)
    ok = _nudge("（现在是你的时间）去 B 站刷刷吧，看到有意思的记得在评论区 @ 我。")
    st["auto_opens"] = st.get("auto_opens", 0) + 1
    if int(time.strftime("%H")) < 8:
        st["night_opens"] = int(st.get("night_opens", 0)) + 1
    st["last_auto_ts"] = int(time.time())
    save(st)
    log("nudge sent (roll=%.2f prob=%.2f, %d/%d)" % (
        roll, prob, st["auto_opens"], cfg["daily_auto_opens"]))
    return "掷出 %.2f < %.2f，已叫她（%d/%d）" % (
        roll, prob, st["auto_opens"], cfg["daily_auto_opens"])


def status():
    cfg = conf()
    st = reset_day(load())
    ok, why, left = allowed(cfg, st)
    lines = ["现在：%s" % time.strftime("%F %H:%M"),
             "能不能刷：%s（%s）" % ("可以" if ok else "不行", why)]
    if left:
        lines.append("本次窗口剩余：%d 分钟" % (left // 60))
    lines.append("低谷时段：%s" % ", ".join("%s-%s" % (a, b) for a, b in cfg["allowed_windows"]))
    lines.append("今日：自动开 %d/%d，手动开 %d/%d，单次手动上限 %d 分钟"
                 % (st.get("auto_opens", 0), cfg["daily_auto_opens"],
                    st.get("manual_opens", 0), cfg["manual_per_day"],
                    cfg["manual_max_minutes"]))
    try:
        lines.append("Tulpa MCP：%s" % ("开" if mcp_enabled() else "关"))
    except Exception:
        lines.append("Tulpa MCP：查不到")
    return chr(10).join(lines)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or "--status" in a:
        print(status())
    elif "--apply" in a:
        print(apply())
    elif "--open" in a:
        i = a.index("--open")
        mins = int(a[i + 1]) if i + 1 < len(a) else 30
        by = ""
        if "--by" in a:
            j = a.index("--by")
            by = a[j + 1] if j + 1 < len(a) else ""
        ok, msg = open_window(mins, by, manual=True)
        print(msg)
    elif "--close" in a:
        print(close_window("manual")[1])
    else:
        print(status())
