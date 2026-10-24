#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补丁漂移检测：上游插件发新版时，验证本地补丁还能不能打上。

为什么需要：
    `apply_local_patches.py` 管着 node_modules 里的 5 处本地改动。上游一升级，
    任何一处的锚点（anchor）都可能变，补丁就静默失效——不报错，只是某个功能
    悄悄不工作（已经踩过：看图端点坏了一整天没人发现）。

做法：
    1. 读本地已装版本
    2. 查 registry 的最新版本
    3. 有新版本 → 下载 tgz、解包到临时目录
    4. 在**干净的新版本**上逐条打补丁，然后用 `node --check` 自检
    5. 打不上/语法坏的 → 告警（可发邮件），退出码 2

退出码：
    0  一切正常（没新版，或新版全部可打）
    1  环境问题（registry 不通等）
    2  有补丁打不上 → 需要人工跟进

用法：
    python3 patch_doctor.py                 # 只看报告
    python3 patch_doctor.py --mail          # 必要时发邮件
    python3 patch_doctor.py --ci            # CI 模式：有漂移即非零退出
    python3 patch_doctor.py --version 0.9.9 # 指定版本验证（跳过查最新）
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

BASE = "/opt/dsh"
PKG = "dsh-onebot-qq"
LIB = os.path.join(BASE, "profiles/web/node_modules", PKG, "lib")
REGISTRY = os.environ.get("PATCH_DOCTOR_REGISTRY", "https://registry.npmmirror.com")


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "patch-doctor/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def installed_version():
    try:
        p = os.path.join(os.path.dirname(LIB), "package.json")
        with io.open(p, encoding="utf-8") as f:
            return json.load(f).get("version") or "?"
    except Exception:
        return "?"


def latest_version():
    data = json.loads(_get("%s/%s" % (REGISTRY, PKG)).decode("utf-8"))
    return (data.get("dist-tags") or {}).get("latest") or "?"


def fetch_lib(version, dest):
    """下载指定版本并解出 lib/ 目录到 dest，返回该目录。"""
    url = "%s/%s/-/%s-%s.tgz" % (REGISTRY, PKG, PKG, version)
    raw = _get(url, timeout=120)
    tgz = os.path.join(dest, "pkg.tgz")
    with open(tgz, "wb") as f:
        f.write(raw)
    with tarfile.open(tgz, "r:gz") as t:
        t.extractall(dest)
    return os.path.join(dest, "package", "lib")


def node_check(libdir):
    node = shutil.which("node") or "/opt/node/bin/node"
    if not os.path.exists(node) and not shutil.which("node"):
        return None, "没有 node，跳过语法自检"
    bad = []
    for name in ("prompt.js", "bridge.js", "inbound.js", "index.js", "cq.js"):
        p = os.path.join(libdir, name)
        if not os.path.exists(p):
            bad.append("%s（文件不存在）" % name)
            continue
        r = subprocess.run([node, "--check", p], capture_output=True)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace").strip().splitlines()
            bad.append("%s: %s" % (name, err[0] if err else "语法错误"))
    return bad, ""


def verify_against(libdir, apply_all=True):
    """把补丁打到给定的干净 lib 目录上，返回 (逐条结果, 语法问题)。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import apply_local_patches as alp
    alp.LIB = libdir                     # 让补丁函数指向这次的目标目录
    results = []
    for name, fn in alp.PATCHES:
        try:
            r = fn(apply_all)
        except Exception as e:
            r = "异常: %s" % str(e)[:80]
        results.append((name, r))
    bad, note = node_check(libdir)
    return results, (bad or []), note


def report(local_v, remote_v, results, syntax_bad, note, target_v):
    lines = []
    lines.append("本地已装：%s" % local_v)
    lines.append("registry 最新：%s" % remote_v)
    lines.append("本次验证目标：%s" % target_v)
    lines.append("")
    drift = []
    for name, r in results:
        ok = r in ("已打", "未打")
        mark = "OK  " if ok else "DRIFT"
        lines.append("  [%s] %-34s %s" % (mark, name, r))
        if not ok:
            drift.append((name, r))
    if syntax_bad:
        lines.append("")
        lines.append("语法自检失败：")
        for b in syntax_bad:
            lines.append("  - " + b)
    if note:
        lines.append("")
        lines.append("（%s）" % note)
    return lines, drift


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mail", action="store_true", help="有漂移时发邮件")
    ap.add_argument("--ci", action="store_true", help="CI 模式：有漂移返回 2")
    ap.add_argument("--version", default="", help="指定要验证的上游版本")
    ap.add_argument("--local-lib", default="", help="用本地目录代替下载（调试用）")
    a = ap.parse_args()

    local_v = installed_version()
    try:
        remote_v = a.version or latest_version()
    except Exception as e:
        print("查 registry 失败：%s" % str(e)[:120])
        return 1

    # 没有新版本且不指定版本时，仍然验证一次当前版本（防止本地被动过）
    target_v = a.version or remote_v
    tmp = tempfile.mkdtemp(prefix="patchdoc-")
    try:
        if a.local_lib and os.path.isdir(a.local_lib):
            libdir = a.local_lib
        else:
            libdir = fetch_lib(target_v, tmp)
        results, syntax_bad, note = verify_against(libdir)
    except Exception as e:
        print("准备上游代码失败：%s" % str(e)[:160])
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    lines, drift = report(local_v, remote_v, results, syntax_bad, note, target_v)
    print(chr(10).join(lines))

    new_version = (remote_v != local_v) and not a.version
    if new_version and not drift and not syntax_bad:
        print("")
        print("上游有新版 %s，5 处补丁在它上面全部可打 —— 可以升级并重打补丁。"
              % remote_v)

    bad = bool(drift or syntax_bad)
    if bad:
        print("")
        print("=== 需要人工跟进：有补丁打不上 ===")
        for name, r in drift:
            print("  %s → %s" % (name, r))
        if a.mail:
            try:
                import mailer
                body = ("上游 %s（本地 %s）上，本地补丁出现漂移：%s"
                        % (target_v, local_v, chr(10) + chr(10).join(lines)))
                ok, why = mailer.send("【补丁漂移】%s 有补丁打不上了" % PKG, body)
                print("邮件: %s" % why)
            except Exception as e:
                print("邮件发送失败: %s" % str(e)[:100])
    else:
        print("")
        print("结论：%d 处补丁全部可打。" % len(results))

    if a.ci and bad:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
