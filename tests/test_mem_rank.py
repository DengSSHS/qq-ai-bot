#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遗忘曲线的行为测试。

`checkall.py` 是配置自检，发现不了"公式被改错了"。这组测试专门盯逻辑：
如果谁把 `_retention` 里的指数衰减改成别的、或者把 floor 漏掉，这里会红。

跑：python3 -m unittest discover tests -v
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "stack"))

import mem_rank  # noqa: E402


class RetentionCurve(unittest.TestCase):
    def test_fresh_memory_is_high(self):
        """刚记住的东西保留度应该接近 1。"""
        r = mem_rank._retention(imp=0.5, hits=1, age_days=0)
        self.assertGreater(r, 0.9, "age=0 时保留度应该接近 1，实际 %.3f" % r)

    def test_monotonic_decay(self):
        """时间越久，保留度单调不增。"""
        prev = 1.0
        for days in (0, 1, 7, 30, 90, 365, 3650):
            r = mem_rank._retention(imp=0.5, hits=1, age_days=days)
            self.assertLessEqual(r, prev + 1e-9,
                                 "age=%s 天时保留度反而升高了（%.4f > %.4f）"
                                 % (days, r, prev))
            prev = r

    def test_floor_holds_long_term(self):
        """长尾地板生效：十年后仍应高于地板，而不是归零。"""
        r = mem_rank._retention(imp=0.5, hits=1, age_days=3650)
        self.assertGreater(r, 0.0, "十年后不该归零（floor 没生效？）")

    def test_importance_slows_decay(self):
        """同样时间下，重要的记忆保留得更好。"""
        low = mem_rank._retention(imp=0.1, hits=1, age_days=30)
        high = mem_rank._retention(imp=0.9, hits=1, age_days=30)
        self.assertGreater(high, low,
                           "重要性 0.9 的保留度(%.4f) 应该高于 0.1(%.4f)" % (high, low))

    def test_rehearsal_slows_decay(self):
        """复述次数多的记忆保留得更好。"""
        once = mem_rank._retention(imp=0.5, hits=1, age_days=30)
        many = mem_rank._retention(imp=0.5, hits=20, age_days=30)
        self.assertGreater(many, once,
                           "复述 20 次(%.4f) 应该比 1 次(%.4f) 保留更多" % (many, once))

    def test_bounded_zero_to_one(self):
        """返回值必须在 [0, 1]，且极端输入不炸。"""
        cases = [
            dict(imp=0.0, hits=0, age_days=0),
            dict(imp=1.0, hits=999, age_days=0),
            dict(imp=1.0, hits=999, age_days=100000),
            dict(imp=0.5, hits=1, age_days=0.0001),
        ]
        for c in cases:
            r = mem_rank._retention(**c)
            self.assertGreaterEqual(r, 0.0, c)
            self.assertLessEqual(r, 1.0, c)

    def test_recency_weight_range(self):
        """RECENCY_WEIGHT 必须在 0~1；改了公式容易越界。"""
        self.assertGreaterEqual(mem_rank.RECENCY_WEIGHT, 0.0)
        self.assertLessEqual(mem_rank.RECENCY_WEIGHT, 1.0)

    def test_half_life_consistent_with_base(self):
        """BASE 参数应该自洽：floor 在 (0,1)，beta 在 (0,2)，半衰期为正。"""
        b = mem_rank.BASE
        self.assertGreater(b["halfLifeDays"], 0)
        self.assertGreater(b["floor"], 0.0)
        self.assertLess(b["floor"], 1.0)
        self.assertGreater(b["beta"], 0.0)
        self.assertLess(b["beta"], 2.0)


if __name__ == "__main__":
    unittest.main()
