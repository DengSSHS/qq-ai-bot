#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preflight 的 Python 侧检查：语法、可解析性。

单独成文件，避免 bash heredoc 与 set -e/pipefail 交互导致的误判。
输出 ASCII，退出码 0=全过。
"""
import ast
import glob
import json
import sys

fails = []


def check_py38(path):
    src = open(path, encoding="utf-8").read()
    try:
        ast.parse(src, feature_version=(3, 8))
    except SyntaxError as e:
        fails.append("%s: %s (line %s)" % (path, e.msg, e.lineno))


def main():
    targets = sorted(glob.glob("stack/*.py")) + sorted(glob.glob("*.py"))
    for f in targets:
        if f.endswith(os.path.basename(__file__)):
            continue
        check_py38(f)
    print("  checked %d python files for 3.8 compatibility" % len(targets))

    # JSON 必须是合法 JSON
    for f in sorted(glob.glob("stack/*.json")):
        try:
            json.load(open(f, encoding="utf-8"))
        except Exception as e:
            fails.append("%s: invalid json (%s)" % (f, e))

    check_doc_numbers()

    if fails:
        for m in fails:
            print("  [FAIL] " + m)
        return 1
    print("  [PASS] python syntax, json parse, doc numbers consistent")
    return 0


def check_doc_numbers():
    """跨文档数字一致性。

    存在的意义：之前 README 说 106 项、CONTRIBUTING 说 106 项、
    实际是浮动的约 100 项；CHANGELOG 说 27 个地址、实际 26 个。
    人眼查不出来，所以让脚本查。
    """
    import re

    # ── ① 文档里不许写死 checkall 的项数（100~199 项是它的区间） ──
    # CHANGELOG 排除：它天然会引用历史错误值做记录
    doc_files = ["README.md", "CONTRIBUTING.md", "DEPLOY.md", "MANUAL.md", "START-HERE.md"]
    for f in doc_files:
        if not os.path.exists(f):
            continue
        for ln, line in enumerate(open(f, encoding="utf-8"), 1):
            # 允许浮动表述：约 100 项 / ~100 项 / 一百来项
            if re.search(r"(约|~|大约|一百来)\s*100\s*项", line):
                continue
            m = re.search(r"(?<![\d`])(1[0-9]{2})\s*项", line)
            if m:
                fails.append(
                    "%s:%d 写死了 checkall 项数（%s 项）。它是浮动值，应写「约 100 项」"
                    % (f, ln, m.group(1)))

    # ── ② 文档说的「必须拦的地址数」要和测试里实际数量一致 ──
    test_file = os.path.join("tests", "test_websurf_ipguard.py")
    if os.path.exists(test_file):
        tree = ast.parse(open(test_file, encoding="utf-8").read())
        actual = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "IpGuard":
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        name = getattr(item.targets[0], "id", "")
                        if name in ("MUST_BLOCK", "MUST_ALLOW"):
                            actual[name] = len(item.value.elts)
        # 只查 README：CHANGELOG 是历史叙述，必然引用过去的错误值做记录
        for f in ("README.md",):
            if not os.path.exists(f) or not actual:
                continue
            text = open(f, encoding="utf-8").read()
            m = re.search(r"(\d+)\s*个必须拦", text)
            if m and "MUST_BLOCK" in actual:
                if int(m.group(1)) != actual["MUST_BLOCK"]:
                    fails.append(
                        "%s 说「%s 个必须拦」，tests 里实际是 %d 个"
                        % (f, m.group(1), actual["MUST_BLOCK"]))
            m = re.search(r"(\d+)\s*个必须放行", text)
            if m and "MUST_ALLOW" in actual:
                if int(m.group(1)) != actual["MUST_ALLOW"]:
                    fails.append(
                        "%s 说「%s 个必须放行」，tests 里实际是 %d 个"
                        % (f, m.group(1), actual["MUST_ALLOW"]))

    # ── ③ 文档若提到测试用例总数，要和实际一致 ──
    n_cases = 0
    for tf in glob.glob("tests/test_*.py"):
        t = ast.parse(open(tf, encoding="utf-8").read())
        for node in ast.walk(t):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                n_cases += 1
    if n_cases:
        for f in ("README.md",):
            if not os.path.exists(f):
                continue
            m = re.search(r"(\d+)\s*个单元测试|新增 `tests/`：(\d+)|(\d+) 个测试", 
                          open(f, encoding="utf-8").read())
            if m:
                claimed = int(next(g for g in m.groups() if g))
                if claimed != n_cases:
                    fails.append("%s 说 %d 个单元测试，实际 %d 个"
                                 % (f, claimed, n_cases))


if __name__ == "__main__":
    import os  # noqa: E402  (放在 main 里用到，保持顶部干净)
    sys.exit(main())
