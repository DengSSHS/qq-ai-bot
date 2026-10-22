#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给她一个会变的"身体状态"：时段决定精神与饥饿，随机决定尾巴和心情。

写进 /opt/dsh/workspace/AGENTS.md 末尾的「## 现在的状态」一节（每轮都会被读到），
每两小时刷一次。她不需要主动说出这些，它只让语气有出处，不像永远精神饱满的机器。
"""
import os
import random
from datetime import datetime

P = "/opt/dsh/workspace/AGENTS.md"
HEAD = "## 现在的状态"


def slot(h):
    if 1 <= h < 6:
        return ("半夜", "很困，脑子发钝，话比白天短，容易走神")
    if 6 <= h < 9:
        return ("清晨", "刚醒，还没完全开机，反应慢半拍")
    if 9 <= h < 11:
        return ("上午", "精神还行，愿意多说两句")
    if 11 <= h < 13:
        return ("中午", "饿了，脑子里老浮现米饭")
    if 13 <= h < 15:
        return ("午后", "犯困，懒得动，能一个字说完不说两句")
    if 15 <= h < 18:
        return ("下午", "恢复正常，注意力能集中")
    if 18 <= h < 20:
        return ("傍晚", "又饿了，盼着吃饭，心情轻快")
    if 20 <= h < 23:
        return ("晚上", "一天里精神最好，话也最多")
    return ("深夜", "困了但还不想睡，说话有点飘")


BODY = ["尾巴凉凉的", "肚子刚刚叫过", "眼睛有点发酸", "尾鳍没什么力气",
        "刚灌了一大口水", "坐久了腰有点僵", "精神头不错", "有点想打哈欠"]
MOOD = ["心里松快", "有点懒", "还算平静", "尾巴闲不住", "莫名有点小得意",
        "有点烦但没处发", "心情不错", "懒得理人但会理你"]


def build(now):
    label, feel = slot(now.hour)
    body = random.choice(BODY)
    mood = random.choice(MOOD)
    return "\n".join([
        HEAD,
        "",
        "- 时间：%s（%s）" % (label, now.strftime("%H:%M")),
        "- 身体：%s，%s" % (feel, body),
        "- 心情：%s" % mood,
        "",
        "这几行只影响你怎么说话，别主动说出来。",
        "",
    ])


def main():
    now = datetime.now()
    try:
        txt = open(P, encoding="utf-8").read()
    except FileNotFoundError:
        txt = "# 长期记忆（机器人用）\n\n"
    i = txt.find(HEAD)
    if i >= 0:
        txt = txt[:i].rstrip() + "\n\n"
    else:
        txt = txt.rstrip() + "\n\n"
    txt = txt + build(now)
    tmp = P + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(txt)
    os.replace(tmp, P)
    print("状态已更新：%s" % now.strftime("%F %T"))


if __name__ == "__main__":
    main()
