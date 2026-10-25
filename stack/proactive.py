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
import glob
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

OWNER = 1709392682
BOT = 3600398389
QUIET = [("01:00", "07:30")]  # 静默时段：她在"睡觉"

# 时段系数：把 rampPerHour 按她自己作息缩放（对齐 body_state 的时段划分）
# 深夜/清晨几乎不想找人，上午一般，晚上精神最好。
SLOT_MULT = [
    ("07:30", "09:00", 0.3),   # 刚醒，不太会主动找人
    ("09:00", "11:00", 0.9),   # 上午：精神还行
    ("11:00", "13:00", 0.6),   # 中午：饿了，心思不在聊天
    ("13:00", "15:00", 0.4),   # 午后：犯困
    ("15:00", "18:00", 1.0),   # 下午：恢复正常
    ("18:00", "20:00", 1.1),   # 傍晚：心情轻快
    ("20:00", "23:00", 1.3),   # 晚上：一天里精神最好，话最多
    ("23:00", "01:00", 0.7),   # 深夜：困了但还不想睡
]


def slot_mult(now_min):
    """返回当前时段的概率系数；不在表里就按 1.0 处理。"""
    for a, b, m in SLOT_MULT:
        start, end = hm(a), hm(b)
        if start <= end:
            if start <= now_min < end:
                return m
        else:                      # 跨零点
            if now_min >= start or now_min < end:
                return m
    return 1.0


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


def last_activity_ts():
    """最后一次性活动（任一方说话）的时间戳。

    取多个来源里最新的那个：
      - qq-actions.log：她发出消息的时刻
      - session 文件 mtime：任何一方的回合都会追加，写入即更新
    以前只看 qq-actions.log，而它在 dry-run 开启时从不生成，
    结果 idle 恒为无穷大，概率模型永远跑在最高档（v0.7.12/13 的失真）。
    """
    cands = [ACTIONS]
    try:
        for pat in ("onebot-private-*", "onebot-group-*"):
            cands.extend(glob.glob(os.path.join(BASE, "sessions", "*", pat,
                                                "session.v4.jsonl.zstd")))
    except Exception:
        pass
    newest = 0.0
    for f in cands:
        try:
            mt = os.path.getmtime(f)
            if mt > newest:
                newest = mt
        except Exception:
            continue
    return newest


def idle_minutes():
    """最后一次有活动到现在多久（分钟）。拿不到任何来源时返回 1e9。"""
    ts = last_activity_ts()
    if not ts:
        return 1e9
    return (time.time() - ts) / 60.0


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
    MAX_CASUAL_DM = 6   # 主动私聊单独上限，避免一天刷屏
    st = load_json(STATE, {})
    if st.get("day") != day:
        st = {"day": day, "fired": {}, "count": 0}

    idle = idle_minutes()
    quiet_now = in_quiet(now_min)

    for r in cfg.get("routines", []):
        rid = r["id"]
        # switch.py off 会打上 off 标记，这里必须尊重它 ——
        # 否则开关只是改了个字段，行为一点没变（这类"改了没生效"今天踩太多次了）
        if r.get("off"):
            if dry:
                print("[%s] 已停用（switch.py off）" % rid)
            continue
        if quiet_now and not r.get("ignoreQuiet"):
            if dry:
                print("[%s] 睡觉时段，不打扰（可用 ignoreQuiet 豁免）" % rid)
            continue
        ramp = float(r.get("rampPerHour", 0) or 0)

        # ---- 累积型（主动私聊）：每次检查都算，不受"每天固定分钟"限制 ----
        # 概率 = rampPerHour × (1 + 超出 minIdle 的小时数)，封顶 rampCap。
        # 同一小时内掷一次（用小时桶哈希），所以是"随时间累积"而不是每分钟重掷。
        if ramp > 0:
            need_min = int(r.get("minIdleMin", 60))
            if idle < need_min:
                if dry:
                    print("[%s] 刚聊过（%.0f 分钟前 < %d），不打扰" % (rid, idle, need_min))
                continue
            cap = float(r.get("rampCap", 0.8))
            hours = max(0.0, (idle - need_min) / 60.0)
            mult = slot_mult(now_min)
            # 模型要点：她本来就有"想说话"的冲动（base 不为 0），
            # 空闲只是加成 —— 而不是"你不理我越久我越急"。
            base = float(r.get("baseChance", 0.06))
            bonus_per_h = float(r.get("idleBonusPerHour", 0.05))
            bonus_cap = float(r.get("idleBonusCap", 0.30))
            idle_bonus = min(bonus_cap, bonus_per_h * hours)

            # 按天稳定随机：有些晚上失眠，有些晚上睡得沉。
            # 用日期哈希，所以同一个晚上跑多少次结果都一样（不会抖）。
            ins_chance = float(r.get("insomniaChance", 0) or 0)
            insomnia_note = ""
            if ins_chance > 0:
                dk = day + r["id"]
                dh = int(hashlib.sha256(dk.encode()).hexdigest()[:8], 16) % 1000 / 1000.0
                if dh < ins_chance:
                    base *= float(r.get("insomniaMult", 3.0))
                    insomnia_note = "｜今晚失眠夜(%.2f<%.2f)" % (dh, ins_chance)
                else:
                    base *= float(r.get("soundSleepMult", 0.2))
                    insomnia_note = "｜今晚睡得沉(%.2f>=%.2f)" % (dh, ins_chance)

            # 可选：乘性空闲加成（夜间用）。空闲只放大她本来就有的冲动，
            # 而不是给"睡得沉"的夜晚垫一个下限。
            mul_per_h = float(r.get("idleMulPerHour", 0) or 0)
            if mul_per_h > 0:
                mul_cap = float(r.get("idleMulCap", 2.0))
                factor = 1.0 + min(mul_cap, mul_per_h * hours)
                prob = min(cap, max(0.0, base * factor * mult))
            else:
                prob = min(cap, max(0.0, (base + idle_bonus) * mult))
            if dry and insomnia_note:
                print("[%s] %s" % (rid, insomnia_note.lstrip("｜")))
            hour_bucket = now.strftime("%Y-%m-%dT%H")
            h = int(hashlib.sha256((hour_bucket + rid).encode()).hexdigest()[:8], 16)
            roll = (h % 1000) / 1000.0
            if dry:
                print("[%s] 空闲 %.0f 分钟｜本轮概率 %.0f%%｜掷出 %.2f -> %s"
                      % (rid, idle, prob * 100, roll, "会发" if roll < prob else "不发"))
            if roll >= prob:
                continue
            if st["fired"].get(rid + "@" + hour_bucket):
                continue
            # 主动私聊用自己的额度：固定型提醒（日记/空间）不该吃它的预算
            # 每条路线独立计数：白天和半夜各有各的额度，
            # 否则白天发过之后，半夜那条会被"额度已用完"永久拦住。
            ckey = "dmCount_" + rid
            dm_used = int(st.get(ckey) or 0)
            dm_budget = int(r.get("dailyBudget") or cfg.get("dmDailyBudget") or budget)
            if dm_used >= dm_budget:
                if dry:
                    print("[%s] 主动额度已用完（%d/%d）" % (rid, dm_used, dm_budget))
                break
            if dry:
                print("     内容：%s" % r["prompt"][:60])
                continue
            inject(r["prompt"])
            st["fired"][rid + "@" + hour_bucket] = now.isoformat()
            # 只记自己的额度，别占全局 count（否则日记提醒会被饿死）
            st[ckey] = int(st.get(ckey) or 0) + 1
            log("fired %s at %s (idle=%.0fm prob=%.2f count=%d)"
                % (rid, now.strftime("%H:%M"), idle, prob, st["count"]))
            continue

        # ---- 固定型（日记提醒等）：窗口内按日期哈希取一个分钟，到点发一次 ----
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
        # 定时型 routine 每天只触发一次（靠 fired 记账），不再受全局额度限制。
        # 以前这里 break，导致主动私聊把 count 用满后日记提醒全天不发
        # （2026-10-25 实测：11:07 count=5，noon/evening 全被挡）。
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
