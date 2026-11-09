#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量追番：一次排多集，串行跑，断点续传（2026-11-09）

为什么需要：`bili_watch_episode.py` 一次只处理一集，13 集要么手工喊 13 次，
要么有个东西替你把它们排好。这个就是那个东西。

关键设计（都是踩过坑换来的）：
  · **串行**：两核机器上并行两个 whisper 只会互相拖慢，还容易 OOM。
  · **断点续传**：每集有自己的 .progress，跑过的不重来；中途被杀，下次接着跑。
  · **失败重试**：B 站接口会瞬时风控（实测 season 返回空分集），每集最多试 3 次。
  · **时间预算**：--max-minutes 到了就停，剩下的下次继续——不把整个窗口吃满，
    她还得留着时间陪主人说话。

用法：
    # 把第 4 到第 13 集排上，最多占 150 分钟
    /opt/tulpa/.venv/bin/python bili_watch_batch.py --from 4 --through 13 --max-minutes 150
    # 只跑第 4 集（验证用）
    ... --from 4 --through 4
    # 重做（连同已有的也重来）
    ... --from 4 --through 6 --redo
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time

WATCH_PY = "/opt/dsh/bili_watch_episode.py"
PYTHON = "/opt/tulpa/.venv/bin/python"
D = "/opt/tulpa/data/bilibili/watch"


def say(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def done_until(season, ep):
    p = os.path.join(D, "ss%d" % season, "ep%d.progress" % ep)
    try:
        return float(io.open(p, encoding="utf-8").read().strip())
    except Exception:
        return 0.0


def run_one(season, ep, redo):
    cmd = [PYTHON, WATCH_PY, "--season", str(season), "--ep", str(ep)]
    if redo:
        cmd.append("--redo")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="73077")
    ap.add_argument("--from", dest="ep_from", type=int, default=4)
    ap.add_argument("--through", type=int, default=13)
    ap.add_argument("--max-minutes", type=int, default=300)
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()

    season = int(args.season)
    started = time.time()
    budget = args.max_minutes * 60
    ok_eps, skipped, failed = [], [], []

    for ep in range(args.ep_from, args.through + 1):
        if time.time() - started > budget:
            say("时间预算用完了（%d 分钟），剩下的下次继续" % args.max_minutes)
            break
        if done_until(season, ep) >= 1000 and not args.redo:
            skipped.append(ep)
            continue

        ok = False
        for attempt in (1, 2, 3):
            say("第 %d 集 第 %d 次尝试" % (ep, attempt))
            rc, out = run_one(season, ep, args.redo)
            tail = [ln for ln in out.strip().split("\n") if ln.strip()][-1:] or [""]
            say("   → rc=%s %s" % (rc, tail[0][:110]))
            if rc == 0:
                ok = True
                break
            time.sleep(60)

        if ok:
            ok_eps.append(ep)
        else:
            failed.append(ep)

    say("本轮: 完成 %s / 跳过(已看过) %s / 失败 %s"
        % (ok_eps or "无", skipped or "无", failed or "无"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
