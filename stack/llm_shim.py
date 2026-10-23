#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 转发层：给上游请求强制加上"关思考"

为什么需要它
------------
DSH 的 provider 配置里 `reasoning: "off"` 表达的是"不指定档位"，也就是
**什么字段都不发**；而部分聚合网关（例如千问平台的 OpenAI 兼容口）默认
就是开思考，于是"关"这个动作在链路上消失了。

实测：同一个 deepseek-v4.1-flash-1017 模型，
  - 默认（不发字段）：思考 261 字
  - thinking={"type":"disabled"}：思考 0 字
  - 真实群里一轮：思考 379~1387 字，output 117~276 token，单次 9~13s

自适应（重要）
--------------
不同网关认的字段不一样，所以这里**不假定第一个能生效**：
转发响应时嗅探有没有产生 reasoning_content，没关掉就自动换下一个候选字段；
全部候选都失败会打 ERROR 日志，不会静默失效。

想强制指定字段：环境变量 SHIM_DISABLE_FIELD=thinking 或 enable_thinking

用法
----
    python3 llm_shim.py            # 监听 127.0.0.1:8890
    python3 llm_shim.py --port 8891

然后把 provider 的 baseURL 指过来：
    https://<你的上游地址>/compatible-mode/v1
  → http://127.0.0.1:8890/v1

只依赖标准库。日志在 /opt/dsh/memory/llm_shim.log。
"""
import argparse
import http.client
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 上游真实地址（含路径前缀），以及本转发层对外呈现的前缀
UPSTREAM = os.environ.get("LLM_UPSTREAM", "https://api.deepseek.com")
LOCAL_BASE = os.environ.get("LLM_LOCAL_BASE", "/v1")
LOG = os.environ.get("LLM_SHIM_LOG", "/opt/dsh/memory/llm_shim.log")

# 禁用思考的候选写法，按顺序尝试
DISABLE_PAYLOADS = [
    ("thinking", {"type": "disabled"}),   # DeepSeek 系 / 多数聚合口
    ("enable_thinking", False),           # 通义 / 千问系
]

# 运行状态：当前用哪个候选、是否已确认生效、连续失败次数
_state = {
    "idx": 0,
    "ok": False,
    "fails": 0,
    "last_alarm": 0.0,
}

# 响应里"思考内容非空" / "思考内容为空" 的特征
RE_NONEMPTY = re.compile(r'"reasoning_content"\s*:\s*"[^"]+"')
RE_EMPTY = re.compile(r'"reasoning_content"\s*:\s*""')
RE_HAS_FIELD = re.compile(r'"reasoning_content"')


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%F %T"), msg))
    except Exception:
        pass


def picked_payload():
    """当前要注入的 (字段名, 值)。环境变量可强制指定。"""
    forced = os.environ.get("SHIM_DISABLE_FIELD", "").strip()
    if forced:
        for k, v in DISABLE_PAYLOADS:
            if k == forced:
                return k, v
    idx = min(_state["idx"], len(DISABLE_PAYLOADS) - 1)
    return DISABLE_PAYLOADS[idx]


def sniff(text):
    """根据一次响应碎片判断注入是否生效。

    注意：思考被真正关掉时，很多网关**根本不返回 reasoning_content 字段**
    （而不是返回空字符串）。所以"没有该字段"也要视为生效，否则永远确认不了。
    """
    if RE_NONEMPTY.search(text):
        # 上游仍在思考 → 当前字段没用
        _state["ok"] = False
        _state["fails"] += 1
        cur = DISABLE_PAYLOADS[min(_state["idx"], len(DISABLE_PAYLOADS) - 1)][0]
        if _state["idx"] < len(DISABLE_PAYLOADS) - 1:
            _state["idx"] += 1
            nxt = DISABLE_PAYLOADS[_state["idx"]][0]
            log("ALARM 字段 %s 没生效（上游仍在思考），自动切换到 %s" % (cur, nxt))
        else:
            now = time.time()
            if now - _state["last_alarm"] > 600:
                _state["last_alarm"] = now
                log(
                    "ERROR 所有候选字段都关不掉思考（%s）。速度优化已失效——"
                    "请用 SHIM_DISABLE_FIELD 指定正确字段，或确认该网关支持禁用思考。"
                    % ", ".join(k for k, _ in DISABLE_PAYLOADS)
                )
        return

    # 没有非空思考内容：空字段，或压根没有这个字段
    if not _state["ok"]:
        _state["ok"] = True
        cur = DISABLE_PAYLOADS[min(_state["idx"], len(DISABLE_PAYLOADS) - 1)][0]
        kind = "字段为空" if RE_EMPTY.search(text) else "响应里没有该字段"
        log("OK 字段 %s 已生效（%s）" % (cur, kind))


def describe_body(doc):
    """记录请求体构成，便于排查"为什么每次要发 1MB"。"""
    try:
        msgs = doc.get("messages") or []
        by_role = {}
        for m in msgs:
            role = m.get("role", "?")
            content = m.get("content")
            ln = len(content) if isinstance(content, str) else len(json.dumps(content, ensure_ascii=False))
            by_role[role] = by_role.get(role, 0) + ln
        tools_len = len(json.dumps(doc.get("tools") or [], ensure_ascii=False))
        log("BODY msgs=%d chars=%d tools=%d  %s"
            % (len(msgs), sum(by_role.values()), tools_len,
               " ".join("%s:%d" % kv for kv in sorted(by_role.items()))))
        sized = []
        for m in msgs:
            content = m.get("content")
            ln = len(content) if isinstance(content, str) else len(json.dumps(content, ensure_ascii=False))
            head = str(content)[:140].replace(chr(10), " ").replace(chr(13), " ")
            sized.append((ln, m.get("role"), head))
        for ln, role, head in sorted(sized, reverse=True)[:3]:
            log("  TOP %s %7d  %s" % (role, ln, head))
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
        is_completion = "chat/completions" in self.path and method == "POST"

        if is_completion and raw:
            try:
                doc = json.loads(raw)
                key, value = picked_payload()
                doc[key] = value
                describe_body(doc)
                body = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                injected = key
            except Exception:
                body = raw

        u = urllib.parse.urlsplit(UPSTREAM)
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
        probe = bytearray()   # 只留前 32KB，用来嗅探思考是否真的关掉了
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                if len(probe) < 32768:
                    probe.extend(chunk[:32768 - len(probe)])
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

        if is_completion and probe:
            sniff(probe.decode("utf-8", "replace"))

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
    log("listening %s:%d -> %s (候选字段: %s)"
        % (a.host, a.port, UPSTREAM, ", ".join(k for k, _ in DISABLE_PAYLOADS)))
    print("llm shim listening on http://%s:%d  -> %s" % (a.host, a.port, UPSTREAM))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
