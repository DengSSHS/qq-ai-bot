#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""往她的入站管线里塞一条测试消息，但**绝不发到 QQ**。

为什么要有这个：注入通道原本只有全局开关（injectDryRun），而这个开关同时是
「主动消息投递」的唯一通道——打开它主动消息发不出去，关掉它测试消息会真的
发到主人 QQ（2026-10-26 实测：测试脚本注入 5 条假提问，她一条条回，全发出去了）。

现在注入事件可以自带 "dry_run": true（见 apply_local_patches.py 第 6 个补丁）：
这一轮的出站全部被拦截，原文进 trace 日志，QQ 一条都不会收到。

用法（服务器上，root）：
    python3 qq_test_inject.py "你在干嘛"              # 塞一条，等 40 秒看结果
    python3 qq_test_inject.py "..." --wait 90        # 多等一会儿
    python3 qq_test_inject.py "..." --who 群         # 伪装成群消息（默认私聊）
    python3 qq_test_inject.py "..." --as-reminder    # 按系统内部提醒的身份（默认按主人）

输出：她本来会发出去的每一句原文（从 trace 的「注入 dry-run：出站调用已拦截」里取）。
"""
import io
import json
import os
import re
import sys
import time

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
QUEUE = os.path.join(BASE, "media/onebot/qq-inject.jsonl")
LOG = "/var/log/dsh-onebot.log"
OWNER = int(os.environ.get("OWNER_QQ", "1709392682"))
BOT = int(os.environ.get("BOT_QQ", "3600398389"))
GROUP = os.environ.get("TEST_GROUP", "1074978556")


def tail_size():
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def read_new(start):
    try:
        with io.open(LOG, encoding="utf-8", errors="replace") as f:
            f.seek(start)
            return f.read()
    except Exception:
        return ""


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    text = args[0]
    wait = 40
    who = "私聊"
    reminder = False
    for i, a in enumerate(sys.argv):
        if a == "--wait" and i + 1 < len(sys.argv):
            wait = int(sys.argv[i + 1])
        if a == "--who" and i + 1 < len(sys.argv):
            who = sys.argv[i + 1]
        if a == "--as-reminder":
            reminder = True

    now = int(time.time())
    ev = {
        "post_type": "message",
        "message_type": "group" if who.startswith("群") else "private",
        "sub_type": "normal" if who.startswith("群") else "friend",
        "self_id": BOT,
        "user_id": OWNER,
        "time": now,
        "message_id": now * 10 + 3,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": OWNER, "nickname": "主人"},
        # ↓ 关键：这一条只走管线，出站全部拦截
        "dry_run": True,
        # 默认按「主人」身份进管线：否则她一律当系统内部提醒，只会回「不接这条」，
        # 测出来的行为没有参考价值。出站仍然被拦截，QQ 一条都收不到。
        "as_user": not reminder,
    }
    if who.startswith("群"):
        ev["group_id"] = int(GROUP)

    os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
    start = tail_size()
    with io.open(QUEUE, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    print("已注入（dry_run）：%s   目标=%s" % (text[:50], who))
    print("等 %d 秒，看她的回合跑完…" % wait)

    deadline = time.time() + wait
    seen = set()
    texts = []
    while time.time() < deadline:
        time.sleep(3)
        chunk = read_new(start)
        for line in chunk.split("\n"):
            # 格式：[注入拦截] send_msg（dry_run 时间窗） :: 她本来要说的话
            if "[注入拦截]" not in line or "::" not in line:
                continue
            head, _, body = line.partition("::")
            if "send_msg" not in head:
                continue
            text = body.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            texts.append(text)

    if not texts:
        print("\n没抓到她的出站（可能还在跑，或者她这一轮什么都没发）。")
        print("原文可从日志里看：grep '注入 dry-run' %s | tail -20" % LOG)
        return 0

    print("\n她本来会发这些（已拦截，QQ 没收到）：")
    for i, t in enumerate(texts, 1):
        print("  %d. %s" % (i, t.replace("\n", " ")[:200]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
