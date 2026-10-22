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

    if fails:
        for m in fails:
            print("  [FAIL] " + m)
        return 1
    print("  [PASS] python syntax and json parse")
    return 0


if __name__ == "__main__":
    import os  # noqa: E402  (放在 main 里用到，保持顶部干净)
    sys.exit(main())
