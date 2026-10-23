#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一记账：每次模型调用落一行 JSONL，并能汇总成日报。

为什么三处都要接：
    主对话走 llm_shim.py，看图走 seer.py，记忆蒸馏走 memory_distill.py ——
    三个入口都各自直连上游。只在一处记就会漏掉另外两处，所以都调 record()。

为什么不用现成的 llm_shim.log：
    那里已有 "USAGE prompt=… completion=…" 行，但那是给人看的文本，没法按天、
    按服务、按模型聚合。JSONL 追加写、断了不丢、能直接 grep。

价格：
    memory/usage_prices.json 里按模型填「¥ / 百万 token」。没填就只统计 token，
    cost 记 None —— 宁可显示"未配置"，也不要编一个假金额。
"""
import json
import os
import time

BASE = "/opt/dsh"
USAGE = os.path.join(BASE, "memory/usage.jsonl")
PRICES = os.path.join(BASE, "memory/usage_prices.json")

DEFAULT_PRICES = {
    "_note": "按模型填 ¥ / 百万 token。不知道就留 0，报告会显示「未配置价格」。",
    "models": {
        "deepseek-v4.1-flash-1017": {"in": 0.0, "out": 0.0, "cached": 0.0},
    },
}


def _prices():
    try:
        with open(PRICES, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("models"), dict):
            return d["models"]
    except Exception:
        pass
    return DEFAULT_PRICES["models"]


def ensure_prices():
    """首次运行时写一份价格模板，方便直接改。"""
    if os.path.exists(PRICES):
        return
    try:
        os.makedirs(os.path.dirname(PRICES), exist_ok=True)
        with open(PRICES, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_PRICES, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def cost_of(model, inp, out, cached):
    """算一次调用的估算费用；价格没配全就返回 None（不编数字）。"""
    p = _prices().get(model or "")
    if not isinstance(p, dict):
        return None
    try:
        pin = float(p.get("in") or 0.0)
        pout = float(p.get("out") or 0.0)
        pcache = float(p.get("cached") or p.get("in") or 0.0)
    except Exception:
        return None
    if pin == 0.0 and pout == 0.0:
        return None
    inp = int(inp or 0)
    out = int(out or 0)
    cached = int(cached or 0)
    miss = max(0, inp - cached)
    return (miss * pin + cached * pcache + out * pout) / 1e6


def record(service, model=None, inp=None, out=None, cached=None, meta=None):
    """记一次调用。任何异常都吞掉——记账绝不能影响主流程。

    service: agent / vision / distill / other
    """
    try:
        ensure_prices()
        ts = time.time()
        row = {
            "ts": int(ts),
            "d": time.strftime("%F", time.localtime(ts)),
            "svc": service or "other",
            "model": model or "unknown",
            "in": int(inp) if isinstance(inp, (int, float)) else None,
            "out": int(out) if isinstance(out, (int, float)) else None,
            "cached": int(cached) if isinstance(cached, (int, float)) else None,
        }
        row["cost"] = cost_of(row["model"], row["in"], row["out"], row["cached"])
        if meta:
            row["meta"] = meta
        line = json.dumps(row, ensure_ascii=False) + chr(10)
        os.makedirs(os.path.dirname(USAGE), exist_ok=True)
        fd = os.open(USAGE, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def load(days=7):
    """读最近 N 天（按日期字符串比较，够用）。"""
    if not os.path.exists(USAGE):
        return []
    since = time.strftime("%F", time.localtime(time.time() - days * 86400))
    out = []
    try:
        with open(USAGE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if str(r.get("d") or "") >= since:
                    out.append(r)
    except Exception:
        pass
    return out


def summarize(days=7):
    rows = load(days)
    acc = {"calls": 0, "in": 0, "out": 0, "cached": 0, "cost": 0.0,
           "cost_known": 0, "cost_unknown": 0,
           "by_day": {}, "by_svc": {}, "by_model": {}}

    def bump(d, key, r):
        c = d.setdefault(key, {"calls": 0, "in": 0, "out": 0, "cached": 0,
                               "cost": 0.0, "cost_unknown": 0})
        c["calls"] += 1
        c["in"] += int(r.get("in") or 0)
        c["out"] += int(r.get("out") or 0)
        c["cached"] += int(r.get("cached") or 0)
        if r.get("cost") is None:
            c["cost_unknown"] += 1
        else:
            c["cost"] += float(r["cost"])

    for r in rows:
        acc["calls"] += 1
        acc["in"] += int(r.get("in") or 0)
        acc["out"] += int(r.get("out") or 0)
        acc["cached"] += int(r.get("cached") or 0)
        if r.get("cost") is None:
            acc["cost_unknown"] += 1
        else:
            acc["cost"] += float(r["cost"])
            acc["cost_known"] += 1
        bump(acc["by_day"], str(r.get("d") or "?"), r)
        bump(acc["by_svc"], str(r.get("svc") or "other"), r)
        bump(acc["by_model"], str(r.get("model") or "unknown"), r)
    return acc


def _fmt(n):
    return "{:,}".format(int(n))


def render(days=7):
    s = summarize(days)
    if not s["calls"]:
        return "近 %d 天没有调用记录。" % days
    lines = ["近 %d 天：%s 次调用｜输入 %s｜输出 %s｜缓存命中 %s"
             % (days, _fmt(s["calls"]), _fmt(s["in"]), _fmt(s["out"]),
                _fmt(s["cached"]))]
    if s["cost_unknown"] == s["calls"]:
        lines.append("费用：未配置价格（编辑 memory/usage_prices.json 后重算）")
    else:
        tag = "" if not s["cost_unknown"] else "（其中 %d 次没价格）" % s["cost_unknown"]
        lines.append("费用：约 ¥%.4f%s" % (s["cost"], tag))
    hit = (100.0 * s["cached"] / s["in"]) if s["in"] else 0.0
    lines.append("输入缓存命中率：%.1f%%" % hit)

    def table(title, d, keyname):
        if not d:
            return
        lines.append("")
        lines.append(title)
        for k in sorted(d, key=lambda x: -d[x]["calls"]):
            c = d[k]
            lines.append("  %-28s %6s 次  输入 %10s  输出 %8s  费用 %s"
                         % (k[:28], _fmt(c["calls"]), _fmt(c["in"]), _fmt(c["out"]),
                            "未配置" if c["cost_unknown"] == c["calls"]
                            else "¥%.4f" % c["cost"]))

    table("按天：", s["by_day"], "d")
    table("按服务：", s["by_svc"], "svc")
    table("按模型：", s["by_model"], "model")
    return chr(10).join(lines)


if __name__ == "__main__":
    import sys
    days = 7
    for i, a in enumerate(sys.argv):
        if a == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])
    print(render(days))
