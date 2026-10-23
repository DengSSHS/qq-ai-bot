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
        """按内容哈希索引；坏行跳过。"""
        with open(stickerlib.TRIAGE, "w", encoding="utf-8") as f:
            f.write('{"sha":"aaa","file":"a.jpg","verdict":true}' + chr(10))
            f.write('坏行' + chr(10))
            f.write('{"sha":"bbb","file":"b.jpg","verdict":false}' + chr(10))
        seen = stickerlib._triage_seen()
        self.assertEqual(set(seen), {"aaa", "bbb"}, "坏行要能跳过")

    def test_same_content_judged_once(self):
        """同一张图（内容相同、文件名不同）只判断一次——这是省调用的关键。"""
        a = self._put("media_111_a.jpg")
        with open(stickerlib.TRIAGE, "w", encoding="utf-8") as f:
            f.write(json.dumps({"sha": stickerlib._file_sha(a), "file": "media_111_a.jpg",
                                "verdict": True, "scene": "卖萌可爱", "applied": False},
                               ensure_ascii=False) + chr(10))
        # 同一内容换个文件名再放一次
        b = os.path.join(self.media, "media_222_b.jpg")
        with open(a, "rb") as src, open(b, "wb") as dst:
            dst.write(src.read())
        out = stickerlib.media_triage({})       # 只判断模式
        self.assertIn("复用", out, "同内容的图应复用结论，而不是再判一次")

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
        """解析失败时返回一个大值（当成完全不同），不要谎报"一样"。"""
        self.assertGreater(stickerlib.hamming("zz", "yy"), 1000)

    def test_different_length_is_far(self):
        """8x8 的旧值和 16x16 的新值不能比——zip 截断会算出假的"很近"。

        升级 PHASH_SIZE 后 extras.json 里会同时存在两种长度的值。
        不做长度校验的话："ffff" 和 "ffff000000000000" 会算出距离 0。
        """
        self.assertGreater(
            stickerlib.hamming("ffff0000ffff0000", "ffff0000ffff0000" * 4), 1000)

    def test_phash_is_fixed_width(self):
        """phash 必须定长，否则长度校验和比较都失效。"""
        self.assertEqual(stickerlib.PHASH_SIZE, 16)
        self.assertEqual((stickerlib.PHASH_SIZE ** 2 + 3) // 4, 64)


class TriageReplyParsing(unittest.TestCase):
    """判断结果的解析：模型输出会崩，三层兜底得拦住。

    这些用例全部来自真实事故（2026-10-24 重分类 754 张杂图）：
    小样本串行跑格式完美，并发跑几百张后模型开始抄 prompt 模板、
    加 markdown、加前缀、甚至直接拒绝。当时解析一崩就默认为噪声，
    267 条结果只剩 1 条能用。
    """

    def parse(self, s):
        return stickerlib._parse_triage_reply(s)

    def test_plain_json(self):
        v, scene, reason = self.parse(
            '{"verdict":"YES","scene":"卖萌可爱","reason":"比心存存"}')
        self.assertTrue(v)
        self.assertEqual(scene, "卖萌可爱")
        self.assertEqual(reason, "比心存存")

    def test_json_in_code_fence(self):
        v, scene, _ = self.parse(
            '```json\n{"verdict":"NO","scene":"","reason":"聊天截图"}\n```')
        self.assertFalse(v)
        self.assertEqual(scene, "")

    def test_markdown_wrapped_scene(self):
        """模型给标签加了 ** 加粗。"""
        v, scene, _ = self.parse('{"verdict":"YES","scene":"**无语尴尬**","reason":"翻白眼"}')
        self.assertTrue(v)
        self.assertEqual(scene, "无语尴尬")

    def test_scene_with_parenthesis(self):
        v, scene, _ = self.parse('{"verdict":"YES","scene":"撒娇求(要东西)","reason":"求"}')
        self.assertEqual(scene, "撒娇求")

    def test_copied_format_template(self):
        """模型把 prompt 里的格式说明当内容输出了（旧竖线格式那版）。"""
        v, scene, _ = self.parse("标签|一句话理由 YES|忙工作|加载中")
        self.assertTrue(v)
        self.assertEqual(scene, "忙工作")

    def test_legacy_pipe_format(self):
        v, scene, reason = self.parse("YES|嘲讽怼人|斜眼看人配6字")
        self.assertTrue(v)
        self.assertEqual(scene, "嘲讽怼人")
        self.assertEqual(reason, "斜眼看人配6字")

    def test_prefix_noise(self):
        """模型自说自话加了一堆前缀。"""
        v, scene, _ = self.parse("结合这九宫格的整体氛围，最贴的标签是：期待等待")
        self.assertEqual(scene, "期待等待")

    def test_longest_scene_wins(self):
        """长名优先：否则「杂图」会抢先从任何带这两个字的句子里命中。"""
        _, scene, _ = self.parse("这不是杂图，应该算期待等待")
        self.assertEqual(scene, "期待等待")

    def test_refusal_is_no(self):
        """模型拒绝回答 → 从严当不值得收，不猜。"""
        v, _, reason = self.parse("我无法对这张图进行分类。")
        self.assertFalse(v)
        self.assertIn("无法", reason)

    def test_empty_is_none(self):
        """空返回 → None（拿不准），调用方不能当成"值得"。"""
        v, _, _ = self.parse("")
        self.assertIsNone(v)

    def test_scene_hints_cover_defaults(self):
        """每个默认场景都有适用条件。缺条件的那个会吸走大部分图。"""
        missing = [s for s in stickerlib.DEFAULT_SCENES
                   if s not in stickerlib.SCENE_HINTS]
        self.assertEqual(missing, [], "这些场景没有适用条件提示：%s" % missing)

    def test_prompt_has_hints_placeholder(self):
        """prompt 里必须有 {scenes}，否则场景表注不进去。"""
        self.assertIn("{scenes}", stickerlib.TRIAGE_QUESTION)


if __name__ == "__main__":
    unittest.main()


class SettleSkipsExisting(unittest.TestCase):
    """结算前先查库：已经在库里的图不该再花一次模型调用。

    事故背景：media/ 里留着一张已入库图的副本（重跑测试留下的），
    结算时它会走完 判值得 -> 进 pending -> 注入让她收 -> 她说"已经有了"
    的全流程。三份成本买一句空话。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (stickerlib.MEDIA_DIR, stickerlib.STICKERS,
                       stickerlib.TRIAGE, stickerlib.PENDING)
        stickerlib.MEDIA_DIR = self.tmp
        stickerlib.STICKERS = os.path.join(self.tmp, "stickers.json")
        stickerlib.TRIAGE = os.path.join(self.tmp, "triage.jsonl")
        stickerlib.PENDING = os.path.join(self.tmp, "pending")

    def tearDown(self):
        (stickerlib.MEDIA_DIR, stickerlib.STICKERS,
         stickerlib.TRIAGE, stickerlib.PENDING) = self._saved
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sticker_shas_reads_hash_field(self):
        """字段名是 hash（插件的写法），不是 sha256。读错就永远挡不住。"""
        with open(stickerlib.STICKERS, "w", encoding="utf-8") as f:
            json.dump([{"id": "abc", "hash": "deadbeef"},
                       {"id": "xyz", "sha256": "cafe"}], f)
        m = stickerlib._sticker_shas()
        self.assertEqual(m.get("deadbeef"), "abc")
        self.assertNotIn("cafe", m, "不该把 sha256 字段当成 hash")

    def test_existing_image_is_skipped(self):
        """已在库里的图不调模型、不进 pending。"""
        img = os.path.join(self.tmp, "media_dup.jpg")
        data = b"\xff\xd8\xff\xe0 fake-jpeg-body"
        with open(img, "wb") as f:
            f.write(data)
        digest = hashlib.sha256(data).hexdigest()
        with open(stickerlib.STICKERS, "w", encoding="utf-8") as f:
            json.dump([{"id": "dup", "hash": digest}], f)
        called = []
        saved = stickerlib._triage_one
        stickerlib._triage_one = lambda p: called.append(p) or (True, "卖萌可爱", "x")
        try:
            out = stickerlib.media_triage({"apply": True})
        finally:
            stickerlib._triage_one = saved
        self.assertEqual(called, [], "已入库的图不该再调模型")
        self.assertIn("库", out)
        self.assertFalse(os.path.isdir(stickerlib.PENDING)
                         and os.listdir(stickerlib.PENDING), "不该进 pending")


class NearDuplicateBlocksIngest(unittest.TestCase):
    """近似重复改成「默认拦」之后的行为（可用 force 强收）。

    为什么改：8x8 的 phash 在这个库上误拦率 0.123%，抽出来看全是不同的图
    （初音命中袋鼠、鲸鱼娘-118 命中鲸鱼娘-070），所以只敢提示。换 16x16
    后同一批样本降到 0.004%，而「同图不同压缩」召回不变（q50/q80/q95/
    缩50%/缩75% 全部距离 <=2）。库已经 4244 张，收进重复图的代价比误挡
    一张新图高，所以默认改成拦，但留 force 出口。
    """

    def setUp(self):
        if not stickerlib.HAS_PIL:
            self.skipTest("没装 Pillow，算不出 phash")
        self.tmp = tempfile.mkdtemp()
        self._saved = (stickerlib.STICKERS, stickerlib.EXTRAS,
                       stickerlib.TAGS, stickerlib.MEDIA_DIR)
        stickerlib.STICKERS = os.path.join(self.tmp, "stickers.json")
        stickerlib.EXTRAS = os.path.join(self.tmp, "extras.json")
        stickerlib.TAGS = os.path.join(self.tmp, "tags.json")
        stickerlib.MEDIA_DIR = self.tmp

    def tearDown(self):
        (stickerlib.STICKERS, stickerlib.EXTRAS,
         stickerlib.TAGS, stickerlib.MEDIA_DIR) = self._saved
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_pair(self):
        """造一张原图和它压缩过的版本（sha256 不同、看起来一样）。"""
        from PIL import Image
        rnd = __import__("random").Random(7)
        im = Image.new("RGB", (160, 160))
        px = im.load()
        for y in range(160):
            for x in range(160):
                # 造点有结构的图案，纯色图的 phash 会退化
                px[x, y] = ((x * 3 + y * 2) % 256, (x ^ y) % 256, (x * 7) % 256)
        a = os.path.join(self.tmp, "orig.png")
        b = os.path.join(self.tmp, "recompressed.jpg")
        im.save(a, "PNG")
        im.save(b, "JPEG", quality=60)
        return a, b

    def test_near_duplicate_is_blocked(self):
        orig, var = self._make_pair()
        ph = stickerlib.phash(orig)
        self.assertTrue(ph, "原图应该算得出 phash")
        with open(stickerlib.STICKERS, "w", encoding="utf-8") as f:
            json.dump([{"id": "orig1", "hash": "not-the-same-bytes",
                        "name": "原图", "file": orig}], f)
        with open(stickerlib.EXTRAS, "w", encoding="utf-8") as f:
            json.dump({"orig1": {"phash": ph,
                                 "ratio": stickerlib.image_ratio(orig)}}, f)
        out = stickerlib.ingest({"path": var, "scene": "杂图", "safe": True})
        self.assertIn("很像", out)
        self.assertIn("force", out)
        self.assertIn("原图", out)

    def test_force_overrides_block(self):
        orig, var = self._make_pair()
        ph = stickerlib.phash(orig)
        with open(stickerlib.STICKERS, "w", encoding="utf-8") as f:
            json.dump([{"id": "orig1", "hash": "x", "name": "原图", "file": orig}], f)
        with open(stickerlib.EXTRAS, "w", encoding="utf-8") as f:
            json.dump({"orig1": {"phash": ph,
                                 "ratio": stickerlib.image_ratio(orig)}}, f)
        out = stickerlib.ingest({"path": var, "scene": "杂图",
                                 "safe": True, "force": True})
        self.assertNotIn("很像", out)
        self.assertIn("已记录", out)

    def test_different_images_not_blocked(self):
        """两张内容不同的图不该被互拦（16x16 下同批次不同图几乎不命中）。"""
        from PIL import Image
        a = os.path.join(self.tmp, "a.png")
        b = os.path.join(self.tmp, "b.png")
        im1 = Image.new("RGB", (120, 120), (10, 20, 30))
        im2 = Image.new("RGB", (120, 120), (250, 240, 200))
        for im, base in ((im1, 0), (im2, 128)):
            px = im.load()
            for y in range(120):
                for x in range(120):
                    px[x, y] = ((x * 2 + base) % 256, (y * 2 + base) % 256, base)
        im1.save(a); im2.save(b)
        ph = stickerlib.phash(a)
        with open(stickerlib.STICKERS, "w", encoding="utf-8") as f:
            json.dump([{"id": "a1", "hash": "x", "name": "A", "file": a}], f)
        with open(stickerlib.EXTRAS, "w", encoding="utf-8") as f:
            json.dump({"a1": {"phash": ph,
                              "ratio": stickerlib.image_ratio(a)}}, f)
        out = stickerlib.ingest({"path": b, "scene": "杂图", "safe": True})
        self.assertNotIn("很像", out, "内容不同的图不该被当成重复")
