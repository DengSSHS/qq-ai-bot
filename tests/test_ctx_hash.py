#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前缀稳定性检查的判据测试。

这里守的是一条容易越走越松的界线：

  · 每轮都在变（时间戳/计数器漏进前缀）→ 必须 FAIL，这是真 bug
  · 会话中途有人改了 persona / self_state → 只能 WARN，否则"谁改记忆谁报错"

2026-11-09 之前的版本只有 FAIL 一种结论，我自己改 self_state 时就被它误报过。
"""
import contextlib
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "stack"))

import ctx_hash  # noqa: E402

MARK = "[以下为动态内容·每轮可能变化，冲突时以上面为准]"


def prompt(stable_body, ts, tail="动态内容"):
    return {"text": stable_body + "\n" + MARK + "\n" + tail, "ts": ts}


class Verdict(unittest.TestCase):
    def run_with(self, prompts, changed):
        """跑 main()，返回 (退出码, 输出)"""
        buf = io.StringIO()
        old = (ctx_hash.newest_session, ctx_hash.system_prompts, ctx_hash.changed_between)
        ctx_hash.newest_session = lambda: "/fake/session.zstd"
        ctx_hash.system_prompts = lambda p, limit=6: prompts
        ctx_hash.changed_between = lambda a, b: changed(a, b)
        try:
            with contextlib.redirect_stdout(buf):
                rc = ctx_hash.main()
        finally:
            ctx_hash.newest_session, ctx_hash.system_prompts, ctx_hash.changed_between = old
        return rc, buf.getvalue()

    def test_stable_prefix_is_ok(self):
        rc, out = self.run_with([prompt("A" * 500, 1000 + i) for i in range(4)],
                                lambda a, b: [])
        self.assertEqual(rc, 0)
        self.assertIn("[OK", out)

    def test_per_turn_drift_fails(self):
        # 每轮都变，且没有任何文件改动：就是真 bug
        prompts = [prompt("A" * 500 + str(i) * 50, 1000 + i) for i in range(4)]
        rc, out = self.run_with(prompts, lambda a, b: [])
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL", out)
        self.assertIn("每轮抖动", out)

    def test_mid_session_file_edit_is_only_a_warning(self):
        # 第 2 轮之后内容变了，但两轮之间 self_state 被改过 → 不算故障
        prompts = [prompt("A" * 500, 1000),
                   prompt("B" * 500, 1100),
                   prompt("B" * 500, 1200)]
        rc, out = self.run_with(
            prompts,
            lambda a, b: [("memory/self_state.md", 1050)] if a == 1000 else [])
        self.assertEqual(rc, 0)
        self.assertIn("[WARN", out)
        self.assertIn("self_state.md", out)

    def test_mixed_case_still_fails(self):
        # 一次能解释（文件改了），另一次解释不了 → 仍然 FAIL
        prompts = [prompt("A" * 300, 1000),
                   prompt("B" * 300, 1100),
                   prompt("C" * 300, 1200)]
        rc, out = self.run_with(
            prompts,
            lambda a, b: [("memory/self_state.md", 1050)] if a == 1000 else [])
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL", out)

    def test_missing_timestamp_does_not_crash(self):
        prompts = [{"text": "A" * 300 + "\n" + MARK + "\n尾", "ts": None},
                   {"text": "B" * 300 + "\n" + MARK + "\n尾", "ts": None}]
        rc, out = self.run_with(prompts, lambda a, b: [])
        self.assertEqual(rc, 1)      # 无时间戳就无法解释差异，保守判 FAIL


if __name__ == "__main__":
    unittest.main()
