#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看图工具（MCP / stdio）—— 服务端代看版

背景（踩过的坑）：
1. dsh-onebot 插件按设计不把图片内联给模型，只在文本里留 `[图片]` 占位；
2. 我们把 tool-fs 禁了（能读密钥，不能开），她就没有任何看图手段；
3. 试过让 MCP 工具返回 image 内容段 —— **DSH 的 MCP 客户端会丢弃**，
   模型只收到一行 "[image: image/jpeg, content discarded]"。

所以这里改成：工具自己在服务端调多模态模型，把"看到了什么"用文字返回。
对群聊场景反而更好用 —— 可以直接问"这图什么梗 / 写的什么字"。

工具：
  image_view(path?, question?)  -> 文字描述（默认描述画面 + 抄出图上文字）

安全：路径必须在 media/onebot 或 workspace 下，只收图片扩展名，单张 ≤8MB。
"""
import base64
import json
import os
import re
import sys
import time
import traceback
import urllib.request

ALLOW_DIRS = ["/opt/dsh/media/onebot", "/opt/dsh/workspace"]
EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
MAX_BYTES = 8 * 1024 * 1024
RECENT_WINDOW = 300  # 秒：只认最近收到的图
ENVF = "/opt/dsh/qwen.env"
CACHEF = "/opt/dsh/memory/vision_cache.json"
CACHE_MAX = 300  # 最多记多少张（超出丢最旧的）
CALLS = "/opt/dsh/memory/vision_calls.json"
MAX_PER_HOUR = 12  # 每小时最多真正调几次视觉模型（缓存命中不计）
def _envfile(key):
    """从 qwen.env 读一个键（systemd 的 EnvironmentFile 只对服务生效，
    用 python3 直接跑的脚本拿不到，所以这里自己兜一层）。

    踩过的坑：seer 的默认 BASE 是纯文本接口，systemd 定时任务没有
    VISION_BASE 时会 401，而失败信息只是"判断异常"，看不出是配置问题。
    """
    try:
        for line in open(ENVF, encoding="utf-8"):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


BASE = os.environ.get("VISION_BASE") or _envfile("VISION_BASE") or "https://api.deepseek.com"   # 部署时用环境变量覆盖
MODEL = (os.environ.get("VISION_MODEL") or _envfile("VISION_MODEL")
         or "deepseek-v4.1-flash-1017")
LOG = "/opt/dsh/memory/vision.log"
PROTO = "2024-11-05"

DEFAULT_ASK = ("看一下这张图：1) 画面里是什么、什么情绪/梗；"
               "2) 图上如果有文字，原样抄出来。简短说明，不要编。")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s" % (time.strftime("%F %T"), msg) + chr(10))
    except Exception:
        pass


def api_key():
    # 看图可能走独立的 key。注意：dsh 启动 MCP 子进程时**不继承父环境**，
    # 所以不能只信 os.environ——要从 qwen.env 里读。
    _v = os.environ.get("VISION_API_KEY")
    if _v:
        return _v.strip()
    try:
        for line in open(ENVF, encoding="utf-8"):
            if line.startswith("VISION_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    try:
        for line in open(ENVF, encoding="utf-8"):
            if line.startswith("QWEN_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get("QWEN_API_KEY", "")


def _allowed(path):
    real = os.path.realpath(path)
    for d in ALLOW_DIRS:
        root = os.path.realpath(d)
        if real == root or real.startswith(root + os.sep):
            return True
    return False


def _newest():
    best, best_m = "", 0.0
    for d in ALLOW_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.lower().endswith(EXTS):
                continue
            p = os.path.join(d, fn)
            try:
                m = os.path.getmtime(p)
            except Exception:
                continue
            if m > best_m:
                best, best_m = p, m
    return best


def _ask_model(path, question):
    key = api_key()
    if not key:
        return None, "没有配置看图用的 API key。"
    raw = open(path, "rb").read()
    mime = MIME.get(os.path.splitext(path)[1].lower(), "image/png")
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 400,
        "stream": False,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": question or DEFAULT_ASK},
            {"type": "image_url", "image_url": {"url": "data:%s;base64,%s"
                                                % (mime, base64.b64encode(raw).decode())}},
        ]}],
    }).encode("utf-8")
    req = urllib.request.Request(BASE + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    try:
        import usage_log
        _u = d.get("usage") or {}
        _det = _u.get("prompt_tokens_details") or {}
        _cached = _u.get("prompt_cache_hit_tokens")
        if _cached is None:
            _cached = _det.get("cached_tokens") if isinstance(_det, dict) else None
        usage_log.record("vision", MODEL, _u.get("prompt_tokens"),
                         _u.get("completion_tokens"), _cached)
    except Exception:
        pass
    txt = (d["choices"][0]["message"].get("content") or "").strip()
    log("ask ok %.1fs %s -> %s" % (time.time() - t0, os.path.basename(path), txt[:60]))
    return txt, None


def _cache_key(path):
    """同一张图 -> 同一个 key。文件被替换（大小/mtime 变了）就换 key。"""
    try:
        st = os.stat(path)
    except Exception:
        return ""
    return "%s|%d|%d" % (path, st.st_size, int(st.st_mtime))


def _cache_load():
    try:
        with open(CACHEF, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def _cache_get(key):
    d = _cache_load()
    hit = d.get(key)
    if isinstance(hit, dict) and hit.get("text"):
        return hit["text"]
    return None


def _cache_put(key, text):
    """写入并保持条目数上限（按时间旧->新丢）。"""
    if not key or not text:
        return
    d = _cache_load()
    d[key] = {"text": text, "ts": int(time.time())}
    if len(d) > CACHE_MAX:
        old = sorted(d.items(), key=lambda kv: kv[1].get("ts") or 0)
        for k, _ in old[: len(d) - CACHE_MAX]:
            d.pop(k, None)
    try:
        os.makedirs(os.path.dirname(CACHEF), exist_ok=True)
        tmp = CACHEF + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, CACHEF)
    except Exception as e:
        log("cache write fail: %s" % e)


def _calls_load():
    try:
        with open(CALLS, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            return [int(x) for x in d if isinstance(x, (int, float))]
    except Exception:
        pass
    return []


def _calls_recent():
    """最近一小时内真正调用模型的次数。"""
    now = time.time()
    return [t for t in _calls_load() if now - t < 3600]


def _calls_record():
    now = time.time()
    keep = [t for t in _calls_load() if now - t < 3600]
    keep.append(now)
    try:
        os.makedirs(os.path.dirname(CALLS), exist_ok=True)
        tmp = CALLS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(keep, f)
        os.replace(tmp, CALLS)
    except Exception as e:
        log("calls write fail: %s" % e)


def image_view(args):
    path = (args.get("path") or "").strip()
    question = (args.get("question") or "").strip()

    if not path:
        cand = _newest()
        if not cand:
            return "找不到可看的图片（允许目录里没有图片文件）。"
        if time.time() - os.path.getmtime(cand) > RECENT_WINDOW:
            return ("没拿到图片路径，也没有刚收到的新图（最新的一张是 %s，太旧了）。"
                    "让主人把图再发一次。" % os.path.basename(cand))
        path = cand
    # 允许传 "images/xxx.jpg" 这种相对形式，补成 media 目录
    if not os.path.isabs(path) and not path.startswith("media/"):
        cand = os.path.join(ALLOW_DIRS[0], os.path.basename(path))
        if os.path.exists(cand):
            path = cand
    if not os.path.isabs(path):
        return "要给绝对路径，例如 /opt/dsh/media/onebot/media_xxx.jpg"
    if not _allowed(path):
        return "这个路径不在允许范围（只能看 media/onebot 和 workspace 里的图）。"
    if not path.lower().endswith(EXTS):
        return "这不是图片文件（只支持 %s）。" % ", ".join(EXTS)
    if not os.path.exists(path):
        return "文件不存在：%s" % path
    if os.path.getsize(path) > MAX_BYTES:
        return "图太大了（超过 8MB），看不了。"

    # 同一张图只看一次：文件没变就复用上次的结论，不重复消耗视觉额度
    key = _cache_key(path)
    cached = _cache_get(key) if key else None
    if cached and not question:
        return cached
    if cached and question:
        # 换了问题就不再复用（问题不同答案不同），但也别重复烧钱——让模型基于缓存答
        return cached + chr(10) + chr(10) + "（这张之前看过，上面是上次的结论。你问的「%s」如果上面没提到，就直说没看清这部分。）" % question

    # 硬上限：群图逐张识别会把额度烧穿，这里兜底（缓存命中在上面已经返回，不计）
    used = len(_calls_recent())
    if used >= MAX_PER_HOUR:
        return ("这一小时已经看过 %d 张图，到上限了，先不看新的。"
                "跟发图的人说一声「这张我先存着，晚点再看」，别说自己不会看图。"
                % used)

    try:
        ans, err = _ask_model(path, question)
    except Exception as e:
        log("ask fail: %s" % traceback.format_exc())
        return "看图失败：%s" % e
    if err:
        return err
    if not ans:
        return "图加载出来了，但没读出内容，再说一次或者重发一张。"
    _cache_put(key, ans)
    _calls_record()
    return ans


TOOLS = [{
    "name": "image_view",
    "description": "看图片并告诉你图里是什么【这是唯一的看图工具，旧的 read_image 已删除，不要调它】。收到消息里的 [图片] 占位符时用它：把 [图片:路径] 里的路径作为 path 传进来；没有路径就省略 path，会自动取最新收到的那张。想知道具体细节就写进 question（例如「左边右边分别什么颜色」「这图什么梗」）。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "图片绝对路径，可省略"},
            "question": {"type": "string", "description": "想问关于这张图的什么，可省略"},
        },
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
                    "serverInfo": {"name": "jingjing-seer", "version": "1.0.0"}})
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
        if p.get("name") != "image_view":
            reply(mid, error={"code": -32602, "message": "unknown tool"})
            return
        try:
            out = image_view(p.get("arguments") or {})
            reply(mid, {"content": [{"type": "text", "text": str(out)}], "isError": False})
        except Exception as e:
            log("tool fail: %s" % traceback.format_exc())
            reply(mid, {"content": [{"type": "text", "text": "看图失败：%s" % e}], "isError": True})
        return
    if mid is not None:
        reply(mid, error={"code": -32601, "message": "method not found"})


def main():
    log("start pid=%d model=%s" % (os.getpid(), MODEL))
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