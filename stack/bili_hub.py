#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站能力的统一入口（她只连这一个 MCP）。

为什么要有这层代理：
    一开始我把 Tulpa 的 MCP 直接给了她，然后用"开关 MCP"来做时间窗。
    实测发现行不通：**dsh 的 MCP 连接是启动时建立的**，关掉再开它不会重连，
    于是工具名还在、调用全失败（她当时说"手里好像没有能读视频的工具"）。

    所以改成：Tulpa 的 MCP 一直开着，时间窗的判断放在这层代理里。
    代理自己常驻（stdio，dsh 启动就连上，永不掉线）。

分工：
    Tulpa        读（搜索/推荐/字幕/抽帧/评论），只读，wbi 签名与 SSRF 防护都由它做
    本代理       时间窗 + 配额 + 转发 + 写（评论区 @ 主人）

时间窗规则（bili_window.conf.json 可改）：
    低谷时段（默认 23:30-08:00）：可以自己刷，每天自动额度 3 次
    高峰时段：读工具统一拒绝，返回 ok:false「现在不给我玩」
    主人在对话里说「给你玩 N 分钟」→ 她调 bili_open(N) 开窗口，到点自动关
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

BASE = "/opt/dsh"
CONF = os.path.join(BASE, "bili_share.conf.json")
LOG = os.path.join(BASE, "memory/bili-share.jsonl")
WATCHED = os.path.join(BASE, "memory/bili-watched.jsonl")
TULPA_URL = "http://127.0.0.1:18777/mcp"
TULPA_CONN = "/opt/dsh/tulpa-conn.json"   # 持久；/tmp 重启会被清空
TULPA_CONN_FALLBACK = "/tmp/conn.json"

READ_TOOLS = {
    "search_videos": "按关键词搜 B 站视频，返回标题/UP主/时长/BV号",
    "get_recommended_videos": "登录账号的推荐流（就像刷首页）",
    "get_video_info": "单个视频的详情（简介、分P、统计）",
    "read_video": "读视频内容：异步任务，字幕优先（带时间戳），同时抽帧",
    "get_video_job": "查 read_video 的任务状态，succeeded 后取结果",
    "get_video_comments": "读评论区",
    "get_video_frames": "取已抽好的画面帧",
    "get_bilibili_status": "B 站登录状态",
    "cancel_video_job": "取消一个读视频任务",
    "get_video_danmaku": "读一条视频的弹幕（观众反应：哪段在笑、刷什么梗）",
    "explore_bilibili": "自主逛：不给关键词也行，从记忆里挑好奇点去搜",
}


def log(msg):
    try:
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + chr(10))
    except Exception:
        pass


def conf():
    try:
        return json.load(io.open(CONF, encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------- 转发给 Tulpa
def _tulpa_token():
    for p in (TULPA_CONN, TULPA_CONN_FALLBACK):
        try:
            tok = json.load(io.open(p, encoding="utf-8")).get("token") or ""
            if tok:
                return tok
        except Exception:
            continue
    return ""



def forward(name, args):
    """把工具调用转给 Tulpa 的 MCP。无状态：每次带 initialize 也没关系。"""
    tok = _tulpa_token()
    if not tok:
        return {"ok": False, "error": {"code": "no_token", "message": "代理没有 Tulpa 的访问凭据"}}

    def post(payload):
        req = urllib.request.Request(
            TULPA_URL, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + tok,
                     "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    try:
        post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "bili-hub", "version": "1"}}})
        d = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": name, "arguments": args or {}}})
    except Exception as e:
        return {"ok": False, "error": {"code": "tulpa_unreachable",
                                       "message": "B 站服务没连上：%s" % str(e)[:80]}}

    content = ((d.get("result") or {}).get("content") or [])
    text = content[0].get("text") if content else ""
    try:
        return json.loads(text)
    except Exception:
        return {"ok": False, "error": {"code": "bad_response", "message": str(text)[:120]}}


# ---------------------------------------------------------------- 时间窗
def gate():
    """读之前先过这一关。返回 (能不能, 原因)。"""
    try:
        sys.path.insert(0, BASE)
        import bili_window
        ok, why, left = bili_window.allowed()
        return ok, why, left
    except Exception as e:
        return True, "时间窗模块不可用（%s），放行" % str(e)[:40], 0


# ---------------------------------------------------------------- 写：评论区 @
def lim_cfg():
    """每天限制从 limits.json 读，改配置就生效（2026-10-26）。"""
    d = {"bili_share_per_day": 3, "bili_share_interval_min": 20,
         "bili_share_same_video_days": 7}
    try:
        with open(os.path.join(BASE, "limits.json"), encoding="utf-8") as f:
            d.update({k: v for k, v in json.load(f).items() if k in d})
    except Exception:
        pass
    return d


def limits():
    now = time.time()
    recs = []
    try:
        for line in io.open(LOG, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ok") and r.get("bvid") and r.get("ts", 0) > now - 7 * 86400:
                recs.append(r)
    except Exception:
        pass
    cfg = lim_cfg()
    daily_max = int(cfg["bili_share_per_day"])
    gap_s = int(cfg["bili_share_interval_min"]) * 60
    today0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    sent = [r for r in recs if r.get("ts", 0) >= today0]
    last = max([r.get("ts", 0) for r in recs], default=0)
    return {"daily_max": daily_max, "sent_today": len(sent),
            "left_today": max(0, daily_max - len(sent)),
            "next_ok_in_s": max(0, int(last + gap_s - now)) if last else 0}


def share(args):
    bvid = (args.get("bvid") or "").strip()
    note = (args.get("note") or "").strip()[:40]
    if not bvid.upper().startswith("BV"):
        return {"ok": False, "error": {"code": "bad_bvid", "message": "bvid 要以 BV 开头"}}
    c = conf()
    mid, name = c.get("owner_mid"), (c.get("owner_name") or "").strip()
    if not mid or not name:
        return {"ok": False, "error": {"code": "no_owner", "message": "没配置主人 B站 mid"}}

    lim = limits()
    if lim["left_today"] <= 0:
        return {"ok": False, "error": {"code": "daily_limit", "message": "今天分享额度用完了（见 limits.json）"}, "limits": lim}
    if lim["next_ok_in_s"] > 0:
        return {"ok": False, "error": {"code": "too_soon",
                                       "message": "刚分享过，%d 分钟后再来" % (lim["next_ok_in_s"] // 60)},
                "limits": lim}
    try:
        for line in io.open(LOG, encoding="utf-8"):
            r = json.loads(line) if line.strip() else {}
            if r.get("ok") and (r.get("bvid") or "").upper() == bvid.upper():
                return {"ok": False, "error": {"code": "duplicate",
                                               "message": "这个视频已经分享过了"}}
    except Exception:
        pass

    sys.path.insert(0, BASE)
    import bili_at
    res = bili_at.post_comment(bvid, note or "这个我觉得挺有意思", at_mid=mid, at_name=name)
    log(json.dumps({"ts": int(time.time()), "bvid": bvid, "note": note,
                    "ok": bool(res.get("ok")), "rpid": res.get("rpid")}, ensure_ascii=False))
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or {"code": "unknown"},
                "note": "硬拒绝，别重试"}
    return {"ok": True, "bvid": bvid, "rpid": res.get("rpid"), "shared_with": name,
            "note": "已在他评论区 @ 他，通知会到。审核期内游客可能看不到，不影响通知。",
            "limits": limits()}


# ---------------------------------------------------------------- 工具表
TOOLS = [{
    "name": "bili_budget",
    "description": "看今天的 B 站浏览 token 额度还剩多少（会计入每次搜索/读视频的返回体量）。",
    "inputSchema": {"type": "object", "properties": {}},
}, {
    "name": "bili_now",
    "description": "看现在能不能刷 B 站、还剩多久。刷之前先查它——现在是低谷还是高峰。",
    "inputSchema": {"type": "object", "properties": {}},
}, {
    "name": "bili_open",
    "description": ("主人说「给你玩」「去刷会儿」时调它，开一个限时浏览窗口。"
                    "minutes 是分钟数（他说了就用他的，没说填 25）。"
                    "每天最多 2 次、单次最长 60 分钟。只在主人让你玩时调。"),
    "inputSchema": {"type": "object",
                    "properties": {"minutes": {"type": "integer", "description": "多少分钟"}}},
}, {
    "name": "bili_share_to_owner",
    "description": ("在视频评论区 @ 主人，把视频递给他（他收到 B 站通知）。"
                    "bvid 是视频号，note 是 20 字内的理由。一天最多 3 条，"
                    "同一个视频只分享一次。"),
    "inputSchema": {"type": "object",
                    "properties": {"bvid": {"type": "string"},
                                   "note": {"type": "string", "description": "20 字内"}},
                    "required": ["bvid"]},
}]
for _n, _d in READ_TOOLS.items():
    TOOLS.append({"name": _n, "description": _d + "（受浏览时间窗限制）",
                  "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True}})

# HUB_WATCH: 追番（看正片整集，不是二创拆解）
TOOLS.extend([{
    "name": "bili_watch_episode",
    "description": ("看番剧正片的整集（本地转写 + 每窗口抽一帧），断点续传。"
                    "season 是番剧 ss 号（MyGO = 73077），ep 是第几集。"
                    "后台跑，约 3 分钟看 5 分钟剧情；跑完用 bili_watch_read 读台词稿。"
                    "别用 search_videos 找番剧——那只会搜到别人的解说。"),
    "inputSchema": {"type": "object",
                    "properties": {"season": {"type": "integer", "description": "番剧 ss 号，默认 73077"},
                                   "ep": {"type": "integer", "description": "第几集，从 1 开始"}},
                    "required": ["ep"]},
}, {
    "name": "bili_watch_status",
    "description": "看某一集看到哪儿了（百分比、已转写多少句、是否还在跑）。",
    "inputSchema": {"type": "object",
                    "properties": {"season": {"type": "integer"}, "ep": {"type": "integer"}},
                    "required": ["ep"]},
}, {
    "name": "bili_watch_read",
    "description": ("读已看过的台词稿（带绝对秒数时间码）。from_sec 从第几秒开始，"
                    "limit 最多几句（默认 120）。这是正片内容，不是别人拆解。"),
    "inputSchema": {"type": "object",
                    "properties": {"season": {"type": "integer"}, "ep": {"type": "integer"},
                                   "from_sec": {"type": "number"},
                                   "limit": {"type": "integer"}},
                    "required": ["ep"]},
}, {
    "name": "bili_frame_grab",
    "description": ("从某一集里抠几张画面直接看（用已缓存的视频，秒出）。"
                    "番剧画面里有硬字幕，比转写稿靠谱——转写会打转、会带回放。"
                    "注意：片头曲位置每集不同，OP 前面常有一小段冷开场正片，"
                    "所以别假设第 0 秒就是 OP；要定位就用 range_start/range_end 扫一段。"
                    "不传 times 就按均分取 count 张；给了 times 就按指定秒数。"),
    "inputSchema": {"type": "object",
                    "properties": {"season": {"type": "integer", "description": "番剧 ss 号，默认 73077"},
                                   "ep": {"type": "integer"},
                                   "count": {"type": "integer", "description": "取几张，默认 4，最多 8"},
                                   "times": {"type": "array", "items": {"type": "number"},
                                             "description": "指定秒数，最多 8 个"},
                                   "range_start": {"type": "number",
                                                   "description": "只看某一段时给起点秒数（如看开头 4 分钟填 0）"},
                                   "range_end": {"type": "number",
                                                 "description": "只看某一段时给终点秒数（如 240）"}},
                    "required": ["ep"]},
}])


def _content_blocks(out):
    """工具结果转 MCP content：图片以 image 块返回，文字照旧进 text 块。

    番剧的硬字幕就在画面里，图能到模型眼前比文字描述可靠得多。
    """
    body = {k: v for k, v in out.items() if k != "__images__"} if isinstance(out, dict) else out
    blocks = []
    if body or not (isinstance(out, dict) and out.get("__images__")):
        blocks.append({"type": "text", "text": json.dumps(body, ensure_ascii=False)})
    for path in (out.get("__images__") if isinstance(out, dict) else None) or []:
        try:
            import base64
            with open(path, "rb") as fh:
                blocks.append({"type": "image", "mimeType": "image/jpeg",
                               "data": base64.b64encode(fh.read()).decode()})
        except Exception:
            continue
    return blocks or [{"type": "text", "text": "{}"}]


def reply(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + chr(10))
    sys.stdout.flush()


def _read_quality(res):
    """读视频的结果到底能不能用——把"成功但空"如实降级。

    Tulpa 在媒体下载失败时仍然返回 state=succeeded，只是字幕空、没有帧、
    evidence_level 掉成 metadata_only（2026-10-25 实测 10 次里 5 次这样）。
    对她来说"成功但空"最坑：她会当成自己看过了。
    """
    if not isinstance(res, dict) or not res.get("ok"):
        return res
    body = res.get("result") if isinstance(res.get("result"), dict) else res
    if not isinstance(body, dict):
        return res
    if str(body.get("state") or "") not in ("", "succeeded"):
        return res
    entries = ((body.get("speech") or {}).get("entries")) or []
    frames = body.get("frames") or []
    if entries or frames:
        return res
    warnings = body.get("warnings") or []
    codes = [str(w.get("code")) for w in warnings if isinstance(w, dict) and w.get("code")]
    why = "、".join(codes) or "无字幕无画面"
    out = dict(res)
    out["ok"] = False
    out["state"] = "empty"
    out["error"] = {"code": "read_empty",
                    "message": "这个视频没读到内容（%s），只有元数据" % why,
                    "warnings": warnings[:4]}
    out["note"] = ("不算看过，别当成看过。换一个视频试；"
                   "连续两个都这样就跟主人说一声，别死磕。")
    return out


def wait_job(res, budget_s=30, interval=1.5):
    """read_video 是异步任务：这里替她把排队等完，省得她手动轮询又等不及。"""
    if not isinstance(res, dict) or not res.get("ok"):
        return res
    job = res.get("job_id")
    if not job or str(res.get("state") or "") not in ("queued", "running"):
        return res
    deadline = time.time() + budget_s
    last = res
    while time.time() < deadline:
        time.sleep(interval)
        cur = forward("get_video_job", {"job_id": job})
        if not isinstance(cur, dict):
            break
        last = cur
        if str(cur.get("state") or "") not in ("queued", "running"):
            cur["waited_seconds"] = round(budget_s - max(0.0, deadline - time.time()), 1)
            return cur
    out = dict(last)
    out["ok"] = False
    out.setdefault("error", {"code": "job_timeout",
                             "message": "等了 %d 秒还没跑完" % budget_s})
    out["note"] = "还在跑就别死等，先干别的，或者跟主人说一句。"
    return out


# ---- HUB_WATCH：追番（正片整集 / 断点续传）--------------------------------
WATCH_DIR = "/opt/tulpa/data/bilibili/watch"
WATCH_PY = "/opt/dsh/bili_watch_episode.py"
WATCH_PYTHON = "/opt/tulpa/.venv/bin/python"
WATCH_FFMPEG = ("/opt/tulpa/.venv/lib/python3.13/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")


def _watch_files(season, ep):
    d = os.path.join(WATCH_DIR, "ss%s" % season)
    return dict(dir=d,
                jsonl=os.path.join(d, "ep%s.jsonl" % ep),
                md=os.path.join(d, "ep%s.md" % ep),
                prog=os.path.join(d, "ep%s.progress" % ep),
                meta=os.path.join(d, "ep%s.meta.json" % ep),
                log=os.path.join(d, "ep%s.log" % ep),
                lock=os.path.join(d, "ep%s.lock" % ep))


def _watch_state(season, ep):
    f = _watch_files(season, ep)
    meta = {}
    try:
        meta = json.load(io.open(f["meta"], encoding="utf-8"))
    except Exception:
        pass
    done = 0.0
    try:
        done = float(io.open(f["prog"], encoding="utf-8").read().strip())
    except Exception:
        pass
    lines = 0
    if os.path.exists(f["jsonl"]):
        lines = sum(1 for ln in io.open(f["jsonl"], encoding="utf-8") if ln.strip())
    duration = float(meta.get("duration") or 0)
    running = False
    try:
        pid = int(io.open(f["lock"], encoding="utf-8").read().strip())
        os.kill(pid, 0)
        running = True
    except Exception:
        pass
    return dict(season=season, ep=ep, title=meta.get("title"), duration_seconds=duration,
                done_until=round(done, 1), lines=lines, running=running,
                percent=round(done / duration * 100, 1) if duration else 0.0,
                finished=bool(duration) and done >= duration - 1,
                transcript=f["md"] if os.path.exists(f["md"]) else None)


def watch_episode(args):
    """开始/继续看某一集；已在跑就只报进度，不重复起进程。"""
    season = int(args.get("season") or 73077)
    ep = int(args.get("ep") or 1)
    st = _watch_state(season, ep)
    if st["finished"]:
        st["note"] = "这一集已看完，用 bili_watch_read 读台词稿。"
        return st
    if st["running"]:
        st["note"] = "正在看，过一会儿用 bili_watch_status 查。"
        return st
    f = _watch_files(season, ep)
    os.makedirs(f["dir"], exist_ok=True)
    log = io.open(f["log"], "ab")
    proc = subprocess.Popen([WATCH_PYTHON, WATCH_PY, "--season", str(season), "--ep", str(ep)],
                            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True)
    io.open(f["lock"], "w", encoding="utf-8").write(str(proc.pid))
    st.update(running=True, pid=proc.pid,
              note="后台开始看这一集：约 3 分钟看 5 分钟剧情，用 bili_watch_status 查进度。")
    return st


def watch_read(args):
    """读台词稿（正片内容）。"""
    season = int(args.get("season") or 73077)
    ep = int(args.get("ep") or 1)
    start = float(args.get("from_sec") or 0)
    limit = max(1, min(int(args.get("limit") or 120), 600))
    f = _watch_files(season, ep)
    if not os.path.exists(f["jsonl"]):
        return {"ok": False, "error": {"code": "not_watched", "message": "这一集还没开始看"},
                "note": "先调 bili_watch_episode。"}
    rows = []
    for ln in io.open(f["jsonl"], encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if float(r.get("t0") or 0) < start:
            continue
        rows.append(r)
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: r.get("t0") or 0)
    return {"ok": True, "ep": ep, "from_sec": start, "count": len(rows),
            "lines": [{"t": r.get("t0"), "text": r.get("text")} for r in rows],
            "note": "本地转写，可能误听；时间码是绝对秒数。"}


def frame_grab(args):
    """抠几张画面给她看：番剧的硬字幕就在画面里，比转写靠谱。"""
    season = int(args.get("season") or 73077)
    ep = int(args.get("ep") or 1)
    times = args.get("times") or []
    count = max(1, min(int(args.get("count") or 4), 8))
    f = _watch_files(season, ep)
    video = os.path.join(f["dir"], "ep%d.video.mp4" % ep)
    if not os.path.exists(video):                      # 兼容旧版按季存的文件
        legacy = os.path.join(f["dir"], "video.mp4")
        if ep == 1 and os.path.exists(legacy):
            video = legacy
    if not os.path.exists(video):
        return {"ok": False,
                "error": {"code": "no_media",
                          "message": "这一集还没缓存视频，先调一次 bili_watch_episode（会顺带下载）"},
                "note": "下载一次性，之后抠帧几秒就好。"}
    meta = {}
    try:
        meta = json.load(io.open(f["meta"], encoding="utf-8"))
    except Exception:
        pass
    duration = float(meta.get("duration") or 0)
    r0, r1 = args.get("range_start"), args.get("range_end")
    if times:
        stamps = [round(float(t), 2) for t in times[:8]]
    elif r0 is not None and r1 is not None and float(r1) > float(r0):
        # 只看一段（比如开头几分钟，找 OP 到底从哪开始）
        a, b = float(r0), float(r1)
        stamps = [round(a + (b - a) * (i + 0.5) / count, 2) for i in range(count)]
    else:
        # 均分抽帧常落在空镜头/过场上（实测 178 秒是一滩水面，没有字幕）。
        # 改成从台词里挑：在"有说话"的句子里均匀取样，这样每张画面都带那句字幕。
        stamps = []
        rows = []
        if os.path.exists(f["jsonl"]):
            for ln in io.open(f["jsonl"], encoding="utf-8"):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                text = str(r.get("text") or "").strip()
                if len(text) >= 6 and "音楽" not in text and "字幕" not in text:
                    rows.append(float(r.get("t0") or 0))
        rows.sort()
        if len(rows) >= count:
            step = len(rows) / count
            stamps = [round(rows[int(i * step)] + 0.4, 2) for i in range(count)]
        elif duration:
            stamps = [round(duration * (i + 0.5) / count, 2) for i in range(count)]
        else:
            stamps = [60.0 * (i + 1) for i in range(count)]
    outdir = os.path.join(f["dir"], "grab")
    os.makedirs(outdir, exist_ok=True)
    saved = []
    for t in stamps:
        path = os.path.join(outdir, "t%05d.jpg" % int(t))
        subprocess.run([WATCH_FFMPEG, "-nostdin", "-v", "error", "-ss", str(t), "-i", video,
                        "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "3", "-y", path],
                       capture_output=True, timeout=90)
        if os.path.exists(path) and os.path.getsize(path) > 2000:
            saved.append({"t": t, "path": path})
    if not saved:
        return {"ok": False, "error": {"code": "grab_failed", "message": "没抠出画面"}}
    return {"ok": True, "ep": ep, "count": len(saved), "frames": saved,
            "__images__": [x["path"] for x in saved],
            "note": "画面里的硬字幕才是最可靠的台词来源。"}


def call_tool(name, args):
    if name == "bili_now":
        ok, why, left = gate()
        return {"ok": True, "can_browse": ok, "reason": why, "left_minutes": left // 60}
    if name == "bili_open":
        import bili_window
        mins = int(args.get("minutes") or 25)
        ok, msg = bili_window.open_window(mins, by="对话授权", manual=True)
        return {"ok": ok, "message": msg,
                "note": "" if ok else "被限频拒绝了，别重试，跟主人说一声"}
    if name == "bili_share_to_owner":
        return share(args)
    if name == "bili_frame_grab":
        return frame_grab(args)
    if name == "bili_watch_episode":
        # 追番单集要吃 16 分钟双核：纳入浏览窗口限制（和刷 B 站同一个时段），
        # 但**不占 token 额度**——它不花模型钱，只花 CPU。
        ok, why, left = gate()
        if not ok:
            return {"ok": False, "error": {"code": "window_closed", "message": why},
                    "note": "现在不是能刷 B 站的时候，别硬开追番任务。跟主人说一声。"}
        return watch_episode(args)
    if name == "bili_watch_status":
        return _watch_state(int(args.get("season") or 73077), int(args.get("ep") or 1))
    if name == "bili_watch_read":
        return watch_read(args)
    if name == "bili_budget":
        import bili_budget
        bili_budget.ensure_conf()
        return {"ok": True, **bili_budget.status()}

    if name == "explore_bilibili":
        return explore(args)
    if name in READ_TOOLS:
        ok, why, left = gate()
        if not ok:
            return {"ok": False, "error": {"code": "window_closed", "message": why},
                    "note": "现在不是能刷的时候。别反复试，跟主人说一声就好。"}
        import bili_budget
        bok, bwhy, _ = bili_budget.check()
        if not bok:
            return {"ok": False, "error": {"code": "budget_exhausted", "message": bwhy},
                    "note": "额度没了，别重试。可以跟主人说一声。",
                    "budget": bili_budget.status()}
        res = forward(name, args)
        # 异步任务替她等完（C），再把"成功但空"如实降级（B）
        if name == "read_video":
            res = wait_job(res)
        if name in ("read_video", "get_video_job"):
            res = _read_quality(res)
        try:
            if isinstance(res, dict) and res.get("ok"):
                bili_budget.record(bili_budget.estimate(res))
        except Exception:
            pass
        # 看过什么要留痕，否则她事后想不起来（用户 2026-10-25 反馈）
        if name in ("read_video", "get_video_job", "get_video_danmaku"):
            record_watched(name, args, res)
        return res
    return {"ok": False, "error": {"code": "unknown_tool", "message": name}}



# ---------------------------------------------------------------- 自主逛
def _curiosity_topics(k=3):
    """从她自己的材料里挑候选好奇点：日记 + 看过的标题 + 事实库。

    不调 LLM（省钱、快）：取最近的日记片段和看过的标题，交给调用方去搜。
    """
    cands = []
    # ① 最近看过的标题（避免重复搜同一个）
    try:
        seen = set()
        for ln in io.open(os.path.join(BASE, "memory/bili-watched.jsonl"), encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            t = str(r.get("title") or "").strip()
            if t:
                seen.add(t[:20])
        cands.extend([("看过的", t) for t in list(seen)[-5:]])
    except Exception:
        pass
    # ② 最近几天日记里出现过的名词性片段
    try:
        sys.path.insert(0, BASE)
        import search_index
        rows = search_index.search("好奇 想学 想了解 没看懂", limit=8)
        for kind, head, body, _score in rows:
            if kind == "diary" and body:
                cands.append(("日记", str(body)[:80]))
    except Exception:
        pass
    return cands[:k]


def _pick_topic_by_llm():
    """让模型从最近看过/写过的内容里挑一个真正想再了解的主题。

    为什么要模型挑：直接从标题里截 2-6 个字会截到「每天认识一个」这种前缀，
    搜出来全是无关内容。一次轻量调用换一个像样的关键词，值得。
    """
    import sys as _sys
    _sys.path.insert(0, BASE)
    try:
        import mem_layers
    except Exception:
        return "", ""

    titles, diaries = [], []
    try:
        for ln in io.open(os.path.join(BASE, "memory/bili-watched.jsonl"), encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            t = str(r.get("title") or "").strip()
            if t:
                titles.append(t[:60])
    except Exception:
        pass
    try:
        import search_index
        for kind, head, body, _score in search_index.search("好奇 想弄明白 没看懂 挺有意思", limit=6):
            if kind == "diary" and body:
                diaries.append(str(body)[:120])
    except Exception:
        pass

    if not titles and not diaries:
        return "", ""

    prompt = ("你刚在 B 站看了看东西。下面是你最近看过的标题和你写过的日记片段。\n"
              "挑一个你确实还想再多了解的主题，输出**一个搜索关键词**（2-8 个字，名词短语，不要句子、不要引号）。\n"
              "挑有具体信息量的（比如「抹香鲸」「韦伯效应」「火柴人动画」），不要「每天认识一个」这种空壳。\n"
              "只输出关键词本身。\n\n"
              "看过的标题：\n- " + "\n- ".join(titles[-6:]) + "\n"
              + ("\n日记片段：\n- " + "\n- ".join(diaries[-3:]) + "\n" if diaries else ""))
    try:
        kw = mem_layers._llm(prompt).strip().strip('"\'「」')
    except Exception as e:
        return "", "模型挑词失败：%s" % str(e)[:60]
    kw = re.sub(r"[\s\n]+", "", kw)[:12]
    if not kw or len(kw) < 2:
        return "", "模型没给出可用的关键词"
    return kw, "自己想看的（从最近的记录里挑的）"


def explore(args):
    """自主逛：有 topic 就用 topic，没有就从记忆里挑一个好奇点去搜。"""
    import re as _re
    topic = str(args.get("topic") or "").strip()
    limit = int(args.get("limit") or 5)
    why = "自己指定的方向" if topic else ""

    if not topic:
        # 优先让模型挑（远比从标题里截词靠谱），失败才退回正则
        topic, why = _pick_topic_by_llm()
    if not topic:
        for src, text in _curiosity_topics():
            # 从片段里抽 2-6 个字的关键词：中文取连续片段，去掉标点
            toks = _re.findall(r"[\u4e00-\u9fff]{2,6}", text)
            if toks:
                topic = toks[0]
                why = "从%s里想到的：%s" % (src, text[:40])
                break

    if not topic:
        return {"ok": False,
                "error": {"code": "no_topic", "message": "暂时想不出搜什么；可以直接给我一个关键词。"},
                "note": "也可以先去看看排行榜（get_ranking_videos）找灵感。"}

    res = forward("search_videos", {"query": topic, "limit": limit})
    if not isinstance(res, dict):
        return res
    res["topic_used"] = topic
    if why:
        res["why"] = why
    res.setdefault("note", "搜到了就先看一两个；读完可以自己写一句感受。")
    return res


def record_watched(name, args, res):
    """把"看过什么"落一行盘。

    实测返回（2026-10-25）：
      {"ok":true,"state":"succeeded",
       "result":{"bvid":"BV...","speech":{"entries":[{"start":0.1,"end":1.1,"content":"..."}]}}}
    注意 result 里**没有 title**，字幕文字在 entry["content"]（不是 text）。
    """
    try:
        if not isinstance(res, dict) or not res.get("ok"):
            return
        body = res.get("result") if isinstance(res.get("result"), dict) else res
        if str(body.get("state") or "") not in ("", "succeeded"):
            return

        bvid = str(body.get("bvid") or args.get("bvid") or args.get("video") or "")
        title = ""
        for key in ("title", "video_title", "name"):
            if body.get(key):
                title = str(body[key])[:120]
                break

        summary = ""
        for key in ("summary", "desc", "text"):
            if body.get(key):
                summary = str(body[key]).strip()[:200]
                break
        if not summary:
            speech = body.get("speech")
            if isinstance(speech, dict):
                ents = speech.get("entries") or []
                parts = []
                for e in ents:
                    if not isinstance(e, dict):
                        continue
                    t = (e.get("content") or e.get("text") or "").strip()
                    if t:
                        parts.append(t)
                    if sum(len(x) for x in parts) > 160:
                        break
                summary = "".join(parts)[:200]

        speed = ""
        if isinstance(body.get("speech"), dict):
            speed = str(body["speech"].get("state") or "")

        # 2026-11-09：弹幕是「观众反应」的唯一来源，也要留痕
        dm = ""
        if str(name) == "get_video_danmaku":
            items = body.get("items") or []
            dm = " / ".join(str(x.get("text"))[:24] for x in items[:6] if isinstance(x, dict))

        # 没标题（多数情况）就用 get_video_info 补一次，否则记录里只有 bvid，
        # 事后根本想不起是哪个视频。
        if not title and bvid:
            try:
                info = forward("get_video_info", {"video": bvid})
                ib = info.get("result") if isinstance(info.get("result"), dict) else info
                for key in ("title", "video_title", "name"):
                    if isinstance(ib, dict) and ib.get(key):
                        title = str(ib[key])[:120]
                        break
            except Exception as e:
                log("get_video_info fallback failed: %s" % e)

        if not (bvid or title or summary):
            return
        rec = {"ts": int(time.time()), "tool": name, "bvid": bvid,
               "title": title, "summary": summary, "speech": speed}
        if dm:
            rec["dm"] = dm
        os.makedirs(os.path.dirname(WATCHED), exist_ok=True)
        with open(WATCHED, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
        log("watched logged: %s | %s" % (bvid, (title or summary)[:40]))
    except Exception as e:
        log("record_watched failed: %s" % e)

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        mid, method = req.get("id"), req.get("method")
        if method == "initialize":
            reply({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "bili-hub", "version": "1.0.0"}}})
        elif method == "tools/list":
            reply({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            p = req.get("params") or {}
            try:
                out = call_tool(p.get("name"), p.get("arguments") or {})
            except Exception as e:
                out = {"ok": False, "error": {"code": "crash", "message": str(e)[:140]}}
            reply({"jsonrpc": "2.0", "id": mid, "result": {
                "content": _content_blocks(out),
                "isError": False}})
        elif mid is not None:
            reply({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": "unsupported: %s" % method}})


if __name__ == "__main__":
    main()
