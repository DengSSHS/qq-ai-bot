#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 QQ 会话里蒸馏长期知识，跨群共用。

用户的要求：学习必须是**总体的**，不是切在每个群里各自为政。所以这里是
单一知识池：在 A 群学到的东西，B 群也应该知道。

但完全不分场合会出事（A 群的黑话搬到 B 群用）。所以每条知识带 `scope`：
    all        到处都能用（人物特征、主人的偏好、我的教训）
    g:<群号>   只属于某个群的梗/氛围
    u:<QQ>     只和某个人的私聊里出现的偏好
注入时标明来源，并在提示里规定「标了群的只在该群用」。

为什么不写 notes.md：
    notes.md 是她自己的手写区，而 tool_notes_write 是 `open(NOTES, "w")` ——
    整文覆盖。蒸馏写进去，她下次一写笔记就全没了。所以知识放独立的
    knowledge.json，再渲染成 knowledge.md 给人看。

为什么每条都要证据：
    一次对话里的偶发事件当规律记下来，越学越偏。所以每条带 count / first /
    last / 来源会话；count 只有 1 的条目单独标注，提醒这是单次观察。

用法：
    python3 memory_distill.py            # 只看，不写文件（默认）
    python3 memory_distill.py --apply    # 真写
    python3 memory_distill.py --apply --limit 3   # 只处理最近 3 个会话
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request

BASE = "/opt/dsh"
SESSIONS = os.path.join(BASE, "sessions")
KNOW = os.path.join(BASE, "memory/knowledge.json")
KNOW_MD = os.path.join(BASE, "memory/knowledge.md")
AGENTS = os.path.join(BASE, "workspace/AGENTS.md")
LOG = os.path.join(BASE, "memory/distill.log")

BLOCK_BEGIN = "<!-- auto-memory:begin 由 memory_distill.py 维护，手改这段会被覆盖 -->"
BLOCK_END = "<!-- auto-memory:end -->"

# 会被时间打过的东西不配当长期记忆：模型名、接口、配额、数量。
# 它们不是“学到的东西”，只是今天的运行状态，两周后就错了。
JUNK_RE = re.compile(r"(模型名|接口|tokenhub|服务器|工作区|AGENTS\.md|表情库共|付费|配额|部署|代码仓)", re.I)
# 关于工具能力的断言多数是模型看对话瞎推的（实测出现过把 qq_group_history
# 说成“拿不到正文”，而 prompt 里明写着会返回正文）。这类一条都不收。
CAP_CLAIM_RE = re.compile(r"(qq_|sticker_|web_|工具|接口).{0,12}(只能|拿不到|不支持|无法|没有)")
CAP_CLAIM_RE2 = re.compile(r"(只能|拿不到|不支持|无法).{0,12}(qq_|sticker_|web_|工具|接口)")

MAX_MSGS = 120           # 每个会话最多看多少条消息
MAX_CHARS = 7000         # 每个会话喂给模型的字数上限
SIM_THRESHOLD = 0.72     # 条目相似度阈值，超过就当成同一条合并

KINDS = ("person", "meme", "pref", "lesson", "fact")

PROMPT = """你在帮一个 QQ 群里的聊天角色整理"长期记忆"。
下面是她和一个聊天对象最近的对话（可能跨群、跨天）。请只提炼**以后再对话还用得上**的东西。

五类，kind 字段用英文：
- person：某个人的稳定特征、称呼、说话方式、雷区（不要真实姓名/地址/电话/账号）
- meme：反复出现的梗、黑话、口头禅，以及它是什么意思
- pref：主人或群友的偏好——喜欢什么、讨厌什么、要她怎么说话
- lesson：她翻过的车、被纠正过的做法（"别这样"）
- fact：固定事实，比如这个群主要聊什么、作息、群的性质

硬规则：
1. 只记有证据的。一次性的偶发事件不要记（比如"今天吃了面条"）。
2. 每条不超过 40 个字，写成陈述句，不要写"主人说"这类转述前缀。
3. 不记隐私：真实姓名、住址、电话、身份证、账号密码、别人的私事。
4. 不记她自己的运行信息（模型名、服务器、代码）。
5. 宁可少记，不要凑数。没有可记的就返回 []。

只输出 JSON 数组，元素形如：{"kind":"pref","text":"主人在夜里话多，白天忙"}
不要输出任何解释。

对话如下：
---
{digest}
---"""


def log(msg):
    line = "%s %s" % (time.strftime("%F %T"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + chr(10))
    except Exception:
        pass
    print(line)


def _text_call(user, max_tokens=1400):
    """用和看图同一个网关的文本模型。"""
    import seer
    key = seer.api_key()
    if not key:
        raise RuntimeError("没有 API key（seer.api_key() 为空）")
    body = json.dumps({
        "model": seer.MODEL,
        "max_tokens": max_tokens,
        "stream": False,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        seer.BASE + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return (d["choices"][0]["message"].get("content") or "").strip()


def _chan_of(dirname):
    """会话目录名 → (scope, 可读频道名)。"""
    m = re.match(r"onebot-group-(\d+)(?:-[a-z0-9]+)?$", dirname)
    if m:
        return "g:%s" % m.group(1), "群%s" % m.group(1)
    m = re.match(r"onebot-private-(\d+)(?:-[a-z0-9]+)?$", dirname)
    if m:
        return "u:%s" % m.group(1), "私聊%s" % m.group(1)
    return None, None


def _sessions(limit=None):
    """列出现存的 QQ 会话，返回 [(scope, label, path, mtime)]，新的在后。"""
    out = []
    if not os.path.isdir(SESSIONS):
        return out
    for ws in os.listdir(SESSIONS):
        wsd = os.path.join(SESSIONS, ws)
        if not os.path.isdir(wsd):
            continue
        for d in os.listdir(wsd):
            scope, label = _chan_of(d)
            if not scope:
                continue
            f = os.path.join(wsd, d, "session.v4.jsonl.zstd")
            if os.path.isfile(f):
                out.append((scope, label, f, os.path.getmtime(f)))
    out.sort(key=lambda x: x[3])
    if limit:
        out = out[-int(limit):]
    return out


def _digest(path):
    """把会话压成一段可读文本，返回 (文本, 首条日期, 末条日期)。"""
    try:
        raw = subprocess.run(["zstd", "-dc", path], capture_output=True,
                             timeout=120).stdout.decode("utf-8", "replace")
    except Exception as e:
        log("解压失败 %s: %s" % (path, e))
        return "", "", ""
    msgs = []
    for line in raw.split(chr(10)):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("type") not in ("user/message", "assistant/message"):
            continue
        d = r.get("data") or {}
        msg = d.get("message") or {}
        role = msg.get("role") or ("user" if r.get("type") == "user/message" else "assistant")
        text = ""
        for part in (msg.get("content") or []):
            if isinstance(part, dict) and part.get("type") == "text":
                text += str(part.get("text") or "")
        text = text.strip()
        if not text:
            continue
        # 丢掉系统注入的大段 prompt / 记忆块，那些不是对话内容
        if text.startswith("（当前时间") or len(text) > 1500:
            continue
        msgs.append((role, text))
    if not msgs:
        return "", "", ""
    msgs = msgs[-MAX_MSGS:]
    lines = []
    total = 0
    for role, text in msgs:
        who = "主人" if role == "user" else "鲸鲸"
        piece = "%s: %s" % (who, text.replace(chr(10), " ")[:300])
        total += len(piece)
        if total > MAX_CHARS:
            break
        lines.append(piece)
    return (chr(10).join(lines),
            time.strftime("%F", time.localtime(os.path.getmtime(path))),
            time.strftime("%F", time.localtime(os.path.getmtime(path))))


def _parse_entries(reply, dropped=None):
    """从模型回答里取 JSON 数组。容错：带 ``` 包裹、前后有废话。"""
    dropped = dropped if dropped is not None else []
    txt = (reply or "").replace("```json", "").replace("```", "").strip()
    i, j = txt.find("["), txt.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        arr = json.loads(txt[i:j + 1])
    except Exception:
        return []
    out = []
    for it in arr if isinstance(arr, list) else []:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "").strip().lower()
        text = str(it.get("text") or "").strip()
        if kind not in KINDS or not text or len(text) > 120:
            continue
        if JUNK_RE.search(text) or CAP_CLAIM_RE.search(text) or CAP_CLAIM_RE2.search(text):
            dropped.append(text)
            continue
        out.append({"kind": kind, "text": text})
    return out


def _norm(s):
    return re.sub(r"[\s，,。.！!？?、；;：:「」“”\"'（）()【】\[\]~…—\-]+", "", str(s)).lower()


def _similar(a, b):
    """字符二元组 Jaccard，够用且不引依赖。"""
    def grams(s):
        s = _norm(s)
        return set(s[i:i + 2] for i in range(max(0, len(s) - 1))) or {s}
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / float(len(ga | gb))


def _load():
    try:
        with open(KNOW, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("entries"), list):
            return d
    except Exception:
        pass
    return {"entries": [], "updated": 0}


def _merge(store, cand, scope, label, today):
    """合并一条候选：相似就累加计数，否则新增。返回 'new' / 'merged'。"""
    entries = store["entries"]
    best, best_sim = None, 0.0
    for e in entries:
        if e.get("kind") != cand["kind"]:
            continue
        s = _similar(e.get("text", ""), cand["text"])
        if s > best_sim:
            best, best_sim = e, s
    if best is not None and best_sim >= SIM_THRESHOLD:
        best["count"] = int(best.get("count") or 1) + 1
        best["last"] = today
        ev = best.setdefault("evidence", [])
        tag = "%s@%s" % (label, today)
        if tag not in ev:
            ev.append(tag)
            best["evidence"] = ev[-6:]
        # 证据变多了就升为全局：同一件事在两个以上场合出现，就不是某个群特有的
        if best.get("scope") != "all" and len(ev) >= 2:
            best["scope"] = "all"
        return "merged"
    entries.append({
        "id": "k%d" % (int(time.time() * 1000) % 10 ** 9 + len(entries)),
        "kind": cand["kind"], "text": cand["text"], "scope": scope,
        "count": 1, "first": today, "last": today,
        "evidence": ["%s@%s" % (label, today)],
    })
    return "new"


KIND_TITLE = {"person": "人", "meme": "梗和黑话", "pref": "偏好",
              "lesson": "我翻过的车", "fact": "群与环境"}


def _render(store, max_chars=2600):
    """渲染成人看的 markdown（也是注入源）。"""
    entries = store.get("entries") or []
    if not entries:
        return ""
    out = ["# 长期记忆（自动整理）", "",
           "跨群汇总。标了「群/私聊」的只在那里用；标「通用」的到处能用。",
           "只出现过 1 次的标了（单次观察），别当定论。", ""]
    for kind in KINDS:
        group = [e for e in entries if e.get("kind") == kind]
        if not group:
            continue
        group.sort(key=lambda e: (-int(e.get("count") or 1), e.get("last") or ""))
        out.append("## %s" % KIND_TITLE[kind])
        for e in group[:14]:
            scope = e.get("scope") or "all"
            if scope == "all":
                tag = "通用"
            elif scope.startswith("g:"):
                tag = "群" + scope[2:]
            else:
                tag = "私聊" + scope[2:]
            cnt = int(e.get("count") or 1)
            extra = "" if cnt > 1 else "（单次观察）"
            out.append("- [%s] %s%s" % (tag, e.get("text", ""), extra))
        out.append("")
    txt = chr(10).join(out)
    if len(txt) > max_chars:
        # 超长就按行截断，保住前面的高优先条目
        cut = []
        total = 0
        for line in txt.split(chr(10)):
            if total + len(line) > max_chars:
                break
            cut.append(line)
            total += len(line) + 1
        txt = chr(10).join(cut) + chr(10) + "（还有更多，见 memory/knowledge.md）"
    return txt


def _render_block(store, max_chars=2200):
    """给 AGENTS.md 用的紧凑版。它会进每轮提示，必须短。"""
    entries = store.get("entries") or []
    if not entries:
        return ""
    lines = ["## 自动整理（跨群汇总）",
             "标了「群/私聊」的只在那里用；标「通用」的到处能用。（单次）= 只看到过一次，别当定论。"]
    for kind in KINDS:
        group = [e for e in entries if e.get("kind") == kind]
        if not group:
            continue
        group.sort(key=lambda e: (-int(e.get("count") or 1), e.get("last") or ""))
        lines.append("### %s" % KIND_TITLE[kind])
        for e in group[:10]:
            scope = e.get("scope") or "all"
            if scope == "all":
                tag = "通用"
            elif scope.startswith("g:"):
                tag = "群" + scope[2:]
            else:
                tag = "私聊" + scope[2:]
            cnt = int(e.get("count") or 1)
            lines.append("- [%s] %s%s" %
                         (tag, e.get("text", ""), "" if cnt > 1 else "（单次）"))
    txt = chr(10).join(lines)
    if len(txt) > max_chars:
        cut, total = [], 0
        for line in txt.split(chr(10)):
            if total + len(line) > max_chars:
                break
            cut.append(line)
            total += len(line) + 1
        txt = chr(10).join(cut) + chr(10) + "（还有更多，见 memory/knowledge.md）"
    return txt


def _write_agents_block(block):
    """把 block 写进 AGENTS.md 的托管区；区块外的内容（她手写的）一字不动。"""
    try:
        with open(AGENTS, encoding="utf-8") as f:
            old = f.read()
    except Exception:
        old = "# 长期记忆（鲸鲸用）" + chr(10)
    managed = BLOCK_BEGIN + chr(10) + block + chr(10) + BLOCK_END
    i, j = old.find(BLOCK_BEGIN), old.find(BLOCK_END)
    if i >= 0 and j > i:
        new = old[:i] + managed + old[j + len(BLOCK_END):]
    else:
        new = old.rstrip() + chr(10) * 2 + managed + chr(10)
    bak = AGENTS + ".bak-distill-" + time.strftime("%Y%m%d")
    if old and not os.path.exists(bak):
        try:
            with open(bak, "w", encoding="utf-8") as f:
                f.write(old)
        except Exception:
            pass
    tmp = AGENTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new)
    os.replace(tmp, AGENTS)
    return len(new)


def run(apply_=False, limit=None, verbose=True):
    got = _sessions(limit=limit)
    if not got:
        return "没有找到 QQ 会话。"
    store = _load()
    before = len(store["entries"])
    stats = {"new": 0, "merged": 0, "sessions": 0, "failed": 0, "dropped": 0}
    dropped = []
    today = time.strftime("%F")

    for scope, label, path, _mt in got:
        digest, d1, d2 = _digest(path)
        if not digest:
            continue
        try:
            reply = _text_call(PROMPT.replace("{digest}", digest))
            cands = _parse_entries(reply, dropped)
            stats["dropped"] = len(dropped)
        except Exception as e:
            stats["failed"] += 1
            log("提炼失败 %s: %s" % (label, str(e)[:120]))
            continue
        stats["sessions"] += 1
        if verbose:
            print("  [%s] 抽出 %d 条" % (label, len(cands)))
        for c in cands:
            if _merge(store, c, scope, label, today) == "new":
                stats["new"] += 1
            else:
                stats["merged"] += 1

    after = len(store["entries"])
    summary = ("会话 %d 个｜新增 %d 条｜合并 %d 条｜条目 %d → %d｜过滤垃圾 %d｜失败 %d"
               % (stats["sessions"], stats["new"], stats["merged"], before, after,
                  stats["dropped"], stats["failed"]))
    if verbose and dropped:
        print("  被过滤掉的（会过期/能力断言）:")
        for d in dropped[:6]:
            print("    - " + d[:60])
    if not apply_:
        return "[dry-run] " + summary + "（没写文件）"
    store["updated"] = int(time.time())
    os.makedirs(os.path.dirname(KNOW), exist_ok=True)
    tmp = KNOW + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    os.replace(tmp, KNOW)
    md = _render(store)
    tmp = KNOW_MD + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md + chr(10))
    os.replace(tmp, KNOW_MD)
    # 同时写进 AGENTS.md 的托管区 —— 那份文件才是每轮都进提示的
    block = _render_block(store)
    size = _write_agents_block(block)
    log("distill " + summary)
    return (summary + "｜knowledge.md %d 字｜AGENTS.md 托管区已更新（全文 %d 字）"
            % (len(md), size))


def main():
    apply_ = "--apply" in sys.argv
    limit = None
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
    try:
        print(run(apply_=apply_, limit=limit))
    except Exception:
        log("distill crash: " + traceback.format_exc()[:400])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
