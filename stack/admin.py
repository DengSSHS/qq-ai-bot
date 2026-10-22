#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理通道 MCP：让主人在私聊里用一句话切换工具模式

工具：
  tool_mode(mode)      mode = "chat" | "full"
  tool_mode_status()   看现在是哪个模式

为什么用"请求文件 + systemd path"：重启服务不能在服务自己的工具调用里做，
交给外部的 path unit 执行，她只需要把请求写下去。
"""
import json
import os
import sys
import time
import traceback

REQ = "/opt/dsh/toolmode.request"
STATE = "/opt/dsh/toolmode.state"
LOG = "/opt/dsh/memory/admin.log"
PROTO = "2024-11-05"


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s" % (time.strftime("%F %T"), msg) + chr(10))
    except Exception:
        pass


def tool_mode(args):
    mode = str(args.get("mode") or "").strip().lower()
    if mode not in ("chat", "full"):
        return "mode 只能是 chat 或 full。chat = 精简（只留聊天要用的），full = 全量（含插件管理）。"
    try:
        with open(REQ, "w", encoding="utf-8") as f:
            json.dump({"mode": mode, "at": int(time.time())}, f)
    except Exception as e:
        return "写请求失败：%s" % e
    log("request mode=%s" % mode)
    if mode == "full":
        return "已经请求切到全量模式，大约 20 秒后生效（服务会重启一次，重启期间我不在线）。"
    return "已经请求切回聊天模式，大约 20 秒后生效（服务会重启一次）。"


def tool_status(_args=None):
    m = "chat"
    try:
        m = json.loads(open(STATE, encoding="utf-8").read()).get("mode", "chat")
    except Exception:
        pass
    pend = os.path.exists(REQ)
    return "当前工具模式：%s%s" % (m, "（有一个切换正在生效，等 20 秒）" if pend else "")


TOOLS = [
    {
        "name": "tool_mode",
        "description": "切换自己手里的工具档位。chat=精简（只留聊天、表情、记忆、联网、画图），full=全量（再加上插件管理等运维工具）。只有主人在私聊里明确说要切换时才用。",
        "inputSchema": {
            "type": "object",
            "properties": {"mode": {"type": "string", "description": "chat 或 full"}},
            "required": ["mode"],
        },
    },
    {
        "name": "tool_mode_status",
        "description": "看自己现在是哪个工具档位。主人问「你现在是什么模式」时用。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

HANDLERS = {"tool_mode": tool_mode, "tool_mode_status": tool_status}


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + chr(10))
    sys.stdout.flush()


def handle(req):
    m, mid, p = req.get("method"), req.get("id"), req.get("params") or {}
    if m == "initialize":
        reply(mid, {"protocolVersion": PROTO,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "jingjing-admin", "version": "1.0.0"}})
        return
    if m in ("notifications/initialized", "initialized"):
        return
    if m == "ping":
        reply(mid, {})
        return
    if m == "tools/list":
        reply(mid, {"tools": TOOLS})
        return
    if m == "tools/call":
        name = p.get("name")
        args = p.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            reply(mid, error={"code": -32602, "message": "unknown tool: %s" % name})
            return
        try:
            out = fn(args)
            reply(mid, {"content": [{"type": "text", "text": str(out)}], "isError": False})
        except Exception as e:
            log("tool error: %s" % traceback.format_exc())
            reply(mid, {"content": [{"type": "text", "text": "出错了：%s" % e}], "isError": True})
        return
    if mid is not None:
        reply(mid, error={"code": -32601, "message": "method not found: %s" % m})


def main():
    log("start pid=%d" % os.getpid())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception:
            log("crash: %s" % traceback.format_exc())


if __name__ == "__main__":
    main()