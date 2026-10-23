#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSRF 防护的地址判定测试。

这是安全边界，最该有回归测试：谁改动了 `_ip_blocked_reason`，
把内网/元数据地址放行了，这里必须红。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "stack"))

import websurf  # noqa: E402


class IpGuard(unittest.TestCase):
    MUST_BLOCK = [
        # 回环
        "127.0.0.1", "127.0.0.2", "127.1.2.3",
        # 私网 A/B/C
        "10.0.0.1", "10.255.255.254",
        "172.16.0.1", "172.31.255.254",
        "192.168.0.1", "192.168.255.254",
        # 链路本地（含云元数据）
        "169.254.169.254", "169.254.0.1",
        # CGNAT
        "100.64.0.1", "100.127.255.254",
        # 基准测试网段
        "198.18.0.1",
        # 保留 / 组播 / 未指定
        "240.0.0.1", "224.0.0.1", "239.255.255.250", "0.0.0.0",
        # IPv6
        "::1", "::", "fc00::1", "fd00::1", "fe80::1", "ff02::1",
        # IPv4 映射到 IPv6（容易被忽略的绕过）
        "::ffff:127.0.0.1", "::ffff:169.254.169.254",
    ]

    MUST_ALLOW = [
        "8.8.8.8", "1.1.1.1", "114.114.114.114",
        "223.5.5.5", "180.76.76.76",
        "2400:3200::1",   # 阿里 DNS 的公网 IPv6
    ]

    def test_blocks_private_and_metadata(self):
        for ip in self.MUST_BLOCK:
            with self.subTest(ip=ip):
                reason = websurf._ip_blocked_reason(ip)
                self.assertIsNotNone(reason, "应该拦住 %s，却被放行了" % ip)

    def test_allows_public(self):
        for ip in self.MUST_ALLOW:
            with self.subTest(ip=ip):
                reason = websurf._ip_blocked_reason(ip)
                self.assertIsNone(reason, "公网地址 %s 被误拦（%s）" % (ip, reason))

    def test_garbage_is_blocked(self):
        """无法解析的输入按"拦"处理，不能当公网放过去。"""
        for bad in ("", "not-an-ip", "999.999.999.999", "::gggg"):
            with self.subTest(bad=bad):
                self.assertIsNotNone(websurf._ip_blocked_reason(bad),
                                     "无法解析的 %r 应该被拦" % bad)

    def test_localhost_name_rejected(self):
        """主机名的黑名单（不查 DNS，纯字符串判断）。"""
        for host in ("localhost", "x.localhost", "a.local", "b.internal"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError, msg="%s 应该被拒绝" % host):
                    websurf._resolve_safe(host, 80)


if __name__ == "__main__":
    unittest.main()
