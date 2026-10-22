# -*- coding: utf-8 -*-
"""日记 / 长期笔记的全文检索索引。

方案抄自 GitHub: fellowgeek/mcp-memory（FTS5 + 触发器 + bm25 排序），
但做了一处关键修正：**tokenizer 必须指定为 trigram**。
FTS5 默认分词器按空白和 ASCII 词边界切词，一整段中文会被当成一个巨大的词，
所以原项目在中文语料上搜索等于失效（搜「米饭」永远 0 结果）。
trigram 以三字符滑窗建索引，才能做中文子串与多词匹配。
"""
import os
import re
import sqlite3
import time

MEM = os.environ.get("MEMORY_DIR", "/opt/dsh/memory")
DIARY = os.path.join(MEM, "diary")
NOTES = os.path.join(MEM, "notes.md")
DB = os.path.join(MEM, "search.db")

MIN_GRAM = 3


def _entries():
    """抽出 (kind, head, body) 三元组：日记按 ### 分块，笔记整体一块。"""
    out = []
    try:
        files = sorted(f for f in os.listdir(DIARY) if f.endswith(".md"))
    except FileNotFoundError:
        return out
    for fn in files:
        try:
            with open(os.path.join(DIARY, fn), encoding="utf-8") as f:
                t = f.read()
        except Exception:
            continue
        for blk in re.split(r"^### ", t, flags=re.M)[1:]:
            head, _, body = blk.partition("\n")
            head = head.strip()
            if head:
                out.append(("diary", head, body.strip()))
    try:
        with open(NOTES, encoding="utf-8") as f:
            nt = f.read()
    except Exception:
        nt = ""
    if nt.strip():
        out.append(("notes", "notes", nt))
    return out


def _connect():
    d = sqlite3.connect(DB)
    d.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS idx USING fts5(
               kind UNINDEXED, head UNINDEXED, body,
               tokenize='trigram'
           )"""
    )
    return d


def rebuild():
    rows = _entries()
    d = _connect()
    try:
        d.execute("DELETE FROM idx")
        d.executemany("INSERT INTO idx(kind, head, body) VALUES(?,?,?)", rows)
        d.commit()
    finally:
        d.close()
    return len(rows)


def _count():
    try:
        d = _connect()
        try:
            return d.execute("SELECT count(*) FROM idx").fetchone()[0]
        finally:
            d.close()
    except Exception:
        return 0


def _like_all(tokens, limit):
    d = _connect()
    try:
        where = " AND ".join("(body LIKE ? OR head LIKE ?)" for _ in tokens)
        args = []
        for t in tokens:
            args.extend(["%" + t + "%"] * 2)
        args.append(limit)
        return d.execute(
            "SELECT kind, head, body, 0 AS s FROM idx WHERE " + where + " LIMIT ?", args
        ).fetchall()
    finally:
        d.close()


def search(query, limit=8):
    """返回 [(kind, head, body, score)]，score 越小越相关（bm25）。"""
    q = (query or "").strip()
    if not q:
        return []
    if _count() == 0:
        rebuild()

    tokens = [t for t in re.split(r"\s+", q) if t]
    rows = []
    long_tokens = [t for t in tokens if len(t) >= MIN_GRAM]
    if long_tokens and len(long_tokens) == len(tokens):
        expr = " AND ".join('"' + t.replace('"', '""') + '"' for t in long_tokens)
        d = _connect()
        try:
            rows = d.execute(
                "SELECT kind, head, body, bm25(idx) AS s FROM idx "
                "WHERE idx MATCH ? ORDER BY s LIMIT ?",
                (expr, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            d.close()
    if not rows:
        rows = _like_all(tokens, limit)
    return rows


def ensure(max_age=90):
    try:
        fresh = time.time() - os.path.getmtime(DB) <= max_age
    except Exception:
        fresh = False
    if not fresh:
        try:
            rebuild()
            return True
        except Exception:
            return False
    return False


def stats():
    return {"rows": _count(), "db": DB}
