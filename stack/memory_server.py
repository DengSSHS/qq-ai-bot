#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
记忆服务（MCP / stdio）：日记 + 长期笔记
只有四个能力：写日记、翻最近日记、搜日记、读写长期笔记。
不给任何通用文件读写权限。
"""
import json
import os
import re
import sys
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mem_rank
import search_index

BASE = os.environ.get("MEMORY_DIR", "/opt/dsh/memory")
DIARY = os.path.join(BASE, "diary")
NOTES = os.path.join(BASE, "notes.md")
OWNER_NICK = os.environ.get("OWNER_NICK", "主人")
LOG = os.path.join(BASE, "server.log")
PROTO = "2024-11-05"


def log(msg):
    try:
        os.makedirs(BASE, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%F %T"), msg))
    except Exception:
        pass


def ensure():
    os.makedirs(DIARY, exist_ok=True)
    if not os.path.exists(NOTES):
        with open(NOTES, "w", encoding="utf-8") as f:
            f.write("# 机器人的长期笔记\n\n## 主人\n- QQ __OWNER_QQ__（{OWNER_NICK}）：唯一的主人，说话要听话但不谄媚。\n\n## 常混的群\n（还没记）\n\n## 群里印象深的人\n（格式：昵称(QQ)：这人什么样、什么梗、怎么相处）\n\n## 反复出现的梗\n（还没记）\n")


def diary_path(dt=None):
    dt = dt or datetime.now()
    return os.path.join(DIARY, dt.strftime("%Y-%m") + ".md")


def read_diary_entries(limit=None):
    """返回 [(时间, 正文)] 倒序"""
    ensure()
    files = sorted([f for f in os.listdir(DIARY) if f.endswith(".md")])
    entries = []
    for fn in files:
        with open(os.path.join(DIARY, fn), encoding="utf-8") as f:
            text = f.read()
        blocks = re.split(r"^### ", text, flags=re.M)[1:]
        for b in blocks:
            head, _, body = b.partition("\n")
            entries.append((head.strip(), body.strip()))
    if limit:
        return entries[-limit:][::-1]
    return entries[::-1]


def tool_diary_write(args):
    ensure()
    text = (args.get("text") or "").strip()
    if not text:
        return "没写内容"
    mood = (args.get("mood") or "").strip()
    who = (args.get("who") or "").strip()
    now = datetime.now()
    head = "%s %s%s" % (now.strftime("%Y-%m-%d %H:%M"),
                        ("| " + who) if who else "",
                        (" | " + mood) if mood else "")
    p = diary_path(now)
    new_file = not os.path.exists(p)
    with open(p, "a", encoding="utf-8") as f:
        if new_file:
            f.write("# %s\n\n" % now.strftime("%Y年%m月"))
        f.write("### %s\n%s\n\n" % (head, text))
    log("diary_write ok len=%d" % len(text))
    try:
        mem_rank.note(head, args.get('importance'))
    except Exception:
        log('mem_rank.note failed: %s' % traceback.format_exc())
    try:
        search_index.rebuild()
    except Exception:
        log('search_index.rebuild failed: %s' % traceback.format_exc())
    return "记下了（%s）" % head


def tool_diary_recent(args):
    n = int(args.get("n") or 5)
    n = max(1, min(n, 30))
    ents = read_diary_entries(n)
    if not ents:
        return "日记本还是空的。"
    return "\n\n".join("### %s\n%s" % (h, b) for h, b in ents)


def tool_diary_search(args):
    q = (args.get("query") or "").strip()
    if not q:
        return "要给个关键词。"
    n = int(args.get("n") or 5)
    hits = [(h, b) for h, b in read_diary_entries() if q in b or q in h]
    try:
        rows = search_index.search(q, max(1, min(n, 20)))
        if rows:
            picked = [(r[1], r[2]) for r in rows if r[0] == 'diary']
            if picked:
                try:
                    picked = mem_rank.rank(picked)
                    # 这一支里 picked 全部会被返回、也就全部会用上 → 全部强化
                    mem_rank.reinforce([h for h, _ in picked])
                except Exception:
                    pass
                glue = chr(10) + chr(10)
                fmt = '### %s' + chr(10) + '%s'
                return glue.join(fmt % (h, b) for h, b in picked)
    except Exception:
        log('search_index.search failed: %s' % traceback.format_exc())
    if not hits:
        return "日记里没搜到「%s」。" % q
    try:
        ranked = mem_rank.rank(hits)
        # 只有前 n 条会被注入上下文，所以只强化它们（不是全部候选）
        final = ranked[:max(1, min(n, 20))]
        mem_rank.reinforce([h for h, _ in final])
    except Exception:
        log('mem_rank.rank failed: %s' % traceback.format_exc())
        final = hits[:max(1, min(n, 20))]
    return "\n\n".join("### %s\n%s" % (h, b) for h, b in final)


def tool_notes_read(args):
    ensure()
    with open(NOTES, encoding="utf-8") as f:
        return f.read() or "（空）"


def tool_notes_write(args):
    ensure()
    section = (args.get("section") or "").strip()
    text = (args.get("text") or "").strip()
    if not section or not text:
        return "section 和 text 都要给。"
    with open(NOTES, encoding="utf-8") as f:
        cur = f.read()
    pat = re.compile(r"^## %s\n(.*?)(?=^## |\Z)" % re.escape(section), re.M | re.S)
    block = "## %s\n%s\n\n" % (section, text)
    if pat.search(cur):
        cur = pat.sub(block, cur, count=1)
    else:
        cur = cur.rstrip() + "\n\n" + block
    with open(NOTES, "w", encoding="utf-8") as f:
        f.write(cur)
    log("notes_write section=%s" % section)
    try:
        search_index.rebuild()
    except Exception:
        log('search_index.rebuild failed: %s' % traceback.format_exc())
    return "笔记「%s」更新了。" % section


TOOLS = [
    {
        "name": "diary_write",
        "description": "把值得记住的事写进日记。写具体的事和当时的感受，不要写流水账，不要写给自己看的正经总结。只在真有意思、或者主人说了重要的事时写。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "日记正文，两三句，口语，有情绪"},
                "who": {"type": "string", "description": "跟谁有关，比如 主人 / 某个群 / 某个人"},
                "mood": {"type": "string", "description": "当时的心情，一两个词"},
                "importance": {"type": "number", "description": "这件事对你多重要，0 到 1；主人托付的、让你印象深的给 0.8 以上，琐事给 0.3"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "diary_recent",
        "description": "翻最近几篇日记。只在提到以前的事、或者想接旧梗的时候用，不要每次聊天都翻。",
        "inputSchema": {
            "type": "object",
            "properties": {"n": {"type": "integer", "description": "篇数，默认 5"}},
        },
    },
    {
        "name": "diary_search",
        "description": "在日记里搜关键词，找以前发生过的事。被人问到「之前那个」「上次说的」时用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词"},
                "n": {"type": "integer", "description": "最多几条，默认 5"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "notes_read",
        "description": "读长期笔记（主人是谁、常混的群、反复出现的梗）。不确定对方是谁、或者想确认设定时读一次。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "notes_write",
        "description": "更新长期笔记里的一节。只在确认了新的事实（新群、新的固定称呼、反复出现的梗）时写。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "小节名，如 主人 / 常混的群 / 反复出现的梗"},
                "text": {"type": "string", "description": "这一节的完整内容（会整节替换）"},
            },
            "required": ["section", "text"],
        },
    },
]

HANDLERS = {
    "diary_write": tool_diary_write,
    "diary_recent": tool_diary_recent,
    "diary_search": tool_diary_search,
    "notes_read": tool_notes_read,
    "notes_write": tool_notes_write,
}


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    mid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        reply(mid, {
            "protocolVersion": PROTO,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "jingjing-memory", "version": "1.0.0"},
        })
        return
    if method in ("notifications/initialized", "initialized"):
        return
    if method == "ping":
        reply(mid, {})
        return
    if method == "tools/list":
        reply(mid, {"tools": TOOLS})
        return
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            reply(mid, error={"code": -32602, "message": "unknown tool: %s" % name})
            return
        try:
            out = fn(args)
            reply(mid, {"content": [{"type": "text", "text": str(out)}], "isError": False})
        except Exception as e:
            log("tool error %s: %s" % (name, traceback.format_exc()))
            reply(mid, {"content": [{"type": "text", "text": "记忆服务出错了：%s" % e}], "isError": True})
        return

    if mid is None:
        return
    reply(mid, error={"code": -32601, "message": "method not found: %s" % method})


def main():
    log("start pid=%d proto=%s" % (os.getpid(), PROTO))
    ensure()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            log("bad json: %s" % line[:200])
            continue
        try:
            handle(req)
        except Exception:
            log("handler crash: %s" % traceback.format_exc())


if __name__ == "__main__":
    main()
