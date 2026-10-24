#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日一份邮件日报：花了多少、有没有异常、QQ 掉过线没有。

为什么用邮件而不是 QQ：
    QQ 掉线时出站通道断，没法通知；邮件是唯一必然还通的渠道。
    所以这封日报除了"看账"，还兼着"心跳"——如果连着两天收不到，
    说明机器或服务出问题了。

设计原则：任何一段取数失败都不能让整封邮件发不出去（逐段 try），
否则最需要告警的时候恰恰发不出去。
"""
import os
import subprocess
import sys
import time

BASE = "/opt/dsh"
INCIDENTS = os.path.join(BASE, "memory/qq-incidents.log")


def _section(title, fn):
    try:
        txt = fn()
    except Exception as e:
        txt = "(取数失败: %s)" % str(e)[:80]
    txt = (txt or "").strip()
    if not txt:
        return []
    return ["", "== %s ==" % title, txt]


def _usage():
    import usage_log
    return usage_log.render(1)


def _ops():
    import ops_report
    return ops_report.render(24, False)


def _budget():
    import budget
    return budget.render()


def _qq():
    """今天的掉线事件。"""
    today = time.strftime("%F")
    lines = []
    try:
        for line in open(INCIDENTS, encoding="utf-8"):
            if line.startswith(today):
                lines.append("  " + line[len(today):].strip())
    except Exception:
        return ""
    if not lines:
        return "今天没有掉线记录。"
    return ("今天 %d 条事件：\n" % len(lines)) + chr(10).join(lines[-8:])


def build():
    parts = ["鲸鲸 %s 日报" % time.strftime("%F"), ""]
    parts.append("今天是 %s（%s）" % (time.strftime("%F"), "星期" + "日一二三四五六"[time.localtime().tm_wday]))
    for title, fn in (("用量", _usage), ("排障（近 24 小时）", _ops),
                      ("预算", _budget), ("QQ 连接", _qq)):
        parts.extend(_section(title, fn))
    parts += ["", "-- ",
              "详细数据在服务器：memory/usage.jsonl、memory/qq-incidents.log",
              "随时查看：python3 /opt/dsh/usage_log.py --days 7",
              "          python3 /opt/dsh/ops_report.py --hours 24",
              "          python3 /opt/dsh/qqguard.py --status"]
    return chr(10).join(parts)


def subject():
    bits = []
    try:
        import budget
        st = budget.state()
        if st.get("used"):
            bits.append("输入输出 %s token" % "{:,}".format(int(st["used"])))
    except Exception:
        pass
    try:
        today = time.strftime("%F")
        n = 0
        for line in open(INCIDENTS, encoding="utf-8"):
            if line.startswith(today) and "断线" in line:
                n += 1
        bits.append("掉线 %d 次" % n)
    except Exception:
        pass
    head = "【鲸鲸日报】" + time.strftime("%m-%d")
    return head + (" · " + " · ".join(bits) if bits else "")


def main():
    dry = "--dry-run" in sys.argv
    body = build()
    subj = subject()
    if dry:
        print(subj)
        print()
        print(body)
        return 0
    try:
        import mailer
    except Exception as e:
        print("mailer 不可用: %s" % e)
        return 1
    if not mailer.configured():
        print("邮件未配置，跳过日报")
        return 0
    ok, why = mailer.send(subj, body)
    print(("日报已发送: " if ok else "日报发送失败: ") + why)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
