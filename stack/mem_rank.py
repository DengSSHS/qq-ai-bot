# -*- coding: utf-8 -*-
"""日记检索排序：移植仿生记忆模块 v1.1.4 的遗忘曲线（humanRetention）。
不删除任何日记，只影响检索排序：重要、常被想起的往前排；久未提及的往后沉。"""
import json
import math
import os
import re
import time
from datetime import datetime

DIARY = os.path.join(os.environ.get("MEMORY_DIR", "/opt/dsh/memory"), "diary")
INDEX = os.path.join(DIARY, "index.json")

# 参数取自 retention-model.js 的 episodic 档
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
    """items: [(head, body)] -> 按遗忘曲线加权排序，同时记一次复述（hits+1）。"""
    store = _load()
    now = time.time()
    scored = []
    for head, body in items:
        e = _meta(head, store)
        age = (now - float(e.get("created") or _created(head))) / 86400.0
        s = _retention(float(e.get("imp", 0.6)), int(e.get("hits", 0)), age)
        scored.append((s, head, body))
        e["hits"] = int(e.get("hits", 0)) + 1
        e["last"] = now
        store[head] = e
    _save(store)
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(h, b) for _, h, b in scored]


def stats():
    return {"entries": len(_load()), "index": INDEX}
