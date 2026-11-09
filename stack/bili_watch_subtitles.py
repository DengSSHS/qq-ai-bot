#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""番剧字幕 OCR：用视觉模型读画面硬字幕（2026-11-09）

为什么换掉 whisper：
  这台机器只有 2 核。whisper small 单集要 16 分钟、把 CPU 吃满，而且快语速对白
  只能听清三分之一（用户原话）。而番剧是**硬字幕**，画面里就是原文——
  直接读画面比听写准得多，代价是 token（用户明确同意用 token 换准确度）。

实测（ep4）：
  裁到字幕带后 272 token/帧（整帧 912），单帧 0.6–0.9 秒
  输出直接是双语： 「真的要让这家伙加入吗 / ホントに入れんの こいつ」

做法：
  · 每 step 秒（默认 2 秒）抓一帧，只裁底部字幕带 → OCR → 收集 (时刻, 文本)
  · 连续相同的文本合并成一条（字幕停留 2–4 秒是常态），得到「文本 + 起止时间」
  · 8 路并发；总 token 记账写进 memory/subtitle-ocr.log

产出与 whisper 版兼容：ep<N>.jsonl（{t0,t1,text}）+ ep<N>.md + ep<N>.meta.json
所以 bili_watch_read / bili_frame_grab 不用改。

用法：
    /opt/tulpa/.venv/bin/python bili_watch_subtitles.py --season 73077 --ep 4
    ... --ep 4 --step 2 --workers 8 --max-minutes 200
"""
import argparse
import base64
import concurrent.futures as cf
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

FF = "/opt/tulpa/.venv/lib/python3.13/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
TULPA = "/opt/tulpa"
WATCH = "/opt/tulpa/data/bilibili/watch"
QWEN_ENV = "/opt/dsh/qwen.env"
LOG = "/opt/dsh/memory/subtitle-ocr.log"

PROMPT = "读出图中的字幕文字。只输出字幕本身，一行一条；没有文字输出 NONE。"


def say(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def api_key():
    for ln in io.open(QWEN_ENV, encoding="utf-8"):
        if ln.startswith("QWEN_API_KEY="):
            return ln.split("=", 1)[1].strip()
    raise SystemExit("qwen.env 里没有 QWEN_API_KEY")


KEY = api_key()


def log(msg):
    try:
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%F %T"), msg))
    except Exception:
        pass


def ocr_frame(path, model="qwen3.5-ocr", timeout=120):
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    body = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}],
        "temperature": 0}
    req = urllib.request.Request("http://127.0.0.1:8890/v1/chat/completions",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + KEY,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    tokens = int((d.get("usage") or {}).get("total_tokens") or 0)
    return text.strip(), tokens


def grab(video, t, path, width=640):
    subprocess.run([FF, "-nostdin", "-v", "error", "-ss", str(t), "-i", video, "-frames:v", "1",
                    "-vf", "crop=iw:ih*0.26:0:ih*0.74,scale=%d:-2" % width, "-q:v", "3", "-y", path],
                   capture_output=True, timeout=90)
    return os.path.exists(path) and os.path.getsize(path) > 2000


def band_sig(video, t):
    """字幕带的 16x16 灰度指纹：和上一帧一样就不用再花 token 问一遍。"""
    r = subprocess.run([FF, "-nostdin", "-v", "error", "-ss", str(t), "-i", video, "-frames:v", "1",
                        "-vf", "crop=iw:ih*0.26:0:ih*0.74,scale=16:16,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True, timeout=60)
    return r.stdout


NOISE = re.compile(
    r"剧审|网字|备案|登记号|^IF$|^[0-9]{12,}$|"          # 剧审编号/编号残片
    r"^#\d+\s|预告|次回|第\d+集|"                       # 下一集预告条
    r"^(bilibili|哔哩哔哩)", re.I)


def clean(text):
    """去掉水印、剧审号、下集预告条、NONE。"""
    out = []
    for ln in str(text or "").split("\n"):
        s = ln.strip()
        if not s or s.upper() == "NONE":
            continue
        if NOISE.search(s):
            continue
        out.append(s)
    return " / ".join(out)


def same(a, b):
    """两段字幕是否算同一条：去掉空格标点后，一方包含另一方。"""
    na = re.sub(r"[\s、。！？!?…「」『』（）()]", "", a)
    nb = re.sub(r"[\s、。！？!?…「」『』（）()]", "", b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=73077)
    ap.add_argument("--ep", type=int, required=True)
    ap.add_argument("--step", type=float, default=2.0, help="每几秒抓一帧")
    ap.add_argument("--workers", type=int, default=8)
    # 2026-11-09 实测：1280 宽 274 token/帧，640 宽 94 token/帧，文本 15/17 一致。
    ap.add_argument("--width", type=int, default=640, help="字幕带缩放宽度，默认 640（省 64%% token）")
    ap.add_argument("--dup-diff", type=float, default=2.0,
                    help="字幕带像素平均差小于这个值就当作同一句，跳过 OCR")
    ap.add_argument("--max-minutes", type=int, default=240)
    args = ap.parse_args()

    sys.path.insert(0, TULPA)
    from tulpa_bilibili.content import Content
    from tulpa_bilibili.net import SafeHTTP
    from tulpa_bilibili.settings import Settings

    d = os.path.join(WATCH, "ss%d" % args.season)
    os.makedirs(d, exist_ok=True)
    meta_p = os.path.join(d, "ep%d.meta.json" % args.ep)
    video = os.path.join(d, "ep%d.video.mp4" % args.ep)

    # 缺媒体就先按老路下载（这里只下视频，OCR 不需要音轨）
    if not os.path.exists(video):
        c = Content(Settings("/opt/tulpa/data/bilibili"), net=SafeHTTP())
        info = c.info("ss%s" % args.season, args.ep)
        streams = c.play(info)
        SafeHTTP().get(streams["video"], purpose="media", output=video,
                       max_bytes=256 * 1024 * 1024)
        say("视频下好 %.1fMB" % (os.path.getsize(video) / 1e6))

    meta = {}
    try:
        meta = json.load(io.open(meta_p, encoding="utf-8"))
    except Exception:
        c = Content(Settings("/opt/tulpa/data/bilibili"), net=SafeHTTP())
        info = c.info("ss%s" % args.season, args.ep)
        meta = dict(title=info["title"], duration=info["duration_seconds"], ep=args.ep,
                    season=int(args.season), ep_id=info.get("ep_id"))
        io.open(meta_p, "w", encoding="utf-8").write(json.dumps(meta, ensure_ascii=False))

    duration = float(meta["duration"])
    times = []
    t = 0.0
    while t < duration:
        times.append(round(t, 1))
        t += args.step
    say("ep%d《%s》 %.0f 秒，每 %.1f 秒一帧 → %d 帧，%d 路并发"
        % (args.ep, meta.get("title", ""), duration, args.step, len(times), args.workers))

    started, spent_tokens, done, prev_t = time.time(), 0, 0, None
    results = {}

    def work(t):
        path = "/tmp/_sub_%d_%d.jpg" % (args.ep, int(t))
        if not grab(video, t, path, args.width):
            return t, None, 0, None      # None = 这帧没有可用画面
        sig = band_sig(video, t)
        try:
            text, tok = ocr_frame(path)
        except Exception:
            return t, None, 0, sig
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return t, clean(text), tok, sig

    prev_sig, skipped = None, 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for tt, text, tok, sig in ex.map(work, times):
            done += 1
            if sig is not None and prev_sig is not None and len(sig) == len(prev_sig):
                diff = sum(abs(a - b) for a, b in zip(sig, prev_sig)) / max(1, len(sig))
                if diff < args.dup_diff:
                    # 字幕带没变 → 同一句，直接复用上一帧的结果，不花 token
                    results[tt] = results.get(prev_t, "")
                    prev_t, prev_sig = tt, sig
                    skipped += 1
                    continue
            prev_sig = sig
            prev_t = tt
            results[tt] = text if text is not None else ""
            spent_tokens += tok
            if done % 60 == 0:
                el = time.time() - started
                say("  %3.0f%%  %d/%d 帧  token %dk  已用 %.0f 分  预计还剩 %.0f 分"
                    % (100.0 * done / len(times), done, len(times), spent_tokens // 1000,
                       el / 60, (len(times) - done) * el / max(1, done) / 60))
            if time.time() - started > args.max_minutes * 60:
                say("到时间上限，先在 %d/%d 帧处停下" % (done, len(times)))
                break

    # 连续相同 → 合并成带起止时间的条目
    rows = []
    for tt in times:
        if tt not in results:
            continue
        text = results[tt]
        if not text:
            continue
        if rows and same(rows[-1]["text"], text):
            rows[-1]["t1"] = round(min(duration, tt + args.step), 2)
            continue
        rows.append(dict(t0=tt, t1=round(min(duration, tt + args.step), 2), text=text))

    fmt = os.path.join(d, "ep%d.jsonl" % args.ep)
    with io.open(fmt, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    md = os.path.join(d, "ep%d.md" % args.ep)
    with io.open(md, "w", encoding="utf-8") as f:
        f.write("# %s · 第 %d 集\n\n" % (meta.get("title", ""), args.ep))
        f.write("来源：**画面硬字幕 OCR**（qwen3.5-ocr，每 %.1f 秒一帧），不是听写。\n" % args.step)
        f.write("时长 %.0f 秒；字幕条 %d 条。\n\n" % (duration, len(rows)))
        for r in rows:
            f.write("[%02d:%02d] %s\n" % (int(r["t0"]) // 60, int(r["t0"]) % 60, r["text"]))

    meta.update(lines=len(rows), source="subtitle_ocr", step=args.step,
                tokens=spent_tokens, skipped_frames=skipped, width=args.width,
                ocr_at=time.strftime("%F %T"))
    io.open(meta_p, "w", encoding="utf-8").write(json.dumps(meta, ensure_ascii=False))
    say("完成：%d 条字幕，token %dk，跳过 %d 帧（画面未变），用时 %.0f 分 → %s"
        % (len(rows), spent_tokens // 1000, skipped, (time.time() - started) / 60, md))
    log("ep%d season=%s frames=%d tokens=%d lines=%d"
        % (args.ep, args.season, len(results), spent_tokens, len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
