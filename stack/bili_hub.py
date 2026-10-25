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
import sys
import time
import urllib.request

BASE = "/opt/dsh"
CONF = os.path.join(BASE, "bili_share.conf.json")
LOG = os.path.join(BASE, "memory/bili-share.jsonl")
WATCHED = os.path.join(BASE, "memory/bili-watched.jsonl")
TULPA_URL = "http://127.0.0.1:18777/mcp"
TULPA_CONN = "/tmp/conn.json"

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
    try:
        return json.load(io.open(TULPA_CONN, encoding="utf-8"))["token"]
    except Exception:
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


def reply(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + chr(10))
    sys.stdout.flush()


def _read_quality(res):
    """读视频的结果到底能不能用——把"成功但空"如实降级。

    Tulpa 在媒体下载失败时仍然返回 state=succeeded，只是字幕空、没有帧、
    evidence_level 掉成 metadata_only（2026-10-25 实测 10 次里 5 次这样）。
    对她来说"成功但空"最坑：她会当成自己看过了。

    反过来，只有帧没有字幕是**合法**结果（有些视频本来没有字幕/CC），
    所以 entries 和 frames 有一个就算读到了。
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
    """read_video 是异步任务：这里替她把排队等完，省得她手动轮询又等不及。

    2026-10-25 实测：她 2 秒查一次、查 3~4 次就放弃，
    有个 job 在她最后一次查询之后 6 秒才完成，结果她自己没取到。
    """
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
    if name == "bili_budget":
        import bili_budget
        bili_budget.ensure_conf()
        return {"ok": True, **bili_budget.status()}

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
        if name in ("read_video", "get_video_job"):
            record_watched(name, args, res)
        return res
    return {"ok": False, "error": {"code": "unknown_tool", "message": name}}


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
                "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                "isError": False}})
        elif mid is not None:
            reply({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": "unsupported: %s" % method}})


if __name__ == "__main__":
    main()
