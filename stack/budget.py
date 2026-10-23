#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预算硬上限：超了就不再花钱，而不是事后看着数字心疼。

和 usage_log.py 的分工：
    usage_log.py  记账（事后，回答"花了多少"）
    budget.py     闸门（事前，回答"还能不能花"）

两档：
    软线（默认 80%） → 停掉「非必要」调用：看图判断、记忆蒸馏
    硬线（100%）    → 主对话也停，返回明确错误，她能如实告诉主人

为什么分级：主对话停了，她就哑了；但"给表情库打标签""整理记忆"这类
批处理停下来完全无害，第二天额度恢复再跑就行。所以先砍后者。

计数来源就是 usage.jsonl —— 同一份数据，不重复统计，跨天按日期分桶自动归零。
"""
import json
import os
import time

BASE = "/opt/dsh"
USAGE = os.path.join(BASE, "memory/usage.jsonl")
CFG = os.path.join(BASE, "budget.json")

# 默认关闭。原因：这是个刹车，装完不该让人莫名其妙被刹车别住——
# 别人的日用量、模型价格都不一样，默认值猜错了就是"机器人突然不说话了"。
# 要用就填好 daily_tokens 再把 enabled 改成 true。
DEFAULTS = {
    "_note": "默认关闭。要用：设 enabled=true 并按自己的量填 daily_tokens"
             "（每天输入+输出 token 总和的上限），soft_ratio 是软线比例。",
    "enabled": False,
    "daily_tokens": 3000000,
    "soft_ratio": 0.8,
}

# 哪些服务属于「非必要」：超软线就先停它们
NONESSENTIAL = ("vision", "distill", "other")

_cache = {"key": None, "used": 0}


def _cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in d.items() if k in DEFAULTS})
    return out


def ensure_cfg():
    if os.path.exists(CFG):
        return
    try:
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def used_today(force=False):
    """今天已用 token（输入+输出）。按文件 mtime 缓存 5 秒，避免每次请求都扫文件。"""
    today = time.strftime("%F")
    try:
        mt = os.path.getmtime(USAGE)
    except Exception:
        return 0
    key = (today, mt)
    if not force and _cache["key"] == key:
        return _cache["used"]
    total = 0
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
                if r.get("d") != today:
                    continue
                total += int(r.get("in") or 0) + int(r.get("out") or 0)
    except Exception:
        return 0
    _cache["key"] = key
    _cache["used"] = total
    return total


def state():
    c = _cfg()
    used = used_today()
    limit = int(c.get("daily_tokens") or 0)
    ratio = (used / float(limit)) if limit > 0 else 0.0
    if not c.get("enabled"):
        level = "off"
    elif limit <= 0:
        level = "off"
    elif ratio >= 1.0:
        level = "hard"
    elif ratio >= float(c.get("soft_ratio") or 0.8):
        level = "soft"
    else:
        level = "ok"
    return {"level": level, "used": used, "limit": limit, "ratio": ratio}


def allow(service):
    """返回 (能不能调, 拒绝原因)。任何异常都放行——闸门坏了不能把人锁在外面。"""
    try:
        st = state()
    except Exception:
        return True, ""
    if st["level"] in ("ok", "off"):
        return True, ""
    used, limit = st["used"], st["limit"]
    if st["level"] == "hard":
        pct = int(100 * st["ratio"])
        return False, ("今日 token 预算已用尽（%s / %s，%d%%）。这不是故障，是预算熔断。"
                       "明天零点自动恢复；要立刻恢复就调高 /opt/dsh/budget.json 的 daily_tokens。"
                       % ("{:,}".format(used), "{:,}".format(limit), pct))
    # soft：只停非必要调用
    svc = (service or "other").lower()
    if svc in NONESSENTIAL:
        return False, ("今日 token 已过软线（%s / %s，%d%%），暂停「%s」这类非必要调用。"
                       "主对话不受影响，明天恢复。"
                       % ("{:,}".format(used), "{:,}".format(limit),
                          int(100 * st["ratio"]), svc))
    return True, ""


def render():
    st = state()
    c = _cfg()
    lines = []
    if st["level"] == "off":
        lines.append("预算闸门：未启用（budget.json 里 enabled=false 或 daily_tokens<=0）")
        lines.append("今日已用：%s token" % "{:,}".format(st["used"]))
        return "\n".join(lines)
    lines.append("今日已用：%s / %s token（%d%%）"
                 % ("{:,}".format(st["used"]), "{:,}".format(st["limit"]),
                    int(100 * st["ratio"])))
    lines.append("软线 %d%%｜硬线 100%%｜当前状态：%s"
                 % (int(100 * float(c.get("soft_ratio") or 0.8)), st["level"]))
    left = st["limit"] - st["used"]
    lines.append("剩余：%s token" % "{:,}".format(max(0, left)))
    return "\n".join(lines)


if __name__ == "__main__":
    ensure_cfg()
    print(render())
