#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫码次数保险：没登上就别一直请求登录。

背景（2026-10-26 实录）：
    被风控踢下线后，NapCat 会停在扫码模式，**每 2 分钟自动换一张二维码**——
    每一张都是一次登录请求。挂着一小时就是 30 次，风控只会更严。

规则：
    容器在跑 + 没登录 + 已经超过 GRACE 分钟 → 停容器（停止请求）
    登录正常时什么都不做

用法：
    python3 qq_churn_guard.py            # 检查并按需停容器
    python3 qq_churn_guard.py --dry
"""
import os
import re
import subprocess
import sys
import time

CONTAINER = "napcat"
GRACE_MIN = float(os.environ.get("QQ_CHURN_GRACE_MIN", "15"))
STATE = "/opt/dsh/memory/qq_churn.json"
LOG = "/opt/dsh/memory/qq_churn.log"


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def sh(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "replace").strip()
    except Exception as e:
        return "ERR %s" % e


def main():
    dry = "--dry" in sys.argv
    running = "running" in sh("docker ps --format '{{.Names}} {{.State}}' | grep %s" % CONTAINER)
    if not running:
        print("容器没在跑，无需处理")
        return 0

    sys.path.insert(0, "/opt/dsh")
    try:
        import qq_status
        st = qq_status.status()
    except Exception as e:
        st = {"online": None, "reason": "qq_status 失败: %s" % e}

    if st.get("online"):
        print("[在线] 不做处理")
        return 0

    # 首次发现掉线的时刻
    first = 0.0
    try:
        import json
        first = float(json.load(open(STATE)).get("offline_since") or 0)
    except Exception:
        pass
    now = time.time()
    if not first:
        first = now
        if not dry:
            try:
                import json
                open(STATE, "w", encoding="utf-8").write(json.dumps({"offline_since": first}))
            except Exception:
                pass

    waited = (now - first) / 60.0
    print("[掉线] 已等待 %.0f 分钟（阈值 %.0f）%s" % (waited, GRACE_MIN, st.get("reason", "")))
    if waited < GRACE_MIN:
        return 0

    if dry:
        print("（dry）会停止容器，避免继续请求登录")
        return 0
    sh("cd /opt/qqbot && docker compose stop napcat")
    log("掉线超过 %.0f 分钟，已停容器（停止扫码请求）" % waited)
    try:
        os.remove(STATE)
    except Exception:
        pass
    print("已停容器：不再每 2 分钟请求一次登录。需要登录时执行 python3 qq_login.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
