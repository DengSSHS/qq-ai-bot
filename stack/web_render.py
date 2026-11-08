#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器渲染（Playwright + headless Chromium）—— websurf 的重型兜底

为什么需要：贴吧/知乎/微博这类站点整页都是 SPA，HTML 里只有一个
`<div id="app"></div>`，抓源码永远是空壳。想读到正文只能真的跑浏览器。

设计约束（服务器只有 1.9G 内存，QQ 已占 500M）：
  · 不常驻。按需启动、读完就 close，进程退干净。
  · 屏蔽图片/字体/媒体，省内存也省流量。
  · 只允许公网 http(s)：复用 websurf 的 SSRF 校验。
  · 超时硬上限，卡死也不会挂着占内存。

用法：python3 web_render.py <url> [wait_ms] [max_chars]
输出：JSON 一行 {"ok":true,"title":..,"text":..} 或 {"ok":false,"error":..}
"""
import json
import os
import re
import sys

sys.path.insert(0, "/opt/dsh")

# 页面内挑正文容器：文字多、链接少的块
PICK_JS = r"""
() => {
  const bad = /nav|header|footer|sidebar|side-bar|menu|breadcrumb|toolbar|topbar|advert|recommend|hot-?list|relate/i;
  const dirty = (el) => (bad.test(el.className || '') || bad.test(el.id || ''));
  const score = (el) => {
    if (dirty(el)) return 0;
    const t = (el.innerText || '').trim();
    if (t.length < 120) return 0;
    const cjkOrWord = (t.match(/[\u4e00-\u9fff]|[A-Za-z]{2,}/g) || []).length;
    if (cjkOrWord < t.length * 0.35) return 0;
    let linkLen = 0;
    el.querySelectorAll('a').forEach(a => { linkLen += (a.innerText || '').length; });
    return t.length * (1 - Math.min(1, linkLen / Math.max(1, t.length)));
  };
  let best = null, bestScore = 0;
  const cands = [...document.querySelectorAll('article, main, [class*=content], [id*=content], [class*=post], [id*=post]')];
  cands.push(document.body);
  for (const el of cands) {
    const s = score(el);
    if (s > bestScore) { best = el; bestScore = s; }
  }
  const node = (best || document.body).cloneNode(true);
  // 把侧栏/推荐/导航这些子树从副本里摘掉，再取文字
  // （既按 class/id 匹配，也按标签名——很多站点只给 <nav> 不给 class）
  node.querySelectorAll('nav, header, footer, aside, [role=navigation]').forEach(el => el.remove());
  node.querySelectorAll('*').forEach(el => { if (dirty(el)) el.remove(); });
  return node.innerText;
}
"""

BLOCK = {"image", "media", "font"}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "要给网址"}))
        return
    url = sys.argv[1]
    wait_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 2500
    max_chars = int(sys.argv[3]) if len(sys.argv) > 3 else 6000

    # 复用 websurf 的 SSRF 校验，别让群友一句话就让我去访问内网
    try:
        import websurf
        websurf.fetch  # noqa: F401  只为确认模块可用
    except Exception as e:
        print(json.dumps({"ok": False, "error": "校验模块不可用: %s" % e}))
        return
    try:
        websurf._resolve_safe(*_host_port(url))
    except Exception as e:
        print(json.dumps({"ok": False, "error": "网址被拒: %s" % e}))
        return

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(json.dumps({"ok": False, "error": "没有 playwright: %s" % e}))
        return

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-extensions", "--disable-background-networking",
                "--disable-features=Translate,BackForwardCache",
                "--blink-settings=imagesEnabled=false",
            ])
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
                locale="zh-CN",
                viewport={"width": 1280, "height": 900},
            )
            # 图/字体/媒体一律不加载：省内存，正文不需要它们
            ctx.route("**/*", lambda route: route.abort()
                      if route.request.resource_type in BLOCK else route.continue_())
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # 等正文块出现（不同站点的正文容器不一样，谁先出现就用谁）
            try:
                page.wait_for_selector(
                    "article, main, .d_post_content, .j_thread_list, [class*=content], [id*=content]",
                    timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(min(wait_ms, 1500))
            title = page.title()
            text = page.evaluate(PICK_JS) or ""
            browser.close()
    except Exception as e:
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])},
                         ensure_ascii=False))
        return

    JUNK = re.compile(
        r"^(window\.|function\s*\(|var\s|const\s|let\s|[{}();]+$|!function|"
        r"Player version|Video (url|volume|time|duration)|icon_|[0-9]{2}:[0-9]{2}$)")
    kept = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or JUNK.match(s):
            continue
        # 导航小标签（“关注 / 发贴 / 精华 / 视频”这种 1~4 字的词）不是正文。
        # 判据：很短且没有任何标点——正常句子哪怕是“好短。”也带标点。
        if len(s) <= 4 and not re.search(r"[\u3000-\u303f\uff00-\uffef.,!?;:]", s):
            continue
        kept.append(s)
    text = "\n".join(kept)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（已截断）"
    print(json.dumps({"ok": True, "title": title, "text": text}, ensure_ascii=False))


def _host_port(url):
    import urllib.parse
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("只允许 http/https")
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


if __name__ == "__main__":
    main()
