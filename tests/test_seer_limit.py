#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""seer 的每小时识别上限测试。

背景（2026-10-25）：
    人设放宽后，她在群里对**任何一张图**都去识别：3 秒一张、几千 token 一张。
    人设收紧是一半，另一半必须由代码兜底——否则模型偶尔听话、偶尔不听话，
    账单是主人付。

规则：只对**新图**计数（缓存命中免费），超过 MAX_PER_HOUR 直接拒绝并给出话术。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SEER = os.path.join(HERE, "..", "stack", "seer.py")


def load_seer(tmp):
    spec = importlib.util.spec_from_file_location("seer_limit_test", SEER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seer_limit_test"] = mod
    spec.loader.exec_module(mod)
    mod.CACHEF = os.path.join(tmp, "cache.json")
    mod.CALLS = os.path.join(tmp, "calls.json")
    return mod


class RateLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_seer(self.tmp)
        # seer 的路径白名单只放行 media/onebot 与 workspace，
        # 测试里把允许目录指到临时目录（否则会被正确地拒绕）
        self.m.ALLOW_DIRS = [self.tmp]
        self.img = os.path.join(self.tmp, "x.jpg")
        with open(self.img, "wb") as f:
            f.write(b"fake")
        self.calls = []

        def fake_ask(path, question):
            self.calls.append(path)
            return "一张测试图", None

        self.m._ask_model = fake_ask

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fill(self, n):
        now = time.time()
        with open(self.m.CALLS, "w", encoding="utf-8") as f:
            json.dump([now - 5] * n, f)

    def test_under_limit_calls_model(self):
        self._fill(3)
        out = self.m.image_view({"path": self.img})
        self.assertEqual(out, "一张测试图")
        self.assertEqual(len(self.calls), 1)

    def test_at_limit_refuses(self):
        self._fill(self.m.MAX_PER_HOUR)
        out = self.m.image_view({"path": self.img})
        self.assertIn("到上限了", out)
        self.assertEqual(self.calls, [], "到上限后不许再调模型")

    def test_old_calls_expire(self):
        """一小时前的调用不该占额度。"""
        old = time.time() - 3700
        with open(self.m.CALLS, "w", encoding="utf-8") as f:
            json.dump([old] * 50, f)
        self.assertEqual(len(self.m._calls_recent()), 0)
        out = self.m.image_view({"path": self.img})
        self.assertEqual(out, "一张测试图")

    def test_cache_hit_does_not_consume_quota(self):
        """同一张图第二次问，走缓存，不该消耗额度。"""
        self._fill(0)
        self.m.image_view({"path": self.img})
        self.assertEqual(len(self.m._calls_recent()), 1)
        self.m.image_view({"path": self.img})
        self.assertEqual(len(self.m._calls_recent()), 1, "缓存命中不应记一次调用")

    def test_refusal_message_is_actionable(self):
        self._fill(self.m.MAX_PER_HOUR)
        out = self.m.image_view({"path": self.img})
        # 要告诉她「怎么说」，而不是只报个错
        self.assertIn("先存着", out)
        self.assertIn("上限", out)


if __name__ == "__main__":
    unittest.main()
