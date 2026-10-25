#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发一条通知：优先用 qqguard 的直连通道（真发到 QQ），失败退回邮件。

用法：python3 notify.py "标题" "正文"
"""
import importlib.util
import sys

BASE = "/opt/dsh"


def main():
    subject = sys.argv[1] if len(sys.argv) > 1 else "通知"
    body = sys.argv[2] if len(sys.argv) > 2 else ""
    text = "【%s】%s" % (subject, body)

    ok = False
    try:
        spec = importlib.util.spec_from_file_location("qqguard", BASE + "/qqguard.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        ok, why = m.send_direct(text)
        print("QQ:", "已发" if ok else "失败", why)
    except Exception as e:
        print("QQ 通道异常:", e)

    if not ok:
        try:
            sys.path.insert(0, BASE)
            import mailer
            ok2, why2 = mailer.send(subject, body)
            print("邮件:", "已发" if ok2 else "失败", why2)
        except Exception as e:
            print("邮件通道异常:", e)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
