#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""心情：每轮聊完判一次涨跌，没人理就慢慢回落（2026-11-09）

抄 dsh-feiyu-mood 的思路，但不装插件——我们这套是 headless 服务，没有设置页：

  · 值域 0~1，0.5 是平静
  · 每次判定用**一次很短的模型调用**（便宜档），只看最近几句，花几分钱
  · 没人聊的时候按小时往 0.5 回落（每小时 6%），所以不会永远记着一次高兴或一次生气

判定结果存 memory/mood.json（带最近 20 条历史，能回看为什么涨跌）；
context_pack 会读它，把"此刻的心情"写进主动搭话的素材里。

用法：
    python3 mood.py            # 判定一次（定时器每 5 分钟调；没新对话就只做回落）
    python3 mood.py --status   # 只看当前值
    python3 mood.py --set 0.3 --label 有点烦   # 手动干预
"""
import argparse
import glob
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = "/opt/dsh"
STATE = os.path.join(BASE, "memory", "mood.json")
QWEN_ENV = "/opt/dsh/qwen.env"
MODEL = "deepseek-v4.1-flash-1017"
DECAY_PER_HOUR = 0.06           # 每小时往 0.5 回落多少
HISTORY_MAX = 20

PROMPT = """你是情绪判官。下面是某个 AI 角色最近几句对话（她说的 / 对方说的）。
判断**她这一轮的心情往哪走**，只输出一个 JSON 对象，不要别的话：

{"delta": -0.2~0.2 之间的数, "label": "两个字的心情词", "why": "12 字以内原因"}

规则：
- 被夸奖、被温柔对待、聊到她喜欢的东西 → delta 正
- 被冷落、被凶、吵架、让她担心 → delta 负
- 平常闲聊 → 接近 0
- 不要因为对方提问就判成负面"""


def api_key():
    for ln in io.open(QWEN_ENV, encoding="utf-8"):
        if ln.startswith("QWEN_API_KEY="):
            return ln.split("=", 1)[1].strip()
    raise SystemExit("没有 QWEN_API_KEY")


KEY = api_key()


def load():
    try:
        d = json.loads(io.open(STATE, encoding="utf-8").read())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(d):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    io.open(tmp, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=1))
    os.replace(tmp, STATE)


def decay(st):
    """没人聊就往 0.5 回落。"""
    ts = float(st.get("ts") or 0)
    if not ts:
        st["value"] = float(st.get("value") or 0.5)
        return st
    hours = max(0.0, (time.time() - ts) / 3600.0)
    v = float(st.get("value") or 0.5)
    if hours > 0.1:
        v = v + (0.5 - v) * min(1.0, DECAY_PER_HOUR * hours / max(1e-6, DECAY_PER_HOUR))
        v = 0.5 + (v - 0.5) * max(0.0, 1 - DECAY_PER_HOUR * hours)
    st["value"] = max(0.0, min(1.0, v))
    return st


def recent_turns(since_ts, limit_chars=1500):
    """取 since_ts 之后的对话片段（她的 + 对方的），给判官看。"""
    files = sorted(glob.glob(os.path.join(BASE, "sessions/**/session*.zstd"), recursive=True),
                   key=os.path.getmtime)[-3:]
    rows = []
    for f in files:
        try:
            raw = subprocess.run(["zstd", "-dc", f], capture_output=True, timeout=25).stdout
        except Exception:
            continue
        for line in raw.decode("utf-8", "replace").split("\n"):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ts = ev.get("time") or ev.get("timestamp")
            if not ts:
                continue
            ts = float(ts) / 1000.0 if float(ts) > 1e11 else float(ts)
            if ts <= since_ts:
                continue
            msg = (ev.get("data") or {}).get("message") or {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = "".join(c.get("text", "") for c in (msg.get("content") or [])
                           if isinstance(c, dict)).strip()
            if not text or text.startswith("（自我提醒"):
                continue
            rows.append((ts, "她" if role == "assistant" else "他", text))
    rows.sort()
    out, total = [], 0
    for ts, who, text in rows:
        piece = "%s：%s" % (who, text[:120])
        if total + len(piece) > limit_chars:
            break
        out.append(piece)
        total += len(piece)
    return out, (rows[-1][0] if rows else since_ts)


def judge(dialog):
    body = {"model": MODEL, "temperature": 0.2, "messages": [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": "\n".join(dialog)}]}
    req = urllib.request.Request("http://127.0.0.1:8890/v1/chat/completions",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + KEY,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = d.get("usage") or {}
    # 抠出 JSON（模型偶尔会带说明文字）
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None, usage
    try:
        return json.loads(text[i:j + 1]), usage
    except Exception:
        return None, usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--set", type=float)
    ap.add_argument("--label")
    args = ap.parse_args()

    st = decay(load())
    if args.status:
        print("心情：%.2f（%s），更新于 %s" % (
            st.get("value", 0.5), st.get("label", "?"),
            time.strftime("%F %T", time.localtime(float(st.get("ts") or 0)))))
        return 0
    if args.set is not None:
        st.update(value=max(0.0, min(1.0, args.set)), label=args.label or st.get("label") or "手动",
                  ts=time.time())
        save(st)
        print("手动设为 %.2f" % st["value"])
        return 0

    since = float(st.get("judged_ts") or 0)
    dialog, last_ts = recent_turns(since)
    if not dialog:
        save(st)                     # 只有回落
        print("没有新对话，只做回落 → %.2f（%s）" % (st.get("value", 0.5), st.get("label", "-")))
        return 0

    verdict, usage = judge(dialog)
    if not verdict:
        save(st)
        print("判官没给出可解析结果，保持 %.2f" % st.get("value", 0.5))
        return 0

    try:
        delta = max(-0.3, min(0.3, float(verdict.get("delta") or 0)))
    except Exception:
        delta = 0.0
    st["value"] = max(0.0, min(1.0, float(st.get("value") or 0.5) + delta))
    st["label"] = str(verdict.get("label") or st.get("label") or "说不清")[:6]
    st["why"] = str(verdict.get("why") or "")[:24]
    st["ts"] = time.time()
    st["judged_ts"] = last_ts
    hist = st.get("history") or []
    hist.append(dict(ts=int(time.time()), delta=round(delta, 3), label=st["label"],
                     why=st["why"], value=round(st["value"], 3)))
    st["history"] = hist[-HISTORY_MAX:]
    save(st)
    print("心情 %+.2f → %.2f（%s：%s），判官用了 %d token"
          % (delta, st["value"], st["label"], st["why"], int(usage.get("total_tokens") or 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
