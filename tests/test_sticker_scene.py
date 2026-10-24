#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sticker_pick 的 scene 容错测试。

背景（2026-10-25 实测）：
    人设让模型把「这次的场合」写进 scene，比如 scene="群里有人在开玩笑"。
    但 scene 是精确子串匹配固定分类名（开心笑/嘲讽怼人/无语尴尬…），
    自然语言一个都匹配不上 → 返回「一张都没有」→ 模型干脆不发表情。
    结果一天 76 条发送，表情 0 条，sticker_pick 调用 0 次。

修法：scene 没命中就退回全库（保留 query/安全/去重过滤），并告诉她可用分类名。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "..", "stack", "stickerlib.py")


def load_lib(tmp):
    """导入模块并把它的所有数据路径指到临时目录。"""
    spec = importlib.util.spec_from_file_location("stickerlib_test", LIB)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stickerlib_test"] = mod
    spec.loader.exec_module(mod)
    mod.STICKERS = os.path.join(tmp, "stickers.json")
    mod.TAGS = os.path.join(tmp, "tags.json")
    mod.EXTRAS = os.path.join(tmp, "extras.json")
    return mod


class SceneFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_lib(self.tmp)

        # 两张图、两种场景
        files = []
        for name in ("a.png", "b.png"):
            p = os.path.join(self.tmp, name)
            with open(p, "wb") as f:
                f.write(b"x" * 16)
            files.append(p)

        with open(self.m.STICKERS, "w", encoding="utf-8") as f:
            json.dump([
                {"id": "id_a", "name": "鲸鱼娘-001", "file": files[0], "hits": 3},
                {"id": "id_b", "name": "菲比-002", "file": files[1], "hits": 1},
            ], f, ensure_ascii=False)

        with open(self.m.TAGS, "w", encoding="utf-8") as f:
            json.dump({
                "id_a": {"scene": "开心笑", "text": ""},
                "id_b": {"scene": "无语尴尬", "text": ""},
            }, f, ensure_ascii=False)

        with open(self.m.EXTRAS, "w", encoding="utf-8") as f:
            json.dump({}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exact_scene_still_filters(self):
        out = self.m.pick({"scene": "开心笑"})
        self.assertIn("挑到", out)
        self.assertIn("id_a", out)
        self.assertNotIn("id_b", out)

    def test_natural_language_scene_falls_back(self):
        """这是本次修复的核心：自然语言场景不该再返回「一张都没有」。"""
        out = self.m.pick({"scene": "群里有人在开玩笑"})
        self.assertIn("挑到", out)
        self.assertNotIn("一张都没有", out)
        self.assertIn("没对上任何分类", out)

    def test_fallback_lists_known_scenes(self):
        out = self.m.pick({"scene": "乱写的"})
        self.assertIn("开心笑", out)
        self.assertIn("无语尴尬", out)

    def test_no_stickers_at_all_still_errors(self):
        with open(self.m.STICKERS, "w", encoding="utf-8") as f:
            json.dump([], f)
        out = self.m.pick({"scene": "开心笑"})
        self.assertIn("读不到", out)

    def test_query_mismatch_with_scene_falls_back_to_full_library(self):
        """query 也会过滤；scene 失配时退回全库但 query 依然生效。"""
        out = self.m.pick({"scene": "乱写的", "query": "菲比"})
        self.assertIn("挑到", out)
        self.assertIn("id_b", out)
        self.assertNotIn("id_a", out)


if __name__ == "__main__":
    unittest.main()
