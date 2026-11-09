#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token 用量监视：只提醒，绝不拦截（2026-11-09）

背景：主人把 B 站浏览改成无限制了（见 bili_window.conf.json），
于是唯一的刹车只剩下「模型花了多少钱」。但 `budget.py` 一旦 enabled=true 就会
**真的拦**（软线停非必要调用、硬线全停）——那和他要的"不拦她"冲突。

所以这里做旁路：读 usage 日志算当天用量，越过阈值就发邮件说一声，什么都不拦。

阈值依据（2026-10-24 ~ 11-09 实测）：
    正常日  0.5–1.7M      开发重度日  15–17.6M     当天 5.0M
    所以 8M 才开始提醒（正常日根本碰不到），15M 再响一次。

配置：/opt/dsh/budget_watch.conf.json
    {"enabled": true, "thresholds": [8000000, 15000000]}
状态：/opt/dsh/memory/budget-watch.json（记当天已提醒过哪几档）

用法：
    python3 budget_watch.py            # 检查一次（定时器每 30 分钟调）
    python3 budget_watch.py --force    # 忽略状态，立刻检查并可发信
"""
import io
import json
import os
import sys
import time

sys.path.insert(0, "/opt/dsh")
import budget                                          # noqa: E402
import mailer                                          # noqa: E402

CONF = "/opt/dsh/budget_watch.conf.json"
STATE = "/opt/dsh/memory/budget-watch.json"
LOG = "/opt/dsh/memory/budget-watch.log"
DEFAULT = {"enabled": True, "thresholds": [8000000, 15000000]}


def log(msg):
    try:
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%F %T"), msg))
    except Exception:
        pass


def conf():
    try:
        c = json.load(io.open(CONF, encoding="utf-8"))
    except Exception:
        c = dict(DEFAULT)
        try:
            io.open(CONF, "w", encoding="utf-8").write(json.dumps(DEFAULT, ensure_ascii=False, indent=1))
        except Exception:
            pass
    out = dict(DEFAULT)
    out.update(c or {})
    return out


def load_state():
    try:
        st = json.load(io.open(STATE, encoding="utf-8"))
    except Exception:
        st = {}
    today = time.strftime("%F")
    if st.get("date") != today:
        st = {"date": today, "notified": []}
    return st


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        io.open(tmp, "w", encoding="utf-8").write(json.dumps(st, ensure_ascii=False))
        os.replace(tmp, STATE)
    except Exception as e:
        log("save_state failed: %s" % e)


def main():
    c = conf()
    if not c.get("enabled", True):
        print("监视已关闭")
        return 0
    used = budget.used_today(force=True)
    st = load_state()
    fired = [t for t in c.get("thresholds") or [] if used >= int(t)]
    new = [t for t in fired if str(t) not in [str(x) for x in st.get("notified", [])]]
    print("今日 %.2fM / 阈值 %s" % (used / 1e6, [round(int(t) / 1e6, 1) for t in c.get("thresholds") or []]))

    for t in new:
        body = ("今天到现在用了 %.2fM token（提醒阈值 %.1fM）。\n\n"
                "这只是提醒，**没有拦她**——B 站浏览已经是无限制模式，\n"
                "所以用量会长得快。如果这个速度不是你想要的，可以：\n"
                "  · 把 /opt/dsh/bili_window.conf.json 的 allowed_windows 改回 [[\"18:00\",\"08:00\"]]\n"
                "  · 或者把 daily_auto_opens 从 999 调小\n"
                "  · 想硬拦就把 /opt/dsh/budget.json 的 enabled 改成 true（会真的停服务）\n\n"
                "今天各时段用量：/opt/dsh/memory/usage.jsonl\n"
                % (used / 1e6, int(t) / 1e6))
        ok, why = mailer.send("【用量提醒】今天已 %.1fM token（未拦截）" % (used / 1e6), body)
        log("threshold %s fired: used=%.2fM mail=%s %s" % (t, used / 1e6, ok, why))
        print("  已提醒 %.1fM：%s" % (int(t) / 1e6, why))
        if ok:
            st.setdefault("notified", []).append(str(t))
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
