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


def say(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def paths(season, ep):
    d = os.path.join(WATCH, "ss%d" % season)
    os.makedirs(d, exist_ok=True)
    return dict(dir=d,
                audio=os.path.join(d, "audio.m4a"),
                video=os.path.join(d, "video.mp4"),
                jsonl=os.path.join(d, "ep%d.jsonl" % ep),
                md=os.path.join(d, "ep%d.md" % ep),
                prog=os.path.join(d, "ep%d.progress" % ep),
                meta=os.path.join(d, "ep%d.meta.json" % ep),
                frames=os.path.join(d, "ep%d.frames" % ep))


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
    subprocess.run([WHISPER, "-m", MODEL, "-f", wav, "-l", "ja", "-t", str(threads),
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
    ap.add_argument("--window", type=int, default=300, help="每片多少秒，默认 300")
    ap.add_argument("--threads", type=int, default=2, help="whisper 线程数")
    ap.add_argument("--status", action="store_true", help="只看进度")
    args = ap.parse_args()

    p = paths(int(args.season), args.ep)
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

    say("第 %d 集《%s》 %.0f 秒，已看到 %.0f 秒（%.0f%%）"
        % (args.ep, info["title"], duration, done, done / duration * 100))
    ensure_media(info, p)

    while done < duration - 1:
        length = min(args.window, duration - done)
        tag = "ep%d_%d" % (args.ep, int(done))
        t0 = time.time()
        rows = transcribe_window(p["audio"], done, length, tag, args.threads)
        with io.open(p["jsonl"], "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.makedirs(p["frames"], exist_ok=True)
        got = grab_frame(p["video"], done + length / 2,
                         os.path.join(p["frames"], "t%05d.jpg" % int(done + length / 2)))
        done += length
        write_progress(p, done)
        say("  %3.0f%%  [%d–%ds] %d 句  用时 %.0fs  画面:%s"
            % (done / duration * 100, done - length, done, len(rows), time.time() - t0,
               "有" if got else "无"))

    # 收尾：导出人看的台词稿
    lines = [json.loads(x) for x in io.open(p["jsonl"], encoding="utf-8") if x.strip()]
    lines.sort(key=lambda r: r["t0"])
    with io.open(p["md"], "w", encoding="utf-8") as f:
        f.write("# %s · 第 %d 集\n\n" % (info["title"], args.ep))
        f.write("时长 %.0f 秒；台词 %d 句；来源：本地 whisper 转写（可能误听）。\n\n"
                % (duration, len(lines)))
        for r in lines:
            f.write("[%02d:%02d] %s\n" % (r["t0"] // 60, r["t0"] % 60, r["text"]))
    say("完成：%d 句台词 → %s" % (len(lines), p["md"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
