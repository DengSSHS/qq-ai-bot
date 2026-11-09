#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""追番器：把一集**完整**看完，断点续传（2026-11-09）

为什么需要它：
  · 她真正的追番应该是「正片第 N 集」，不是别人拆解的投稿（之前搜 "MyGO"
    出来的全是泛式的解说、意识形态分析这类二创）。
  · 单集 1430 秒 < 时长上限 1800 秒，技术上读得下；但一次跑 15 分钟会卡住
    调用方、中断就得从零重来。所以这里按窗切片、每片落盘、记进度，
    随时能被杀掉、下次从断点继续。
  · 13 集不可能一口气（约 3.2 小时 CPU）。它的设计就是「一晚一集」。

产出（都在 /opt/tulpa/data/bilibili/watch/<ss>/ 下）：
  ep<N>.jsonl     每行 {t0, t1, text}：带绝对时间码的台词
  ep<N>.md        人看的完整台词稿（含时间码）
  ep<N>.progress  断点（已处理到第几秒）
  ep<N>.frames/   每个窗口中间时刻的一张画面（证明看的是正片，不是音频空转）

用法：
  /opt/tulpa/.venv/bin/python bili_watch_episode.py --season 73077 --ep 1
  ... --ep 1 --window 300 --threads 2      # 调整切片与线程
  ... --ep 1 --status                      # 只看进度
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, "/opt/tulpa")

from tulpa_bilibili.content import Content          # noqa: E402
from tulpa_bilibili.net import SafeHTTP             # noqa: E402
from tulpa_bilibili.settings import Settings        # noqa: E402

ROOT = "/opt/tulpa/data/bilibili"
WATCH = os.path.join(ROOT, "watch")
FFMPEG = "/opt/tulpa/.venv/lib/python3.13/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
WHISPER = "/opt/tulpa/tools/whispercpp/bin/whisper-cli"
MODEL = "/opt/tulpa/.cache/voice-models/ggml-small-q5_1.bin"


def classify_head(p, season, ep, rows):
    """判断开头 0–90 秒到底是什么：片头曲 / 上集回放 / 直接进剧情。

    为什么要分：4-gram 重合会把「片头曲」误判成「上集回放」——
    每集的 OP 是同一首歌，歌词当然和上一集高度重合（实测 ep3 对 ep2 是 47%）。
    真正的回放素材来自上一集**正片中间**，而 OP 会出现在每集的**同一位置**。
    所以多比一次「同位置」：
      · 与上集同位置(0–90s) 重合 ≥50% → 是同一首片头曲
      · 否则与上集整片重合 ≥30%     → 判为上集回放
    """
    if ep <= 1 or not rows:
        return dict(kind="无（第 1 集）", until=0, ratio=0.0)
    prev_p = os.path.join(os.path.dirname(p["jsonl"]), "ep%d.jsonl" % (ep - 1))
    if not os.path.exists(prev_p):
        return dict(kind="无法比对（上一集没转写）", until=0, ratio=0.0)
    try:
        prows = [json.loads(x) for x in io.open(prev_p, encoding="utf-8") if x.strip()]
    except Exception:
        return dict(kind="无法比对（上一集稿损坏）", until=0, ratio=0.0)
    if not prows:
        return dict(kind="无法比对", until=0, ratio=0.0)

    head = _norm("".join(r["text"] for r in rows if float(r["t0"]) < 90))
    if len(head) < 40:
        return dict(kind="开头几乎没台词", until=0, ratio=0.0)
    g = {head[i:i + 4] for i in range(len(head) - 3)}

    prev_head = _norm("".join(r["text"] for r in prows if float(r["t0"]) < 90))
    pg = {prev_head[i:i + 4] for i in range(len(prev_head) - 3)}
    same_pos = (sum(1 for x in g if x in pg) / max(1, len(g))) if pg else 0.0

    prev_all = _norm("".join(r["text"] for r in prows))
    anywhere = sum(1 for x in g if x in prev_all) / max(1, len(g))

    # 文本分不清「片头曲」和「上集回放」：实测 ep3 开头是 OP（画面里滚着制作人员表），
    # 但因为 ep2 正片里有一场同一首歌的 live，歌词 4-gram 重合 28%，会被误判成回放。
    # 所以文本只用来给"重合率"，是否成立交给画面证据（见 look_head）。
    if same_pos >= 0.5:
        return dict(kind="片头曲（与上一集同一首）", until=0, ratio=round(same_pos, 3))
    if anywhere >= 0.30:
        return dict(kind="与上一集有重合，但需看画面才能确认是回放还是剧情里的同一场演出",
                    until=0, ratio=round(anywhere, 3), candidate_recap=True)
    return dict(kind="直接进剧情", until=0, ratio=round(anywhere, 3))


def export_only(p, info, ep, season):
    """只重做收尾：去重 + 回放判定 + 写 md（不重新转写）。"""
    if not os.path.exists(p["jsonl"]):
        say("没有 jsonl，无法重做")
        return
    rows = [json.loads(x) for x in io.open(p["jsonl"], encoding="utf-8") if x.strip()]
    rows.sort(key=lambda r: r["t0"])
    rows = dedupe(rows)
    rows, looped = collapse_loops(rows)
    if looped:
        say("折叠循环重复 %d 句（打转段只保留一次）" % looped)
    # 过滤后的结果也写回 jsonl，否则 md 和 jsonl 两边对不上（一个是 389 行，一个是 405 行）
    with io.open(p["jsonl"], "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    head = classify_head(p, season, ep, rows)
    recap_until = head["until"]
    duration = float(info["duration_seconds"])
    with io.open(p["md"], "w", encoding="utf-8") as f:
        f.write("# %s · 第 %d 集\n\n" % (info["title"], ep))
        f.write("时长 %.0f 秒；台词 %d 句；来源：本地 whisper 转写（可能误听）。\n"
                % (duration, len(rows)))
        f.write(NAME_GLOSSARY + "\n")
        if looped:
            f.write("（本集折叠了 %d 句打转重复；时间轴上仍有覆盖）\n" % looped)
        gaps = find_gaps(rows, duration=duration)
        if gaps:
            f.write("以下时间段没有可信转写（片头/片尾曲、纯音乐或静场）——"
                    "歌词在画面里，用 bili_frame_grab(range_start=, range_end=) 看："
                    + "、".join("%d–%ds" % (g[0], g[1]) for g in gaps[:8]) + "\n")
        f.write("开头说明：不做自动判定。每集 OP 位置不同，而且 OP 前面常有一小段冷开场正片"
                "（用户 2026-11-09 指出）；与上一集的台词重合率 %.0f%% 仅供参考。\n"
                % (head["ratio"] * 100))
        f.write("\n")
        for r in rows:
            mark = "（上集回放）" if recap_until and r["t0"] < recap_until else ""
            f.write("[%02d:%02d] %s%s\n" % (r["t0"] // 60, r["t0"] % 60, r["text"], mark))
    meta = {}
    try:
        meta = json.loads(io.open(p["meta"], encoding="utf-8").read())
    except Exception:
        pass
    meta.update(title=info["title"], duration=duration, ep=ep, season=season,
                recap_until=recap_until, head_kind=head["kind"],
                head_overlap=head["ratio"], lines=len(rows))
    io.open(p["meta"], "w", encoding="utf-8").write(json.dumps(meta, ensure_ascii=False))
    say("重做完成：%d 句，回放标记 %s → %s" % (len(rows), recap_until or "无", p["md"]))


# ==== 2026-11-09 人名归一（用户指出「灯有两个名字，祥子两个拼法」）====
# 统一成日语片段里最常见的那一种写法，中文译名另附词汇表，方便她对照。
NAME_MAP = [
    ("トモリ", "ともり"), ("燈", "灯"), ("高町さん", "高松さん"),
    ("サキちゃん", "さきちゃん"), ("咲希", "祥子"),
    ("アノン", "あのん"), ("愛音", "あのん"),
    ("ソヨ", "そよ"), ("爽世", "そよ"),
    ("タキ", "たき"), ("立希", "たき"),
    ("ラナ", "らな"), ("楽名", "楽奈"),
]

NAME_GLOSSARY = """人名对照（转写里统一用左边日语写法）：
  ともり = 高松灯 / 高松燈
  さきちゃん・祥子 = 丰川祥子 / 豊川祥子
  あのん = 千早爱音 / 千早愛音
  そよ = 长崎爽世 / 長崎そよ
  たき = 椎名立希
  らな = 要乐奈 / 要楽奈
"""


def normalize_names(text):
    """把同一个角色的多种转写合成一种。长的先替换，避免「サキちゃん」被「サキ」切碎。"""
    out = str(text or "")
    for src, dst in sorted(NAME_MAP, key=lambda x: -len(x[0])):
        if src in out:
            out = out.replace(src, dst)
    return out


MUSIC_ONLY = re.compile(r"^[\[〔【(（]?\s*[音음]楽\s*[\]〕】)）]?$")


def collapse_loops(rows, run_min=4, ratio=0.75):
    """把「同一句循环刷屏」折成一句（用户指出：中间到片尾会卡住重复，最要命）。

    whisper 在音乐/静音段偶尔会陷进复读，且每次措辞略有差别，
    精确去重抓不到，所以用相似度串连。返回 (rows, 折叠掉的句数)。
    """
    import difflib
    out, dropped, i = [], 0, 0
    while i < len(rows):
        j = i + 1
        while j < len(rows) and difflib.SequenceMatcher(
                None, rows[i]["text"], rows[j]["text"]).ratio() > ratio:
            j += 1
        if j - i >= run_min:
            kept = dict(rows[i])
            kept["looped"] = j - i
            kept["t1"] = rows[j - 1]["t1"]
            out.append(kept)
            dropped += j - i - 1
            i = j
        else:
            out.append(rows[i])
            i += 1
    return out, dropped


def find_gaps(rows, min_gap=15.0, duration=None):
    """找出「没有可信转写」的时间段（片头曲、片尾曲、纯音乐、静场）。

    用户指出「片头片尾只有配乐，没歌词」——实测 whisper 在这些段落只给 (音楽)
    或干脆沉默，而画面里是花体歌词 + 演职员表，OCR 也读不出来（试过 tesseract）。
    所以这里不假装能识字，只如实标出空段，让她去看画面。
    """
    gaps = []
    if not rows:
        return gaps
    if duration and rows[0]["t0"] > min_gap:
        gaps.append([0.0, rows[0]["t0"]])
    for a, b in zip(rows, rows[1:]):
        if b["t0"] - a["t1"] >= min_gap:
            gaps.append([round(a["t1"], 1), round(b["t0"], 1)])
    if duration and duration - rows[-1]["t1"] >= min_gap:
        gaps.append([round(rows[-1]["t1"], 1), round(duration, 1)])
    return gaps


def dedupe(rows):
    """合并连续重复：whisper 打转时会连着吐同一句，只留第一次并记次数。"""
    out = []
    for r in rows:
        text = normalize_names((r.get("text") or "").strip())
        if MUSIC_ONLY.match(text):          # 纯 "[音楽]" 行没有信息量，丢掉
            continue
        # 音乐段会被 whisper 转成和弦/拉丁字母噪音（实测 ep6 开头：
        # 「B E Major 7 B E Major 7 B E F sharp F sharp」「B7…E… F賞…」）。
        # 判据：几乎没有日文假名/汉字，且拉丁字母占比很高 → 不是台词。
        _jp = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]", text))
        _lat = len(re.findall(r"[A-Za-z]", text))
        if _lat >= 3 and _jp <= 1 and _lat >= len(text) * 0.3:
            continue
        # 退化行：同一个字重复 8 次以上（片头音乐常被转成「ハッハッハッ…」），
        # 或者去掉括号后不到 2 个字符 —— 没有信息量
        if len(text) >= 8 and len(set(text)) <= 2:
            continue
        if len(re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]", "", text)) < 2:
            continue
        if out and text and text == (out[-1].get("text") or "").strip():
            out[-1]["repeat"] = out[-1].get("repeat", 1) + 1
            out[-1]["t1"] = r.get("t1") or out[-1].get("t1")
            continue
        out.append(dict(r))
    return out


def _norm(t):
    return "".join(ch for ch in str(t or "") if ch.isalnum())


def detect_recap(p, season, ep, lines):
    """开头是不是上一集的回放：拿本集前 90 秒的台词，和上一集最后 120 秒比字面重合。"""
    if ep <= 1 or not lines:
        return 0
    prev = os.path.join(os.path.dirname(p["jsonl"]), "ep%d.jsonl" % (ep - 1))
    if not os.path.exists(prev):
        return 0
    try:
        prows = [json.loads(x) for x in io.open(prev, encoding="utf-8") if x.strip()]
    except Exception:
        return 0
    if not prows:
        return 0
    # 和上一集整片比：回放取的是上集高潮画面，可能在中段而不在片尾
    tail = _norm("".join(r.get("text", "") for r in prows))
    head = _norm("".join(r.get("text", "") for r in lines if float(r.get("t0") or 0) < 90))
    if len(head) < 40 or len(tail) < 40:
        return 0
    # 用 4-gram 重合率，比整串包含更稳（回放会有轻微转写差异）
    grams = {head[i:i + 4] for i in range(len(head) - 3)}
    hit = sum(1 for g in grams if g in tail)
    ratio = hit / max(1, len(grams))
    return 90 if ratio >= 0.30 else 0


def say(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def paths(season, ep):
    d = os.path.join(WATCH, "ss%d" % season)
    os.makedirs(d, exist_ok=True)
    # 媒体必须按集存！以前是 ss 目录下一份 audio.m4a，ensure_media 见文件存在就跳过，
    # 结果第 2 集开始全部转的都是第 1 集的音轨（实测 ep2/ep3 的 jsonl md5 完全相同）。
    return dict(dir=d,
                audio=os.path.join(d, "ep%d.audio.m4a" % ep),
                video=os.path.join(d, "ep%d.video.mp4" % ep),
                jsonl=os.path.join(d, "ep%d.jsonl" % ep),
                md=os.path.join(d, "ep%d.md" % ep),
                prog=os.path.join(d, "ep%d.progress" % ep),
                meta=os.path.join(d, "ep%d.meta.json" % ep),
                frames=os.path.join("/opt/dsh/workspace/bili-frames/ss%d" % season, "ep%d" % ep))


def read_progress(p):
    try:
        return float(io.open(p["prog"], encoding="utf-8").read().strip())
    except Exception:
        return 0.0


def write_progress(p, seconds):
    tmp = p["prog"] + ".tmp"
    io.open(tmp, "w", encoding="utf-8").write("%.2f" % seconds)
    os.replace(tmp, p["prog"])


def ensure_media(info, p):
    """音轨和视频都要：转写用音轨，抽帧用视频（音频流里没有画面）。"""
    need = [k for k in ("audio", "video")
            if not (os.path.exists(p[k]) and os.path.getsize(p[k]) > 500_000)]
    if not need:
        return
    c = Content(Settings(ROOT), net=SafeHTTP())
    streams = c.play(info)
    for kind in need:
        url = streams.get(kind)
        if not url:
            raise SystemExit("这一集缺少 %s 流，无法完整处理" % kind)
        SafeHTTP().get(url, purpose="media", output=p[kind],
                       max_bytes=256 * 1024 * 1024)
        say("%s 下好 %.1fMB" % (kind, os.path.getsize(p[kind]) / 1e6))


def transcribe_window(audio, start, length, tag, threads):
    wav = "/tmp/_watch_%s.wav" % tag
    r = subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-ss", str(start), "-t", str(length),
                        "-i", audio, "-vn", "-ac", "1", "-ar", "16000", "-y", wav],
                       capture_output=True, timeout=180)
    if not os.path.exists(wav) or os.path.getsize(wav) < 1000:
        return []
    out = "/tmp/_watch_%s" % tag
    # -mc 0：不继承上文 → 片尾音乐不会再"复读上一句"（实测 ep1 尾段复读 25 次）
    # 注意**不要**加 -sns：它按"非语音 token"过滤，会把整段笑声/拟声压没——
    # 实测 ep3 的 900–1200 秒是笑场戏，加了 -sns 直接 0 句，去掉后 81 句。
    # 纯 [音楽] 标记在下面用 MUSIC_ONLY 过滤，不要交给引擎。
    subprocess.run([WHISPER, "-m", MODEL, "-f", wav, "-l", "ja", "-t", str(threads),
                    "-mc", "0",
                    "-oj", "-of", out, "-np"], capture_output=True, timeout=1800)
    os.unlink(wav)
    try:
        data = json.load(io.open(out + ".json", encoding="utf-8"))
    except Exception:
        return []
    rows = []
    for item in data.get("transcription") or []:
        text = str(item.get("text") or "").strip()
        off = item.get("offsets") or {}
        if not text:
            continue
        rows.append(dict(t0=round(start + (off.get("from") or 0) / 1000.0, 2),
                         t1=round(start + (off.get("to") or 0) / 1000.0, 2),
                         text=text))
    return rows


def grab_frame(audio_video, stamp, out_path):
    r = subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-ss", str(stamp), "-i", audio_video,
                        "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "4", "-y", out_path],
                       capture_output=True, timeout=90)
    return os.path.exists(out_path) and os.path.getsize(out_path) > 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="73077", help="番剧 ss 号（不带 ss 前缀）")
    ap.add_argument("--ep", type=int, default=1, help="第几集（从 1 开始）")
    # 默认 180 秒：300 秒时第一段窗口就要跑 5 分钟音频（约 3 分钟 CPU），
    # 一旦有别的任务抢 CPU（实测被拖到 896 秒），外部看到的就是"一直 0%"。
    ap.add_argument("--window", type=int, default=180, help="每片多少秒，默认 180")
    ap.add_argument("--threads", type=int, default=2, help="whisper 线程数")
    ap.add_argument("--status", action="store_true", help="只看进度")
    ap.add_argument("--recheck", action="store_true",
                    help="不重新转写，只重跑去重/回放标记/导出 md")
    ap.add_argument("--redo", action="store_true",
                    help="重转这一集（换了参数/修了 bug 之后用）")
    args = ap.parse_args()

    p = paths(int(args.season), args.ep)
    if args.redo:
        for key in ("jsonl", "prog", "md"):      # 键名是 prog（不是 progress）
            target = p.get(key)
            if target and os.path.exists(target):
                os.unlink(target)
        say("已清空第 %d 集的旧转写，从头重来" % args.ep)
    c = Content(Settings(ROOT), net=SafeHTTP())
    info = c.info("ss%s" % args.season, args.ep)
    duration = float(info["duration_seconds"])
    done = read_progress(p)

    if args.status:
        print(json.dumps(dict(title=info["title"], ep=args.ep, duration=duration,
                              done_until=done, percent=round(done / duration * 100, 1),
                              lines=sum(1 for _ in io.open(p["jsonl"], encoding="utf-8"))
                              if os.path.exists(p["jsonl"]) else 0), ensure_ascii=False))
        return 0

    # 记下标题/时长，供 bili_watch_status 直接读（不用再联网查）
    io.open(p["meta"], "w", encoding="utf-8").write(json.dumps(
        dict(title=info["title"], duration=duration, ep=args.ep,
             season=int(args.season), ep_id=info.get("ep_id")), ensure_ascii=False))

    if args.recheck:
        export_only(p, info, args.ep, int(args.season))
        return 0

    say("第 %d 集《%s》 %.0f 秒，已看到 %.0f 秒（%.0f%%）"
        % (args.ep, info["title"], duration, done, done / duration * 100))
    ensure_media(info, p)

    while done < duration - 1:
        length = min(args.window, duration - done)
        tag = "ep%d_%d" % (args.ep, int(done))
        t0 = time.time()
        rows = transcribe_window(p["audio"], done, length, tag, args.threads)
        if not rows:
            say("  这一窗口没出句子，加长重试一次")
            rows = transcribe_window(p["audio"], done, length, tag + "r", args.threads)
        with io.open(p["jsonl"], "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.makedirs(p["frames"], exist_ok=True)
        got = grab_frame(p["video"], done + length / 2,
                         os.path.join(p["frames"], "t%05d.jpg" % int(done + length / 2)))
        done += length
        write_progress(p, done)
        left_s = (duration - done) / max(1e-6, length / max(1.0, time.time() - t0))
        say("  %3.0f%%  [%d–%ds] %d 句  用时 %.0fs  预计还剩 %.0f 分钟  画面:%s"
            % (done / duration * 100, done - length, done, len(rows), time.time() - t0,
               left_s / 60, "有" if got else "无"))

    # 收尾：去重 + 标回放 + 导出人看的台词稿
    if not os.path.exists(p["jsonl"]):
        say("没有产生任何台词（转写窗口没跑或全部失败）——先查 /tmp 日志")
        return 1
    lines = [json.loads(x) for x in io.open(p["jsonl"], encoding="utf-8") if x.strip()]
    lines.sort(key=lambda r: r["t0"])
    lines = dedupe(lines)
    lines, looped = collapse_loops(lines)
    if looped:
        say("折叠循环重复 %d 句" % looped)
    head = classify_head(p, int(args.season), args.ep, lines)
    recap_until = head["until"]
    say("开头判定：%s（与上一集重合 %.0f%%）" % (head["kind"], head["ratio"] * 100))

    meta = json.loads(io.open(p["meta"], encoding="utf-8").read())
    meta["recap_until"] = recap_until
    meta["lines"] = len(lines)
    io.open(p["meta"], "w", encoding="utf-8").write(json.dumps(meta, ensure_ascii=False))
    with io.open(p["md"], "w", encoding="utf-8") as f:
        f.write("# %s · 第 %d 集\n\n" % (info["title"], args.ep))
        f.write("时长 %.0f 秒；台词 %d 句；来源：本地 whisper 转写（可能误听）。\n\n"
                % (duration, len(lines)))
        for r in lines:
            mark = "（上集回放）" if recap_until and r["t0"] < recap_until else ""
            f.write("[%02d:%02d] %s%s\n" % (r["t0"] // 60, r["t0"] % 60, r["text"], mark))
    say("完成：%d 句台词 → %s" % (len(lines), p["md"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
