#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主动搭话的真实素材包（2026-11-09）

用户的批评：
    「主动搭话他只会机械式地回复，而不会有自己的思考，也不是今天真正的什么天气。
      他只是随便给你随手发一句机械式的话。」

问题确实在结构上：`scheduler.decide()` 只给一句写死的模板提示，`gen_line(hint)` 直接照着编，
里面**没有任何真实世界的数据**——所以她只能产出"今天也要好好过呀"这类空话。

这个模块给她补上"能思考的原料"：

  · 现在的真实时间、星期、今天第几天
  · **湘潭真实天气**（open-meteo，免 key）：温度、体感、天气现象、湿度、今日温差、降水概率
  · 今天聊了多少句、最后在聊什么（从 session 里取，脱敏只取片段）
  · 最近看过什么（memory/bili-watched.jsonl 的标题）
  · 今天日记有没有写过、昨天最后一句（保持连续感）
  · 她自己的状态（body_state：困/饿/清醒）

调用方（scheduler.run）把它拼在 hint 前面，并明确要求：
**只能用这里面的真实信息说话，没有具体依据时宁可不发。**

用法：
    python3 context_pack.py            # 打印素材包（自检用）
    from context_pack import build; build("morning")
"""
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = "/opt/dsh"
CACHE = os.path.join(BASE, "memory", "context-pack-cache.json")
LAT, LON = 27.83, 112.94          # 湖南湘潭（用户所在）
TZ = "Asia%2FShanghai"

WMO = {
    0: "晴", 1: "晴间少云", 2: "多云", 3: "阴", 45: "有雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "细雨", 56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷雨伴冰雹", 99: "强雷雨伴冰雹",
}


def _read(path, default=None):
    try:
        return io.open(path, encoding="utf-8").read()
    except Exception:
        return default


def _json(path, default):
    try:
        return json.loads(_read(path, "") or "null") or default
    except Exception:
        return default


def weather(cache_minutes=25):
    """湘潭当前天气；失败返回 None（宁缺勿造）。"""
    c = _json(CACHE, {})
    now = time.time()
    if c.get("weather") and now - float(c.get("weather_ts") or 0) < cache_minutes * 60:
        return c["weather"]
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&timezone=%s&forecast_days=1" % (LAT, LON, TZ))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        cur, day = d.get("current") or {}, d.get("daily") or {}
        w = dict(
            temp=cur.get("temperature_2m"),
            feels=cur.get("apparent_temperature"),
            humidity=cur.get("relative_humidity_2m"),
            wind=cur.get("wind_speed_10m"),
            code=cur.get("weather_code"),
            desc=WMO.get(-1 if cur.get("weather_code") is None else int(cur["weather_code"]), "天气不明"),
            tmin=(day.get("temperature_2m_min") or [None])[0],
            tmax=(day.get("temperature_2m_max") or [None])[0],
            rain=(day.get("precipitation_probability_max") or [None])[0],
        )
        c["weather"], c["weather_ts"] = w, now
        io.open(CACHE, "w", encoding="utf-8").write(json.dumps(c, ensure_ascii=False))
        return w
    except Exception as e:
        # 拿不到就返回旧值，别编
        old = c.get("weather")
        if old:
            old = dict(old)
            old["stale"] = True
            return old
        return None


def chat_today(limit_minutes=180):
    """最近聊天的概况：条数 + 最后几句（脱敏只取短片段）。"""
    import glob
    files = sorted(glob.glob(os.path.join(BASE, "sessions/**/session*.zstd"), recursive=True),
                   key=os.path.getmtime)[-3:]
    since = time.time() - limit_minutes * 60
    rows = []
    for f in files:
        try:
            raw = subprocess.run(["zstd", "-dc", f], capture_output=True, timeout=25).stdout
        except Exception:
            continue
        for line in raw.decode("utf-8", "replace").split("\n"):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ts = ev.get("time") or ev.get("timestamp")
            if not ts:
                continue
            ts = float(ts) / 1000.0 if float(ts) > 1e11 else float(ts)
            if ts < since:
                continue
            msg = (ev.get("data") or {}).get("message") or {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = "".join(c.get("text", "") for c in (msg.get("content") or [])
                           if isinstance(c, dict)).strip()
            if text:
                rows.append((ts, role, text))
    rows.sort()
    # 她的私聊会话里可能全是自己说的话（主动搭话也写进这里），
    # 所以"他说了什么"要单独从所有会话里找 user 角色的最新几条。
    user_msgs, all_msgs = [], []
    for f in files:
        try:
            raw = subprocess.run(["zstd", "-dc", f], capture_output=True, timeout=25).stdout
        except Exception:
            continue
        for line in raw.decode("utf-8", "replace").split("\n"):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ts = ev.get("time") or ev.get("timestamp")
            if not ts:
                continue
            ts = float(ts) / 1000.0 if float(ts) > 1e11 else float(ts)
            msg = (ev.get("data") or {}).get("message") or {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = "".join(c.get("text", "") for c in (msg.get("content") or [])
                           if isinstance(c, dict)).strip()
            if not text or text.startswith("（自我提醒"):
                continue
            if role == "user":
                user_msgs.append((ts, text))
            elif ts >= since:
                all_msgs.append((ts, text))
    user_msgs.sort()
    return dict(count=len(rows),
                user=[t[:40] for _, t in user_msgs[-3:]],
                assistant=[t[:40] for _, t in all_msgs[-2:]])


def read_recent(n=3):
    """最近看过/读过什么（B 站）。"""
    out = []
    for line in (_read(os.path.join(BASE, "memory/bili-watched.jsonl"), "") or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        title = str(r.get("title") or "").strip()
        if title:
            out.append(title[:44])
    return out[-n:]


def diary_state():
    """今天日记写过没、昨天最后一句。"""
    d = os.path.join(BASE, "memory/diary")
    today = time.strftime("%F")
    wrote, last_prev = False, ""
    try:
        for f in sorted(os.listdir(d)):
            if not f.endswith(".md"):
                continue
            body = _read(os.path.join(d, f), "") or ""
            if today in f or today in body[:200]:
                wrote = True
            else:
                lines = [l.strip() for l in body.split("\n") if l.strip().startswith("-")]
                if lines:
                    last_prev = lines[-1][:60]
    except Exception:
        pass
    return dict(wrote_today=wrote, last_line=last_prev)


def body_state():
    try:
        sys.path.insert(0, BASE)
        import body_state
        st = body_state.describe() if hasattr(body_state, "describe") else None
        return str(st)[:80] if st else ""
    except Exception:
        return ""


def build(reason=""):
    """拼出一段"真实素材"，给主动搭话当依据。"""
    now = time.localtime()
    wk = "周一二三四五六日"[now.tm_wday]
    lines = ["【现在是真实信息，不是说给你听的话】",
             "时间：%s %s %d:%02d" % (time.strftime("%F", now), wk, now.tm_hour, now.tm_min)]
    w = weather()
    if w and w.get("temp") is not None:
        lines.append("湘潭天气：%s %s°C（体感 %s°C），今天 %s~%s°C，降水概率 %s%%，湿度 %s%%%s"
                     % (w.get("desc"), w.get("temp"), w.get("feels"),
                        w.get("tmin"), w.get("tmax"), w.get("rain"), w.get("humidity"),
                        "（这条是旧数据）" if w.get("stale") else ""))
    else:
        lines.append("湘潭天气：这次没取到（不要编天气）")
    ch = chat_today()
    if ch["user"]:
        lines.append("最近他说过：%s" % " / ".join(ch["user"]))
    else:
        # 不要留空让人瞎猜：明确说"没听到他说什么"，她就知道别假装知道
        lines.append("最近他没跟你说什么（别假装知道他在干嘛）")
    if ch["count"]:
        lines.append("你这边最近三小时有 %d 条动静" % ch["count"])
    rec = read_recent()
    if rec:
        lines.append("最近看过：%s" % " / ".join(rec))
    d = diary_state()
    lines.append("日记：%s%s" % ("今天已经写过" if d["wrote_today"] else "今天还没写",
                              ("；上次落尾：" + d["last_line"]) if d["last_line"] else ""))
    b = body_state()
    if b:
        lines.append("你的状态：%s" % b)
    lines.append("")
    lines.append("要求：上面是真实情况。挑其中**一件具体的**（比如他现在冷不冷、刚才聊到哪、"
                 "你看到的东西）说一句，带上真实细节；没有具体依据就宁可不发，不要凑话。")
    return "\n".join(lines)


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "manual"))
