#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一集字幕压成内部报告（便宜模型干，主模型只读 600 字）

为什么：
  主模型（她的会话）读整集字幕要 ~4k token，而且她真正需要的是"这集发生了什么、
  哪几句值得说"，不是逐行文本。让一个便宜模型先读全文、写一份内部报告，
  主模型只读报告 —— 这就是 dsh-bili-subagent 那套"子代理去看、伙伴来说"的思路，
  只是我们没装那个插件，用本地脚本 + 同一个网关实现。

用法：
    /opt/tulpa/.venv/bin/python bili_ep_report.py --ep 7
    ... --ep 7 --refresh        # 强制重生成
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.request

D = "/opt/tulpa/data/bilibili/watch"
QWEN_ENV = "/opt/dsh/qwen.env"
MODEL = "deepseek-v4.1-flash-1017"      # 便宜档；换模型只改这里
PROMPT = """下面是一部动画某一集的**中文字幕**（画面硬字幕 OCR，可能有错字）。
请写一份**给伙伴自己看**的内部报告，不要写成评论、不要客套：

1. 主线：3–5 条，按发生顺序，每条一句话。
2. 名场面/关键台词：2–3 条，带时间（如 12:34），原句尽量准确。
3. 情绪走向：一句话，从开头到结尾气氛怎么变。
4. 值得跟他提的点：1–2 条，写"为什么值得说"，不要写具体说法。

总量控制在 600 字以内。字幕里没有的不要编。"""


def api_key():
    for ln in io.open(QWEN_ENV, encoding="utf-8"):
        if ln.startswith("QWEN_API_KEY="):
            return ln.split("=", 1)[1].strip()
    raise SystemExit("没有 QWEN_API_KEY")


KEY = api_key()


def call(prompt, content, timeout=180):
    body = {"model": MODEL, "temperature": 0.3, "messages": [
        {"role": "system", "content": prompt},
        {"role": "user", "content": content}]}
    req = urllib.request.Request("http://127.0.0.1:8890/v1/chat/completions",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + KEY,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return text.strip(), (d.get("usage") or {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=73077)
    ap.add_argument("--ep", type=int, required=True)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    d = os.path.join(D, "ss%d" % args.season)
    jl = os.path.join(d, "ep%d.jsonl" % args.ep)
    out = os.path.join(d, "ep%d.report.md" % args.ep)
    if not os.path.exists(jl):
        print("这一集还没有字幕稿：%s" % jl)
        return 1
    if os.path.exists(out) and not args.refresh:
        print("已有报告（--refresh 可重生成）：%s" % out)
        return 0

    rows = [json.loads(x) for x in io.open(jl, encoding="utf-8") if x.strip()]
    rows.sort(key=lambda r: r["t0"])
    # 给便宜模型的是「时间 + 文本」，不带别的噪音
    body = "\n".join("[%02d:%02d] %s" % (int(r["t0"]) // 60, int(r["t0"]) % 60, r["text"])
                     for r in rows)
    t0 = time.time()
    text, usage = call(PROMPT, body)
    dt = time.time() - t0
    if not text:
        print("模型没给内容")
        return 1

    meta_p = os.path.join(d, "ep%d.meta.json" % args.ep)
    meta = {}
    try:
        meta = json.loads(io.open(meta_p, encoding="utf-8").read())
    except Exception:
        pass
    head = "# %s · 第 %d 集 · 内部报告\n\n" % (meta.get("title", ""), args.ep)
    note = ("（由 %s 从字幕稿压缩生成；输入 %d 字，输出 %d 字，%d token，%.0f 秒）\n\n"
            % (MODEL, len(body), len(text), int(usage.get("total_tokens") or 0), dt))
    io.open(out, "w", encoding="utf-8").write(head + note + text + "\n")

    meta["report"] = os.path.basename(out)
    meta["report_tokens"] = int(usage.get("total_tokens") or 0)
    io.open(meta_p, "w", encoding="utf-8").write(json.dumps(meta, ensure_ascii=False))
    print("报告写好：%s" % out)
    print("压缩：%d 字 → %d 字（%.1f%%），%d token，%.0f 秒"
          % (len(body), len(text), 100.0 * len(text) / max(1, len(body)),
             int(usage.get("total_tokens") or 0), dt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
