#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qqguard 的"假在线"识别测试。

背景（2026-10-24 真实事故）：
    22:51 起她一句话都没回，但 OneBot 的反向 WS 一直是 ESTAB，
    于是看护认为"在线"，20 多分钟没有任何告警。
    真正的原因是腾讯把登录态作废了——QQ 侧已退出，只剩 daemon 的 WS 挂着。

所以 classify_log 是这次修补的核心：NapCat 日志格式一变，这组测试要立刻红。
"""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "..", "stack", "qqguard.py")


def load_guard():
    spec = importlib.util.spec_from_file_location("qqguard_test", GUARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["qqguard_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class LogoutDetection(unittest.TestCase):
    def setUp(self):
        self.m = load_guard()

    def test_tonight_real_line(self):
        """事故当晚 NapCat 打的那行，必须能认出来。"""
        lines = [
            "10-24 23:20:34 [info] 正在快速登录  3600398389",
            "10-24 23:20:34 [error] 快速登录错误： 登录态已失效，请重新登录。",
        ]
        self.assertEqual(self.m.classify_log(lines), "登录态已失效")

    def test_user_identity_invalid(self):
        lines = ["[error] 快速登录错误： 你的用户身份已失效，为保证账号安全，请你重新登录。"]
        self.assertEqual(self.m.classify_log(lines), "用户身份已失效")

    def test_waiting_for_scan(self):
        lines = ["[warn] 请扫描下面的二维码，然后在手Q上授权登录："]
        self.assertEqual(self.m.classify_log(lines), "请扫描下面的二维码")

    def test_captcha_required(self):
        lines = ["[info] 需要验证码, proofWaterUrl: https://ti.qq.com/..."]
        self.assertEqual(self.m.classify_log(lines), "需要验证码")

    def test_login_error_code(self):
        lines = ["10-24 23:27:21 [error] [Core] [Login] Login Error,ErrType:  1  ErrCode: 3"]
        self.assertEqual(self.m.classify_log(lines), "Login Error,ErrType")

    def test_healthy_traffic_is_not_logout(self):
        """正常收发不能误判——否则每次说话都告警。"""
        lines = [
            "[info] 鲸鲸 | 发送 -> 群聊 [理工游戏群(1074978556)] [鲸鲸(3600398389)] 鲸鲸唱不了歌。",
            "[info] 鲸鲸 | 接收 <- 群聊 [理工游戏群(1074978556)] [小鲸鱼(3865059730)] 什么歌这么戳人",
            "[info] 正在快速登录  3600398389",
            "[info] 登录成功",
        ]
        self.assertIsNone(self.m.classify_log(lines))

    def test_empty_and_garbage(self):
        self.assertIsNone(self.m.classify_log([]))
        self.assertIsNone(self.m.classify_log(["", "   ", "0" * 64]))

    def test_marker_constants_are_nonempty(self):
        """防止有人手滑把标记清空，导致检测永远返回 None。"""
        self.assertTrue(len(self.m.LOGOUT_MARKERS) >= 4)
        for marker in self.m.LOGOUT_MARKERS:
            self.assertTrue(marker.strip())


if __name__ == "__main__":
    unittest.main()
