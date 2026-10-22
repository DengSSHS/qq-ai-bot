#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生图工具（MCP / stdio）

调千问平台的图像接口，把图下载到本地，返回文件路径；
之后她可以用 qq_send_image 把这张图发出去。

为什么返回路径而不是 URL：平台给的是带签名的临时地址（一小时过期），
发到 QQ 也不稳定，落地成本地文件最可靠。
"""
import json
import os
import sys
import time
import traceback
import urllib.request

BASE = os.environ.get("IMAGE_BASE", "")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "qwen-image-3.0-pro")
ENVF = "/opt/dsh/qwen.env"
OUTDIR = "/opt/dsh/media/onebot"
MODEL = os.environ.get("IMAGEN_MODEL", "qwen-image-3.0-pro")
LOG = "/opt/dsh/memory/imagen.log"
PROTO = "2024-11-05"


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s" % (time.strftime("%F %T"), msg) + chr(10))
    except Exception:
        pass


def api_key():
    try:
        with open(ENVF, encoding="utf-8") as f:
            for line in f:
                if line.startswith("QWEN_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get("QWEN_API_KEY", "")


def generate(prompt, size="1024x1024"):
    key = api_key()
    if not key:
        return "没有配 API key，画不了。"
    body = json.dumps({"model": MODEL, "prompt": prompt, "n": 1, "size": size}).encode("utf-8")
    req = urllib.request.Request(BASE + "/images/generations", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=240) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    cost = time.time() - t0
    items = data.get("data") or []
    if not items:
        return "接口没给图：%s" % json.dumps(data, ensure_ascii=False)[:200]
    item = items[0]
    url = item.get("url")
    b64 = item.get("b64_json")
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, "gen_%d.png" % int(time.time()))
    if url:
        with urllib.request.urlopen(url, timeout=120) as r:
            blob = r.read()
    elif b64:
        import base64
        blob = base64.b64decode(b64)
    else:
        return "接口返回里既没有 url 也没有 b64_json。"
    with open(path, "wb") as f:
        f.write(blob)
    log("gen ok %.1fs %s bytes=%d prompt=%s" % (cost, path, len(blob), prompt[:40]))
    return ("画好了，用时 %.0f 秒。文件：%s" % (cost, path)
            + chr(10) + "要发出去就用 qq_send_image，file 参数填上面这个路径。")


TOOLS = [{
    "name": "image_generate",
    "description": "按文字描述生成一张图。想发画的时候用。生成要 30~60 秒，成功后返回本地文件路径，再用 qq_send_image 发出去。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "想要什么画面，越具体越好"},
            "size": {"type": "string", "description": "尺寸，默认 1024x1024，可选 1280x720 等"},
        },
        "required": ["prompt"],
    },
}]


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
                    "serverInfo": {"name": "jingjing-imagen", "version": "1.0.0"}})
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
        if name != "image_generate":
            reply(mid, error={"code": -32602, "message": "unknown tool: %s" % name})
            return
        try:
            out = generate((args.get("prompt") or "").strip() or "一只可爱的卡通鲸鱼", args.get("size") or "1024x1024")
            reply(mid, {"content": [{"type": "text", "text": out}], "isError": False})
        except Exception as e:
            log("gen fail: %s" % traceback.format_exc())
            reply(mid, {"content": [{"type": "text", "text": "画图失败了：%s" % e}], "isError": True})
        return
    if mid is not None:
        reply(mid, error={"code": -32601, "message": "method not found: %s" % m})


def main():
    log("start model=%s" % MODEL)
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
