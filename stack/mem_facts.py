#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事实层记忆：抽事实 + 标实体 + 语义召回 + 沿实体扩散 + 被想起就加鲜

用户看了「妹居物语」的记忆设计后指出两件事：
  1. 主要逻辑要对，不做可视化（不输出图片）
  2. **扩散很重要** —— 只靠语义召回，她记得的东西很少
     （视频原话：「你只说了面试和下雨，我却会想起火锅，还有你最讨厌的香菜」）

所以这一版加了图扩散，但用最朴素的方式：**实体 → 事实** 的二分图。

写入：一次便宜调用抽出 [{fact, entities}]，事实存进 mem0（infer=False，不再多花一次 LLM），
      实体存进我们自己的 SQLite（fact / edge 两张表，可回看、可删）。
召回：语义 top-20 当种子 → 取种子里的实体 → 找和这些实体相连的其他事实（1 跳）
      → 合并后按 相关/重要/新鲜/复现 排序 → 取前 n
      → 被返回的条目 hits+1（复现效应）

用法：
    python3 mem_facts.py --add "..." --importance 0.8
    python3 mem_facts.py --recall "他今天面试还下雨"
    python3 mem_facts.py --backfill        # 把已有日记/笔记抽成事实
    python3 mem_facts.py                   # 看统计
"""
import argparse
import io
import json
import math
import os
import sqlite3
import sys
import time
import urllib.request

MEM0LIB = "/opt/mem0lib"
BASE = "/opt/dsh"
SIDE_DB = os.path.join(BASE, "memory", "facts-side.db")
QWEN_ENV = os.path.join(BASE, "qwen.env")
MODEL_LLM = "deepseek-v4.1-flash-1017"
MODEL_EMB = "qwen3.7-text-embedding-flash"
HALF_LIFE_DAYS = 30.0
HOP_DECAY = 0.72               # 每跳一次打个折（走两步的事就没那么相关了）

EXTRACT_PROMPT = """从下面的对话里抽取**值得长期记住的事实**，每条带**实体**。

要求：
- 事实：中文、第三人称、一句、具体（谁/什么时候/发生了什么/为什么在意）
- 实体：这条事实涉及的人、物、地点、事件、概念（2~6 个短词）。
  实体是用来"串"记忆的，所以要具体：写「火锅」「香菜」「面试」「下雨」，不要写「食物」「天气」这种大类
- 只抽：偏好 / 习惯 / 处境 / 计划 / 情绪来源 / 重要事件 / 托付过的事
- 不抽：寒暄、问句、AI 自己说的话、一次性的情绪波动
- 没有值得记的，返回 {"facts": []}

对话：
{dialogue}

只返回 JSON：{"facts": [{"text": "...", "entities": ["...", "..."]}]}"""

SEEN = os.path.join(BASE, "memory", "facts-ingested.json")     # 已入库的日记块指纹
_inst = None


def _api_key():
    for ln in io.open(QWEN_ENV, encoding="utf-8"):
        if ln.startswith("QWEN_API_KEY="):
            return ln.split("=", 1)[1].strip()
    raise RuntimeError("qwen.env 里没有 QWEN_API_KEY")


def _post(path, payload, timeout=120):
    key = _api_key()
    req = urllib.request.Request("http://127.0.0.1:8890/v1/" + path,
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _side():
    os.makedirs(os.path.dirname(SIDE_DB), exist_ok=True)
    con = sqlite3.connect(SIDE_DB, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS fact(
        id TEXT PRIMARY KEY, text TEXT, entities TEXT, importance REAL,
        added REAL, hits INTEGER DEFAULT 0, last_hit REAL DEFAULT 0)""")
    con.execute("CREATE TABLE IF NOT EXISTS edge(entity TEXT, fact_id TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_edge_entity ON edge(entity)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_edge_fact ON edge(fact_id)")
    return con


def memory():
    global _inst
    if _inst is not None:
        return _inst
    if not os.path.isdir(MEM0LIB):
        raise RuntimeError("没装 mem0（/opt/mem0lib 不存在）")
    if MEM0LIB not in sys.path:
        sys.path.insert(0, MEM0LIB)
    os.environ["MEM0_TELEMETRY"] = "False"
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


# ---------------------------------------------------------------- 写入
def extract(dialogue):
    """一次便宜调用：抽事实 + 标实体。"""
    d = _post("chat/completions", {
        "model": MODEL_LLM, "temperature": 0.1, "max_tokens": 900,
        "messages": [{"role": "user",
                      "content": EXTRACT_PROMPT.replace("{dialogue}", dialogue)}]})
    text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return []
    try:
        data = json.loads(text[i:j + 1])
    except Exception:
        return []
    out = []
    for it in data.get("facts") or []:
        if isinstance(it, str):
            out.append((it.strip(), []))
        elif isinstance(it, dict) and it.get("text"):
            ents = [str(x).strip() for x in (it.get("entities") or []) if str(x).strip()]
            out.append((str(it["text"]).strip(), ents[:8]))
    return [x for x in out if x[0]]


def remember(text, importance=0.5, kind="fact", who="owner"):
    text = (text or "").strip()
    if not text:
        return []
    facts = extract(text)
    if not facts:
        return []
    m = memory()
    con = _side()
    saved = []
    for fact, ents in facts:
        try:
            r = m.add(fact, user_id=who, infer=False,
                      metadata={"kind": kind, "importance": float(importance),
                                "entities": ents})
        except Exception:
            continue
        res = (r or {}).get("results") or []
        mid = str((res[0].get("id") if res else "") or "")
        if not mid:
            continue
        con.execute("INSERT OR REPLACE INTO fact(id,text,entities,importance,added,hits,last_hit) "
                    "VALUES(?,?,?,?,?,0,0)",
                    (mid, fact, json.dumps(ents, ensure_ascii=False), float(importance), time.time()))
        for e in ents:
            con.execute("INSERT INTO edge(entity,fact_id) VALUES(?,?)", (e, mid))
        saved.append(dict(id=mid, memory=fact, entities=ents))
    con.commit()
    con.close()
    return saved


# ---------------------------------------------------------------- 召回
def _fresh(added):
    if not added:
        return 0.5
    return 0.5 ** (max(0.0, (time.time() - float(added)) / 86400.0) / HALF_LIFE_DAYS)


def _score(sem, imp, added, hits, hop):
    bonus = min(0.12, 0.05 * math.log1p(int(hits or 0)))
    base = 0.55 * sem + 0.22 * imp + 0.18 * _fresh(added) + bonus
    return round(base * (HOP_DECAY if hop else 1.0), 3)


def recall(query, n=5, who="owner", pool=20, hops=1, entities_hint=6):
    """语义种子 + 沿实体扩散。返回的条目 hits+1。"""
    query = (query or "").strip()
    if not query:
        return []
    m = memory()
    con = _side()

    # ① 语义种子
    try:
        hits = m.search(query, filters={"user_id": who}, top_k=pool, threshold=0.35)
        rows = (hits.get("results") if isinstance(hits, dict) else hits) or []
    except Exception:
        rows = []

    seeds = {}
    for r in rows:
        mid = str(r.get("id") or "")
        if mid:
            seeds[mid] = float(r.get("score") or 0)

    # ② 选桥接实体：**按稀有度**（IDF），不按出现次数。
    #    实测教训：「主人」这种实体连着所有事实，靠它扩散等于把所有事都捞进来（噪声）；
    #    「毛肚」「换寝室」这种只出现一两次的才有信息量。
    seed_ents = []
    if seeds:
        rows2 = con.execute(
            "SELECT e.entity, COUNT(DISTINCT e.fact_id) n FROM edge e WHERE e.fact_id IN (%s) "
            "GROUP BY e.entity" % ",".join("?" * len(seeds)),
            list(seeds.keys())).fetchall()
        total_facts = con.execute("SELECT COUNT(*) FROM fact").fetchone()[0] or 1
        scored = []
        for ent, n in rows2:
            # 出现在越多事实里 → 越没用
            idf = 1.0 - math.log(1 + n) / math.log(1 + total_facts)
            scored.append((idf, ent))
        scored.sort(reverse=True)
        seed_ents = [e for _, e in scored if _ > 0.15][:entities_hint]

    # ③ 沿实体走 hops 跳
    expanded = {}
    frontier, seen_facts = set(seeds.keys()), set(seeds.keys())
    for hop in range(1, max(1, int(hops)) + 1):
        if not frontier or not seed_ents:
            break
        q = "SELECT DISTINCT fact_id FROM edge WHERE entity IN (%s)" % ",".join("?" * len(seed_ents))
        cand = {r[0] for r in con.execute(q, seed_ents).fetchall()} - seen_facts
        for fid in cand:
            expanded[fid] = hop
        seen_facts |= cand
        # 下一跳用这些新事实的实体
        if cand:
            rows3 = con.execute(
                "SELECT e.entity, COUNT(DISTINCT e.fact_id) n FROM edge e WHERE e.fact_id IN (%s) "
                "GROUP BY e.entity" % ",".join("?" * len(cand)), list(cand)).fetchall()
            tf = con.execute("SELECT COUNT(*) FROM fact").fetchone()[0] or 1
            s3 = sorted(((1.0 - math.log(1 + n) / math.log(1 + tf), e) for e, n in rows3),
                        reverse=True)
            seed_ents = [e for sc, e in s3 if sc > 0.15][:entities_hint]
        frontier = cand

    # ④ 取文本/属性，合并打分
    def fetch(ids):
        if not ids:
            return {}
        q = ("SELECT id,text,entities,importance,added,hits,last_hit FROM fact WHERE id IN (%s)"
             % ",".join("?" * len(ids)))
        return {r[0]: dict(text=r[1], entities=json.loads(r[2] or "[]"), importance=r[3],
                           added=r[4], hits=r[5]) for r in con.execute(q, list(ids)).fetchall()}

    seed_data, exp_data = fetch(list(seeds.keys())), fetch(list(expanded.keys()))
    out = []
    for mid, sem in seeds.items():
        d = seed_data.get(mid)
        if not d:
            continue
        out.append(dict(id=mid, memory=d["text"], hop=0, entities=d["entities"],
                        score=_score(sem, d["importance"], d["added"], d["hits"], 0)))
    for mid, hop in expanded.items():
        d = exp_data.get(mid)
        if not d:
            continue
        # 扩散来的没有向量分：用"和种子实体的重叠度"当相关度。
        # 重叠 1 条实体就够说明有关系（实体本来就是具体的词，不是大类），
        # 所以 baseline 给 0.5，重叠越多越高；再乘 hop 折扣。
        shared = set(d["entities"]) & set(seed_ents)
        overlap = min(1.0, len(shared) / 2.0)          # 重叠 2 个实体就算满分
        pseudo_sem = 0.5 + 0.45 * overlap
        out.append(dict(id=mid, memory=d["text"], hop=hop, entities=d["entities"],
                        shared=sorted(shared),
                        score=_score(pseudo_sem, d["importance"], d["added"], d["hits"], hop)))

    out.sort(key=lambda x: -x["score"])
    top = out[:max(1, int(n))]

    now = time.time()
    for it in top:
        con.execute("UPDATE fact SET hits=hits+1, last_hit=? WHERE id=?", (now, it["id"]))
    con.commit()
    con.close()
    return top



def _seen_load():
    try:
        return set(json.loads(io.open(SEEN, encoding="utf-8").read()) or [])
    except Exception:
        return set()


def _seen_save(seen):
    try:
        os.makedirs(os.path.dirname(SEEN), exist_ok=True)
        tmp = SEEN + ".tmp"
        io.open(tmp, "w", encoding="utf-8").write(json.dumps(sorted(seen)[-2000:], ensure_ascii=False))
        os.replace(tmp, SEEN)
    except Exception:
        pass


def _fingerprint(text):
    import hashlib
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


def ingest(text, importance=0.55, kind="diary"):
    """把一段文本抽成事实入库，同一段内容只入一次（按内容指纹去重）。"""
    text = (text or "").strip()
    if len(text) < 20:
        return []
    fp = _fingerprint(text)
    seen = _seen_load()
    if fp in seen:
        return []
    got = remember(text, importance, kind)
    if got:
        seen.add(fp)
        _seen_save(seen)
    return got


def backfill(limit=400, per_batch=6):
    """把已有日记摊成事实（一次性）。

    日记格式是：
        ### 2026-11-08 03:37 | 主人 | 困困的
        正文两句话
    所以按 "### " 切条目，几段拼成一批交给抽取（省调用次数）。
    """
    import glob
    files = sorted(glob.glob(os.path.join(BASE, "memory/diary/*.md")))
    total = 0
    for f in files:
        try:
            body = io.open(f, encoding="utf-8").read()
        except Exception:
            continue
        blocks, cur = [], []
        for line in body.split("\n"):
            if line.startswith("### "):
                if cur:
                    blocks.append("\n".join(cur))
                cur = [line[4:].strip()]
            elif cur and line.strip():
                cur.append(line.strip())
        if cur:
            blocks.append("\n".join(cur))
        if not blocks:
            continue
        for i in range(0, len(blocks), per_batch):
            chunk = "\n\n".join(blocks[i:i + per_batch])[:3500]
            got = ingest(chunk, importance=0.55, kind="diary")
            total += len(got)
            print("  %s 第 %d 批 → %d 条（累计 %d）" % (os.path.basename(f), i // per_batch + 1,
                                                        len(got), total))
            if total >= limit:
                return total
    return total


def stats():
    con = _side()
    n, ents, hits = con.execute(
        "SELECT COUNT(*), (SELECT COUNT(DISTINCT entity) FROM edge), COALESCE(SUM(hits),0) FROM fact"
    ).fetchone()
    con.close()
    return dict(facts=n, entities=ents, total_hits=hits,
                size_kb=round(os.path.getsize(SIDE_DB) / 1024, 1) if os.path.exists(SIDE_DB) else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--add")
    ap.add_argument("--recall")
    ap.add_argument("--importance", type=float, default=0.6)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--hops", type=int, default=1)
    ap.add_argument("--backfill", action="store_true")
    a = ap.parse_args()
    if a.add:
        print(json.dumps(remember(a.add, a.importance), ensure_ascii=False, indent=1))
    elif a.recall:
        for it in recall(a.recall, a.n, hops=a.hops):
            via = ("（经 %s）" % "、".join(it["shared"])) if it.get("shared") else ""
            print("%.3f  跳%d %s [%s]  %s" % (it["score"], it["hop"], via,
                                            "、".join(it["entities"][:4]), it["memory"][:60]))
    elif a.backfill:
        print("共补充 %d 条" % backfill())
    else:
        print(json.dumps(stats(), ensure_ascii=False))
