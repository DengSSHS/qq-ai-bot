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
def phash(path, size=8, highfreq_factor=4):
    """感知哈希（DCT 版）。没有 Pillow 时返回 None。

    用途：抓"同一张图的不同压缩/分辨率版本"。sha256 抓不到这种，
    因为它们字节不同。
    """
    if not HAS_PIL:
        return None
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
        return "%016x" % int(bits, 2)
    except Exception:
        return None


def hamming(a, b):
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 64


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
        out.append("- id=%s  名字=%s  场景=%s" % (sid, name, s_scene or "未标注"))
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

    # ② 近似重复：只提示，不拦（见 find_near_duplicate 里的实测教训）
    near = find_near_duplicate(path, extras, stickers)
    near_note = ""
    if near:
        sid, dist = near
        near_note = (chr(10) + "注意：图库里的 id=%s 和这张很像（感知哈希差 %d 位、"
                     "宽高比也一致），可能是同一张的不同清晰度版本。"
                     "你要是想省空间就别存了；不确定就存着。" % (sid, dist))

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
    if near_note:
        msg.append(near_note)
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
                        "不适合转发的只留记录、不参与挑图。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "本地图片绝对路径"},
                "scene": {"type": "string", "description": "场景标签"},
                "safe": {"type": "boolean", "description": "是否适合转发（省略则按关键词粗判）"},
                "ocr": {"type": "string", "description": "图上文字，用于安全粗判"},
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


def backfill(limit=0):
    """给库里存量图片补算 pHash。

    为什么需要：pHash 只能在图片能解码时算。库里的 4000+ 张是早先入库的，
    没有这个字段——不回填的话，"新图入库时查近似重复"就没东西可比。
    断点续跑：已算过的跳过。
    """
    stickers = load_json(STICKERS, [])
    extras = load_extras()
    if not stickers:
        print("stickers.json 为空")
        return 1
    total = len(stickers)
    def _incomplete(s):
        if not isinstance(s, dict) or not s.get("file"):
            return False
        m = extras.get(s.get("id"), {})
        # phash 和 ratio 都要有：只有 phash 时判重会因缺比例而永远不敢判
        return not m.get("phash") or m.get("ratio") is None
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


def main():
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
