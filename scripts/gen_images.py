# -*- coding: utf-8 -*-
"""生成 README 用的两张图：banner.png（头图） / architecture.png（架构图）。

要点：中文一律用微软雅黑（Consolas 没有中文字形，会渲染成方框），
纯英文标识符才用 Consolas。改文案后重跑即可，产物落在 docs/img/。
"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), 'docs', 'img')
os.makedirs(OUT, exist_ok=True)

BG = (13, 17, 23)
PANEL = (22, 27, 34)
DEEP = (17, 22, 29)
BORDER = (48, 54, 61)
WHITE = (230, 237, 243)
GRAY = (139, 148, 158)
DIM = (110, 118, 129)
ACCENT = (88, 166, 255)
GREEN = (63, 185, 80)
ORANGE = (240, 136, 62)
PURPLE = (163, 113, 247)

MS = r'C:\Windows\Fonts\msyh.ttc'
MSB = r'C:\Windows\Fonts\msyhbd.ttc'
CON = r'C:\Windows\Fonts\consola.ttf'
CONB = r'C:\Windows\Fonts\consolab.ttf'


def f(path, size):
    return ImageFont.truetype(path, size)


def rrect(d, box, r, fill=None, outline=None, w=2):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=w)


def centered(d, box, text, font, fill):
    tw = d.textlength(text, font=font)
    d.text((box[0] + (box[2] - box[0] - tw) / 2, box[1]), text, font=font, fill=fill)


# ── banner ────────────────────────────────────────────────────
W, H = 1280, 380
img = Image.new('RGB', (W, H), BG)
d = ImageDraw.Draw(img)

d.rectangle([0, 0, W, 3], fill=ACCENT)
d.rectangle([64, 74, 69, 224], fill=ACCENT)

d.text((94, 64), 'QQ 群里的 AI 角色', font=f(MSB, 60), fill=WHITE)
d.text((96, 146), '被 @ 才说话 · 有长期记忆 · 会看图上网 · 会自己去 B 站刷视频',
       font=f(MS, 26), fill=GRAY)
d.text((96, 192), 'NapCat + OneBot v11 + DeepSeek Harness + 7 个自建 MCP',
       font=f(MS, 20), fill=DIM)

# 右上角仓库名
name = 'github.com/DengSSHS/qq-ai-bot'
repo_f = f(CON, 19)
d.text((W - 64 - d.textlength(name, font=repo_f), 40), name, font=repo_f, fill=DIM)

# 分隔线
d.line([64, 254, W - 64, 254], fill=BORDER, width=1)

stats = [
    ('607 MB', '常驻内存', WHITE),
    ('1~2 s', '单轮回复', WHITE),
    ('7', '自建 MCP', WHITE),
    ('0', '第三方依赖', GREEN),
]
x, y = 64, 276
num_f, lab_f = f(CONB, 38), f(MS, 20)
for val, lab, col in stats:
    d.text((x, y), val, font=num_f, fill=col)
    d.text((x, y + 50), lab, font=lab_f, fill=DIM)
    x += 285

img.save(os.path.join(OUT, 'banner.png'))
print('banner.png ok')

# ── architecture ──────────────────────────────────────────────
W2, H2 = 1240, 790
im = Image.new('RGB', (W2, H2), BG)
g = ImageDraw.Draw(im)

g.text((56, 36), '整条链路', font=f(MSB, 32), fill=WHITE)
g.text((56, 82), '从 QQ 消息到一次回复，中间只有四个环节', font=f(MS, 18), fill=DIM)

box_fb = f(MSB, 20)
small = f(MS, 17)
chip = f(CON, 16)
zh_chip = f(MS, 16)

CX, CW = 56, 700


def node(y, h, title_, desc, outline=BORDER, fill=PANEL, tcol=WHITE):
    rrect(g, [CX, y, CX + CW, y + h], 10, fill=fill, outline=outline)
    g.text((CX + 24, y + 16), title_, font=box_fb, fill=tcol)
    g.text((CX + 24, y + 46), desc, font=small, fill=GRAY)
    return y + h


def arrow(y_from, y_to, label):
    cx = CX + CW // 2
    g.line([cx, y_from, cx, y_to - 8], fill=BORDER, width=2)
    g.polygon([(cx - 7, y_to - 8), (cx + 7, y_to - 8), (cx, y_to)], fill=BORDER)
    g.text((cx + 14, (y_from + y_to) // 2 - 9), label, font=chip, fill=DIM)


y = 124
b = node(y, 74, 'QQ 群 / 私聊', '群友说话，或者你私聊下指令', outline=GREEN, tcol=GREEN)
arrow(b, b + 40, 'onebot v11')

b = node(b + 40, 74, 'NapCat', 'Docker 里的 QQ 协议端，反向 WebSocket 连上来',
         outline=ORANGE, tcol=ORANGE)
arrow(b, b + 40, 'WebSocket')

top = b + 40
ds_h = 190
rrect(g, [CX, top, CX + CW, top + ds_h], 12, fill=DEEP, outline=PURPLE)
g.text((CX + 24, top + 14), 'DSH 宿主（Node · systemd 常驻）', font=box_fb, fill=PURPLE)

inner = [
    ('dsh-onebot-qq 插件', '限流 / 白名单 / 注入 / 主动接话'),
    ('模型', '任意 OpenAI 兼容接口'),
    ('llm_shim', '强制关思考（快 4~5 倍）+ 计量'),
]
iy = top + 52
for t, s in inner:
    rrect(g, [CX + 24, iy, CX + CW - 24, iy + 38], 8, fill=PANEL, outline=BORDER)
    g.text((CX + 40, iy + 8), t, font=zh_chip, fill=WHITE)
    g.text((CX + 240, iy + 8), s, font=small, fill=GRAY)
    iy += 44

top2 = top + ds_h + 40
arrow(top + ds_h, top2, 'stdio')

# MCP 区：4 + 3 两排，统一宽度
mcps = [('memory', True), ('websurf', True), ('seer', True), ('imagen', True),
        ('sticker', True), ('admin', True), ('bili', False)]
mc_h = 168
rrect(g, [CX, top2, CX + CW, top2 + mc_h], 12, fill=DEEP, outline=ACCENT)
g.text((CX + 24, top2 + 12), '7 个 Python MCP（stdio，按需拉起）', font=box_fb, fill=ACCENT)

bw, gap = 152, 12
x0 = CX + 24
for i, (m, on) in enumerate(mcps):
    row, col = divmod(i, 4)
    bx = x0 + col * (bw + gap)
    by = top2 + 48 + row * 34
    rrect(g, [bx, by, bx + bw, by + 26], 6, fill=PANEL, outline=BORDER if on else DIM)
    centered(g, (bx, by + 4, bx + bw, by + 26), m, chip, WHITE if on else DIM)
g.text((x0, top2 + 122), '灰色 = 可选模块，不装不影响任何功能', font=zh_chip, fill=DIM)

# 右侧两栏
RX = CX + CW + 52
RW = W2 - RX - 56
rrect(g, [RX, 124, RX + RW, 124 + 300], 12, fill=PANEL, outline=BORDER)
g.text((RX + 24, 142), '性格层', font=box_fb, fill=WHITE)
g.text((RX + 24, 172), '让它不像机器人的部分', font=zh_chip, fill=DIM)
iy = 208
for it in ['说话节奏', '不做主持人', '不汇报操作', '记忆分层', '作息 / 什么时候闭嘴', '主动接话（有配额）']:
    g.ellipse([RX + 24, iy + 8, RX + 30, iy + 14], fill=GREEN)
    g.text((RX + 42, iy), it, font=zh_chip, fill=GRAY)
    iy += 34

rrect(g, [RX, 452, RX + RW, 452 + 260], 12, fill=PANEL, outline=BORDER)
g.text((RX + 24, 470), '运维层', font=box_fb, fill=WHITE)
g.text((RX + 24, 500), '改了 ≠ 生效了，而且通常不报错', font=zh_chip, fill=DIM)
iy = 536
for it in ['preflight.sh 静态检查', 'verify.sh 部署实况', '补丁漂移检测', 'MCP 健康 / 掉线看护', '日报邮件']:
    g.ellipse([RX + 24, iy + 8, RX + 30, iy + 14], fill=ACCENT)
    g.text((RX + 42, iy), it, font=zh_chip, fill=GRAY)
    iy += 34

im.save(os.path.join(OUT, 'architecture.png'))
print('architecture.png ok')
