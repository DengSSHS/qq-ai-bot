#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三层记忆：临时 / 大记忆库 / 核心。

为什么要有这个文件：之前三块各自都在，但没有"接线"——
  · 大记忆库（日记 + knowledge.json + FTS）只有她主动调工具才会用，
    实测她几乎不调（MEM_HINT 只是提醒她"你可以去查"，她多数时候不回查）；
  · 临时层只有原始对话，长会话一压缩就断片；
  · 核心记忆（AGENTS.md）是静态的，靠每天 03:10 蒸馏一次。

这个脚本负责接线，三个子命令对应三层：

  recall   每轮按当前消息去大记忆库捞相关的几条（有预算上限）
  summary  读/写临时层的滚动摘要（每轮注入，保证长会话不断片）
  idle     空闲 ≥10 分钟后，把这一段临时记忆压成摘要 + 事实，写回库里

设计原则：
  · recall 是只读的，失败就返回空，绝不让桥接卡住（子进程有超时）。
  · idle 的写入是保守的：只写摘要和带证据的事实条目，不擅自改日记正文。
  · 三层各管一段：核心记忆（AGENTS.md）依然由 memory_distill.py 每天重写，
    这里只读不写，避免两个程序抢同一个文件。

用法：
    python3 mem_layers.py recall --text "你昨天看了什么"
    python3 mem_layers.py summary            # 打印当前摘要
    python3 mem_layers.py summary --set "…"  # 手动覆盖
    python3 mem_layers.py idle               # 空闲检查（供 timer 调用）
    python3 mem_layers.py idle --dry         # 只看会做什么
    python3 mem_layers.py status             # 三层各有多少东西
"""
import io
import json
import os
import re
import subprocess
import sys
import time

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
MEM = os.path.join(BASE, "memory")
SUMMARY = os.path.join(MEM, "session_summary.md")
STATE = os.path.join(MEM, "layers_state.json")
DIARY_DIR = os.path.join(MEM, "diary")
KNOW = os.path.join(MEM, "knowledge.json")
SESS_ROOT = os.path.join(BASE, "sessions")
ACTIONS = os.path.join(BASE, "media", "onebot", "qq-actions.log")
SHIM = os.environ.get("LLM_SHIM", "http://127.0.0.1:8890/v1/chat/completions")
MODEL = os.environ.get("LLM_MODEL", "deepseek-v4.1-flash-1017")

RECALL_BUDGET = int(os.environ.get("RECALL_BUDGET", "1500"))  # 每轮最多注入多少字
RECALL_K = int(os.environ.get("RECALL_K", "5"))
IDLE_MIN = float(os.environ.get("IDLE_MIN", "10"))
SUMMARY_MAX = int(os.environ.get("SUMMARY_MAX", "1200"))


def _limits():
    """三个旋钮从 limits.json 读，改配置就生效（不用改代码/重启服务）。"""
    global RECALL_K, RECALL_BUDGET, IDLE_MIN
    try:
        lim = rdj(os.path.join(BASE, "limits.json"), {})
        if isinstance(lim, dict):
            RECALL_K = int(lim.get("recall_k") or RECALL_K)
            RECALL_BUDGET = int(lim.get("recall_budget") or RECALL_BUDGET)
            IDLE_MIN = float(lim.get("idle_minutes") or IDLE_MIN)
    except Exception:
        pass


_limits()

# 检索时丢掉的虚词：中文 2/3 字滑窗会产生大量这类碎片
STOP = set("的了是我你他她它在我你它们这那什么怎么为什么一个也就都还又很太真没不要把被给对跟于而且但如果因为所以然后现在时候可以要会能吗呢啊吧嘛呀哦嗯行好啊得好说吧来去上下里外很更最更"
           "有没别再才刚正被从向到过着地得们个条只件点些")


# ----------------------------------------------------------------- 小工具
def rd(p, default=""):
    try:
        return io.open(p, encoding="utf-8").read()
    except Exception:
        return default


def wr(p, s):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    io.open(tmp, "w", encoding="utf-8", newline="\n").write(s)
    os.replace(tmp, p)


def rdj(p, default=None):
    """读 JSON；文件不存在 / 内容为 null 时返回 default（之前这里会把 None 漏出去，
    导致 idle 路径 AttributeError: 'NoneType' object has no attribute 'get'）。"""
    try:
        v = json.loads(rd(p, "null"))
    except Exception:
        v = None
    if v is None:
        return {} if default is None else default
    return v


def wrj(p, obj):
    wr(p, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def newest_mtime():
    """最后一次"有活动"的时间：会话文件 / 动作日志里最新的那个。"""
    newest = 0.0
    cands = [ACTIONS]
    try:
        for root, _dirs, files in os.walk(SESS_ROOT):
            for f in files:
                if f.startswith("session.") and ".zstd" in f:
                    cands.append(os.path.join(root, f))
    except Exception:
        pass
    for p in cands:
        try:
            m = os.path.getmtime(p)
            if m > newest:
                newest = m
        except Exception:
            continue
    return newest


# ------------------------------------------------------- 大记忆库：检索
def _clip(s, n):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _tokens(q, cap=14):
    """把一句话切成检索用的词：中文用 2/3 字滑窗，英文按空白，去停用词。

    为什么要切：FTS 分词对无空格中文不友好，整句当 token 永远匹配不上
    （实测 recall 返回空就是这个原因）。
    """
    q = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", " ", q or "").strip()
    if not q:
        return []
    out, seen = [], set()
    for part in q.split():
        if re.search(r"[\u4e00-\u9fff]", part):
            for n in (3, 2):
                for i in range(len(part) - n + 1):
                    t = part[i:i + n]
                    if t in STOP or all(c in STOP for c in t):
                        continue
                    if t not in seen:
                        seen.add(t)
                        out.append(t)
        elif part not in seen:
            seen.add(part)
            out.append(part)
    return out[:cap]


def _knowledge_entries():
    data = rdj(KNOW, {})
    if isinstance(data, dict):
        for key in ("entries", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return data if isinstance(data, list) else []


def _knowledge_save(entries):
    store = rdj(KNOW, {})
    if isinstance(store, dict):
        store["entries"] = entries
        store["updated"] = int(time.time())
        wrj(KNOW, store)
    else:
        wrj(KNOW, {"entries": entries, "updated": int(time.time())})


def _session_summaries(toks, k=3):
    """会话摘要也是 L3 的一部分（每次空闲蒸馏追加一行）。"""
    out = []
    d = os.path.join(MEM, "session_log")
    try:
        files = sorted(os.listdir(d))[-4:]
    except Exception:
        return out
    for name in files:
        txt = rd(os.path.join(d, name), "")
        for block in txt.split("### ")[1:]:
            head, _, body = block.partition("\n")
            body = body.strip()
            if not body:
                continue
            hit = sum(1 for t in toks if t in body)
            if hit:
                out.append((hit, head.strip(), body))
    out.sort(key=lambda x: -x[0])
    return out[:k]


def _bili_watched(toks, k=3):
    """她看过的 B 站视频（标题 + 备注）也参与召回，否则“昨天那个视频”永远查不到。"""
    out = []
    p = os.path.join(MEM, "bili-watched.jsonl")
    try:
        lines = io.open(p, encoding="utf-8").read().split("\n")
    except Exception:
        return out
    for ln in lines[-200:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            it = json.loads(ln)
        except Exception:
            continue
        body = " ｜".join(str(it.get(x) or "") for x in ("title", "note", "summary") if it.get(x))
        if not body:
            continue
        hit = sum(1 for t in toks if t in body)
        if hit:
            out.append((hit, str(it.get("bvid") or ""), body))
    out.sort(key=lambda x: -x[0])
    return out[:k]


def recall(text, k=RECALL_K, budget=RECALL_BUDGET):
    """按当前消息检索大记忆库，返回可直接注入的文本块（可能为空串）。"""
    toks = _tokens(text)
    if not toks:
        return ""

    # 日记：多个 token 各查一次 FTS，取最好分数合并
    rows, best = [], {}
    try:
        sys.path.insert(0, BASE)
        import search_index
        for t in toks[:10]:
            for kind, head, body, score in search_index.search(t, limit=5):
                if kind != "diary":
                    continue
                key = (head, body)
                if key not in best or score < best[key][0]:
                    best[key] = (score, kind, head, body)
    except Exception:
        pass
    rows = list(best.values())

    # 事实库：命中 token 越多越靠前
    facts = []
    for it in _knowledge_entries():
        if not isinstance(it, dict):
            continue
        body = str(it.get("text") or "")
        if not body:
            continue
        hit = sum(1 for t in toks if t in body)
        if hit:
            facts.append((hit, int(it.get("count") or 1), body, str(it.get("kind") or "")))
    facts.sort(key=lambda x: (-x[0], -x[1]))

    diary = [(str(h or ""), str(b or "")) for _s, _k, h, b in rows]
    try:
        sys.path.insert(0, BASE)
        import mem_rank
        diary = mem_rank.rank(diary)
    except Exception:
        diary.sort(key=lambda x: x[0])

    lines, used = [], 0
    for _hit, _cnt, body, kind in facts[: max(1, k // 2)]:
        line = "· ［%s］%s" % (kind or "事实", _clip(body, 220))
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)

    # 看过什么 / 上一段聊了什么：这两类最容易“查不到”
    for _hit, head, body in _bili_watched(toks, k=2):
        line = "· ［看过%s］%s" % ((" " + head) if head else "", _clip(body, 200))
        if used + len(line) > budget or len(lines) >= k:
            break
        lines.append(line)
        used += len(line)
    for _hit, head, body in _session_summaries(toks, k=2):
        line = "· ［上次聊到%s］%s" % ((" " + head) if head else "", _clip(body, 200))
        if used + len(line) > budget or len(lines) >= k:
            break
        lines.append(line)
        used += len(line)

    for head, body in diary:
        if len(lines) >= k:
            break
        line = "· ［日记%s］%s" % ((" " + head) if head else "", _clip(body, 260))
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)

    if not lines:
        return ""
    return ("[记忆片段·这是我以前记下的，不是主人现在说的话]\n"
            + "\n".join(lines)
            + "\n[以上仅作参考：与当前话题无关就忽略，别硬扯]")


# --------------------------------------------------- 临时层：滚动摘要
def summary_read():
    return rd(SUMMARY, "").strip()


def summary_write(text):
    text = (text or "").strip()
    if len(text) > SUMMARY_MAX:
        text = text[: SUMMARY_MAX - 1] + "…"
    wr(SUMMARY, text + "\n" if text else "")
    return text


# ------------------------------------------------- 临时层：读最近的对话
def _decode_session(path, tail_lines=60):
    """解一段会话（多帧 zstd，用系统 zstd 命令行最省事）。"""
    try:
        p = subprocess.run(["zstd", "-dc", path], capture_output=True, timeout=20)
        raw = (p.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return []
    lines = [ln for ln in raw.split("\n") if ln.strip()]
    out = []
    for ln in lines[-tail_lines:]:
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        typ = str(ev.get("type") or "")
        data = ev.get("data") or {}
        msg = data.get("message") or {}
        role = msg.get("role")
        text = ""
        for c in (msg.get("content") or []):
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                text += c["text"]
        if not text.strip():
            continue
        if typ in ("user/message", "assistant/message") or role in ("user", "assistant"):
            if role in ("user", "assistant"):
                out.append((role, text.strip()))
    return out


def recent_session():
    newest, path = 0.0, None
    try:
        for root, _d, files in os.walk(SESS_ROOT):
            for f in files:
                if f.startswith("session.") and ".zstd" in f:
                    p = os.path.join(root, f)
                    m = os.path.getmtime(p)
                    if m > newest:
                        newest, path = m, p
    except Exception:
        pass
    return path, newest


# ------------------------------------------------------------ 蒸馏（LLM）
def _api_key():
    """从 qwen.env 或环境变量取 key（llm-shim 要 Bearer 认证，实测 401）。"""
    for name in ("QWEN_API_KEY", "LLM_API_KEY"):
        v = os.environ.get(name)
        if v:
            return v.strip()
    try:
        for line in io.open(os.path.join(BASE, "qwen.env"), encoding="utf-8"):
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() in ("QWEN_API_KEY", "LLM_API_KEY") and v.strip():
                return v.strip()
    except Exception:
        pass
    return ""


def _llm(prompt, timeout=90):
    import urllib.request

    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(SHIM, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    key = _api_key()
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    return (data["choices"][0]["message"]["content"] or "").strip()


def _parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    for cand in (m.group(0),):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def distill(dry=False):
    path, mtime = recent_session()
    if not path:
        return "没有会话文件，跳过"
    turns = _decode_session(path, tail_lines=120)[-24:]
    # 测试消息会进她的上下文，如果不滤掉，蒸馏会把「测试里说过的话」当成真事实写进库里。
    # 约定：测试工具（qq_test_inject.py）会给自己发的句子加 [测试] 前缀。
    clean, skip_next = [], False
    for role, text in turns:
        if role == "user" and "[测试]" in text:
            skip_next = True
            continue
        if skip_next and role == "assistant":
            skip_next = False
            continue
        skip_next = False
        clean.append((role, text))
    turns = clean
    if len(turns) < 4:
        return "最近这段对话太少（%d 条，已滤掉测试），跳过" % len(turns)
    convo = "\n".join(("[主人] " if r == "user" else "[鲸鲸] ") + _clip(t, 300) for r, t in turns)

    prompt = (
        "下面是鲸鲸（一只拟人鲸鱼娘）和她主人最近的对话片段。请做三件事，只输出 JSON：\n"
        + '{"summary": "这段对话的滚动摘要，120 字以内，保留能接上话的线索（谁说了什么、'
        + '情绪、正在做的事）", "facts": [{"kind": "fact|pref|person|lesson", '
        + '"text": "一句话事实"}], "skip": false}\n'
        + "规则：\n"
        + "1. facts 只写「以后还用得上」的：主人的偏好/习惯/约定、人的身份、踩过的坑。"
        + "闲聊、寒暄、一次性玩笑不要写。没有就给空数组。\n"
        + "2. 摘要要写成能被下一轮直接接上的样子，不要写成观后感。\n"
        + "3. 这段对话没有值得留下的信息时，返回 {\"skip\": true}。\n\n"
        + "对话：\n" + convo
    )
    try:
        raw = _llm(prompt)
    except Exception as e:
        return "蒸馏失败（LLM 不可用：%s）" % str(e)[:80]
    data = _parse_json(raw)
    if not data:
        return "蒸馏失败（返回不是 JSON）"
    if data.get("skip"):
        return "蒸馏：这段没有值得留下的东西"

    summ = str(data.get("summary") or "").strip()
    facts = data.get("facts") or []
    if dry:
        return "（dry）摘要→%s｜事实 %d 条：%s" % (
            _clip(summ, 120), len(facts), "; ".join(str(f.get("text"))[:40] for f in facts[:4]))

    if summ:
        summary_write(summ)
        # 方案要求：摘要"追加"进 L3，不是只存在临时文件里——
        # 这样“上周聊过什么”也能被召回，且热缓冲的原文没被动过。
        try:
            logpath = os.path.join(MEM, "session_log", time.strftime("%Y-%m.md"))
            os.makedirs(os.path.dirname(logpath), exist_ok=True)
            with io.open(logpath, "a", encoding="utf-8") as f:
                f.write("### %s\n%s\n\n" % (time.strftime("%Y-%m-%d %H:%M"), summ))
        except Exception:
            pass

    added = 0
    if facts:
        entries = list(_knowledge_entries())
        exist = {str(i.get("text")) for i in entries if isinstance(i, dict)}
        today = time.strftime("%Y-%m-%d")
        for f in facts[:5]:
            t = str((f or {}).get("text") or "").strip()
            if not t or t in exist or len(t) > 200:
                continue
            entries.append({
                "id": "k%d" % int(time.time() * 1000),
                "kind": str((f or {}).get("kind") or "fact"),
                "text": t,
                "scope": "u:1709392682",
                "count": 1,
                "first": today, "last": today,
                "evidence": ["蒸馏@%s" % today],
            })
            exist.add(t)
            added += 1
        _knowledge_save(entries)
    return "蒸馏完成：摘要已更新，新增事实 %d 条" % added


# ------------------------------------------------------------- idle 检查
def idle_check(dry=False):
    idle = (time.time() - newest_mtime()) / 60.0 if newest_mtime() else 1e9
    st = rdj(STATE, {})
    last = float(st.get("last_distill") or 0)
    if idle < IDLE_MIN:
        return "还在聊（空闲 %.1f 分钟 < %.0f），不动" % (idle, IDLE_MIN)
    if last and last >= newest_mtime():
        return "这段已经蒸馏过了（空闲 %.1f 分钟）" % idle
    msg = distill(dry=dry)
    if not dry:
        st["last_distill"] = time.time()
        st["idle_min"] = round(idle, 1)
        wrj(STATE, st)
    return "空闲 %.1f 分钟 → %s" % (idle, msg)


def status():
    path, mtime = recent_session()
    rows = 0
    try:
        sys.path.insert(0, BASE)
        import search_index
        rows = search_index._count()
    except Exception:
        pass
    know = len(_knowledge_entries())
    agents = os.path.join(BASE, "workspace", "AGENTS.md")
    return "\n".join([
        "核心记忆  AGENTS.md %d 字" % len(rd(agents, "")),
        "大记忆库  日记索引 %d 条｜事实 %d 条" % (rows, know),
        "临时记忆  摘要 %d 字｜最近会话 %s（%.0f 分钟前）" % (
            len(summary_read()), os.path.basename(path or "-"),
            (time.time() - mtime) / 60.0 if mtime else -1),
    ])


# ------------------------------------------------------------------ CLI
def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]

    def opt(name, default=None):
        if name in args:
            i = args.index(name)
            if i + 1 < len(args):
                return args[i + 1]
        return default

    if cmd == "recall":
        _limits()
        text = opt("--text", "")
        if not text and not sys.stdin.isatty():
            text = sys.stdin.read()
        print(recall(text))
        return 0
    if cmd == "summary":
        val = opt("--set")
        if val is not None:
            print(summary_write(val))
            return 0
        print(summary_read())
        return 0
    if cmd == "idle":
        _limits()
        print(idle_check(dry="--dry" in args))
        return 0
    if cmd == "distill":
        print(distill(dry="--dry" in args))
        return 0
    if cmd == "status":
        print(status())
        return 0
    print("未知子命令：%s" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
