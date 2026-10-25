#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一调度器：只产出「意图」，执行器负责执行。

为什么要有这个文件（架构欠账第 5 条）：
    过去"该写日记了""该主动说句话"都是往 qq-inject.jsonl 塞一条**假用户消息**，
    于是她会当成主人在说话，语义完全错，还导致"不接这条"那种拒答。
    日记已经改成带外（diary_outband.py），这个文件把剩下的也搬出来：

        scheduler  →  意图（memory/intents.jsonl）  →  执行器
                      ├─ chat.initiate → 生成一句话 → qq-outbox.jsonl → bridge 闸门
                      └─ diary.write   → diary_outband.py

    意图永远不会变成"用户消息"。

用法：
    python3 scheduler.py              # 判定 + 执行（timer 调这个）
    python3 scheduler.py --dry        # 只打印判定，不写意图不执行
    python3 scheduler.py --decide     # 只判定、写意图，不执行
    python3 scheduler.py --run        # 只执行待办意图
    python3 scheduler.py --force chat.initiate   # 强制产一条（调试）
"""
import io
import json
import os
import re
import sys
import time
from datetime import datetime

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
sys.path.insert(0, BASE)

MEM = os.path.join(BASE, "memory")
INTENTS = os.path.join(MEM, "intents.jsonl")
STATE = os.path.join(MEM, "scheduler_state.json")
OUTBOX = os.path.join(BASE, "media", "onebot", "qq-outbox.jsonl")
OWNER = int(os.environ.get("OWNER_QQ", "1709392682"))
CHAT = "private:%d" % OWNER


def log(msg):
    try:
        with io.open(os.path.join(MEM, "scheduler.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def rdj(p, default):
    try:
        return json.loads(io.open(p, encoding="utf-8").read())
    except Exception:
        return default


def wrj(p, obj):
    tmp = p + ".tmp"
    io.open(tmp, "w", encoding="utf-8", newline="\n").write(
        json.dumps(obj, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, p)


# ---------------------------------------------------------------- 判定
def decide(dry=False):
    """按 routines.json 的时间窗/配额产出意图。判定逻辑复用 proactive.py。"""
    import proactive as P

    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute
    cfg = rdj(os.path.join(BASE, "routines.json"), {"dailyBudget": 4, "routines": []})
    st = rdj(STATE, {})
    if st.get("day") != day:
        st = {"day": day, "fired": {}, "count": 0}

    idle = P.idle_minutes()
    quiet = P.in_quiet(now_min)
    out = []

    for r in cfg.get("routines", []):
        rid = r["id"]
        if r.get("off"):
            continue
        if quiet and not r.get("ignoreQuiet"):
            continue
        # 固定型（每天一次）：到点 + 空闲够
        fm = P.fire_minute(r, day)
        if now_min < fm or now_min > P.hm(r["window"][1]) + 30:
            continue
        key = rid + "@" + day
        if st["fired"].get(key):
            continue
        need = int(r.get("minIdleMin", 45))
        if idle < need:
            continue
        # 主动私聊有单独的每日额度
        ckey = "count_" + rid
        cap = int(r.get("dailyBudget") or cfg.get("dailyBudget") or 4)
        if int(st.get(ckey) or 0) >= cap:
            continue
        out.append({
            "ts": int(time.time()),
            "kind": "chat.initiate" if rid != "qzone_daily" else "qzone.post",
            "reason": rid,
            "hint": r.get("prompt", ""),
            "chat_id": CHAT,
            "state": "new",
        })
        st["fired"][key] = now.isoformat()
        st[ckey] = int(st.get(ckey) or 0) + 1
        st["count"] = int(st.get("count") or 0) + 1

    if dry:
        print("空闲 %.0f 分钟｜%s" % (idle, "睡觉时段" if quiet else "清醒时段"))
        if not out:
            print("  没有要产出的意图")
        for it in out:
            print("  → %-14s reason=%s" % (it["kind"], it["reason"]))
        return out

    if out:
        with io.open(INTENTS, "a", encoding="utf-8") as f:
            for it in out:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        wrj(STATE, st)
        log("产出意图 %d 条：%s" % (len(out), ", ".join(i["reason"] for i in out)))
    return out


# ---------------------------------------------------------------- 执行
def _persona(limit=1200):
    try:
        t = io.open(os.path.join(BASE, "media/onebot/persona.md"), encoding="utf-8").read()
    except Exception:
        return ""
    return t if len(t) <= limit else t[: int(limit * 0.75)] + "\n……\n" + t[-int(limit * 0.25):]


def _summary():
    try:
        return io.open(os.path.join(MEM, "session_summary.md"), encoding="utf-8").read().strip()
    except Exception:
        return ""


def gen_line(hint):
    """生成一句主动消息的正文（短、不问问题、不像客服）。"""
    import mem_layers

    prompt = (
        "你在扮演「鲸鲸」（拟人鲸鱼娘）。现在没人跟你说话，是**你自己**想跟主人说一句。\n"
        "要求：一到两句、口语、别提问、别总结、别解释你为什么说话；"
        "不要提「提醒」「任务」「系统」「模型」。只输出这句话本身。\n\n"
        + ("上次聊到的：\n" + _summary() + "\n\n" if _summary() else "")
        + ("你的人设（节选）：\n" + _persona() + "\n\n" if _persona() else "")
        + "这次的由头：\n" + hint.strip()[:600] + "\n"
    )
    text = mem_layers._llm(prompt).strip()
    text = re.sub(r"^[\"'「『]|[\"'」』]$", "", text).strip()
    return text[:200]


def pending():
    if not os.path.isfile(INTENTS):
        return []
    rows = []
    for line in io.open(INTENTS, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            it = json.loads(line)
        except Exception:
            continue
        if it.get("state") == "new":
            rows.append(it)
    return rows


def _mark(rows_done):
    """把已处理的意图标成 done（整文件重写，量很小）。"""
    if not rows_done:
        return
    done_keys = {(r["ts"], r["reason"]) for r in rows_done}
    out = []
    for line in io.open(INTENTS, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            it = json.loads(line)
        except Exception:
            continue
        if (it.get("ts"), it.get("reason")) in done_keys:
            it["state"] = "done"
            it["done_at"] = int(time.time())
        out.append(it)
    tmp = INTENTS + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for it in out:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.replace(tmp, INTENTS)


def run(dry=False):
    todo = pending()
    if not todo:
        print("没有待办意图")
        return 0
    done = []
    for it in todo:
        kind = it.get("kind")
        if kind == "diary.write":
            cmd = [sys.executable, os.path.join(BASE, "diary_outband.py")]
        elif kind == "chat.initiate":
            text = gen_line(it.get("hint", ""))
            if not text:
                log("生成失败，跳过 reason=%s" % it.get("reason"))
                continue
            if dry:
                print("（dry）%s → %s" % (it.get("reason"), text))
                continue
            os.makedirs(os.path.dirname(OUTBOX), exist_ok=True)
            with io.open(OUTBOX, "a", encoding="utf-8") as f:
                f.write(json.dumps({"chat_id": it.get("chat_id", CHAT), "text": text,
                                    "source": "proactive:" + str(it.get("reason"))},
                                   ensure_ascii=False) + "\n")
            log("chat.initiate → outbox: %s" % text[:60])
            done.append(it)
            continue
        elif kind == "qzone.post":
            cmd = [sys.executable, os.path.join(BASE, "qzone_post.py")]
        else:
            log("未知意图 kind=%s，跳过" % kind)
            continue

        if dry:
            print("（dry）会执行：%s" % " ".join(cmd))
            continue
        try:
            import subprocess
            subprocess.run(cmd, timeout=180)
            log("执行 %s（%s）" % (kind, it.get("reason")))
            done.append(it)
        except Exception as e:
            log("执行失败 %s: %s" % (kind, str(e)[:80]))
    if not dry:
        _mark(done)
    return 0


def force(kind):
    if kind == "chat.initiate":
        it = {"ts": int(time.time()), "kind": kind, "reason": "force",
              "hint": "你刚睡醒，随便跟主人说一句。", "chat_id": CHAT, "state": "new"}
        with io.open(INTENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
        print("已产出一条 chat.initiate 意图")
        return 0
    print("未知 kind")
    return 2


def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    if "--force" in args:
        i = args.index("--force")
        return force(args[i + 1] if i + 1 < len(args) else "")
    if "--run" in args:
        return run(dry)
    if "--decide" in args:
        decide(dry)
        return 0
    decide(dry)
    if not dry:
        run(dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
