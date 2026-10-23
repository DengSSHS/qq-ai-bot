#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 转发层的候选字段自适应测试。

背景：这个转发层的唯一目的是"把思考关掉"。如果注入的字段上游不认，
它会**静默失效**——日志看着正常，速度优化没了。这组测试保证它会自己发现并切换。
"""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(HERE, "..", "stack", "llm_shim.py")


def load_shim():
    """每次重新导入，避免上一个用例的全局状态污染。"""
    spec = importlib.util.spec_from_file_location("llm_shim_test", SHIM)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["llm_shim_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class CandidateSwitching(unittest.TestCase):
    def setUp(self):
        self.m = load_shim()

    def test_starts_with_first_candidate(self):
        self.assertEqual(self.m.picked_payload()[0], self.m.DISABLE_PAYLOADS[0][0])

    def test_switches_when_thinking_still_on(self):
        """上游仍在思考 → 必须切到下一个候选。"""
        before = self.m.picked_payload()[0]
        self.m.sniff('{"choices":[{"delta":{"reasoning_content":"我得想想"}}]}')
        after = self.m.picked_payload()[0]
        self.assertNotEqual(before, after, "发现思考后应该切换候选字段")
        self.assertFalse(self.m._state["ok"])

    def test_stops_at_last_candidate(self):
        """所有候选都失败后不再越界，并记录失败次数。"""
        for _ in range(5):
            self.m.sniff('{"choices":[{"delta":{"reasoning_content":"还在想"}}]}')
        idx = self.m._state["idx"]
        self.assertEqual(idx, len(self.m.DISABLE_PAYLOADS) - 1, "索引不能越界")
        self.assertGreaterEqual(self.m._state["fails"], 3, "应该累计失败次数")

    def test_marks_ok_when_thinking_disabled(self):
        """响应里 reasoning_content 为空 → 判定生效。"""
        self.m.sniff('{"choices":[{"delta":{"reasoning_content":"","content":"好"}}]}')
        self.assertTrue(self.m._state["ok"], "空思考应该被判定为关闭成功")

    def test_no_field_means_disabled(self):
        """思考真被关掉时，很多网关根本不返回 reasoning_content 字段
        （而不是返回空串）。这种情况必须判定为"生效"，否则永远确认不了。"""
        self.m.sniff('{"choices":[{"delta":{"content":"直接回答"}}]}')
        self.assertTrue(self.m._state["ok"], "没有思考字段应视为关闭成功")
        self.assertEqual(self.m._state["fails"], 0, "不应算失败")
        self.assertEqual(self.m._state["idx"], 0, "不应切换候选")

    def test_env_override(self):
        """SHIM_DISABLE_FIELD 能强制指定字段（跳过自适应）。"""
        self.m.os.environ["SHIM_DISABLE_FIELD"] = "enable_thinking"
        try:
            self.assertEqual(self.m.picked_payload()[0], "enable_thinking")
        finally:
            del self.m.os.environ["SHIM_DISABLE_FIELD"]

    def test_concurrent_sniff_no_lost_update(self):
        """多线程同时判定失败，fails 计数不能丢。

        服务是 ThreadingHTTPServer，_state 是共享字典。`+=` 是读-改-写三步，
        理论上不加锁会丢更新。

        **诚实说明**：在 CPython 的 GIL 下，这个用例实测抓不住去锁的情况
        （竞态窗口太窄）。它的价值是底线检查 + 记录这个约束存在，
        不能当作"锁已被证明有效"的证据。要真验证得在 free-threaded 构建上跑。
        """
        import threading
        N = 20
        payload = '{"choices":[{"delta":{"reasoning_content":"还在想"}}]}'
        threads = [threading.Thread(target=self.m.sniff, args=(payload,))
                   for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.m._state["fails"], N,
                         "并发 %d 次失败应该正好记 %d 次，实际 %d（锁丢了？）"
                         % (N, N, self.m._state["fails"]))

    def test_concurrent_no_index_overshoot(self):
        """并发切换候选时，idx 不能越界。"""
        import threading
        payload = '{"choices":[{"delta":{"reasoning_content":"想"}}]}'
        threads = [threading.Thread(target=self.m.sniff, args=(payload,))
                   for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(self.m._state["idx"], len(self.m.DISABLE_PAYLOADS) - 1)
        self.assertGreaterEqual(self.m._state["idx"], 0)

    def test_body_discussing_field_not_misjudged(self):
        """正文里谈论这些字段名时，不能被误判成上游在思考。

        诚实说明：在标准 JSON/SSE 响应里，正文的引号会被转义成 \"，
        裸正则要求裸引号，所以**其实匹配不上**——这条误判风险比看起来小。
        改成结构化解析是为了不依赖这个巧合（网关若返回非严格 JSON 就会露）。
        """
        import json as _json
        body = _json.dumps({
            "choices": [{"delta": {
                "content": '这个字段叫 "reasoning_content": "示例" ，用它判断思考'
            }}]
        }, ensure_ascii=False)
        self.m.sniff(body)
        self.assertTrue(self.m._state["ok"], "正文提到字段名不应被判为上游仍在思考")
        self.assertEqual(self.m._state["fails"], 0)
        self.assertEqual(self.m._state["idx"], 0, "不应因此切换候选")

    def test_structured_sse_nonempty(self):
        """标准 SSE 结构里的非空思考 → 判定失效并切换。"""
        self.m.sniff('data: {"choices":[{"delta":{"reasoning_content":"我真的在想"}}]}')
        self.assertFalse(self.m._state["ok"])
        self.assertEqual(self.m._state["idx"], 1)

    def test_structured_sse_empty(self):
        """标准 SSE 结构里的空思考 → 判定生效。"""
        self.m.sniff('data: {"choices":[{"delta":{"reasoning_content":"","content":"好"}}]}')
        self.assertTrue(self.m._state["ok"])

    def test_top_level_reasoning(self):
        """有些网关把思考放顶层，不是 choices[].delta。"""
        self.m.sniff('data: {"reasoning_content":"顶层思考"}')
        self.assertFalse(self.m._state["ok"], "顶层思考字段也要识别")

    def test_non_json_fallback_is_a_known_gap(self):
        """非 JSON 响应（网关异常、纯文本回显）时只能回退正则——这是已知局限。

        这种情况信息不足：既拿不到 choices 结构，也分不清"思考内容"和"正文里
        引用了这个字段名"。记录在这里，免得以后误以为全覆盖。
        """
        self.m.sniff('some plain text: "reasoning_content": "x"')
        # 回退正则会把它当成"上游在思考"——这是错的，但当前无法区分
        self.assertFalse(self.m._state["ok"])

    def test_payload_shape(self):
        """注入的值必须是预期的形状，别在重构时改错。"""
        key, value = self.m.DISABLE_PAYLOADS[0]
        self.assertEqual(key, "thinking")
        self.assertEqual(value, {"type": "disabled"})
        for k, v in self.m.DISABLE_PAYLOADS:
            self.assertIsInstance(k, str)
            self.assertTrue(v is False or isinstance(v, dict))


if __name__ == "__main__":
    unittest.main()
