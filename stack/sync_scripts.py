#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把仓库里的脚本同步到 /opt/dsh，但**绝不覆盖别人改过的文件**。

问题（用户 2026-10-25）：
    别人肯定会自己改一些东西。如果更新把这些改动挤掉，"更新"就成了破坏。

做法：三方比对。
    安装/上次同步时，把每个文件的 sha256 记进 deploy-manifest.json（基线）。
    更新时对每个文件看三方：
        磁盘现在的哈希、基线的哈希、仓库新版本的哈希
      - 磁盘 == 基线 → 没人动过 → **直接更新**
      - 磁盘 != 基线，且 == 新版本 → 已经是新版 → 不用动
      - 磁盘 != 基线，且 != 新版本 → **别人改过** → 保留他的，把新版写到 .new 旁边，
        并在报告里列出来（让他自己决定合并还是丢弃）

用法：
    python3 sync_scripts.py            # 同步（默认不覆盖本地改动）
    python3 sync_scripts.py --check    # 只报告会做什么
    python3 sync_scripts.py --adopt    # 把当前磁盘内容当作新基线（"这些改动我认了"）
    python3 sync_scripts.py --force    # 强制用仓库版本覆盖（慎用）
"""
import hashlib
import io
import json
import os
import shutil
import sys

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
REPO = os.environ.get("REPO_DIR", "")
MANIFEST = os.path.join(BASE, "memory", "deploy-manifest.json")
CONFLICTS = os.path.join(BASE, "memory", "deploy-conflicts.json")

# 只同步这些模式（仓库 stack/ → /opt/dsh/）
PATTERNS = (".py", ".sh")


def sha(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def find_repo():
    if REPO and os.path.isdir(REPO):
        return REPO
    for d in (os.path.join(BASE, "repo"), "/opt/qq-ai-bot", "/root/qq-ai-bot",
              "/opt/qqbot/repo", BASE):
        if os.path.isdir(os.path.join(d, "stack")):
            return d
    return ""


def load_manifest():
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def save_manifest(d):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, MANIFEST)


def notify_conflicts(conflicts):
    """把冲突告诉使用者（同一次冲突只提醒一次，不重复刷屏）。"""
    if not conflicts:
        return
    sig = "|".join("%s:%s" % (n, h[:8]) for n, h in sorted(conflicts))
    try:
        with open(CONFLICTS, encoding="utf-8") as f:
            prev = json.load(f).get("sig", "")
    except Exception:
        prev = ""
    if prev == sig:
        return  # 同一个冲突已经提醒过了

    names = ", ".join(n for n, _ in conflicts[:6])
    more = "" if len(conflicts) <= 6 else " 等 %d 个" % len(conflicts)
    subject = "更新时发现 %d 个文件被你改过（未覆盖）" % len(conflicts)
    body = (
        "这些文件你（或使用者）改过，更新时保留了你的版本，没有覆盖：\n\n"
        + "\n".join("  - %s" % n for n, _ in conflicts[:12])
        + ("\n  …还有 %d 个" % (len(conflicts) - 12) if len(conflicts) > 12 else "")
        + "\n\n新版本已经放在同目录的「文件名.new」里，你可以：\n"
        "  1. 什么都不做 —— 继续用自己的版本（下次更新还会这样提示，但只提醒一次）\n"
        "  2. 用新版 —— 把 .new 覆盖过去\n"
        "  3. 先看差异 —— diff -u 文件名 文件名.new\n\n"
        "提醒：改插件（node_modules）请用 apply_local_patches.py，那个不会被更新冲掉。"
    )
    sent = False
    try:
        import subprocess
        py = sys.executable or "python3"
        r = subprocess.run([py, os.path.join(BASE, "notify.py"), subject, body],
                           timeout=60, check=False, capture_output=True)
        sent = (r.returncode == 0)
        if not sent:
            err = (r.stderr or b"").decode("utf-8", "replace")[:200]
            print("  (提醒没发出去，下次更新会再试：%s)" % err.strip())
    except Exception as e:
        print("  (提醒发送异常，下次更新会再试：%s)" % e)

    # 只有真的发出去了才记“已提醒”。否则没发出去却不再提醒，等于把话吞了。
    if sent:
        try:
            os.makedirs(os.path.dirname(CONFLICTS), exist_ok=True)
            with open(CONFLICTS, "w", encoding="utf-8") as f:
                json.dump({"sig": sig, "files": [n for n, _ in conflicts]}, f,
                          ensure_ascii=False, indent=1)
        except Exception:
            pass


def main():
    check_only = "--check" in sys.argv
    adopt = "--adopt" in sys.argv
    force = "--force" in sys.argv

    repo = find_repo()
    if not repo:
        print("找不到仓库目录（需要含 stack/ 的目录），跳过脚本同步")
        return 0

    src_dir = os.path.join(repo, "stack")
    manifest = load_manifest()
    new_manifest = dict(manifest)

    stats = {"新增": [], "更新": [], "保留本地改动": [], "已是最新": [], "无变化": []}
    conflicts = []

    for name in sorted(os.listdir(src_dir)):
        if not name.endswith(PATTERNS):
            continue
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(BASE, name)
        new_hash = sha(src)
        disk_hash = sha(dst)
        base_hash = manifest.get(name, "")

        if adopt:
            new_manifest[name] = disk_hash or new_hash
            stats["无变化"].append(name)
            continue

        if not disk_hash:
            if not check_only:
                shutil.copy2(src, dst)
            new_manifest[name] = new_hash
            stats["新增"].append(name)
        elif disk_hash == new_hash:
            new_manifest[name] = new_hash
            stats["已是最新"].append(name)
        elif disk_hash == base_hash:
            # 没人动过 → 安全更新
            if not check_only:
                shutil.copy2(src, dst)
            new_manifest[name] = new_hash
            stats["更新"].append(name)
        elif not base_hash:
            # 没有基线，且磁盘内容与仓库不同 —— 搞不清是不是人改的。
            # 宁可不更新，也不能把人家的改动抹掉。
            if not check_only:
                shutil.copy2(src, dst + ".new")
            new_manifest[name] = disk_hash
            stats["保留本地改动"].append(name +
                                       "（无基线，已保守处理；确认后可 --adopt）")
            conflicts.append((name, new_hash))
        else:
            # 别人改过，且新版也不同 → 绝不覆盖
            if force:
                if not check_only:
                    shutil.copy2(src, dst)
                    new_manifest[name] = new_hash
                stats["更新"].append(name + "（--force 覆盖）")
            else:
                if not check_only:
                    shutil.copy2(src, dst + ".new")
                stats["保留本地改动"].append(name)
                conflicts.append((name, new_hash))

    if not check_only:
        save_manifest(new_manifest)

    for k in ("新增", "更新", "保留本地改动"):
        if stats[k]:
            print("%s %d 个：%s" % (k, len(stats[k]), ", ".join(stats[k][:8])))
    if stats["保留本地改动"]:
        print()
        print("  [注意] 下面这些文件你（或使用者）改过，**没有覆盖**：")
        for n in stats["保留本地改动"]:
            print("     %-28s 新版本已放在 %s.new" % (n, n))
        print("     想保留自己的版本：什么都不用做（下次更新还会这样提示）")
        print("     想用新版本：把 .new 覆盖过去")
        print("     想合并：diff -u %s %s.new" % (stats["保留本地改动"][0],
                                                 stats["保留本地改动"][0]))
        print("     想让当前内容成为新基线（以后按它比对）：--adopt")

    # 冲突要主动告诉使用者（同一次冲突只提醒一次，避免每次跑都刷屏）
    if conflicts and not check_only:
        notify_conflicts(conflicts)

    if not any(stats[k] for k in stats if k != "无变化"):
        print("脚本无需同步（%d 个文件已是仓库版本）" % len(stats["无变化"]) + str(len(stats["已是最新"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
