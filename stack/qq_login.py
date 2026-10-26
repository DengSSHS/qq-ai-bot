#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键准备登录：起容器 → 等二维码 → 打印链接 + 导出图片。

为什么要脚本化：手动起容器后二维码只有约 2 分钟有效，而且
容器在扫码模式下每 2 分钟自动刷新一次——刷太久会被风控当成异常登录。
所以流程应该是「准备好 → 立刻扫」，而不是一直挂着等。

用法：
    python3 qq_login.py            # 起容器并给出当前码（约 35 秒）
    python3 qq_login.py --stop     # 停下容器（结束扫码模式，停止刷登录请求）
    python3 qq_login.py --status   # 只看状态
"""
import os
import re
import shutil
import subprocess
import sys
import time

CONTAINER = "napcat"
QR = "/app/napcat/cache/qrcode.png"
LOCAL = "/tmp/qr-now.png"


def sh(cmd, timeout=120):
    r = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
    return (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")


def state():
    out = sh("docker ps -a --format '{{.Names}} {{.State}}' | grep %s" % CONTAINER)
    return out.strip()


def start():
    print("1) 启动容器…")
    print(sh("cd /opt/qqbot && docker compose up -d napcat").strip()[-200:])
    url = ""
    for _ in range(12):
        time.sleep(6)
        logs = sh("docker logs --since 3m napcat 2>&1")
        found = re.findall(r"https://txz\.qq\.com[^\s]+", logs)
        if found:
            url = found[-1]
            break
    sh("docker cp %s:%s %s 2>/dev/null" % (CONTAINER, QR, LOCAL))
    print("\n2) 扫码链接（约 2 分钟有效）：")
    print("   " + (url or "（没拿到，看看 docker logs napcat）"))
    print("   图片：%s" % (LOCAL if os.path.exists(LOCAL) else "（导出失败）"))
    print("\n3) 扫完立刻验证：cd /opt/dsh && python3 qq_login.py --status")
    return 0 if url else 1


def main():
    if "--stop" in sys.argv:
        print(sh("cd /opt/qqbot && docker compose stop napcat").strip()[-100:])
        print("容器：", state())
        return 0
    if "--status" in sys.argv:
        print("容器：", state() or "（不存在）")
        print(sh("cd /opt/dsh && python3 qq_status.py").strip())
        return 0
    return start()


if __name__ == "__main__":
    sys.exit(main())
