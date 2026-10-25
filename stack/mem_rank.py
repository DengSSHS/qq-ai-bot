# -*- coding: utf-8 -*-
"""日记检索排序：遗忘曲线排序（通用工程近似：指数衰减 + 长尾地板，
借鉴记忆研究的公开结论；口径与 NOTICE.md 一致，不是某个具体项目的代码移植）。

不删除任何日记，只影响检索排序：重要、常被想起的往前排；久未提及的往后沉。

两个函数职责分开：
    rank(items)       纯函数，只排序，不写盘（高频调用零 I/O）
    reinforce(heads)  只对真正注入上下文的条目记一次复述（hits+1）
为什么这么拆：旧版 rank() 会给所有参与排序的候选 hits+1，于是“常被检索但从未
被真正用上”的记忆也在被强化，hits 无限膨胀又把半衰期拉长——那是副作用，不是设计。
"""
import json
import math
import os
import re
import time
from datetime import datetime

DIARY = os.path.join(os.environ.get("MEMORY_DIR", "/opt/dsh/memory"), "diary")
INDEX = os.path.join(DIARY, "index.json")

# 参数是通用工程取值（半衰期 60 天 / 地板 0.20 / 长尾指数 0.66），不是某项目参数
BASE = {"halfLifeDays": 60.0, "floor": 0.20, "beta": 0.66}
RECENCY_WEIGHT = 0.35


def _load():
    try:
        with open(INDEX, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d):
    try:
        os.makedirs(DIARY, exist_ok=True)
        tmp = INDEX + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, INDEX)
    except Exception:
        pass


def _created(head):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})", head or "")
    if not m:
        return time.time()
    try:
        return datetime(*[int(x) for x in m.groups()]).timestamp()
    except Exception:
        return time.time()


def _meta(head, store):
    e = store.get(head)
    if not isinstance(e, dict):
        e = {"imp": 0.6, "hits": 0, "last": _created(head), "created": _created(head)}
        store[head] = e
    return e


def _retention(imp, hits, age_days):
    half = BASE["halfLifeDays"] * (0.72 + imp * 0.78) * (1.0 + min(2.4, math.log1p(hits) * 0.22))
    floor = BASE["floor"]
    exp_ = -0.6931471805599453 * math.pow(max(age_days, 0.0) / half, BASE["beta"])
    r = floor + (1 - floor) * math.exp(max(-745.0, exp_))
    return max(floor, min(1.0, 1 - RECENCY_WEIGHT * (1 - r)))


def note(head, importance=None):
    store = _load()
    e = _meta(head, store)
    if importance is not None:
        try:
            e["imp"] = max(0.0, min(1.0, float(importance)))
        except Exception:
            pass
    _save(store)


def rank(items):
    """items: [(head, body)] -> 按遗忘曲线加权排序。

    纯函数：不修改 hits、不写盘。想记“被想起一次”请另外调 reinforce()。
    """
    store = _load()
    now = time.time()
    scored = []
    for head, body in items:
        e = _meta(head, store)
        age = (now - float(e.get("created") or _created(head))) / 86400.0
        s = _retention(float(e.get("imp", 0.6)), int(e.get("hits", 0)), age)
        scored.append((s, head, body))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(h, b) for _, h, b in scored]


def reinforce(heads):
    """只对真正被注入上下文的条目记一次复述（hits+1，落一次盘）。

    为什么不能放在 rank() 里：那样所有候选都被强化，包括从未真正被用上的，
    hits 无限膨胀 → 半衰期被 log1p(hits) 拉长 → 越常进候选池越忘不掉。
    """
    heads = [h for h in heads if h]
    if not heads:
        return 0
    store = _load()
    now = time.time()
    for head in heads:
        e = _meta(head, store)
        e["hits"] = int(e.get("hits", 0)) + 1
        e["last"] = now
        store[head] = e
    _save(store)
    return len(heads)


def stats():
    return {"entries": len(_load()), "index": INDEX}
