#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文稳定性检查：稳定前缀必须跨轮一致（缓存能不能命中的硬判据）。

原理：prompt.js 在动态内容前插了一行分界线
    [以下为动态内容·每轮可能变化，冲突时以上面为准]
这行以上的内容如果每轮都变，前缀缓存永远命中不了（历史 bug：时间戳插在前缀里，
缓存命中率 24%）。所以这里读最近几轮的 system 提示，取分界线之前的哈希比对。

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

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
MARK = "[以下为动态内容"


def newest_session():
    cands = []
    for root, _dirs, files in os.walk(os.path.join(BASE, "sessions")):
        for f in files:
            if f.startswith("session.") and ".zstd" in f:
                p = os.path.join(root, f)
                cands.append((os.path.getmtime(p), p))
    return max(cands)[1] if cands else None


def system_prompts(path, limit=6):
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
        if text:
            out.append(text)
    return out[-limit:]


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
    for i, text in enumerate(prompts, 1):
        marker_at = text.find(MARK)
        cut = marker_at
        if cut <= 0:
            # 老版本没有分界线：时间戳本来就在末尾（bridge.js 里 parts.complete + header），
            # 按它切一下，否则每轮秒数不同会误判成"前缀不稳定"。
            cut = text.rfind("（当前时间")
        stable = text[:cut] if cut > 0 else text
        volatile_len = len(text) - len(stable)
        rows.append(dict(
            turn=i,
            total=len(text),
            stable=len(stable),
            volatile=volatile_len,
            hash=hashlib.sha1(stable.encode("utf-8")).hexdigest()[:12],
            has_marker=marker_at > 0,
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

    hashes = {r["hash"] for r in rows if r["has_marker"]}
    marked = [r for r in rows if r["has_marker"]]
    if not marked:
        print("\n[WARN] 这些 system 提示里没有动态分界线（可能是旧版本产生的）")
        return 0
    if len(hashes) == 1:
        print("\n[OK  ] 稳定前缀跨 %d 轮完全一致（%s），缓存可以命中" % (len(marked), list(hashes)[0]))
        return 0
    print("\n[FAIL] 稳定前缀在 %d 轮里出现了 %d 种，缓存必然 miss" % (len(marked), len(hashes)))
    for h in sorted(hashes):
        print("   -", h)
    return 1


if __name__ == "__main__":
    sys.exit(main())
