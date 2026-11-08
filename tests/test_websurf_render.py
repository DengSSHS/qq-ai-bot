#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器能力的两条关键回归：

1) 正文提取要挑"正文块"，不能被导航/侧栏挤满 —— 之前 clean_text 直接
   get_text()，SPA 站点抓回来只有一堆菜单，看起来像"浏览器太弱"。
2) 重型渲染器必须是"增强"而不是"必需"：没装、内存不够、超时，都要
   安静地退化成轻量抓取，绝不能抛异常把整个 web_get 拖垮。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "stack"))

import websurf  # noqa: E402

NAVY_PAGE = """<html><body>
<nav><a href="/">首页</a><a href="/hot">热门</a><a href="/me">我的</a></nav>
<aside><a href="/a">推荐A</a><a href="/b">推荐B</a><a href="/c">推荐C</a></aside>
<article>
<p>%s</p>
<p>%s</p>
</article>
<footer>版权所有</footer>
</body></html>"""

SPA_SHELL = """<html><head><title>百度贴吧</title></head>
<body><div id="app"></div><script src="/app.js"></script></body></html>"""


class CleanText(unittest.TestCase):
    def test_picks_article_over_nav(self):
        body = ("抹香鲸是齿鲸亚目中体型最大的物种，成年雄性可达十六米。" * 3,
                "它们主要捕食大王乌贼，潜水深度可以超过一千米。" * 3)
        text = websurf.clean_text(NAVY_PAGE % body, 3000)
        self.assertIn("抹香鲸", text)
        self.assertIn("大王乌贼", text)
        # 导航和页脚被剔掉了
        self.assertNotIn("版权所有", text)
        self.assertLess(text.count("推荐A"), 1)

    def test_spa_shell_yields_nothing(self):
        text = websurf.clean_text(SPA_SHELL, 3000)
        self.assertLess(len(text.strip()), 80, "纯脚本壳不该被当成正文")


class RenderFallback(unittest.TestCase):
    """渲染器必须是可失败的增强项。"""

    def test_missing_renderer_is_not_fatal(self):
        old = websurf.RENDER_PY
        websurf.RENDER_PY = "/nonexistent/web_render.py"
        try:
            ok, why = websurf.render_page("https://example.com/")
        finally:
            websurf.RENDER_PY = old
        self.assertFalse(ok)
        self.assertIn("没装", why)

    def test_low_memory_skips_browser(self):
        old_mem = websurf._mem_available_mb
        old_py = websurf.RENDER_PY
        websurf._mem_available_mb = lambda: 10.0
        websurf.RENDER_PY = os.path.join(HERE, "..", "stack", "web_render.py")
        try:
            ok, why = websurf.render_page("https://example.com/")
        finally:
            websurf._mem_available_mb = old_mem
            websurf.RENDER_PY = old_py
        self.assertFalse(ok)
        self.assertIn("内存", why)

    def test_render_never_raises_on_bad_url(self):
        ok, why = websurf.render_page("not-a-url")
        self.assertFalse(ok)
        self.assertTrue(why)


class ToolSchema(unittest.TestCase):
    def test_web_get_exposes_render_switch(self):
        tool = [t for t in websurf.TOOLS if t["name"] == "web_get"][0]
        props = tool["inputSchema"]["properties"]
        self.assertIn("render", props)
        self.assertEqual(props["render"]["enum"], ["auto", "never", "always"])


if __name__ == "__main__":
    unittest.main()
