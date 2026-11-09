#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 README 的 banner（docs/img/banner.png）

为什么用代码画而不是 AI 生图：banner 上要写中文标题和能力清单，
生图模型的文字基本必糊；这里用思源黑体排版，尺寸稳定、改文案重跑即可。

用法：
    python3 make_banner.py                  # 写 docs/img/banner.png
    python3 make_banner.py --out /tmp/x.png
"""
import argparse
import io
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 340
FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

BG_TOP = (10, 18, 32)
BG_BOTTOM = (18, 34, 58)
ACCENT = (96, 190, 255)          # 鲸蓝
ACCENT2 = (255, 176, 122)        # 暖橙（情绪/心）
TEXT = (236, 243, 250)
MUTED = (150, 170, 190)

TITLE = "qq-ai-bot"
SUBTITLE = "一个住在 QQ 群里的 AI 角色"
FEATURES = [
    ("记忆", "会淡忘，也会被唤起"),
    ("心情", "会涨会落，也会回稳"),
    ("看番", "读画面硬字幕，不听写"),
]
FOOTNOTE = "2 核 2G 入门服务器 · 核心 ~750 MB · 7 个自建 MCP · 核心零依赖"


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def gradient(w, h, top, bottom):
    img = Image.new("RGB", (1, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((w, h))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "img", "banner.png")

    img = gradient(W, H, BG_TOP, BG_BOTTOM).convert("RGBA")
    d = ImageDraw.Draw(img)

    # 右上角的柔光（模拟水面反光）
    glow = Image.new("RGBA", (600, 600), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r in range(300, 0, -4):
        alpha = int(26 * (1 - r / 300.0) ** 2)
        gd.ellipse([300 - r, 300 - r, 300 + r, 300 + r], fill=ACCENT + (alpha,))
    img.alpha_composite(glow, (W - 380, -220))

    # 左侧竖条
    d.rectangle([56, 58, 62, H - 58], fill=ACCENT)

    f_title = font(FONT_BOLD, 66)
    f_sub = font(FONT_REG, 26)
    f_chip = font(FONT_BOLD, 22)
    f_chipd = font(FONT_REG, 19)
    f_foot = font(FONT_REG, 18)

    d.text((92, 62), TITLE, font=f_title, fill=TEXT)
    d.text((94, 146), SUBTITLE, font=f_sub, fill=MUTED)

    # 三枚能力标签。
    # 半透明块必须画在单独图层再 alpha_composite —— 直接在 RGBA 上 fill 带 alpha，
    # 转成 RGB 时会变成实心白（第一版就踩了这个）。
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    x = 92
    y = 200
    for name, desc in FEATURES:
        tw = ld.textlength(name, font=f_chip)
        dw = ld.textlength(desc, font=f_chipd)
        box_w = int(max(tw + 40, tw + dw + 44))
        ld.rounded_rectangle([x, y, x + box_w, y + 64], radius=14,
                             fill=(38, 68, 104, 150), outline=ACCENT + (90,), width=1)
        ld.text((x + 20, y + 9), name, font=f_chip, fill=ACCENT)
        ld.text((x + 20, y + 37), desc, font=f_chipd, fill=(186, 205, 222))
        x += box_w + 16
        if x > W - 300:
            break
    img.alpha_composite(layer)

    d.text((92, H - 44), FOOTNOTE, font=f_foot, fill=(120, 140, 160))

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    img.convert("RGB").save(out, "PNG", optimize=True)
    print("写好 %s（%d×%d，%.0f KB）" % (out, W, H, os.path.getsize(out) / 1024))


if __name__ == "__main__":
    main()
