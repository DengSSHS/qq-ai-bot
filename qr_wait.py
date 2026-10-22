#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等 NapCat 登录：提取二维码/链接，登录成功则退出 0，超时退 2。"""
import os, re, subprocess, sys, time

ob = sys.argv[1]
ansi = re.compile(chr(27) + r"\[[0-9;]*[A-Za-z]")
deadline = time.time() + 180
printed = False
last = 0

while time.time() < deadline:
    if os.path.exists(ob):
        print("\n  [登录成功] 继续部署")
        sys.exit(0)
    try:
        out = subprocess.run(["docker", "logs", "--tail", "200", "napcat"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        out = ""
    clean = ansi.sub("", out)
    urls = []
    for u in re.findall(r"https?://[^\s\"'<>]+", clean):
        if u not in urls and "github" not in u.lower():
            urls.append(u)

    if (urls or len(clean) > 200) and not printed:
        printed = True
        tail = [l for l in clean.splitlines() if l.strip()][-40:]
        print("  ---------- napcat 日志尾部 ----------")
        print("\n".join(tail))
        if urls:
            print("\n  ---------- 二维码链接 ----------")
            for u in urls[-3:]:
                print("    " + u)
            print("  （在浏览器打开链接，再用手机 QQ 扫屏幕上的二维码）")
        print("  -----------------------------------")
        print("  或者走网页终端：ssh -L 6099:127.0.0.1:6099 root@<服务器>")
        print("  然后浏览器打开 http://127.0.0.1:6099 用 WebUI Token 登录")
        print("  -----------------------------------", flush=True)

    if time.time() - last > 20:
        last = time.time()
        print("  等待扫码中...（剩余 %d 秒）" % int(deadline - time.time()), flush=True)
    time.sleep(4)

print("\n  [超时] 180 秒内没检测到登录")
print("  让人类手动执行：docker logs --tail 100 napcat")
sys.exit(2)
