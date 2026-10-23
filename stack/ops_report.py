#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运维/排障视角的报表：失败、延迟、工具错误。

和 usage_log.py 的分工：
    usage_log.py   花了多少（token / 费用 / 按天按服务）
    ops_report.py  哪里不对（失败率 / 慢请求 / 哪个工具老出错）

为什么要单独一个：修 bug 的时候「今天 12 万 token」没有用，需要的是
「23:41 那次 upstream 502，耗时 30s」「qq_send_sticker 失败 7 次，全是路径不存在」。

⚠️ 已知限制（别被误报骗了）：
    上半部分（请求/失败/延迟）读 usage.jsonl，每条带自己的时间戳，**时间准确**。
    下半部分（工具调用）读会话文件，只能按**文件修改时间**筛会话 —— 一个跨天的
    长会话文件会把几天前的旧失败一起算进"近 24 小时"。实测踩过：报告显示
    「actionRatePerMinute=2 超限」，其实那是昨天的记录，配置早已改成 20。
    所以工具失败率只能当"近期有没有异常"的信号，要定时间得回会话里翻。

用法：
    python3 ops_report.py --hours 24
    python3 ops_report.py --hours 168 --sessions
"""
import json
import os
import subprocess
import sys
import time

BASE = "/opt/dsh"
USAGE = os.path.join(BASE, "memory/usage.jsonl")
SESSIONS = os.path.join(BASE, "sessions")


def _rows(hours):
    if not os.path.exists(USAGE):
        return []
    since = time.time() - hours * 3600
    out = []
    try:
        with open(USAGE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if int(r.get("ts") or 0) >= since:
                    out.append(r)
    except Exception:
        pass
    return out


def _pct(vals, p):
    if not vals:
        return 0
    vals = sorted(vals)
    i = min(len(vals) - 1, int(round((len(vals) - 1) * p)))
    return vals[i]


def _counts(since=None):
    """统计 usage.jsonl 里各类事件。"""
    if not os.path.exists(USAGE):
        return None
    total = err = nousage = 0
    durs = []
    fails = []
    slow = []
    tokens = {"in": 0, "out": 0, "cached": 0}
    cost = 0.0
    cost_unknown = 0
    for r in _rows(24 * 365):
        if since and int(r.get("ts") or 0) < since:
            continue
        total += 1
        st = r.get("status")
        bad = (st is not None and (isinstance(st, int) and st >= 400
                                   or isinstance(st, str) and st != "200"))
        if bad or r.get("err"):
            err += 1
            if len(fails) < 8:
                fails.append(r)
        if r.get("in") is None and r.get("out") is None:
            nousage += 1
        else:
            tokens["in"] += int(r.get("in") or 0)
            tokens["out"] += int(r.get("out") or 0)
            tokens["cached"] += int(r.get("cached") or 0)
            if r.get("cost") is None:
                cost_unknown += 1
            else:
                cost += float(r["cost"])
        if r.get("dur_ms") is not None:
            durs.append(int(r["dur_ms"]))
            slow.append(r)
    slow.sort(key=lambda x: -int(x.get("dur_ms") or 0))
    return {"total": total, "err": err, "nousage": nousage,
            "durs": durs, "fails": fails, "slow": slow[:5],
            "tokens": tokens, "cost": cost, "cost_unknown": cost_unknown}


def _tool_stats(hours):
    """从会话里统计工具调用与失败。"""
    since = time.time() - hours * 3600
    calls = {}
    fails = {}
    total = 0
    if not os.path.isdir(SESSIONS):
        return None
    for ws in os.listdir(SESSIONS):
        wsd = os.path.join(SESSIONS, ws)
        if not os.path.isdir(wsd):
            continue
        for d in os.listdir(wsd):
            if not d.startswith("onebot-"):
                continue
            f = os.path.join(wsd, d, "session.v4.jsonl.zstd")
            if not os.path.isfile(f) or os.path.getmtime(f) < since:
                continue
            try:
                raw = subprocess.run(["zstd", "-dc", f], capture_output=True,
                                     timeout=120).stdout.decode("utf-8", "replace")
            except Exception:
                continue
            idmap = {}
            for line in raw.split(chr(10)):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                t = r.get("type")
                dd = r.get("data") or {}
                msg = dd.get("message") or {}
                if t == "assistant/message":
                    for part in (msg.get("content") or []):
                        if isinstance(part, dict) and part.get("type") == "tool-call":
                            n = part.get("name") or "?"
                            calls[n] = calls.get(n, 0) + 1
                            total += 1
                            # 结果里只带 toolCallId，不带工具名，所以先记下映射
                            cid = part.get("toolCallId") or part.get("id")
                            if cid:
                                idmap[str(cid)] = n
                elif t == "tool/result":
                    if msg.get("isError"):
                        txt = "".join(str(p.get("text") or "")
                                      for p in (msg.get("content") or [])
                                      if isinstance(p, dict))
                        cid = msg.get("toolCallId") or dd.get("toolCallId")
                        n = str(idmap.get(str(cid)) or msg.get("toolName")
                                or dd.get("toolName") or "?")
                        fails.setdefault(n, []).append(
                            txt.replace(chr(10), " ")[:90] or "(空)")
    return {"total": total, "calls": calls, "fails": fails}


def render(hours=24, with_sessions=False):
    since = time.time() - hours * 3600
    c = _counts(since)
    L = []
    span = "%d 小时" % hours if hours < 48 else "%d 天" % (hours // 24)
    if not c or not c["total"]:
        L.append("近 %s：没有调用记录。" % span)
    else:
        rate = 100.0 * c["err"] / c["total"]
        L.append("近 %s：请求 %d 次｜失败 %d 次（%.1f%%）｜无 usage 的 %d 次"
                 % (span, c["total"], c["err"], rate, c["nousage"]))
        if c["durs"]:
            L.append("延迟：p50 %dms｜p95 %dms｜最慢 %dms"
                     % (_pct(c["durs"], 0.5), _pct(c["durs"], 0.95),
                        max(c["durs"])))
        tk = c["tokens"]
        hit = (100.0 * tk["cached"] / tk["in"]) if tk["in"] else 0.0
        L.append("token：输入 %s｜输出 %s｜缓存命中 %.1f%%"
                 % ("{:,}".format(tk["in"]), "{:,}".format(tk["out"]), hit))
        if c["cost_unknown"]:
            L.append("费用：%d 次有价格 ¥%.4f，%d 次未配置价格"
                     % (c["total"] - c["err"] - c["cost_unknown"],
                        c["cost"], c["cost_unknown"]))
        else:
            L.append("费用：约 ¥%.4f" % c["cost"])

    if c and c["fails"]:
        L.append("")
        L.append("最近的失败（修 bug 先看这里）：")
        for r in reversed(c["fails"][:6]):
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(int(r.get("ts") or 0)))
            L.append("  %s  %-14s %-22s %s"
                     % (when, str(r.get("status")), str(r.get("path") or "")[:22],
                        str(r.get("err") or "")[:60]))

    if c and c["slow"]:
        L.append("")
        L.append("最慢的几次：")
        for r in c["slow"][:4]:
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(int(r.get("ts") or 0)))
            L.append("  %s  %6dms  in=%sB out=%sB  %s"
                     % (when, int(r.get("dur_ms") or 0), r.get("in_bytes"),
                        r.get("out_bytes"), str(r.get("path") or "")[:20]))

    if with_sessions:
        t = _tool_stats(hours)
        if t:
            L.append("")
            nfail = sum(len(v) for v in t["fails"].values())
            rate = (100.0 * nfail / t["total"]) if t["total"] else 0.0
            L.append("工具调用：%d 次｜结果里标失败 %d 次（%.1f%%）"
                     % (t["total"], nfail, rate))
            if t["fails"]:
                L.append("  失败最多的工具：")
                for name, errs in sorted(t["fails"].items(), key=lambda x: -len(x[1]))[:5]:
                    L.append("    %-30s %d 次  例：%s" % (name, len(errs), errs[0][:60]))
            if t["calls"]:
                L.append("  最常调用：")
                for name, n in sorted(t["calls"].items(), key=lambda x: -x[1])[:6]:
                    L.append("    %-30s %d 次" % (name, n))
    return chr(10).join(L)


def main():
    hours, with_sessions = 24, False
    for i, a in enumerate(sys.argv):
        if a == "--hours" and i + 1 < len(sys.argv):
            hours = int(sys.argv[i + 1])
        if a == "--sessions":
            with_sessions = True
    print(render(hours, with_sessions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
