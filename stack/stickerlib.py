#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表情库增强层（MCP / stdio）

为什么要有这个：插件的 `qq_random_sticker` 是纯 `Math.random()`——
4250 张里随机抽，热身期还行，用久了就两个毛病：

1. **没有"惯用表情"**。真人发图是偏心眼的，会反复甩那几张顺手的；
2. **连续撞图**。随机不记得刚发过什么，可能连着两条同一张。

插件其实已经记了 `hits`（使用次数）和 `lastUsed`（最后使用时间），
只是挑的时候不看。本工具补上这两件事，发送仍然走插件的 `qq_send_sticker`，
不重复实现发送逻辑。

工具
----
    sticker_pick(scene?, query?, n?)  -> 按话题 + 热度挑若干候选，返回 id/名字/文件
    sticker_ingest(path, scene, safe?) -> 入库前查重 + 安全判断，不合格只留标注
    sticker_stats()                    -> 场景分布 + 热度榜

依赖
----
只用标准库。pHash 感知去重要 Pillow（可选）：装了就启用，没装降级为
sha256 精确去重（现状），并在日志里说清楚。
"""
import hashlib
import json
import os
import random
import re
import sys
import time
import traceback

BASE = "/opt/dsh"
STICKERS = os.path.join(BASE, "media/onebot/stickers/stickers.json")
TAGS = os.path.join(BASE, "workspace/sticker_full_tags.json")
EXTRAS = os.path.join(BASE, "sticker-pack/extras.json")   # 本工具自己的附加数据
LOG = os.path.join(BASE, "memory/stickers.log")
PROTO = "2024-11-05"

# 加权随机的形状参数：
#   alpha 越大越偏向热门；0 = 纯随机。0.6 是"有惯用但不垄断"的折中。
#   recent_penalty 让刚发过的降权，避免连续撞图。
ALPHA = float(os.environ.get("STICKER_ALPHA", "0.6"))
RECENT_PENALTY = float(os.environ.get("STICKER_RECENT_PENALTY", "0.25"))
RECENT_WINDOW_S = 1800          # 30 分钟内发过的算"刚发过"

try:
    from PIL import Image          # 可选：pHash 需要
    HAS_PIL = True
except Exception:
    HAS_PIL = False


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%F %T"), msg))
    except Exception:
        pass


# ── 数据读写 ────────────────────────────────────────────────────────────
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_extras():
    return load_json(EXTRAS, {})


# ── 感知哈希（可选）────────────────────────────────────────────────────
# 感知哈希的分辨率：16x16 = 256 bit。
#
# 为什么不是默认的 8x8（实测，2026-10-24）：
#   这个库的图多为纯色底、大头、构图简单，8x8 的 DCT 低频特征容易退化。
#   抽 300 张跑 44850 对，d<=2 且比例一致的有 55 对（0.123%），抽出来看
#   全是不同的图：「初音-188」命中「袋鼠-ResizedImage」，「鲸鱼娘-118」命中
#   「鲸鱼娘-070」，还有一张「鲸鱼娘-186」同时命中 6 张不同的图——它的
#   phash 退化了，跟谁都“像”。
#   换 16x16 后同一批样本只剩 2 对（0.004%，降到 1/30），而「同图不同压缩/
#   缩放」的召回率没变：q50/q80/q95/缩 50%/缩 75% 五种变体，40 张图全部
#   在距离 <=2 内抓到（8x8 也是 100%）。
#   结论：16x16 白拿一个数量级的精度，不漏判。
PHASH_SIZE = 16


def phash(path, size=None, highfreq_factor=4):
    """感知哈希（DCT 版）。没有 Pillow 时返回 None。

    用途：抓"同一张图的不同压缩/分辨率版本"。sha256 抓不到这种，
    因为它们字节不同。

    返回定长十六进制串（size×size 位 / 4）。**长度必须固定**：
    早期用 "%016x" 写死 16 个字符，一旦调大 size 就会丢高位零、
    长度不固定，跟定长旧值比较时 hamming 会 zip 截断出完全错误的距离。
    """
    if not HAS_PIL:
        return None
    if size is None:
        size = PHASH_SIZE
    try:
        import math
        img_size = size * highfreq_factor
        im = Image.open(path)
        im = im.convert("L").resize((img_size, img_size), Image.LANCZOS)
        pixels = list(im.getdata())

        # 分离式 DCT-II，先横后竖（够用，图很小）
        rows = [pixels[i * img_size:(i + 1) * img_size] for i in range(img_size)]
        cos_map = [[math.cos((2 * x + 1) * u * math.pi / (2 * img_size))
                    for x in range(img_size)] for u in range(img_size)]
        tmp = []
        for row in rows:
            tmp.append([sum(row[x] * cos_map[u][x] for x in range(img_size))
                        for u in range(img_size)])
        dct = []
        for v in range(img_size):
            dct.append([sum(tmp[y][u] * cos_map[v][y] for y in range(img_size))
                        for u in range(img_size)])

        # 取左上低频 size×size，和均值比大小
        low = [dct[y][x] for y in range(size) for x in range(size)]
        avg = sum(low) / len(low)
        bits = "".join("1" if v > avg else "0" for v in low)
        return "%0*x" % ((size * size + 3) // 4, int(bits, 2))
    except Exception:
        return None


def hamming(a, b):
    """汉明距离。算不出或位数不一致时返回一个足够大的值（当成完全不同）。

    位数校验不是防御性写法，是必需的：extras.json 里可能同时存在用不同
    size 算的 phash（升级 PHASH_SIZE 后的新旧值）。直接 int() 异或的话，
    短的那边会被 zip 截断，算出"很像"的假距离。
    """
    a, b = str(a or ""), str(b or "")
    if not a or not b or len(a) != len(b):
        return 1 << 20          # 远超任何阈值，不会被当成重复
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 1 << 20


def image_ratio(path):
    """宽高比。pHash 单独用不可靠，必须拿比例做交叉校验（见下）。"""
    if not HAS_PIL:
        return None
    try:
        w, h = Image.open(path).size
        return round(w / float(h), 3) if h else None
    except Exception:
        return None


def find_near_duplicate(path, extras, stickers, threshold=2, ratio_tol=0.02):
    """找库里和这张图"看起来就是同一张"的条目。

    ⚠️ 实测教训（别把阈值调回 6）：这个库的图多为纯色底、大头、构图简单，
    DCT 低频特征容易退化。当初用「汉明距离 <= 6」时，随机 260 张里 5.74% 的
    组合被判为重复——抽出来看，「点赞」和「鲸鱼娘-691」距离 0，
    「塔菲」和「白子」距离 0，**全是不同的图**。

    所以判重必须加两个约束：
      1) 汉明距离 <= threshold（默认 2，很严）
      2) 宽高比差 <= ratio_tol（真正的低清版比例是一样的，不同图通常不同）

    而且即使命中，**也只是提示，不拦**——误挡一次就少一张表情，
    用户要的是"表情丰富"。真正拦只有 sha256 精确重复。

    加了比例校验后仍有约 0.09% 的误判（200 张随机样本、19900 对里 18 对，
    含「鲸鱼娘 vs 袋鼠」这种跨系列误判）。所以它只配当"提示"，不能当"判决"。
    """
    mine = phash(path)
    if mine is None:
        return None
    my_ratio = image_ratio(path)
    best = None
    for sid, meta in extras.items():
        stored = meta.get("phash")
        if not stored:
            continue
        d = hamming(mine, stored)
        if d > threshold:
            continue
        if my_ratio is not None:
            other = meta.get("ratio")
            if other is None:
                continue                      # 没比例数据就不敢判，宁可漏
            if abs(other - my_ratio) > ratio_tol:
                continue
        if best is None or d < best[1]:
            best = (sid, d)
    return best


# ── 挑图 ───────────────────────────────────────────────────────────────
def pick(args):
    scene = (args.get("scene") or "").strip()
    query = (args.get("query") or "").strip()
    n = int(args.get("n") or 3)
    n = max(1, min(n, 10))

    stickers = load_json(STICKERS, [])
    tags = load_json(TAGS, {})
    extras = load_extras()
    if not stickers:
        return "表情库读不到（stickers.json 为空或缺失）。"

    now = time.time()
    pool = []
    for s in stickers:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if not sid:
            continue
        meta = tags.get(sid) or {}
        s_scene = meta.get("scene") or ""
        if scene and scene not in s_scene:
            continue
        if query:
            hay = " ".join([str(s.get("name") or ""), s_scene,
                            str(meta.get("text") or "")])
            if query.lower() not in hay.lower():
                continue
        path = s.get("file") or ""
        if not path or not os.path.exists(path):
            continue

        hits = int(s.get("hits") or 0)
        weight = (hits + 1) ** ALPHA

        # 刚发过的降权：避免连着两条同一张
        last = extras.get(sid, {}).get("lastPick") or 0
        if now - last < RECENT_WINDOW_S:
            weight *= RECENT_PENALTY

        # 被标为不适合转发的直接排除
        if extras.get(sid, {}).get("safe") is False:
            continue

        pool.append((sid, s.get("name") or "", path, s_scene, weight))

    if not pool:
        return ("这个条件下一张都没有。scene=%r query=%r —— 换个说法，"
                "或者用 qq_random_sticker 直接随机。" % (scene, query))

    # 加权无放回抽样
    picked = []
    for _ in range(min(n, len(pool))):
        total = sum(p[4] for p in pool)
        r = random.random() * total
        acc = 0.0
        for i, p in enumerate(pool):
            acc += p[4]
            if r <= acc:
                picked.append(pool.pop(i))
                break
        else:
            picked.append(pool.pop())

    # 记录这次挑过（用于防连撞；真正的 hits 由插件 noteUsed 记）
    for sid, _, _, _, _ in picked:
        extras.setdefault(sid, {})["lastPick"] = now
    try:
        save_json(EXTRAS, extras)
    except Exception as e:
        log("save extras fail: %s" % e)

    out = []
    for sid, name, path, s_scene, _ in picked:
        tag = s_scene or "未标注（不传 scene 才挑得到）"
        out.append("- id=%s  名字=%s  场景=%s" % (sid, name, tag))
    body = chr(10).join(out)
    log("pick scene=%r query=%r -> %s" % (scene, query, [p[0] for p in picked]))
    return ("挑到 %d 张（按热度加权，已避开刚发过的）：%s%s"
            "选一张，用 qq_send_sticker(query=那个 id) 发出去。"
            % (len(picked), chr(10), body + chr(10)))


# ── 入库 ───────────────────────────────────────────────────────────────
RISKY_WORDS = ("身份证", "手机号", "银行卡", "地址", "门牌", "工牌", "学号",
               "病历", "快递单", "车牌", "密码", "验证码", "二维码")

SAFE_QUESTION = ("这张图适合当作 QQ 群聊里的表情包转发吗？"
                 "如果含有个人隐私（身份证、手机号、住址、工牌学号、聊天记录截图、"
                 "他人私密影像）或可能引起麻烦的内容，回答 NO 并简述理由；"
                 "如果只是普通梗图/表情/二次元图，回答 YES。"
                 "请以 YES 或 NO 开头，一句话内说完。")


def judge_safe(path):
    """让多模态模型判断"这张图适不适合在群里转发"。

    为什么不用关键词粗判：图里的敏感信息往往是**图片内容**（聊天截图、
    工牌、快递单），OCR 文字不一定命中，而模型看一眼就能判断。
    失败时返回 (None, 原因)，调用方据此决定"不入库"而不是"放行"。
    """
    try:
        import seer                      # 复用已有的看图 MCP 的调用逻辑
        ans, err = seer._ask_model(path, SAFE_QUESTION)
        if err:
            return None, err
        if not ans:
            return None, "模型没返回内容"
        head = ans.strip()[:80]
        upper = ans.strip().upper()
        if upper.startswith("YES") or ans.strip().startswith("是"):
            return True, head
        if upper.startswith("NO") or ans.strip().startswith("不"):
            return False, head
        # 模型没按格式答：从措辞里判
        if any(w in ans for w in ("不适合", "不宜", "隐私", "敏感", "违规")):
            return False, head
        if "适合" in ans:
            return True, head
        return None, head
    except Exception as e:
        return None, "判断失败: %s" % e


def ingest(args):
    path = (args.get("path") or "").strip()
    scene = (args.get("scene") or "").strip()
    safe = args.get("safe")
    ocr = (args.get("ocr") or "").strip()
    if not path:
        return "要给本地图片路径。"
    if not os.path.isfile(path):
        return "文件不存在：%s" % path
    # scene 不能空：空场景的条目在按话题挑图时会被整段跳过，
    # 等于"存了但永远发不出去"（服务器上已有 51 条踩过这个坑）。
    if not scene:
        return ("要给一个场景标签（哪怕填「杂图」）。"
                "空标签的图存进去也用不上——按话题挑的时候会被跳过。")

    stickers = load_json(STICKERS, [])
    extras = load_extras()

    # ① 精确去重（sha256，和插件一致）
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    for s in stickers:
        if isinstance(s, dict) and s.get("hash") == digest:
            return ("这张库里已经有了（id=%s 名字=%s），不重复入库。"
                    "直接 qq_send_sticker(query=%s) 就能发。"
                    % (s.get("id"), s.get("name"), s.get("id")))

    # ② 近似重复：命中就拦（可用 force=true 强收）。
    #
    #    以前是“只提示不拦”，因为 8x8 的 phash 在这个库上误拦率 0.123%（抽
    #    300 张 / 44850 对，55 对命中全是不同的图：初音命中袋鼠、鲸鱼娘-118
    #    命中鲸鱼娘-070）。换 16x16 后同一批样本只剩 2 对（0.004%），而“同图
    #    不同压缩/缩放”的召回没变。库已经 4244 张了，收进一张重复图的代价比
    #    误挡一张新图高——所以默认改成拦，但留 force 出口。
    near = find_near_duplicate(path, extras, stickers)
    if near and not args.get("force"):
        sid, dist = near
        nm = next((x.get("name") for x in stickers if x.get("id") == sid), sid)
        return ("这张和库里已有的很像（id=%s 名字=%s，感知哈希只差 %d 位），"
                "按同一张图处理，不入库。%s"
                "确实是另一张图（同系列但内容不同）就再调一次，传 force=true。"
                % (sid, nm, dist, chr(10)))

    # ③ 安全预筛：让模型看一眼，不问关键词。
    #    拿不到判断就**不入库**——赌输一次是它在别的场景甩出别人的隐私。
    reason = ""
    if safe is None:
        joined = " ".join([ocr, os.path.basename(path)])
        if any(w in joined for w in RISKY_WORDS):
            safe, reason = False, "文件名/文字里出现敏感词"
        else:
            safe, reason = judge_safe(path)
    if safe is None:
        return ("这张图没通过安全判断，先不入库。原因：%s%s"
                "（要强制入库就把 safe=true 显式传进来，但请自己看一眼图。）"
                % (reason, chr(10)))
    verdict = "适合转发" if safe else ("不适合转发：%s" % reason)

    # ④ 记账：只写附加元数据，不动插件的 stickers.json
    sid = digest[:10]
    extras.setdefault(sid, {})
    extras[sid].update({
        "phash": phash(path) or "",
        "ratio": image_ratio(path),
        "safe": safe,
        "scene": scene,
        "ingestedAt": int(time.time()),
        "src": path,
    })
    save_json(EXTRAS, extras)
    log("ingest %s scene=%r safe=%r phash=%s"
        % (os.path.basename(path), scene, safe, extras[sid].get("phash") or "-"))

    msg = ["已记录：id=%s 场景=%s 安全=%s" % (sid, scene or "未标注", verdict)]
    if safe is False:
        msg.append("标为不适合转发 → 已入库但不参与挑图（防止它在别的场景被甩出去）。")
    if not HAS_PIL:
        msg.append("（没装 Pillow，这次只做了 sha256 精确去重，"
                   "抓不到「同一张图的不同清晰度版本」。）")
    msg.append("要让插件真正收录，再调 qq_save_sticker(path=...) —— 那是插件的库。")
    return chr(10).join(msg)


# ── 统计 ───────────────────────────────────────────────────────────────
def stats(_args=None):
    stickers = load_json(STICKERS, [])
    tags = load_json(TAGS, {})
    extras = load_extras()
    if not stickers:
        return "表情库读不到。"

    total = len(stickers)
    used = [s for s in stickers if isinstance(s, dict) and int(s.get("hits") or 0) > 0]
    hits_sum = sum(int(s.get("hits") or 0) for s in stickers if isinstance(s, dict))

    by_scene = {}
    for s in stickers:
        if not isinstance(s, dict):
            continue
        sc = (tags.get(s.get("id")) or {}).get("scene") or "未标注"
        by_scene[sc] = by_scene.get(sc, 0) + 1

    top = sorted([s for s in stickers if isinstance(s, dict)],
                 key=lambda x: -int(x.get("hits") or 0))[:8]

    lines = ["库存 %d 张，用过的 %d 张（%.0f%%），累计发出 %d 次。"
             % (total, len(used), 100.0 * len(used) / max(1, total), hits_sum),
             "",
             "热度榜："]
    for s in top:
        if int(s.get("hits") or 0) == 0:
            break
        lines.append("  %2d 次  %s（%s）"
                     % (int(s.get("hits") or 0), s.get("name"),
                        (tags.get(s.get("id")) or {}).get("scene") or "未标注"))
    if len(used) < total * 0.1:
        lines.append("")
        lines.append("⚠️ 用过的不足 10%——「丰富」这件事还没发生，"
                     "问题不在库存，在她翻来覆去就挑那几张。")

    lines.append("")
    lines.append("场景分布（前 8）：")
    for sc, cnt in sorted(by_scene.items(), key=lambda kv: -kv[1])[:8]:
        lines.append("  %-14s %d" % (sc, cnt))

    lines.append("")
    lines.append("pHash 可用：%s" % ("是" if HAS_PIL else "否（只做 sha256 精确去重）"))
    lines.append("附加元数据条数：%d（%s）" % (len(extras), EXTRAS))
    return chr(10).join(lines)


# ── MCP 管道 ───────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "sticker_pick",
        "description": ("按话题和热度挑表情，比 qq_random_sticker 的纯随机更像人："
                        "常发的那几张更容易被挑中，刚发过的会被压一压。"
                        "挑完用 qq_send_sticker(query=id) 发出去。"
                        "scene 可填：卖萌可爱/嘲讽怼人/开心笑/委屈哭/无语尴尬/"
                        "生气炸毛/吃饿/疑问困惑/震惊离谱/赞同OK/忙工作/撒娇求/"
                        "告别晚安/打招呼/期待等待/拒绝不要/搞颜色或擦边/杂图 等"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scene": {"type": "string", "description": "话题/情绪筛选，可省略"},
                "query": {"type": "string", "description": "名字或图上文字的关键词，可省略"},
                "n": {"type": "integer", "description": "给几个候选，默认 3"},
            },
        },
    },
    {
        "name": "sticker_ingest",
        "description": ("把一张图记进表情库的附加层（不替代插件的 qq_save_sticker）。"
                        "入库前会查重（sha256 精确 + pHash 近似）和做安全预筛，"
                        "不适合转发的只留记录、不参与挑图。"
                        "pHash 近似命中会直接拒收（防止同图不同版本灌进来），"
                        "确实是另一张图就传 force=true。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "本地图片绝对路径"},
                "scene": {"type": "string", "description": "场景标签"},
                "safe": {"type": "boolean", "description": "是否适合转发（省略则按关键词粗判）"},
                "ocr": {"type": "string", "description": "图上文字，用于安全粗判"},
                "force": {"type": "boolean",
                          "description": "跳过近似重复拦截（确实和库里那张不是同一张才用）"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "sticker_stats",
        "description": "看表情库的家底：库存、用过多少、热度榜、场景分布。想知道自己是不是翻来覆去就发那几张时用。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

TOOLS.append({
    "name": "sticker_triage",
    "description": ("筛群友发来的图：判断每张值不值得收进表情库。"
                    "值得的复制到 pending/ 并给出场景，不值得的记理由后删掉。"
                    "默认只看不删（dry-run）；确定要执行就传 apply=true。"
                    "插件给附件 6 小时 TTL，所以过期的图不用管。"),
    "inputSchema": {
        "type": "object",
        "properties": {
            "apply": {"type": "boolean", "description": "true = 真的复制/删除；省略则只判断"},
            "limit": {"type": "integer", "description": "一次最多看几张，默认 20"},
        },
    },
})

HANDLERS = {
    "sticker_pick": pick,
    "sticker_ingest": ingest,
    "sticker_stats": stats,
}


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + chr(10))
    sys.stdout.flush()


def handle(req):
    m, mid, p = req.get("method"), req.get("id"), req.get("params") or {}
    if m == "initialize":
        reply(mid, {"protocolVersion": PROTO,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "stickerlib", "version": "1.0.0"}})
        return
    if m in ("notifications/initialized", "initialized"):
        return
    if m == "ping":
        reply(mid, {})
        return
    if m == "tools/list":
        reply(mid, {"tools": TOOLS})
        return
    if m == "tools/call":
        name = p.get("name")
        fn = HANDLERS.get(name)
        if fn is None:
            reply(mid, error={"code": -32602, "message": "unknown tool"})
            return
        try:
            out = fn(p.get("arguments") or {})
            reply(mid, {"content": [{"type": "text", "text": str(out)}], "isError": False})
        except Exception as e:
            log("tool fail %s: %s" % (name, traceback.format_exc()))
            reply(mid, {"content": [{"type": "text", "text": "出错：%s" % e}], "isError": True})
        return
    if mid is not None:
        reply(mid, error={"code": -32601, "message": "method not found"})


# ── 收图筛选：判断群友发的图值不值得收 ────────────────────────────────
MEDIA_DIR = os.path.join(BASE, "media/onebot")
TRIAGE = os.path.join(BASE, "sticker-pack/triage.jsonl")
PENDING = os.path.join(BASE, "sticker-pack/pending")
TRIAGE_MAX_AGE_H = 5.5          # 插件 TTL 是 6 小时，留半小时余量

TRIAGE_QUESTION = """这张图适合当作 QQ 群聊里的表情包素材收录吗？严格判断，宁缺毋滥。

判"值得收"：表达明确情绪或梗、构图完整、有转发价值（让人想转给别人）、文字清晰可读。
判"不值得"：聊天/通知/网页截图、纯文字、模糊或过曝、广告水印、别人的自拍或隐私、
看不出想表达什么、尺寸怪异的竖长截屏。

如果值得收，从下面这组固定标签里挑**一个**最贴的场景：
{scenes}

硬规则（不遵守会退化成「杂图」或「卖萌可爱」两个垃圾桶）：
1. 「卖萌可爱」只给**真的在卖萌**的（比心、蹭、眨眼、主动示好）。
   图里角色长得可爱但表达的是别的情绪，选那个情绪，不要选它。
2. 有明确情绪就一定选那个情绪标签，不许往「杂图」「卖萌可爱」里塞。
3. 只有纯风景、纯文字、设定图、logo 这类完全看不出情绪的才填「杂图」。

只输出 JSON：{{"verdict":"YES或NO","scene":"标签","reason":"不超过12字"}}
不值得收时 scene 填空字符串。不要输出任何其他内容，不要解释规则。"""


# 每个标签后面那句「适用条件」不是装饰：去掉它，「卖萌可爱」会吸走 65% 的图。
SCENE_HINTS = {
    "卖萌可爱": "撒娇比心蹭蹭，主动示好",
    "开心笑": "高兴、大笑、庆祝",
    "嘲讽怼人": "鄙视、阴阳、diss",
    "委屈哭": "难过、流泪、被欺负",
    "无语尴尬": "沉默、白眼、无话可说",
    "生气炸毛": "发火、愤怒、摔",
    "吃饿": "吃东西、馋、饿",
    "疑问困惑": "问号、疑惑、不解",
    "震惊离谱": "吃惊、难以置信",
    "赞同OK": "点头、同意、收到",
    "忙工作": "工作、学习、加班",
    "撒娇求": "求人、拜托、要东西",
    "告别晚安": "再见、睡觉、下线",
    "打招呼": "你好、在吗、招呼",
    "期待等待": "等、盼、蹲",
    "拒绝不要": "不要、拒绝、摆手",
    "搞颜色或擦边": "性暗示",
    "杂图": "以上都不是",
}


DEFAULT_SCENES = ["卖萌可爱", "嘲讽怼人", "开心笑", "委屈哭", "无语尴尬",
                  "生气炸毛", "吃饿", "疑问困惑", "震惊离谱", "赞同OK",
                  "忙工作", "撒娇求", "告别晚安", "打招呼", "期待等待",
                  "拒绝不要", "搞颜色或擦边", "杂图"]


def _scenes():
    """从库里统计实际用到的场景，再用默认表补足。

    补足的原因：如果库很小或数据异常（比如标签文件只剩一类），
    只给模型 1 个场景等于逼它全都归到那一类。prompt 里的候选越全，
    打标才越有意义。
    """
    names = []
    try:
        d = load_json(TAGS, {})
        import collections
        c = collections.Counter((v.get("scene") or "").strip() for v in d.values())
        names = [k for k, _ in c.most_common() if k]
    except Exception:
        pass
    for s in DEFAULT_SCENES:
        if s not in names:
            names.append(s)
    return names


def _scenes_with_hint():
    """带适用条件的场景表（给 prompt 用）。库里出现非标准场景名时只输出名字。"""
    out = []
    for s in _scenes():
        h = SCENE_HINTS.get(s)
        out.append("%s(%s)" % (s, h) if h else s)
    return out


def _clean_scene_name(s):
    """场景名清洗：容忍 `**卖萌可爱**`、`标签：卖萌可爱`、`卖萌可爱(撒娇)` 这类写法。

    长名优先匹配——否则「杂图」会抢先从任何带这两个字的句子里命中。
    """
    s = str(s or "").strip().strip("*`'\" \t\r\n")
    scenes = _scenes()
    if s in scenes:
        return s
    for cand in sorted(scenes, key=len, reverse=True):
        if cand in s:
            return cand
    return ""


def _verdict_of(s):
    """从一段文字里认判定词 → True / False / None（认不出）。

    只看最后一个词，因为模型可能输出 `一句话理由 YES` 这种把标签接在
    说明后面的形式。不匹配「不知道」「不错」——它们不是判定词。
    """
    t = str(s or "").strip().upper()
    for sep in (":", "："):
        t = t.replace(sep, " ")
    toks = [x for x in t.split() if x]
    cand = toks[-1] if toks else ""
    if not cand:
        return None
    if cand.startswith("YES") or cand.startswith("是"):
        return True
    if cand.startswith("NO") or cand.startswith("否"):
        return False
    if cand.startswith("不值得") or cand.startswith("不适合"):
        return False
    if cand.startswith("值得") or cand.startswith("适合"):
        return True
    return None


def _parse_triage_reply(ans):
    """解析判断结果 → (值得收?, 场景, 理由)。三层兜底，不抛异常。

    为什么要三层（2026-10-24 实测）：小样本串行跑时格式完美，并发跑几百张
    后模型开始抄 prompt 里的格式模板、加 markdown、加前缀、甚至拒绝回答。
    """
    txt = (ans or "").replace("```json", "").replace("```", "").strip()
    if not txt:
        return None, "", "模型返回空"

    obj = None
    i, j = txt.find("{"), txt.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(txt[i:j + 1])
        except Exception:
            obj = None

    if isinstance(obj, dict):
        v = _verdict_of(obj.get("verdict"))
        scene = _clean_scene_name(obj.get("scene"))
        reason = str(obj.get("reason") or "").strip()[:80] or txt[:60]
        if v is not None:
            return v, scene, reason
        # JSON 里没写清 verdict：往下走纯文本兜底

    # 兼容旧竖线格式 VERDICT|场景|理由。
    # 注意：模型的判定词不一定在第一段——实测见过
    # 「标签|一句话理由 YES|忙工作|加载中」这种把 prompt 模板抄在前面的写法。
    parts = [x.strip() for x in txt.split("|")]
    for idx, part in enumerate(parts):
        v = _verdict_of(part)
        if v is None:
            continue
        scene = _clean_scene_name(parts[idx + 1]) if idx + 1 < len(parts) else ""
        reason = parts[idx + 2] if idx + 2 < len(parts) else txt[:60]
        return v, scene, reason

    # 全崩了：从严当 NO，但把原文留在理由里方便人工复查
    return False, _clean_scene_name(txt), txt[:60].replace("\n", " ")


def _triage_one(path):
    """一次模型调用，返回 (值得收?, 场景, 理由)。"""
    try:
        import seer
        q = TRIAGE_QUESTION.replace("{scenes}", " / ".join(_scenes_with_hint()))
        ans, err = seer._ask_model(path, q)
        if err or not ans:
            return None, "", err or "模型没返回"
        return _parse_triage_reply(ans)
    except Exception as e:
        return None, "", "判断异常: %s" % e


def _file_sha(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _sticker_shas():
    """库内已有的 {sha256: id}。

    结算时先用它挡一道：一张已经入库的图又躺在 media/ 里（比如重跑测试
    留下的副本），不挡的话会：调一次模型判“值得收” → 复制进 pending →
    注入一轮让她收 → 她调 ingest 回“库里已经有了”。三份成本买一句空话。

    注意字段名是 `hash`（插件的写法），不是 `sha256`。
    """
    out = {}
    try:
        for s in load_json(STICKERS, []):
            if isinstance(s, dict) and s.get("hash"):
                out[s["hash"]] = s.get("id")
    except Exception:
        pass
    return out


def _triage_seen():
    """返回 {sha256: 判断记录}。

    按**内容哈希**索引而不是文件名：同一张图被不同人反复发，文件名每次都不同
    （media_<时间戳>_<随机>），按名字索引等于每次都得重新调模型判断一遍。
    按内容哈希，判过的图再出现直接复用结论。
    """
    seen = {}
    try:
        with open(TRIAGE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                key = d.get("sha")
                if key:
                    # 后来的记录覆盖前面的（比如 dry-run 后又有 apply 记录）
                    seen[key] = d
    except FileNotFoundError:
        pass
    return seen


def media_triage(args=None):
    """扫群友发来的图，判断值不值得收。

    背景：插件默认给消息附件 6 小时 TTL，过期自动删。所以判断必须在这窗口内做，
    否则图就没了。本工具做三件事：
      1. 判断（一次模型调用拿到 值得?/场景/理由）
      2. 值得的 → 复制到 sticker-pack/pending/，并告诉调用方去调 qq_save_sticker
      3. 不值得的 → 记一笔（triage.jsonl）然后删掉，不等 TTL
    apply=False 时只判断不动文件（默认）。
    """
    args = args or {}
    apply_ = bool(args.get("apply"))
    limit = int(args.get("limit") or 20)

    if not os.path.isdir(MEDIA_DIR):
        return "找不到 %s。" % MEDIA_DIR
    seen = _triage_seen()
    lib_shas = _sticker_shas()
    dup = 0
    now = time.time()

    # 先补做「dry-run 时判断过、但没执行动作」的那批（不重复调模型）
    fixed = 0
    if apply_:
        os.makedirs(PENDING, exist_ok=True)
        for sha, rec in list(seen.items()):
            if rec.get("applied") or rec.get("verdict") is None:
                continue
            fn = rec.get("file") or ""
            src = os.path.join(MEDIA_DIR, fn)
            if not fn or not os.path.isfile(src):
                continue                       # 已经被 TTL 清掉了
            try:
                if rec["verdict"] is True:
                    import shutil
                    shutil.copy2(src, os.path.join(PENDING, fn))
                    act = "copied-to-pending"
                else:
                    os.remove(src)
                    act = "deleted"
                with open(TRIAGE, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"sha": sha, "file": fn,
                                        "verdict": rec["verdict"],
                                        "scene": rec.get("scene", ""),
                                        "reason": rec.get("reason", ""),
                                        "ts": int(now), "applied": True,
                                        "action": act, "deferred_from": rec.get("ts")},
                                       ensure_ascii=False) + chr(10))
                fixed += 1
            except Exception as e:
                log("triage defer fail %s: %s" % (fn, e))

    todo = []
    reused = 0
    for fn in os.listdir(MEDIA_DIR):
        p = os.path.join(MEDIA_DIR, fn)
        if not os.path.isfile(p):
            continue
        if not fn.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            continue
        if fn.startswith("gen_"):        # 生成图不参与，那是她自己画的
            continue
        age_h = (now - os.path.getmtime(p)) / 3600.0
        if age_h > TRIAGE_MAX_AGE_H:      # 快过期了就别花调用去判
            continue
        sha = _file_sha(p)
        if not sha:
            continue
        if sha in lib_shas:
            # 已经在表情库里了：不用判、不用入库。拦在这里能省一次模型调用
            # 和一个注入回合（否则她会白调一次 ingest 然后回“已经有了”）。
            dup += 1
            continue
        if sha in seen:
            # 同内容的图判过 → 复用结论，不重复调模型
            rec = seen[sha]
            if apply_ and not rec.get("applied") and rec.get("verdict") is not None:
                try:
                    os.makedirs(PENDING, exist_ok=True)
                    if rec["verdict"] is True:
                        import shutil
                        shutil.copy2(p, os.path.join(PENDING, fn))
                        act = "copied-to-pending"
                    else:
                        os.remove(p)
                        act = "deleted"
                    with open(TRIAGE, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"sha": sha, "file": fn,
                                            "verdict": rec["verdict"],
                                            "scene": rec.get("scene", ""),
                                            "reason": rec.get("reason", ""),
                                            "ts": int(now), "applied": True,
                                            "action": act, "reused": True},
                                           ensure_ascii=False) + chr(10))
                    fixed += 1
                except Exception as e:
                    log("triage reuse fail %s: %s" % (fn, e))
            reused += 1
            continue
        todo.append((p, fn, sha))
    todo = todo[:limit]

    if not todo:
        parts = []
        if reused:
            parts.append("%d 张复用之前的判断（同内容不再调模型）" % reused)
        if dup:
            parts.append("%d 张库里已经有了，跳过" % dup)
        if fixed:
            parts.append("补执行 %d 张" % fixed)
        return "没有待判断的新图。" + (" " + "，".join(parts) + "。" if parts else "")

    good, bad, unsure = [], [], []
    for p, fn, sha in todo:
        worth, scene, reason = _triage_one(p)
        rec = {"sha": sha, "file": fn, "verdict": worth, "scene": scene,
               "reason": reason, "ts": int(now), "applied": False}
        if worth is True:
            good.append(rec)
        elif worth is False:
            bad.append(rec)
        else:
            unsure.append(rec)

        # 落盘：值得的进 pending，不值得的删（apply 时）
        if apply_:
            try:
                os.makedirs(PENDING, exist_ok=True)
                if worth is True:
                    import shutil
                    shutil.copy2(p, os.path.join(PENDING, fn))
                    rec["applied"] = True
                    rec["action"] = "copied-to-pending"
                elif worth is False:
                    os.remove(p)
                    rec["applied"] = True
                    rec["action"] = "deleted"
            except Exception as e:
                rec["action"] = "error: %s" % e
        with open(TRIAGE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + chr(10))

    head = "看了 %d 张（%s）" % (len(todo), "已执行" if apply_ else "仅判断，没动文件")
    if reused:
        head += "，另有 %d 张是判过的（直接复用结论）" % reused
    if dup:
        head += "，%d 张库里已有（跳过）" % dup
    if fixed:
        head += "，补执行 %d 张" % fixed
    out = [head + "："]
    if good:
        out.append("")
        out.append("值得收 %d 张：" % len(good))
        for r in good:
            out.append("  %s  [%s] %s" % (r["file"], r["scene"] or "未分类", r["reason"][:50]))
        out.append("  收的话调 qq_save_sticker(path=%s%s)" %
                   (PENDING, "/<文件名>"))
    if bad:
        out.append("")
        out.append("不值得收 %d 张%s：" % (len(bad), "（已删）" if apply_ else "（未删，看下面理由）"))
        for r in bad:
            out.append("  %s  %s" % (r["file"], r["reason"][:50]))
    if unsure:
        out.append("")
        out.append("拿不准 %d 张（模型没给准话，保留不动）：" % len(unsure))
        for r in unsure:
            out.append("  %s  %s" % (r["file"], r["reason"][:50]))
    if not apply_:
        out.append("")
        out.append("想真的执行（复制好的 / 删差的）就加 apply=true。")
    return chr(10).join(out)


def backfill(limit=0):
    """给库里存量图片补算 pHash。

    为什么需要：pHash 只能在图片能解码时算。库里的 4000+ 张是早先入库的，
    没有这个字段——不回填的话，"新图入库时查近似重复"就没东西可比。
    断点续跑：已算过的跳过。**但长度不对的不算「算过」**——升级 PHASH_SIZE
    后（8x8 → 16x16），旧值位数不同，跟新值比会直接返回「完全不同」，
    等于判重静默失效。所以这里按期望长度校验，自动重算旧格式。
    """
    stickers = load_json(STICKERS, [])
    extras = load_extras()
    if not stickers:
        print("stickers.json 为空")
        return 1
    total = len(stickers)
    want_len = (PHASH_SIZE * PHASH_SIZE + 3) // 4
    def _incomplete(s):
        if not isinstance(s, dict) or not s.get("file"):
            return False
        m = extras.get(s.get("id"), {})
        # phash 和 ratio 都要有：只有 phash 时判重会因缺比例而永远不敢判
        return (not m.get("phash") or len(m.get("phash") or "") != want_len
                or m.get("ratio") is None)
    todo = [s for s in stickers if _incomplete(s)]
    print("总数 %d，待回填 %d，已算 %d" % (total, len(todo), total - len(todo)))
    if limit:
        todo = todo[:limit]

    done = failed = 0
    t0 = time.time()
    for i, s in enumerate(todo, 1):
        sid, path = s.get("id"), s.get("file")
        if not sid or not os.path.exists(path):
            failed += 1
            continue
        h = phash(path)
        if h:
            extras.setdefault(sid, {})["phash"] = h
            extras[sid]["ratio"] = image_ratio(path)
            done += 1
        else:
            failed += 1
        if i % 200 == 0:
            save_json(EXTRAS, extras)          # 分批落盘，中断也不白跑
            print("  %d/%d  已算 %d  失败 %d  %.0fs"
                  % (i, len(todo), done, failed, time.time() - t0), flush=True)
    save_json(EXTRAS, extras)
    print("完成：算 %d，失败 %d，耗时 %.0fs" % (done, failed, time.time() - t0))
    log("backfill done=%d failed=%d total=%d" % (done, failed, total))
    return 0


# media_triage 定义在文件后半部分，这里补注册（避免前向引用）
HANDLERS["sticker_triage"] = media_triage


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--settle":
        # 凌晨结算：判断 + 执行 + 写日报。limit 放大，一次清完当天积压。
        out = media_triage({"apply": True, "limit": 200})
        try:
            with open(os.path.join(BASE, "memory/sticker-daily.log"),
                      "a", encoding="utf-8") as f:
                f.write("%s%s%s%s" % (time.strftime("%F %T"), chr(10), out, chr(10) * 2))
        except Exception as e:
            log("settle log fail: %s" % e)
        print(out)
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--triage":
        apply_ = "--apply" in sys.argv
        lim = 20
        for a in sys.argv[2:]:
            if a.isdigit():
                lim = int(a)
        print(media_triage({"apply": apply_, "limit": lim}))
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--backfill":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        sys.exit(backfill(limit))
    log("start pid=%d pil=%s alpha=%s" % (os.getpid(), HAS_PIL, ALPHA))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception:
            log("crash: %s" % traceback.format_exc())


if __name__ == "__main__":
    main()
