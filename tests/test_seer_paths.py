#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看图工具的路径白名单测试。

`seer._allowed` 决定模型能读哪些文件。它是安全边界：放开一点，
模型就能读到 tokenhub.env、私钥、会话记录。用临时目录模拟，不碰真实路径。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "stack"))

import seer  # noqa: E402


class PathAllowlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="seer-test-")
        self.allowed = os.path.join(self.tmp, "media")
        self.outside = os.path.join(self.tmp, "secret")
        os.makedirs(self.allowed)
        os.makedirs(self.outside)
        self._orig = list(seer.ALLOW_DIRS)
        seer.ALLOW_DIRS = [self.allowed]

    def tearDown(self):
        seer.ALLOW_DIRS = self._orig
        try:
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass

    def touch(self, path):
        with open(path, "wb") as f:
            f.write(b"x")
        return path

    def test_allows_inside(self):
        p = self.touch(os.path.join(self.allowed, "a.jpg"))
        self.assertTrue(seer._allowed(p), "白名单目录内的文件应该允许")

    def test_allows_subdir(self):
        sub = os.path.join(self.allowed, "sub")
        os.makedirs(sub)
        p = self.touch(os.path.join(sub, "b.png"))
        self.assertTrue(seer._allowed(p), "子目录里的文件也应该允许")

    def test_blocks_outside(self):
        p = self.touch(os.path.join(self.outside, "c.jpg"))
        self.assertFalse(seer._allowed(p), "白名单之外的文件必须拒绝")

    def test_blocks_dotdot_traversal(self):
        """../ 穿越必须挡住。"""
        p = self.touch(os.path.join(self.outside, "d.jpg"))
        sneaky = os.path.join(self.allowed, "..", "secret", "d.jpg")
        self.assertFalse(seer._allowed(sneaky),
                         "用 .. 穿越到白名单外必须拒绝")

    def test_blocks_symlink_escape(self):
        """符号链接指向外面也不行（realpath 应该解析出来）。"""
        real = self.touch(os.path.join(self.outside, "e.jpg"))
        link = os.path.join(self.allowed, "link.jpg")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("当前平台不支持创建符号链接")
        self.assertFalse(seer._allowed(link),
                         "符号链接指到白名单外必须拒绝")

    def test_blocks_sensitive_names(self):
        """就算放在白名单目录里，也不该因为测试松掉就以为安全——
        这里只验证路径判定本身对 /etc 之类一律拒绝。"""
        for p in ("/etc/passwd", "/root/.ssh/id_ed25519", "/opt/dsh/tokenhub.env"):
            with self.subTest(path=p):
                self.assertFalse(seer._allowed(p), "%s 必须拒绝" % p)


if __name__ == "__main__":
    unittest.main()
