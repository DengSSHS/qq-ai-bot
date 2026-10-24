#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""seer 的「同一张图只看一次」缓存测试。

背景：人设现在允许她看群里别人发的图（看一次就答），但反复看同一张会既慢又费钱。
所以 seer.py 里加了以「路径+大小+mtime」为 key 的描述缓存。
这组测试保证：命中、失效、边界都不会悄悄坏掉。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SEER = os.path.join(HERE, "..", "stack", "seer.py")


def load_seer(cache_path):
    spec = importlib.util.spec_from_file_location("seer_test", SEER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seer_test"] = mod
    spec.loader.exec_module(mod)
    mod.CACHEF = cache_path
    return mod


class VisionCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "vision_cache.json")
        self.m = load_seer(self.cache)
        self.img = os.path.join(self.tmp, "a.jpg")
        with open(self.img, "wb") as f:
            f.write(b"fake-image-bytes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_key_stable_for_same_file(self):
        k1 = self.m._cache_key(self.img)
        k2 = self.m._cache_key(self.img)
        self.assertTrue(k1)
        self.assertEqual(k1, k2)

    def test_key_changes_when_file_changes(self):
        """同一个路径换了内容（比如群友发了新图但文件名复用），不能复用旧描述。"""
        k1 = self.m._cache_key(self.img)
        with open(self.img, "wb") as f:
            f.write(b"different-bytes-longer")
        k2 = self.m._cache_key(self.img)
        self.assertNotEqual(k1, k2)

    def test_put_then_get(self):
        key = self.m._cache_key(self.img)
        self.assertIsNone(self.m._cache_get(key))
        self.m._cache_put(key, "一只蓝色的刺猬")
        self.assertEqual(self.m._cache_get(key), "一只蓝色的刺猬")

    def test_missing_key_returns_none(self):
        self.assertIsNone(self.m._cache_get("nope|1|2"))

    def test_put_ignores_empty(self):
        self.m._cache_put("", "text")
        self.m._cache_put("k|1|2", "")
        self.assertIsNone(self.m._cache_get("k|1|2"))

    def test_cache_file_is_valid_json(self):
        key = self.m._cache_key(self.img)
        self.m._cache_put(key, "描述")
        import json
        with open(self.cache, encoding="utf-8") as f:
            d = json.load(f)
        self.assertIn(key, d)
        self.assertEqual(d[key]["text"], "描述")

    def test_trims_to_max_entries(self):
        """条目上限要生效，否则缓存文件会一直长。"""
        self.m.CACHE_MAX = 3
        for i in range(6):
            self.m._cache_put("k%d" % i, "t%d" % i)
        import json
        with open(self.cache, encoding="utf-8") as f:
            d = json.load(f)
        self.assertLessEqual(len(d), 3)


if __name__ == "__main__":
    unittest.main()
