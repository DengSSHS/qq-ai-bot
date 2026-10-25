#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取当前二维码：打印最新扫码链接 + 把图片复制到 /tmp/qr-now.png。

NapCat 在扫码模式下每约 2 分钟自动换一张，所以随时取都是新鲜的。
用法：python3 qr_now.py
"""
import io
import os
import re
import subprocess
import sys


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


urls = re.findall(r"https://txz\.qq\.com[^\s]+", sh("docker logs --since 10m napcat 2>&1"))
url = urls[-1] if urls else ""
print("扫码链接:", url or "（还没有，等容器出码）")

stamp = sh("docker exec napcat ls -l --time-style=+%H:%M:%S /app/napcat/cache/qrcode.png 2>/dev/null")
print("图片时间:", stamp.strip().split()[-2:] if stamp.strip() else "?")

sh("docker cp napcat:/app/napcat/cache/qrcode.png /tmp/qr-now.png")
print("图片:", "/tmp/qr-now.png" if os.path.exists("/tmp/qr-now.png") else "复制失败")
