#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小浏览器（MCP / stdio）：搜索 + 读网页

两个能力：
  web_search(q, n)  -> 走 cn.bing.com 抓标题/链接/摘要
  web_get(url)      -> 抓网页正文（去标签、压空白、限制字符数）

为什么自己做：模型侧自带的联网搜索不一定开得通，也不一定按我们的省钱规则来。
这个服务是本地的、可控的、不额外花 token（只花抓取的时间）。

只用标准库 + 可选 bs4；抓不到就明说抓不到，不编。
"""
import ipaddress
import json
import os
import re
import socket
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 20
MAX_TEXT = 6000
LOG = "/opt/dsh/memory/websurf.log"
PROTO = "2024-11-05"

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s" % (datetime.now().strftime("%F %T"), msg) + chr(10))
    except Exception:
        pass


# ── 防 SSRF：只允许公网 http/https ─────────────────────────────────────
# 她能被群友一句话支使去抓网址，所以必须挡住内网/本机/元数据地址。
# 判定用白名单（is_global），不用黑名单：私网、回环、链路本地、CGNAT、
# 保留段、组播、以及云元数据 169.254.169.254 全都在 is_global 之外。
MAX_REDIRECTS = 5
MAX_BYTES = 3 * 1024 * 1024
REDIRECT_CODES = (301, 302, 303, 307, 308)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不让 urllib 自动跟跳转，每一跳都要重新校验，否则公网地址能 302 到 127.0.0.1。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _ip_blocked_reason(ip_text):
    """返回阻止原因；公网地址返回 None。"""
    try:
        ip = ipaddress.ip_address(str(ip_text).strip().strip("[]"))
    except ValueError:
        return "地址无法识别"
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # is_global 是主判定；但某些版本的 is_global 把组播也算成 global，
    # 所以组播/保留/回环/链路本地这几个再显式排除一次，不靠单点判断。
    if ip.is_multicast or ip.is_reserved or ip.is_loopback \
            or ip.is_link_local or ip.is_unspecified or ip.is_private:
        return str(ip)
    if ip.is_global:
        return None
    return str(ip)


def _resolve_safe(host, port, allow_private=False):
    """解析域名，任何一个解析结果落在非公网就整条拒绝（防 DNS 混答）。

    allow_private=True 只用于**用户自己在配置里写死的固定地址**（例如自建 SearXNG
    监听 127.0.0.1）。信任边界来自"谁提供这个地址"，不是地址本身：
    模型/群友给的 URL 永远走 allow_private=False。
    """
    h = str(host or "").strip().lower().strip("[]")
    if not h:
        raise ValueError("网址里没有主机名")
    if not allow_private and (h == "localhost" or h.endswith(".localhost")
                              or h.endswith(".local") or h.endswith(".internal")):
        raise ValueError("禁止访问本机/内网地址")
    try:
        infos = socket.getaddrinfo(h, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError("域名解析失败：%s" % e)
    addrs = []
    for info in infos:
        ip = info[4][0]
        if not allow_private:
            bad = _ip_blocked_reason(ip)
            if bad:
                raise ValueError("这个网址指向内网/本机地址（%s），已阻止" % bad)
        if ip not in addrs:
            addrs.append(ip)
    if not addrs:
        raise ValueError("域名没有解析出可用地址")
    return addrs


def _getaddrinfo_pinned(host, ip):
    """把某个域名的解析结果钉死成已校验的那个 IP，防 DNS rebinding 换地址。"""
    real = socket.getaddrinfo
    host = str(host)
    ip = str(ip)
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET

    def patched(h, p, *a, **kw):
        if str(h) == host:
            return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, p))]
        return real(h, p, *a, **kw)

    return real, patched


def _fetch_once(url, timeout):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("只允许 http/https 网址")
    host = parts.hostname
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    addrs = _resolve_safe(host, port)

    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml",
    })
    # 空的 ProxyHandler：不吃环境变量里的代理，避免绕开校验
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    last = None
    for ip in addrs:
        real, patched = _getaddrinfo_pinned(host, ip)
        try:
            socket.getaddrinfo = patched
            with opener.open(req, timeout=timeout) as r:
                raw = r.read(MAX_BYTES)
                charset = r.headers.get_content_charset() or "utf-8"
                return raw, charset
        except urllib.error.HTTPError as e:
            if e.code in REDIRECT_CODES:
                loc = e.headers.get("Location") or ""
                e.close()
                if not loc:
                    raise ValueError("重定向没有给出目标地址")
                raise _Redirect(urllib.parse.urljoin(url, loc))
            raise
        except (OSError, socket.timeout) as e:
            last = e
            continue
        finally:
            socket.getaddrinfo = real
    raise ValueError("连不上：%s" % last)


class _Redirect(Exception):
    def __init__(self, url):
        Exception.__init__(self, url)
        self.url = url


def fetch(url, timeout=TIMEOUT, _hop=0):
    try:
        raw, charset = _fetch_once(url, timeout)
    except _Redirect as r:
        if _hop >= MAX_REDIRECTS:
            raise ValueError("重定向超过了 %d 次，放弃" % MAX_REDIRECTS)
        log("redirect %s -> %s" % (url, r.url))
        return fetch(r.url, timeout, _hop + 1)
    except ValueError as e:
        log("blocked %s : %s" % (url, e))
        raise
    for enc in (charset, "utf-8", "gb18030"):
        try:
            return raw.decode(enc, "strict")
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def clean_text(html, limit=MAX_TEXT):
    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "iframe", "svg"]):
            tag.decompose()
        text = soup.get_text(chr(10))
    else:
        html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", html)
    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    out = chr(10).join(lines)
    if len(out) > limit:
        out = out[:limit] + chr(10) + "…（已截断，原文更长）"
    return out


def _strip(s):
    return re.sub(r"(?s)<[^>]+>", "", s or "").replace("&nbsp;", " ").replace("&amp;", "&").strip()


SEARXNG_URL = (os.environ.get("SEARXNG_URL") or "").rstrip("/")
SEARCH_BACKEND = (os.environ.get("SEARCH_BACKEND") or "auto").strip().lower()


def _search_searxng(q, n):
    """自建 SearXNG 的 JSON API。

    为什么加这个：另外两个后端靠正则解析搜索引擎的 HTML，上游一改版就碎。
    SearXNG 返回 JSON，结构稳定；而且可以自建，不受出口限制影响。

    地址来自用户的环境变量（不是模型给的），所以允许内网——这正是
    fetch(allow_private=True) 的用途。模型/群友给的 URL 仍然只允许公网。
    """
    if not SEARXNG_URL:
        raise RuntimeError("没有配置 SEARXNG_URL")
    url = "%s/search?q=%s&format=json" % (SEARXNG_URL, urllib.parse.quote(q))
    raw = fetch(url, allow_private=True)
    doc = json.loads(raw)
    items = []
    for r in (doc.get("results") or []):
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip()
        link = str(r.get("url") or "").strip()
        snippet = str(r.get("content") or "").strip()
        if title and link:
            items.append((title, link, snippet))
        if len(items) >= n:
            break
    return items


def _search_so(q, n):
    """360 搜索：中文效果最好，结果块 class=res-list"""
    url = "https://www.so.com/s?q=" + urllib.parse.quote(q)
    html = fetch(url)
    items = []
    for b in re.split(r"<li[^>]*class=\"res-list", html)[1:]:
        m = re.search(r"<h3[^>]*>.*?<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", b, re.S)
        if not m:
            continue
        title = _strip(m.group(2))
        href = m.group(1)
        if href.startswith("/"):
            href = "https://www.so.com" + href
        if not title:
            continue
        sm = re.search(r'class="res-desc"[^>]*>(.*?)</p>', b, re.S) or re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
        snippet = _strip(sm.group(1)) if sm else ""
        items.append((title, href, snippet))
        if len(items) >= n:
            break
    return items


def _search_bing(q, n):
    """Bing：先试 RSS（干净），再退 HTML"""
    items = []
    try:
        xml = fetch("https://cn.bing.com/search?q=" + urllib.parse.quote(q) + "&format=rss&count=%d" % (n + 2))
        for m in re.finditer(r"(?s)<item>(.*?)</item>", xml):
            blk = m.group(1)
            t = re.search(r"<title>(.*?)</title>", blk, re.S)
            l = re.search(r"<link>(.*?)</link>", blk, re.S)
            d = re.search(r"<description>(.*?)</description>", blk, re.S)
            if t and l:
                items.append((_strip(t.group(1)), _strip(l.group(1)), _strip(d.group(1)) if d else ""))
            if len(items) >= n:
                break
    except Exception:
        pass
    if items:
        return items
    html = fetch("https://cn.bing.com/search?q=" + urllib.parse.quote(q) + "&count=%d" % (n + 2))
    for b in re.split(r'class="b_algo"', html)[1:]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="(http[^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if m and _strip(m.group(2)):
            items.append((_strip(m.group(2)), m.group(1), ""))
        if len(items) >= n:
            break
    return items


def tool_web_search(args):
    q = (args.get("q") or args.get("query") or "").strip()
    if not q:
        return "要给个搜索词。"
    n = int(args.get("n") or 6)
    n = max(1, min(n, 10))
    items, src = [], ""
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", q))
    # 后端顺序：SEARCH_BACKEND 显式指定优先；auto 时配了 SearXNG 就用它打头
    if has_cjk:
        html_order = (("360", _search_so), ("Bing", _search_bing))
    else:
        html_order = (("Bing", _search_bing), ("360", _search_so))
    if SEARCH_BACKEND == "searxng":
        order = (("SearXNG", _search_searxng),) + html_order
    elif SEARCH_BACKEND in ("bing", "so", "360"):
        want = "Bing" if SEARCH_BACKEND == "bing" else "360"
        order = tuple(x for x in html_order if x[0] == want) or html_order
    elif SEARXNG_URL:
        order = (("SearXNG", _search_searxng),) + html_order    # auto + 有配置就优先
    else:
        order = html_order
    for name, fn in order:
        try:
            items = fn(q, n)
        except Exception as e:
            log("search %s fail %s: %s" % (name, q, e))
            items = []
        items = [it for it in items if not re.search(r"(^|//)(news|fanyi|image|map|zhidao)\.so\.com|\.so\.com/s\?|/link\?m=.*&src=onebox", it[1])]
        if items:
            src = name
            break
    if not items:
        return "没搜到结果（所有搜索源都没给出内容），换个说法再试。"
    out = ["（来源：%s）" % src]
    for i, (t, u, s) in enumerate(items, 1):
        out.append("%d. %s" % (i, t))
        out.append("   " + u)
        if s:
            out.append("   " + s[:200])
    return chr(10).join(out)


def tool_web_get(args):
    url = (args.get("url") or "").strip()
    if not url.startswith("http"):
        return "要给一个 http(s) 开头的网址。"
    limit = int(args.get("max_chars") or MAX_TEXT)
    limit = max(500, min(limit, 20000))
    try:
        html = fetch(url)
    except Exception as e:
        log("get fail %s: %s" % (url, e))
        return "打不开这个网址：%s" % e
    text = clean_text(html, limit)
    if not text.strip():
        return "这个页面没有可读正文（可能是脚本渲染的，或者需要登录）。"
    return text


TOOLS = [
    {
        "name": "web_search",
        "description": "联网搜索（走 Bing）。要查新闻、价格、某人近况、某个东西是什么的时候用。返回标题、链接、摘要。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "搜索词"},
                "n": {"type": "integer", "description": "要几条结果，默认 6，最多 10"},
            },
            "required": ["q"],
        },
    },
    {
        "name": "web_get",
        "description": "打开一个网页，取它的正文。想读链接里的内容、看原文的时候用。正文太长会截断。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http(s) 网址"},
                "max_chars": {"type": "integer", "description": "最多返回多少字符，默认 6000"},
            },
            "required": ["url"],
        },
    },
]

HANDLERS = {"web_search": tool_web_search, "web_get": tool_web_get}


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + chr(10))
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    mid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        reply(mid, {
            "protocolVersion": PROTO,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "jingjing-websurf", "version": "1.0.0"},
        })
        return
    if method in ("notifications/initialized", "initialized", "ping"):
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
            reply(mid, {"content": [{"type": "text", "text": "出错了：%s" % e}], "isError": True})
        return
    if mid is None:
        return
    reply(mid, error={"code": -32601, "message": "method not found: %s" % method})


def main():
    log("start pid=%d bs4=%s" % (sys.stdin.fileno(), HAS_BS4))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception:
            log("handler crash: %s" % traceback.format_exc())


if __name__ == "__main__":
    main()
