#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文稳定性检查：稳定前缀必须跨轮一致（缓存能不能命中的硬判据）。

原理：prompt.js 在动态内容前插了一行分界线
    [以下为动态内容·每轮可能变化，冲突时以上面为准]
这行以上的内容如果每轮都变，前缀缓存永远命中不了（历史 bug：时间戳插在前缀里，
缓存命中率 24%）。所以这里读最近几轮的 system 提示，取分界线之前的哈希比对。

2026-11-09 补的一条判据：
「不一致」分两种，不能一律判死。
  · **每轮都在变** —— 真 bug（时间、计数器漏进了前缀），必须 FAIL
  · **会话中途改过一次** —— persona / AGENTS.md / self_state 被人或蒸馏任务
    在会话进行中重写了。这种情况只会让那一次请求 miss，之后照样一致。
    判 FAIL 等于"谁改了记忆文件就报错"，属于误报。

区分办法：看相邻两轮之间，这三个注入文件有没有被改过（mtime 落在两轮时间之间）。
改过 → WARN 并指名道姓；没改过却变了 → FAIL。

用法：
    python3 ctx_hash.py            # 最近 5 轮
    python3 ctx_hash.py --json
"""
import glob
import hashlib
import io
import json
import os
import subprocess
import sys
import time

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
MARK = "[以下为动态内容"

# 这几个文件的内容会进稳定前缀（prompt.js 里 readFileSync 的地方）
WATCH = (
    "memory/self_state.md",
    "media/onebot/persona.md",
    "workspace/AGENTS.md",
)


def newest_session():
    cands = []
    for root, _dirs, files in os.walk(os.path.join(BASE, "sessions")):
        for f in files:
            if f.startswith("session.") and ".zstd" in f:
                p = os.path.join(root, f)
                cands.append((os.path.getmtime(p), p))
    return max(cands)[1] if cands else None


def _norm_ts(v):
    """事件时间统一成秒；拿不到就返回 None。"""
    if v is None:
        return None
    try:
        v = float(v)
    except Exception:
        return None
    return v / 1000.0 if v > 1e11 else v


def system_prompts(path, limit=6):
    """返回 [{text, ts}]，ts 是这条 system 提示被写下的时间（秒，可能 None）。"""
    try:
        raw = subprocess.run(["zstd", "-dc", path], capture_output=True, timeout=30).stdout
    except Exception:
        return []
    out = []
    for line in raw.decode("utf-8", "replace").split("\n"):
        if '"system/message"' not in line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        msg = (ev.get("data") or {}).get("message") or {}
        text = "".join(c.get("text", "") for c in (msg.get("content") or []) if isinstance(c, dict))
        if not text:
            continue
        ts = _norm_ts(ev.get("time") or ev.get("timestamp")
                      or (ev.get("data") or {}).get("time"))
        out.append({"text": text, "ts": ts})
    return out[-limit:]


def changed_between(t0, t1):
    """t0~t1 之间被改过的注入文件（返回 [(相对路径, mtime)]）。"""
    if not t0 or not t1:
        return []
    lo, hi = min(t0, t1), max(t0, t1)
    hits = []
    for rel in WATCH:
        p = os.path.join(BASE, rel)
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if lo < m <= hi:
            hits.append((rel, m))
    return hits


def main():
    p = newest_session()
    if not p:
        print("找不到会话文件")
        return 2
    prompts = system_prompts(p)
    if len(prompts) < 2:
        print("system 提示不足 2 条，无法比较")
        return 2

    rows = []
    for i, item in enumerate(prompts, 1):
        text = item["text"]
        marker_at = text.find(MARK)
        cut = marker_at
        if cut <= 0:
            # 老版本没有分界线：时间戳本来就在末尾（bridge.js 里 parts.complete + header），
            # 按它切一下，否则每轮秒数不同会误判成"前缀不稳定"。
            cut = text.rfind("（当前时间")
        stable = text[:cut] if cut > 0 else text
        rows.append(dict(
            turn=i,
            total=len(text),
            stable=len(stable),
            volatile=len(text) - len(stable),
            hash=hashlib.sha1(stable.encode("utf-8")).hexdigest()[:12],
            has_marker=marker_at > 0,
            ts=item["ts"],
        ))

    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    print("会话: %s" % os.path.basename(p))
    print("%-4s %-9s %-9s %-9s %-14s %s" % ("轮", "总长", "稳定段", "动态段", "前缀哈希", "分界线"))
    for r in rows:
        print("%-4d %-9d %-9d %-9d %-14s %s" % (
            r["turn"], r["total"], r["stable"], r["volatile"], r["hash"],
            "有" if r["has_marker"] else "无（老版本）"))

    marked = [r for r in rows if r["has_marker"]]
    if not marked:
        print("\n[WARN] 这些 system 提示里没有动态分界线（可能是旧版本产生的）")
        return 0

    hashes = {r["hash"] for r in marked}
    if len(hashes) == 1:
        print("\n[OK  ] 稳定前缀跨 %d 轮完全一致（%s），缓存可以命中" % (len(marked), list(hashes)[0]))
        return 0

    # 逐对看：这一对之间有没有注入文件被改过
    pairs = []
    for a, b in zip(marked, marked[1:]):
        if a["hash"] == b["hash"]:
            continue
        pairs.append((a, b, changed_between(a["ts"], b["ts"])))

    explained = [p for p in pairs if p[2]]
    unexplained = [p for p in pairs if not p[2]]

    if unexplained:
        print("\n[FAIL] 稳定前缀在 %d 轮里出现了 %d 种，缓存必然 miss" % (len(marked), len(hashes)))
        for h in sorted(hashes):
            print("   -", h)
        for a, b, _ in unexplained:
            print("   第 %d→%d 轮之间没有任何注入文件改动，内容却变了 —— 有东西在每轮抖动"
                  % (a["turn"], b["turn"]))
        return 1

    # 全都能用"文件中途更新"解释：只让那一次请求 miss，不算故障
    print("\n[WARN] 稳定前缀在会话中途变过 1 次（不是每轮抖动，只影响那一次请求）")
    for a, b, hits in explained:
        for rel, m in hits:
            print("   第 %d→%d 轮之间：%s 于 %s 更新"
                  % (a["turn"], b["turn"], rel, time.strftime("%m-%d %H:%M:%S", time.localtime(m))))
    print("   改完后的 %d 轮是一致的（%s）—— 缓存从那一轮起恢复正常"
          % (len(marked) - 1, marked[-1]["hash"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
