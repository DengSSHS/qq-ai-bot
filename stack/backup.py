#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一备份：一个清单 + 一个 tar + 可校验。

为什么要有这个文件：
    服务器上散着 113 处备份痕迹（.bak-*、switch-backups、dupes、lib-before-rebuild），
    但没有一个"清单"能回答：迁机器时到底该带走什么、什么可以再生。
    漏一个 index.json → 她"失忆"，而且不会报错。

设计：
    · 明确分两类：DATA（必须带走） / REGEN（可再生，不用带）
    · 贴纸库默认不带（2.8G，可重下）；需要时 --with-stickers
    · 会话原文默认不带（37M，属于数据）；需要时 --with-sessions
    · 输出 tar.gz + MANIFEST.txt（含每个文件的 size/sha256）
    · --verify <tar> 在临时目录里解出来逐项校验

用法：
    python3 backup.py                      # 打包（默认）
    python3 backup.py --with-sessions      # 连会话原文一起
    python3 backup.py --with-stickers      # 连贴纸库（2.8G）
    python3 backup.py --verify X.tar.gz    # 校验某个备份
    python3 backup.py --list               # 只看清单，不打包
"""
import hashlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
import time

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
OUT = os.path.join(BASE, "backups")

# (相对路径, 说明)
DATA = [
    ("memory/diary", "日记正文（她自己的记忆）"),
    ("memory/knowledge.json", "结构化事实库"),
    ("memory/session_log", "历史会话摘要（上周聊过什么靠它）"),
    ("memory/session_summary.md", "临时层滚动摘要"),
    ("memory/notes.md", "长期笔记"),
    ("memory/diary/index.json", "日记索引（遗忘曲线的 hits 存这里，漏了就丢排序依据）"),
    ("memory/layers_state.json", "三层记忆的蒸馏进度标记（漏了会重复蒸馏）"),
    ("memory/bili-watched.jsonl", "她看过什么"),
    ("memory/limits.json", "限额配置"),
    ("limits.json", "限额配置（/opt/dsh 根下那份）"),
    ("media/onebot/persona.md", "人设"),
    ("workspace/AGENTS.md", "核心记忆"),
    ("workspace/notes", "工作区笔记（若有）"),
    ("profiles/web/cordis.patch.yml", "插件配置（含所有开关）"),
    ("qwen.env", "API key（敏感，注意别外传）"),
]

OPTIONAL = [
    ("sessions", "会话原文（37M）", "--with-sessions"),
    ("media/onebot/stickers", "贴纸库（2.8G，可重下）", "--with-stickers"),
]

REGEN = [
    "memory/search.db", "memory/vision_cache.json", "memory/*.log",
    "memory/*.qzone*", "memory/schedule-state.json", "memory/qqguard.json",
    "profiles/web/node_modules", "vendor/fork-*", "sessions/*/*/session_projcache",
    "**/__pycache__", "**/*.bak-*", "backups",
]


def sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            if limit is not None:
                limit -= len(b)
                if limit <= 0:
                    break
    return h.hexdigest()


def collect(include_sessions, include_stickers):
    items = list(DATA)
    if include_sessions:
        items.append((OPTIONAL[0][0], OPTIONAL[0][1]))
    if include_stickers:
        items.append((OPTIONAL[1][0], OPTIONAL[1][1]))
    found, missing = [], []
    for rel, why in items:
        p = os.path.join(BASE, rel)
        if os.path.exists(p):
            found.append((rel, why, p))
        else:
            missing.append(rel)
    return found, missing


def build(include_sessions=False, include_stickers=False):
    found, missing = collect(include_sessions, include_stickers)
    if not found:
        print("没有可备份的数据（%s）" % BASE)
        return 1
    os.makedirs(OUT, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    tar_path = os.path.join(OUT, "backup-%s.tar.gz" % stamp)

    lines = ["# 备份清单", "时间: " + time.strftime("%Y-%m-%d %H:%M:%S"),
             "来源: " + BASE, ""]
    lines.append("## DATA（必须带走）")
    total = 0
    for rel, why, p in found:
        if os.path.isdir(p):
            n = sum(len(f) for _r, _d, f in os.walk(p))
            sz = sum(os.path.getsize(os.path.join(r, f))
                     for r, _d, fs in os.walk(p) for f in fs)
            lines.append("  [dir ] %-34s %6d 个文件 %8.1f KB  # %s" % (rel, n, sz / 1024.0, why))
        else:
            sz = os.path.getsize(p)
            lines.append("  [file] %-34s %8.1f KB  sha256:%s  # %s"
                         % (rel, sz / 1024.0, sha256(p)[:16], why))
        total += sz
    if missing:
        lines.append("")
        lines.append("## 缺席（这台机器上没有，正常）")
        for m in missing:
            lines.append("  - " + m)
    lines.append("")
    lines.append("## 不备份（可再生）")
    for r in REGEN:
        lines.append("  - " + r)
    if not include_stickers:
        lines.append("  - media/onebot/stickers（2.8G，用 --with-stickers 才会带）")
    if not include_sessions:
        lines.append("  - sessions（37M，用 --with-sessions 才会带）")
    manifest = "\n".join(lines) + "\n"

    mpath = os.path.join(OUT, "MANIFEST-%s.txt" % stamp)
    io.open(mpath, "w", encoding="utf-8", newline="\n").write(manifest)

    print("打包 %d 项，原始 %.1f MB …" % (len(found), total / 1024.0 / 1024.0))
    t0 = time.time()
    with tarfile.open(tar_path, "w:gz") as tar:
        for rel, _why, p in found:
            tar.add(p, arcname=rel)
        tar.add(mpath, arcname="MANIFEST.txt")
    size = os.path.getsize(tar_path) / 1024.0 / 1024.0
    print("完成: %s（%.1f MB，%.1f 秒）" % (tar_path, size, time.time() - t0))
    print("清单: %s" % mpath)
    _prune(keep=20)
    return 0


def _prune(keep=20):
    """只留最近 N 份（tar 和 manifest 各一套），否则备份自己会变成垃圾场。"""
    try:
        tars = sorted(p for p in os.listdir(OUT) if p.startswith("backup-") and p.endswith(".tar.gz"))
        for old in tars[:-keep]:
            stamp = old[len("backup-"):-len(".tar.gz")]
            for name in (old, "MANIFEST-%s.txt" % stamp):
                try:
                    os.remove(os.path.join(OUT, name))
                except OSError:
                    pass
    except OSError:
        pass


def verify(tar_path):
    if not os.path.isfile(tar_path):
        print("找不到 %s" % tar_path)
        return 1
    tmp = tempfile.mkdtemp(prefix="backup-verify-")
    try:
        with tarfile.open(tar_path) as tar:
            tar.extractall(tmp)
        man = os.path.join(tmp, "MANIFEST.txt")
        if not os.path.isfile(man):
            print("[FAIL] 包里没有 MANIFEST.txt")
            return 2
        text = io.open(man, encoding="utf-8").read()
        want = [ln.split("sha256:")[0].strip().split()[-1]
                for ln in text.split("\n") if "sha256:" in ln]
        files = [ln for ln in text.split("\n") if "[file]" in ln]
        bad = []
        for ln in files:
            rel = ln.split("]")[1].strip().split()[0]
            expect = ln.split("sha256:")[1].split()[0]
            p = os.path.join(tmp, rel)
            if not os.path.isfile(p):
                bad.append((rel, "缺失"))
                continue
            if sha256(p)[:16] != expect:
                bad.append((rel, "校验和不符"))
        dirs = [ln for ln in text.split("\n") if "[dir ]" in ln]
        for ln in dirs:
            rel = ln.split("]")[1].strip().split()[0]
            if not os.path.isdir(os.path.join(tmp, rel)):
                bad.append((rel, "目录缺失"))

        print("清单里 %d 个文件 / %d 个目录" % (len(files), len(dirs)))
        if bad:
            print("[FAIL] %d 项有问题：" % len(bad))
            for rel, why in bad[:10]:
                print("   -", rel, why)
            return 3
        print("[OK  ] 全部校验通过（解出在 %s）" % tmp)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    args = sys.argv[1:]
    if "--list" in args:
        found, missing = collect("--with-sessions" in args, "--with-stickers" in args)
        print("会备份：")
        for rel, why, p in found:
            print("  + %-34s %s" % (rel, why))
        for m in missing:
            print("  - %-34s （没有）" % m)
        return 0
    if "--verify" in args:
        i = args.index("--verify")
        return verify(args[i + 1]) if i + 1 < len(args) else 1
    return build("--with-sessions" in args, "--with-stickers" in args)


if __name__ == "__main__":
    sys.exit(main())
