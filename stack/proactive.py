#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""她的作息调度器（移植 dsh-proactive 的核心机制，不安装那个插件）

移植的五个机制，都来自对 dsh-proactive 的阅读：
  1. 窗口（window）：一条作息只在某个时间段里可能发生，不是固定整点
  2. 抖动（jitter）：时间由「日期 + 作息 id」哈希决定，每天落在窗口内不同分钟，
     看起来像"想起来了"，而不是闹钟
  3. 静默时段（quiet hours）：某些小时她"在睡觉"，绝不打扰
  4. 每日预算（dailyBudget）：一天最多醒几次，超了就等明天
  5. 空闲门（min_idle_seconds）：最近还在跟人聊，就不打断

实现方式：systemd 每 10 分钟跑一次本脚本，命中就往 qq-inject.jsonl 追加一条
"主人的提醒"，走插件已有的真实入站管线。零常驻进程、零依赖。
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime

BASE = "/opt/dsh"
ROUTINES = os.path.join(BASE, "routines.json")
STATE = os.path.join(BASE, "memory", "schedule-state.json")
QUEUE = os.path.join(BASE, "media", "onebot", "qq-inject.jsonl")
ACTIONS = os.path.join(BASE, "media", "onebot", "qq-actions.log")
LOG = os.path.join(BASE, "memory", "schedule.log")

OWNER = __OWNER_QQ__
BOT = __BOT_QQ__
QUIET = [("01:00", "07:30")]  # 静默时段：她在"睡觉"


def hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%F %T"), msg))
    except Exception:
        pass


def load_json(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE)
    except Exception:
        log("save_state failed")


def in_quiet(now_min):
    for a, b in QUIET:
        if hm(a) <= now_min < hm(b):
            return True
    return False


def fire_minute(routine, day):
    """窗口内按 日期+id 哈希取一个稳定但每天不同的分钟数（抖动）"""
    a, b = routine["window"]
    start, end = hm(a), hm(b)
    span = max(1, end - start)
    h = int(hashlib.sha256((day + routine["id"]).encode()).hexdigest()[:8], 16)
    return start + h % span


def idle_minutes():
    """最后一次发消息到现在多久（用 qq-actions.log 的 mtime 近似）"""
    try:
        return (time.time() - os.path.getmtime(ACTIONS)) / 60.0
    except Exception:
        return 1e9


def inject(text):
    now = int(time.time())
    ev = {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "self_id": BOT,
        "user_id": OWNER,
        "time": now,
        "message_id": now * 10 + 7,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": OWNER, "nickname": os.environ.get("OWNER_NICK", "主人")},
    }
    os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
    with open(QUEUE, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def main():
    dry = "--dry-run" in sys.argv
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute

    cfg = load_json(ROUTINES, {"dailyBudget": 4, "routines": []})
    budget = int(cfg.get("dailyBudget", 4))
    st = load_json(STATE, {})
    if st.get("day") != day:
        st = {"day": day, "fired": {}, "count": 0}

    idle = idle_minutes()
    if in_quiet(now_min):
        if dry:
            print("静默时段（%s），不触发" % now.strftime("%H:%M"))
        return

    for r in cfg.get("routines", []):
        rid = r["id"]
        fm = fire_minute(r, day)
        planned = "%02d:%02d" % (fm // 60, fm % 60)
        if now_min < fm:
            if dry:
                print("[%s] 未到点（今天定在 %s）" % (rid, planned))
            continue
        end_min = hm(r["window"][1])
        if now_min > end_min + 30:
            if dry:
                print("[%s] 今天窗口已过（%s-%s），不补" % (rid, r["window"][0], r["window"][1]))
            continue
        if rid in st["fired"]:
            continue
        if st["count"] >= budget:
            if dry:
                print("[%s] 今日预算已用完（%d）" % (rid, budget))
            break
        need = int(r.get("minIdleMin", 45))
        if idle < need:
            if dry:
                print("[%s] 刚聊过（%.0f 分钟前），不打断" % (rid, idle))
            continue
        if dry:
            print("[%s] 会触发（今天定在 %s，已空闲 %.0f 分钟）" % (rid, planned, idle))
            print("     内容：%s" % r["prompt"][:60])
            continue
        inject(r["prompt"])
        st["fired"][rid] = now.isoformat()
        st["count"] += 1
        log("fired %s at %s (count=%d)" % (rid, planned, st["count"]))

    if not dry:
        save_state(st)


if __name__ == "__main__":
    main()
