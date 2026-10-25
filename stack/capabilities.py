#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力 / 风险注册表。

问题：工具散在三个地方（QQ 插件 tools.js、bili_hub、memory_server），
权限只有 toolmode 的 chat/full 两档，没有一张表能回答：
  · 这个能力叫什么、读还是写、风险多高
  · 默认开没开、属于哪个档位
  · 它**真的接上了吗**（"代码在 ≠ 接对了"）

所以这里登记 + 校验。`verify` 字段是判定方式：能在对应文件里找到标记才算接上。

用法：
    python3 capabilities.py            # 表格（含风险统计）
    python3 capabilities.py --check    # 只校验，有问题返回 1
    python3 capabilities.py --json     # 给面板用
"""
import io
import json
import os
import sys

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
LIB = os.path.join(BASE, "profiles/web/node_modules/dsh-onebot-qq/lib")

# id, 类别, 风险, 档位, 默认, 校验方式(文件, 标记)
#   kind: read | write
#   risk: low | medium | high   （write+外部可见=high；写本地=medium；只读=low）
#   mode: chat | full           （toolmode 的两档）
CAPS = [
    # ---- QQ 文本/媒体 ----
    dict(id="qq.send_text", kind="write", risk="low", mode="chat", default=True,
         file="bridge.js", marker="guardedConnectionCall", note="最终回复/主动消息都走闸门"),
    dict(id="qq.reply", kind="write", risk="low", mode="chat", default=True,
         file="tools.js", marker="name: 'qq_reply'", note="引用回复"),
    dict(id="qq.emoji_like", kind="write", risk="low", mode="chat", default=True,
         file="tools.js", marker="set_msg_emoji_like", note="表情回应（零 token）"),
    dict(id="qq.poke", kind="write", risk="medium", mode="chat", default=True,
         file="tools.js", marker="friend_poke", note="拍一拍（白名单+10 分钟冷却）"),
    dict(id="qq.send_image", kind="write", risk="low", mode="chat", default=True,
         file="tools.js", marker="name: 'qq_send_image'"),
    dict(id="qq.send_sticker", kind="write", risk="low", mode="chat", default=True,
         file="tools.js", marker="name: 'qq_send_sticker'"),
    dict(id="qq.send_voice", kind="write", risk="low", mode="chat", default=True,
         file="tools.js", marker="name: 'qq_send_voice'"),
    dict(id="qq.send_video", kind="write", risk="medium", mode="chat", default=True,
         file="tools.js", marker="name: 'qq_send_video'"),
    dict(id="qq.send_file", kind="write", risk="medium", mode="chat", default=True,
         file="tools.js", marker="name: 'qq_send_file'"),
    dict(id="qq.send_forward", kind="write", risk="low", mode="chat", default=True,
         file="tools.js", marker="name: 'qq_send_forward'"),
    # ---- QQ 管理（高风险，full 档）----
    dict(id="qq.napcat_api", kind="write", risk="high", mode="full", default=True,
         file="tools.js", marker="NAPCAT_API_WHITELIST", note="白名单代理，写操作也过闸"),
    dict(id="qq.group_history", kind="read", risk="low", mode="chat", default=True,
         file="tools.js", marker="name: 'qq_group_history'"),
    # ---- 表情库 ----
    dict(id="sticker.list", kind="read", risk="low", mode="chat", default=True,
         file="tools.js", marker="name: 'qq_list_stickers'"),
    dict(id="sticker.save", kind="write", risk="low", mode="chat", default=True,
         file="tools.js", marker="name: 'qq_save_sticker'"),
    dict(id="sticker.collect", kind="write", risk="low", mode="chat", default=True,
         file="tools.js", marker="name: 'qq_collect_sticker'"),
    # ---- 记忆（L3）----
    dict(id="memory.diary_write", kind="write", risk="low", mode="chat", default=True,
         file="memory_server.py", marker="tool_diary_write"),
    dict(id="memory.diary_search", kind="read", risk="low", mode="chat", default=True,
         file="memory_server.py", marker="tool_diary_search"),
    dict(id="memory.diary_recent", kind="read", risk="low", mode="chat", default=True,
         file="memory_server.py", marker="tool_diary_recent"),
    dict(id="memory.notes", kind="read", risk="low", mode="chat", default=True,
         file="memory_server.py", marker="tool_notes_read"),
    dict(id="memory.notes_write", kind="write", risk="medium", mode="chat", default=True,
         file="memory_server.py", marker="tool_notes_write"),
    dict(id="memory.recall_inject", kind="read", risk="low", mode="chat", default=True,
         file="inbound.js", marker="mem_layers.py", note="每轮自动召回（不需要她调工具）"),
    dict(id="memory.idle_distill", kind="write", risk="medium", mode="chat", default=True,
         file="mem_layers.py", marker="def distill", note="空闲 10 分钟蒸馏"),
    # ---- B 站 ----
    dict(id="bili.now", kind="read", risk="low", mode="chat", default=True,
         file="bili_hub.py", marker='"bili_now"'),
    dict(id="bili.open", kind="write", risk="medium", mode="chat", default=True,
         file="bili_hub.py", marker='bili_window.open_window', note="受自动开窗次数限制"),
    dict(id="bili.search", kind="read", risk="low", mode="chat", default=True,
         file="bili_hub.py", marker="search_videos"),
    dict(id="bili.read_video", kind="read", risk="low", mode="chat", default=True,
         file="bili_hub.py", marker="wait_job", note="异步任务自动等待"),
    dict(id="bili.share_to_owner", kind="write", risk="high", mode="chat", default=True,
         file="bili_hub.py", marker="daily_limit", note="真的在 B 站评论区 @ 主人"),
    # ---- 主动 / 调度 ----
    dict(id="proactive.dm", kind="write", risk="medium", mode="chat", default=True,
         file="proactive.py", marker="def inject"),
    dict(id="diary.outband", kind="write", risk="low", mode="chat", default=True,
         file="diary_outband.py", marker="def write_diary", note="带外写日记"),
    # ---- 发送治理 ----
    dict(id="send.audit", kind="read", risk="low", mode="chat", default=True,
         file="send-audit.js", marker="installSendAudit", note="真实 message_id 才算发出"),
    dict(id="send.outbox", kind="write", risk="medium", mode="chat", default=True,
         file="outbox.js", marker="startOutbox", note="Python 侧走同一道闸门"),
    dict(id="scheduler.intent", kind="write", risk="medium", mode="chat", default=True,
         file="scheduler.py", marker="def decide", note="只产出意图，不直接变成用户消息"),
    dict(id="events.adapter", kind="read", risk="low", mode="chat", default=True,
         file="events.js", marker="describeEvent", note="notice 不再被丢"),
]


def _path(cap):
    f = cap["file"]
    # 允许跨目录（记忆层/调度器在 /opt/dsh 根下）
    for cand in (os.path.join(LIB, f), os.path.join(BASE, f)):
        if os.path.isfile(cand):
            return cand
    return None


def check():
    rows = []
    for cap in CAPS:
        p = _path(cap)
        ok = False
        why = "文件不存在"
        if p is not None:
            try:
                text = io.open(p, encoding="utf-8").read()
            except Exception:
                text = ""
            ok = cap["marker"] in text
            why = "已接线" if ok else ("找不到标记 %r" % cap["marker"][:40])
        rows.append((cap, ok, why, p))
    return rows


def main():
    rows = check()
    bad = [r for r in rows if not r[1]]
    if "--json" in sys.argv:
        print(json.dumps([dict(id=c["id"], kind=c["kind"], risk=c["risk"], mode=c["mode"],
                               default=c["default"], ok=ok, why=why,
                               note=c.get("note", ""))
                          for c, ok, why, _p in rows], ensure_ascii=False, indent=1))
        return 0 if not bad else 1
    if "--check" in sys.argv:
        for c, ok, why, p in rows:
            print("%s %-24s %-6s %-6s %s" % ("[OK]  " if ok else "[MISS]", c["id"],
                                             c["kind"], c["risk"], why))
        print("\n%d 项能力，%d 项未接线" % (len(rows), len(bad)))
        return 0 if not bad else 1

    # 表格模式
    print("%-24s %-6s %-6s %-5s %-5s %s" % ("能力", "读写", "风险", "档位", "默认", "说明"))
    print("-" * 92)
    for c, ok, why, _p in rows:
        print("%-24s %-6s %-6s %-5s %-5s %s" % (
            c["id"], c["kind"], c["risk"], c["mode"],
            "开" if c["default"] else "关", c.get("note", "")[:40]))
    by_risk = {}
    for c in CAPS:
        by_risk[c["risk"]] = by_risk.get(c["risk"], 0) + 1
    by_kind = {}
    for c in CAPS:
        by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + 1
    print("\n共 %d 项：%s｜%s｜未接线 %d" % (
        len(CAPS),
        " ".join("%s=%d" % kv for kv in sorted(by_kind.items())),
        " ".join("%s=%d" % kv for kv in sorted(by_risk.items())),
        sum(1 for r in rows if not r[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
