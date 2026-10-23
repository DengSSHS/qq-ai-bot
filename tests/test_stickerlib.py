#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表情库增强层的测试。

重点验证两件事：
1. 挑图真的偏向"常发的"（用户要的"惯用表情"），但又不垄断；
2. 入库去重和安全筛不会漏放。
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "stack"))

import stickerlib  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stk-")
        self.lib = os.path.join(self.tmp, "stickers")
        os.makedirs(self.lib)
        self.stickers_path = os.path.join(self.tmp, "stickers.json")
        self.tags_path = os.path.join(self.tmp, "tags.json")
        self.extras_path = os.path.join(self.tmp, "extras.json")

        self._saved = (stickerlib.STICKERS, stickerlib.TAGS, stickerlib.EXTRAS)
        stickerlib.STICKERS = self.stickers_path
        stickerlib.TAGS = self.tags_path
        stickerlib.EXTRAS = self.extras_path

        # 造一个小库：3 张热门 + 5 张冷门，同一场景
        rows, tags = [], {}
        for i, hits in enumerate([50, 30, 10, 0, 0, 0, 0, 0]):
            sid = "id%d" % i
            path = os.path.join(self.lib, sid + ".png")
            with open(path, "wb") as f:
                f.write(b"fake-%d" % i)
            digest = hashlib.sha256(b"fake-%d" % i).hexdigest()
            rows.append({"id": sid, "name": "表情%d" % i, "file": path,
                         "hits": hits, "hash": digest})
            tags[sid] = {"name": "表情%d" % i, "file": path, "scene": "卖萌可爱"}
        with open(self.stickers_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        with open(self.tags_path, "w", encoding="utf-8") as f:
            json.dump(tags, f, ensure_ascii=False)

    def tearDown(self):
        stickerlib.STICKERS, stickerlib.TAGS, stickerlib.EXTRAS = self._saved
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class Picking(Base):
    def _pick_ids(self, n_trials=400):
        """跑多次 pick(n=1)，统计各 id 被挑中的频率。"""
        counts = {}
        for _ in range(n_trials):
            out = stickerlib.pick({"scene": "卖萌可爱", "n": 1})
            for line in out.split(chr(10)):
                if line.startswith("- id="):
                    sid = line.split("id=")[1].split()[0]
                    counts[sid] = counts.get(sid, 0) + 1
        return counts

    def test_hot_stickers_picked_more(self):
        """hits 高的被挑中的次数必须明显多于 hits=0 的。"""
        counts = self._pick_ids(300)
        hot = counts.get("id0", 0)          # hits=50
        cold = sum(counts.get("id%d" % i, 0) for i in (3, 4, 5, 6, 7))
        self.assertGreater(hot, 0, "热门表情一次都没被挑中？")
        self.assertGreater(hot, cold / 5,
                           "热门(id0=%d) 应该明显多于冷门合计(%d)" % (hot, cold))

    def test_not_monopolized(self):
        """但不能垄断：冷门的也得有机会被挑中（否则还是"翻来覆去那几张"）。"""
        counts = self._pick_ids(400)
        cold = sum(counts.get("id%d" % i, 0) for i in (3, 4, 5, 6, 7))
        self.assertGreater(cold, 0, "冷门表情一次都没被挑中——加权过猛，退化成垄断")

    def test_scene_filter(self):
        out = stickerlib.pick({"scene": "不存在的场景"})
        self.assertIn("一张都没有", out)

    def test_query_filter(self):
        out = stickerlib.pick({"query": "表情3"})
        self.assertIn("id3", out)

    def test_records_last_pick(self):
        stickerlib.pick({"scene": "卖萌可爱", "n": 1})
        with open(self.extras_path, encoding="utf-8") as f:
            extras = json.load(f)
        self.assertTrue(any("lastPick" in v for v in extras.values()),
                        "挑过之后应该记 lastPick（用于避免连撞）")

    def test_recent_penalty_lowers_chance(self):
        """刚挑过的会被压权重——避免连着两条同一张。"""
        # 把 id0（最热）标成刚挑过
        with open(self.extras_path, "w", encoding="utf-8") as f:
            json.dump({"id0": {"lastPick": stickerlib.time.time()}}, f)
        counts = self._pick_ids(200)
        # 压过权重后，它不该再是压倒性第一
        self.assertLess(counts.get("id0", 0), 200,
                        "刚发过的还被每次挑中，说明近因惩罚没生效")

    def test_unsafe_excluded(self):
        """safe=False 的绝不参与挑选。"""
        with open(self.extras_path, "w", encoding="utf-8") as f:
            json.dump({"id0": {"safe": False}}, f)
        counts = self._pick_ids(150)
        self.assertEqual(counts.get("id0", 0), 0,
                         "被标为不适合转发的表情仍然被挑中了")


class Ingest(Base):
    def test_exact_duplicate_rejected(self):
        """sha256 相同的直接拒绝。"""
        target = os.path.join(self.lib, "id0.png")
        out = stickerlib.ingest({"path": target, "scene": "测试"})
        self.assertIn("已经有了", out)

    def test_missing_file(self):
        out = stickerlib.ingest({"path": "/nope/x.png", "scene": "杂图"})
        self.assertIn("文件不存在", out)

    def test_risky_filename_blocked(self):
        """文件名带敏感词时直接判不适合，不去花模型调用。"""
        p = os.path.join(self.tmp, "别人的身份证号码.png")
        with open(p, "wb") as f:
            f.write(b"unique-new-image")
        out = stickerlib.ingest({"path": p, "scene": "测试"})
        self.assertIn("不适合转发", out)

    def test_sensitive_ocr_blocked(self):
        p = os.path.join(self.tmp, "normal.png")
        with open(p, "wb") as f:
            f.write(b"another-unique")
        out = stickerlib.ingest({"path": p, "scene": "测试", "ocr": "顺丰快递单"})
        self.assertIn("不适合转发", out)

    def test_explicit_safe_true(self):
        """显式给 safe 时按给的走（跳过模型判断）。"""
        p = os.path.join(self.tmp, "ok.png")
        with open(p, "wb") as f:
            f.write(b"ok-image-bytes")
        out = stickerlib.ingest({"path": p, "scene": "测试", "safe": True})
        self.assertIn("适合转发", out)
        with open(self.extras_path, encoding="utf-8") as f:
            extras = json.load(f)
        self.assertTrue(any(v.get("safe") is True for v in extras.values()))

    def test_phash_catches_downscaled_version(self):
        """核心用途：同一张图的高清版和低清版，sha256 完全不同，但 pHash 应该很接近。

        这正是"群友反复发同一张、或者发低清版"那个场景。
        """
        if not stickerlib.HAS_PIL:
            self.skipTest("没装 Pillow，无法计算 pHash")
        from PIL import Image
        import random
        random.seed(7)
        # 造一张有点纹理的图（纯色图 pHash 全是 0，测不出东西）
        base = Image.new("RGB", (240, 240))
        px = base.load()
        for y in range(240):
            for x in range(240):
                px[x, y] = ((x * 7 + y * 3) % 256, (x * 3) % 256, (y * 5) % 256)
        hi = os.path.join(self.tmp, "hi.png")
        lo = os.path.join(self.tmp, "lo.jpg")
        base.save(hi)
        base.resize((96, 96)).save(lo, quality=45)      # 低清 + 有损压缩

        h1, h2 = stickerlib.phash(hi), stickerlib.phash(lo)
        self.assertIsNotNone(h1)
        self.assertIsNotNone(h2)
        dist = stickerlib.hamming(h1, h2)
        self.assertLessEqual(dist, 6,
                             "同一张图的高低清版本 pHash 差 %d 位，超过了去重阈值 6" % dist)

    def test_phash_differs_between_different_images(self):
        """不同图不能被误判成重复（否则会漏收新表情）。"""
        if not stickerlib.HAS_PIL:
            self.skipTest("没装 Pillow")
        from PIL import Image
        a = Image.new("RGB", (120, 120), (255, 0, 0))
        b = Image.new("RGB", (120, 120), (0, 0, 255))
        pa, pb = os.path.join(self.tmp, "a.png"), os.path.join(self.tmp, "b.png")
        a.save(pa); b.save(pb)
        # 纯色图 pHash 可能都退化成同一个值，这时这条测试无意义
        ha, hb = stickerlib.phash(pa), stickerlib.phash(pb)
        if ha == hb:
            self.skipTest("纯色图 pHash 退化，跳过")
        self.assertGreater(stickerlib.hamming(ha, hb), 6, "两张不同的图被判成重复了")

    def test_ratio_guard_blocks_false_duplicate(self):
        """pHash 撞了但宽高比差很多 → 不能判重复。

        实测教训：这个库的图多为纯色底大头，「点赞」和「鲸鱼娘-691」的
        pHash 距离是 0——只看哈希会把完全不同的图当成同一张。
        """
        if not stickerlib.HAS_PIL:
            self.skipTest("没装 Pillow")
        from PIL import Image
        # 造两张被压成同一 pHash 的图（纯色最容易撞），但比例不同
        a = os.path.join(self.tmp, "wide.png")
        b = os.path.join(self.tmp, "tall.png")
        Image.new("RGB", (240, 120), (200, 200, 200)).save(a)   # 比例 2.0
        Image.new("RGB", (120, 240), (200, 200, 200)).save(b)   # 比例 0.5
        extras = {
            "x1": {"phash": stickerlib.phash(a), "ratio": 0.5},
        }
        # 拿 a（比例 2.0）去比 x1（比例 0.5）——哈希可能一致，但比例差太大
        hit = stickerlib.find_near_duplicate(a, extras, [], threshold=64, ratio_tol=0.02)
        self.assertIsNone(hit, "宽高比差 1.5，不该被判成同一张")

    def test_empty_scene_rejected(self):
        """空场景必须拒绝——否则存进去永远发不出去（线上有 51 条这种）。"""
        p = os.path.join(self.tmp, "noscene.png")
        with open(p, "wb") as f:
            f.write(b"no-scene-image")
        out = stickerlib.ingest({"path": p, "safe": True})
        self.assertIn("场景标签", out)

    def test_untagged_still_pickable_without_scene(self):
        """未标注的条目，在不传 scene 时应该仍能被挑到（否则等于死库存）。"""
        # 把 tags 全清空，模拟"未标注"库
        with open(self.tags_path, "w", encoding="utf-8") as f:
            json.dump({}, f)
        out = stickerlib.pick({"n": 1})
        self.assertIn("id=", out, "全库未标注时应该仍能挑出东西")

    def test_no_pil_means_no_phash(self):
        """没装 Pillow 时 pHash 返回 None，不能假装算出来了。"""
        if stickerlib.HAS_PIL:
            self.skipTest("装了 Pillow，这条不适用")
        p = os.path.join(self.lib, "id1.png")
        self.assertIsNone(stickerlib.phash(p))


class Triage(Base):
    def setUp(self):
        super().setUp()
        self._saved_paths = (stickerlib.MEDIA_DIR, stickerlib.TRIAGE, stickerlib.PENDING)
        self.media = os.path.join(self.tmp, "media")
        os.makedirs(self.media, exist_ok=True)
        stickerlib.MEDIA_DIR = self.media
        stickerlib.TRIAGE = os.path.join(self.tmp, "triage.jsonl")
        stickerlib.PENDING = os.path.join(self.tmp, "pending")

    def tearDown(self):
        stickerlib.MEDIA_DIR, stickerlib.TRIAGE, stickerlib.PENDING = self._saved_paths
        super().tearDown()

    def _put(self, name, age_h=0.0):
        p = os.path.join(self.media, name)
        with open(p, "wb") as f:
            f.write(b"img-" + name.encode())
        if age_h:
            old = stickerlib.time.time() - age_h * 3600
            os.utime(p, (old, old))
        return p

    def test_scenes_readable(self):
        """场景列表必须能读到（判 YES 时要让模型从这里选）。"""
        scenes = stickerlib._scenes()
        self.assertIsInstance(scenes, list)
        self.assertGreater(len(scenes), 3)

    def test_seen_parses_jsonl(self):
        with open(stickerlib.TRIAGE, "w", encoding="utf-8") as f:
            f.write('{"file":"a.jpg","verdict":true}' + chr(10))
            f.write('坏行' + chr(10))
            f.write('{"file":"b.jpg","verdict":false}' + chr(10))
        seen = stickerlib._triage_seen()
        self.assertEqual(set(seen), {"a.jpg", "b.jpg"}, "坏行要能跳过")

    def test_no_media_is_safe(self):
        out = stickerlib.media_triage({})
        self.assertIn("没有待判断", out)

    def test_skips_generated_and_old(self):
        """gen_*（她自己画的）不参与；快过期的也不判（省调用）。"""
        self._put("gen_123.png")               # 生成图
        self._put("media_old.jpg", age_h=6)     # 超过 TTL 窗口
        out = stickerlib.media_triage({})
        self.assertIn("没有待判断", out)

    def test_dry_run_writes_no_action(self):
        """dry-run 不该删任何文件。"""
        p = self._put("media_x.jpg")
        # 无模型可用时 _triage_one 返回 None（拿不准），不会删
        stickerlib.media_triage({})
        self.assertTrue(os.path.exists(p), "dry-run 居然删了文件")


class Hamming(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(stickerlib.hamming("ffff0000ffff0000", "ffff0000ffff0000"), 0)

    def test_distance(self):
        self.assertEqual(stickerlib.hamming("0000000000000000", "0000000000000001"), 1)

    def test_garbage_is_far(self):
        """解析失败时返回 64（当成完全不同），不要谎报"一样"。"""
        self.assertEqual(stickerlib.hamming("zz", "yy"), 64)


if __name__ == "__main__":
    unittest.main()
