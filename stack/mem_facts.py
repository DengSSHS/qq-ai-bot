#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事实层记忆（基于 mem0）：抽事实 + 语义召回 + 被想起就加鲜（2026-11-09）

用户看了「妹居物语」的记忆设计后说：不用做图/可视化，**主要逻辑是那个就行**。
所以这里只实现逻辑，不碰任何图：

  写入  用中文模板让模型抽「值得长期记住的事实」，带重要性
  召回  语义检索 top-20 → 按 相关/新鲜/重要 重新打分 → 取前 n
  加鲜  每条被召回一次，hits+1、last_hit 刷新；复现效应让它下次更容易被想起
  淡忘  新鲜度按 30 天半衰期衰减（不删除，只是越来越难被想起）

取舍：
  · 用 mem0 做事实抽取和向量存储（成熟、我们网关的 embedding 就够用）
  · 不用它的 graph（要 Neo4j，这台 1.9G 内存扛不住）
  · 把「加鲜」记账放在我们自己的 SQLite 里，不动 qdrant payload —— 出问题好回滚
  · 全部依赖在 /opt/mem0lib，删掉这个目录就等于没装过

用法（自检）：
    python3 mem_facts.py --add "主人周三满课" --importance 0.7
    python3 mem_facts.py --recall "他这周上学累不累"
"""
import argparse
import io
import json
import math
import os
import sqlite3
import sys
import time

MEM0LIB = "/opt/mem0lib"
BASE = "/opt/dsh"
SIDE_DB = os.path.join(BASE, "memory", "facts-side.db")     # hits/last_hit 记账
QWEN_ENV = os.path.join(BASE, "qwen.env")
MODEL_LLM = "deepseek-v4.1-flash-1017"
MODEL_EMB = "qwen3.7-text-embedding-flash"
HALF_LIFE_DAYS = 30.0

ZH_PROMPT = """从对话里抽取**值得长期记住的事实**，用中文、第三人称、每条一句、具体。
只抽：主人的偏好 / 习惯 / 处境 / 计划 / 情绪来源 / 重要事件 / 明确托付过的事。
不抽：寒暄、问句、AI 自己说的话、一次性的情绪波动。
没有值得记的就返回 {"facts": []}。

对话：
{dialogue}

JSON 返回：{"facts": ["...", "..."]}"""

_inst = None


def _api_key():
    for ln in io.open(QWEN_ENV, encoding="utf-8"):
        if ln.startswith("QWEN_API_KEY="):
            return ln.split("=", 1)[1].strip()
    raise RuntimeError("qwen.env 里没有 QWEN_API_KEY")


def _side():
    os.makedirs(os.path.dirname(SIDE_DB), exist_ok=True)
    con = sqlite3.connect(SIDE_DB, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS hit(
        id TEXT PRIMARY KEY, hits INTEGER DEFAULT 0,
        last_hit REAL DEFAULT 0, importance REAL DEFAULT 0.5, added REAL)""")
    return con


def memory():
    """惰性初始化 mem0（首次约 0.5 秒）。不可用时抛异常，调用方自己兜。"""
    global _inst
    if _inst is not None:
        return _inst
    if not os.path.isdir(MEM0LIB):
        raise RuntimeError("没装 mem0（/opt/mem0lib 不存在）")
    if MEM0LIB not in sys.path:
        sys.path.insert(0, MEM0LIB)
    os.environ["MEM0_TELEMETRY"] = "False"          # 别往 PostHog 发东西
    key = _api_key()
    os.environ["OPENAI_API_KEY"] = key
    os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:8890/v1"
    from mem0 import Memory

    cfg = {
        "llm": {"provider": "openai", "config": {
            "model": MODEL_LLM, "openai_base_url": "http://127.0.0.1:8890/v1",
            "api_key": key, "temperature": 0.1, "max_tokens": 800}},
        "embedder": {"provider": "openai", "config": {
            "model": MODEL_EMB, "openai_base_url": "http://127.0.0.1:8890/v1",
            "api_key": key, "embedding_dims": 1024}},
        "vector_store": {"provider": "qdrant", "config": {
            "path": os.path.join(BASE, "memory/mem0-facts"),
            "collection_name": "facts", "embedding_model_dims": 1024, "on_disk": True}},
        "history_db_path": os.path.join(BASE, "memory/mem0-facts-history.db"),
        "version": "v1.1",
    }
    _inst = Memory.from_config(cfg)
    return _inst


def remember(text, importance=0.5, kind="fact", who="owner"):
    """抽事实并写入。返回抽出来的条目（可能 0 条——说明不值得记）。"""
    text = (text or "").strip()
    if not text:
        return []
    m = memory()
    res = m.add(text, user_id=who, prompt=ZH_PROMPT,
                metadata={"kind": kind, "importance": float(importance)})
    out = []
    con = _side()
    for it in (res or {}).get("results") or []:
        mid = str(it.get("id") or "")
        if not mid:
            continue
        out.append(dict(id=mid, memory=it.get("memory")))
        con.execute("INSERT OR REPLACE INTO hit(id,hits,last_hit,importance,added) "
                    "VALUES(?,?,?,?,?)",
                    (mid, 0, 0.0, float(importance), time.time()))
    con.commit()
    con.close()
    return out


def _freshness(added):
    if not added:
        return 0.5
    days = max(0.0, (time.time() - float(added)) / 86400.0)
    return 0.5 ** (days / HALF_LIFE_DAYS)       # 30 天半衰期


def recall(query, n=5, who="owner", pool=20):
    """语义召回 → 按 相关/新鲜/重要/被想起次数 重排 → 取前 n。返回的条目 hits+1。"""
    query = (query or "").strip()
    if not query:
        return []
    m = memory()
    hits = m.search(query, filters={"user_id": who}, top_k=pool, threshold=0.05)
    rows = hits.get("results") if isinstance(hits, dict) else hits
    rows = rows or []
    if not rows:
        return []

    con = _side()
    side = {}
    for r in con.execute("SELECT id,hits,last_hit,importance,added FROM hit"):
        side[r[0]] = dict(hits=r[1], last_hit=r[2], importance=r[3], added=r[4])

    scored = []
    for r in rows:
        mid = str(r.get("id") or "")
        meta = r.get("metadata") or {}
        s = side.get(mid) or {}
        imp = float(s.get("importance") or meta.get("importance") or 0.5)
        added = s.get("added") or 0
        if not added and r.get("created_at"):
            try:
                added = time.mktime(time.strptime(str(r["created_at"])[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                added = 0
        sem = float(r.get("score") or 0)
        fresh = _freshness(added)
        seen = int(s.get("hits") or 0)
        # 被想起过很多次 → 略加权（复现效应），但用 log 压住，别让一条旧事永远霸榜
        bonus = min(0.12, 0.05 * math.log1p(seen))
        final = 0.55 * sem + 0.22 * imp + 0.18 * fresh + bonus
        scored.append(dict(id=mid, memory=r.get("memory"), score=round(final, 3),
                           sem=round(sem, 3), importance=round(imp, 2),
                           freshness=round(fresh, 2), hits=seen))

    scored.sort(key=lambda x: -x["score"])
    top = scored[:max(1, int(n))]

    # 加鲜：这次被想起来的，记账
    now = time.time()
    for it in top:
        if it["id"]:
            con.execute("INSERT INTO hit(id,hits,last_hit,importance,added) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET hits=hits+1, last_hit=?",
                        (it["id"], 1, now, it["importance"], now, now))
    con.commit()
    con.close()
    return top


def stats():
    con = _side()
    n, hits = con.execute("SELECT COUNT(*), COALESCE(SUM(hits),0) FROM hit").fetchone()
    con.close()
    try:
        m = memory()
        total = len(m.get_all(filters={"user_id": "owner"}).get("results") or [])
    except Exception:
        total = -1
    return dict(records=n, total_hits=hits, mem0_total=total,
                size_kb=round(os.path.getsize(SIDE_DB) / 1024, 1) if os.path.exists(SIDE_DB) else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--add")
    ap.add_argument("--recall")
    ap.add_argument("--importance", type=float, default=0.5)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.add:
        print(json.dumps(remember(a.add, a.importance), ensure_ascii=False, indent=1))
    elif a.recall:
        print(json.dumps(recall(a.recall, a.n), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(stats(), ensure_ascii=False))
