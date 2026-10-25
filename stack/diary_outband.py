#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日记：带外写（不经过 QQ 消息通道）。

为什么要有这个文件（2026-10-26 方案第一步）：
    原来的做法是把「该写日记了」伪装成一条主人私聊塞进入站管线
    （inject_diary.sh → qq-inject.jsonl）。结果她按闲聊规则来评估这条消息，
    觉得莫名其妙就回「这条还是不理」——但话本身还是发出去了。
    根因不是标签打没打，而是**任务走了不该走的通道**。

    原始材料（会话原文）本来就在 NapCat 之外，写日记不需要任何 QQ 消息参与。
    所以这里直接：读 sessions → 带人设调模型 → 写 memory/diary。

用法：
    python3 diary_outband.py                # 按当前钟点选 noon/midnight
    python3 diary_outband.py midnight
    python3 diary_outband.py --dry          # 只打印，不落盘

设计约束（照方案的红线）：
  · 复用 memory_distill.py 那套文本通道（llm-shim 8890），不新起网关
  · 必须以她的口吻写（prompt 里带 persona.md，并写明"不要写成系统报表"）
  · 只追加，不重写任何现有数据
  · 写完不主动发 QQ 消息（通知单独开关，默认关）
"""
import io
import json
import os
import re
import sys
import time
from datetime import datetime

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
sys.path.insert(0, BASE)

PERSONA = os.path.join(BASE, "media", "onebot", "persona.md")
DIARY = os.path.join(BASE, "memory", "diary")
LOG = os.path.join(BASE, "memory", "diary_outband.log")


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _clip(s, n):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def material(limit=36):
    """今天的对话素材：最近会话里的 user/assistant 原文（滤掉测试）。"""
    import mem_layers

    path, _m = mem_layers.recent_session()
    if not path:
        return "", []
    turns = mem_layers._decode_session(path, tail_lines=240)[-limit:]
    clean, skip_next = [], False
    for role, text in turns:
        if role == "user" and "[测试]" in text:
            skip_next = True
            continue
        if skip_next and role == "assistant":
            skip_next = False
            continue
        skip_next = False
        if "[内部提醒" in text:      # 内部提醒不是对话内容
            continue
        clean.append((role, text))
    return path, clean


def persona_snippet(limit=1800):
    txt = ""
    try:
        txt = io.open(PERSONA, encoding="utf-8").read()
    except Exception:
        pass
    # 人设很长，取前面（身份/说话方式）+ 结尾（禁忌），中间细节对写日记不重要
    if len(txt) <= limit:
        return txt
    return txt[: int(limit * 0.7)] + "\n……\n" + txt[-int(limit * 0.3):]


def write_diary(text, mood, dry=False):
    """走 memory_server 的写入通道：格式统一、会更新遗忘曲线和检索索引。"""
    if dry:
        return "（dry）会写：%s｜%s" % (_clip(mood, 20), _clip(text, 200))
    try:
        import memory_server
        out = memory_server.tool_diary_write({"text": text, "mood": mood, "who": "主人"})
        return str(out)
    except Exception as e:
        return "写入失败：%s" % str(e)[:120]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry = "--dry" in sys.argv
    slot = args[0] if args else ("midnight" if datetime.now().hour < 6 else "noon")

    path, turns = material()
    if len(turns) < 4:
        msg = "素材太少（%d 条），今天不写" % len(turns)
        log(msg)
        print(msg)
        return 0

    convo = "\n".join(("[主人] " if r == "user" else "[鲸鲸] ") + _clip(t, 400) for r, t in turns)
    hour = datetime.now().hour
    when = "一天过完了（晚上）" if hour >= 18 or hour < 6 else "现在是白天"

    prompt = (
        "你在扮演「鲸鲸」——一只拟人鲸鱼娘。下面是今天你和主人的真实对话。\n"
        "现在%s，请以**你自己的口吻**写一则今天的日记。\n\n"
        "要求：\n"
        "1. 第一人称，像人随手写的，不要写成系统报表、不要罗列事件清单、不要总结成条目。\n"
        "2. 2~4 句，写当时的感觉（高兴、丢脸、烦、被逗笑、堵得慌都行），可以带一点动作描写。\n"
        "3. 只写真的发生过、你确实有感受的事；今天确实没什么值得记的，就返回 {\"skip\": true}。\n"
        "4. 不要提「记忆」「模型」「系统」「工具」「对话记录」这类词。\n\n"
        "只输出 JSON：{\"mood\": \"两三个字的心情\", \"text\": \"日记正文\"}\n\n"
        "你的人设（节选）：\n" + persona_snippet() + "\n\n今天的对话：\n" + convo
    )

    import mem_layers
    try:
        raw = mem_layers._llm(prompt)
    except Exception as e:
        msg = "模型调用失败：%s" % str(e)[:100]
        log(msg)
        print(msg)
        return 1

    m = re.search(r"\{.*\}", raw or "", re.S)
    data = None
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:
            data = None
    if not data:
        log("返回不是 JSON：%s" % _clip(raw, 120))
        print("模型没按要求返回 JSON，跳过")
        return 1
    if data.get("skip"):
        log("模型判断今天没什么可写")
        print("今天没什么值得记的，跳过")
        return 0

    text = str(data.get("text") or "").strip()
    mood = str(data.get("mood") or "").strip()
    if not text:
        print("模型给了空正文，跳过")
        return 1

    out = write_diary(text, mood, dry=dry)
    log("%s 写入：%s｜%s" % (slot, _clip(mood, 20), _clip(text, 80)))
    print(out)
    print("[素材] 会话 %s，%d 条对话" % (os.path.basename(path or "-"), len(turns)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
