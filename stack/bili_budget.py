#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站浏览的 token 预算：给她多少额度，她就刷多少。

为什么单独记一份，不复用 usage.jsonl：
    usage.jsonl 记的是"每一次模型调用花了多少"，但**无法归因**到"这次是因为
    刷 B 站"。所以这里按"工具返回内容的体量"估算：中文约 2 字 = 1 token。
    不精确，但自洽、可审计，而且计量口径就在她花额度的地方。

用法：
    bili_budget.check(est_tokens)  → (能不能, 原因, 剩余)
    bili_budget.record(tokens)     → 记账
    bili_budget.status()           → 给工具查询用
"""
import io
import json
import os
import time

BASE = "/opt/dsh"
CONF = os.path.join(BASE, "bili_budget.conf.json")
STATE = os.path.join(BASE, "memory/bili-budget.json")

DEFAULTS = {
    "_note": "enabled=false 表示不限制（主人自用）。daily_tokens 是每天浏览额度的 token 数。",
    "enabled": False,
    "daily_tokens": 150000,
    "token_per_char": 0.5,      # 中文约 2 字 = 1 token
}


def conf():
    try:
        d = json.load(io.open(CONF, encoding="utf-8"))
        if isinstance(d, dict):
            out = dict(DEFAULTS)
            out.update(d)
            return out
    except Exception:
        pass
    return dict(DEFAULTS)


def ensure_conf():
    if not os.path.exists(CONF):
        io.open(CONF, "w", encoding="utf-8").write(
            json.dumps(DEFAULTS, ensure_ascii=False, indent=1))


def load():
    try:
        d = json.load(io.open(STATE, encoding="utf-8"))
        if isinstance(d, dict):
            if d.get("day") != time.strftime("%F"):
                return {"day": time.strftime("%F"), "used": 0, "calls": 0}
            return d
    except Exception:
        pass
    return {"day": time.strftime("%F"), "used": 0, "calls": 0}


def save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    io.open(tmp, "w", encoding="utf-8").write(json.dumps(st, ensure_ascii=False))
    os.replace(tmp, STATE)


def estimate(obj):
    """按返回内容的字符数估 token。"""
    try:
        chars = len(json.dumps(obj, ensure_ascii=False))
    except Exception:
        chars = 0
    return int(chars * float(conf().get("token_per_char", 0.5)))


def status():
    c, st = conf(), load()
    limit = int(c.get("daily_tokens") or 0)
    used = int(st.get("used") or 0)
    return {
        "enabled": bool(c.get("enabled")),
        "daily_tokens": limit,
        "used_today": used,
        "left_today": max(0, limit - used) if limit else None,
        "calls_today": st.get("calls", 0),
        "note": "" if c.get("enabled") else "现在不限额（主人自用模式）",
    }


def check(est=0):
    c = conf()
    if not c.get("enabled"):
        return True, "不限额", None
    limit = int(c.get("daily_tokens") or 0)
    st = load()
    used = int(st.get("used") or 0)
    left = max(0, limit - used)
    if left <= 0:
        return False, "今天的浏览额度用完了（%d/%d token），明天再来" % (used, limit), 0
    if est and est > left:
        return False, "这一下要 %d token，但只剩 %d 了" % (est, left), left
    return True, "还剩 %d token" % left, left


def record(tokens):
    st = load()
    st["used"] = int(st.get("used") or 0) + int(tokens or 0)
    st["calls"] = int(st.get("calls") or 0) + 1
    save(st)
    return st

