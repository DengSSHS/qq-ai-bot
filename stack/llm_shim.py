#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 转发层：给上游请求强制加上"关思考"

为什么需要它
------------
DSH 的 provider 配置里 `reasoning: "off"` 表达的是"不指定档位"，也就是
**什么字段都不发**；而部分聚合网关（例如千问平台的 OpenAI 兼容口）默认
就是开思考，于是"关"这个动作在链路上消失了。

实测：同一个 deepseek-v4.1-flash-1017 模型，
  - 默认（不发字段）：思考 261 字，总耗时 2.2s（短任务）
  - thinking={"type":"disabled"}：思考 0 字
  - 真实群里一轮：思考 379~1387 字，output 117~276 token，单次 9~13s

这个转发层坐在 DSH 和上游之间，把 `thinking: {"type":"disabled"}` 注入进去，
其余字节原样透传（含 SSE 流式）。

用法
----
    python3 llm_shim.py            # 监听 127.0.0.1:8890
    python3 llm_shim.py --port 8891

然后把 provider 的 baseURL 指过来：
    https://api.deepseek.com
  → http://127.0.0.1:8890/v1

只依赖标准库。日志在 /opt/dsh/memory/llm_shim.log。
"""
import argparse
import http.client
import json
import os
import ssl
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 上游真实地址（含路径前缀），以及本转发层对外呈现的前缀
UPSTREAM = os.environ.get("LLM_UPSTREAM", "https://api.deepseek.com")
LOCAL_BASE = os.environ.get("LLM_LOCAL_BASE", "/v1")
LOG = os.environ.get("LLM_SHIM_LOG", "/opt/dsh/memory/llm_shim.log")
# 要注入的"关思考"写法。不同网关认的字段不同，按顺序试第一个生效的
DISABLE_PAYLOADS = [
    ("thinking", {"type": "disabled"}),      # DeepSeek 系 / 多数聚合口
    ("enable_thinking", False),              # 通义/千问系
]


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%F %T"), msg))
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _upstream_conn(self):
        u = urllib.parse.urlsplit(UPSTREAM)
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=300, context=ctx)
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=300)

    def _forward(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        body = raw
        injected = None

        if raw and "chat/completions" in self.path and method == "POST":
            try:
                doc = json.loads(raw)
                key, value = DISABLE_PAYLOADS[0]
                doc[key] = value
                # 记录请求体构成，便于排查"为什么每次要发 1MB"
                try:
                    msgs = doc.get("messages") or []
                    by_role = {}
                    for m in msgs:
                        r = m.get("role", "?")
                        c = m.get("content")
                        ln = len(c) if isinstance(c, str) else len(json.dumps(c, ensure_ascii=False))
                        by_role[r] = by_role.get(r, 0) + ln
                    tools_len = len(json.dumps(doc.get("tools") or [], ensure_ascii=False))
                    log("BODY msgs=%d chars=%d tools=%d  %s"
                        % (len(msgs), sum(by_role.values()), tools_len,
                           " ".join("%s:%d" % kv for kv in sorted(by_role.items()))))
                    sized = []
                    for m in msgs:
                        c = m.get("content")
                        ln = len(c) if isinstance(c, str) else len(json.dumps(c, ensure_ascii=False))
                        head = str(c)[:140].replace(chr(10), " ").replace(chr(13), " ")
                        sized.append((ln, m.get("role"), head))
                    for ln, role, head in sorted(sized, reverse=True)[:3]:
                        log("  TOP %s %7d  %s" % (role, ln, head))
                except Exception:
                    pass
                body = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                injected = key
            except Exception:
                body = raw

        u = urllib.parse.urlsplit(UPSTREAM)
        # 把本地前缀换成本地上游前缀：/v1/chat/completions
        #   -> /compatible-mode/v1/chat/completions
        rest = self.path
        if LOCAL_BASE and rest.startswith(LOCAL_BASE):
            rest = rest[len(LOCAL_BASE):]
        path = u.path.rstrip("/") + rest
        headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept": self.headers.get("Accept", "*/*"),
        }
        auth = self.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth

        t0 = time.time()
        try:
            conn = self._upstream_conn()
            conn.request(method, path, body=body if method != "GET" else None, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            log("upstream fail: %s" % e)
            msg = json.dumps({"error": {"message": "upstream unreachable: %s" % e}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        total = 0
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
            log("%s %s -> %d  in=%dB out=%dB  %.2fs  inject=%s"
                % (method, self.path, resp.status, len(body), total,
                   time.time() - t0, injected or "-"))

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        self._forward("GET")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8890)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    log("listening %s:%d -> %s" % (a.host, a.port, UPSTREAM))
    print("llm shim listening on http://%s:%d  -> %s" % (a.host, a.port, UPSTREAM))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
