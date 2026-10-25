#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tulpa 看护：进程活着但卡死时，systemd 看不出来，这个能看出来。

systemd 已经负责了三件事：
    Restart=always      进程死了自动拉起
    MemoryMax=450M      内存超了只杀它自己（cgroup 内 OOM）
    OOMScoreAdjust=500  整机内存吃紧时优先杀它

它管不了的：
    进程在、内存也没超，但 MCP 端口不再应答（线程卡死、锁死、DNS 挂住）
    这种情况从 systemd 看是 active，从她那边看是"工具报错"

本脚本每 2 分钟做三件事：
    1. 探 18777 是否应答（401 也算活，那是缺鉴权）
    2. 连续 3 次不应答 → systemctl restart tulpa-mcp
    3. 顺带盯可用内存，低于阈值时记一笔（不杀，只告警——杀由内核按 oom 分数决定）
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "/opt/dsh"
STATE = os.path.join(BASE, "memory/tulpa-guard.json")
LOG = os.path.join(BASE, "memory/tulpa-guard.log")
MCP = "http://127.0.0.1:18777/mcp"
UNIT = "tulpa-mcp"
FAIL_LIMIT = 3
MEM_WARN_MB = 250


def log(msg):
    line = "%s %s" % (time.strftime("%F %T"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + chr(10))
    except Exception:
        pass
    print(line)


def load():
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {"fails": 0, "last_restart": 0}


def save(st):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, STATE)
    except Exception:
        pass


def mcp_alive():
    """发一个 initialize；能收到任何 HTTP 响应（含 401）就算活着。"""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05",
                                  "capabilities": {},
                                  "clientInfo": {"name": "guard", "version": "1"}}}).encode()
    req = urllib.request.Request(MCP, data=body,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500          # 401/403/406 都说明服务在应答
    except Exception:
        return False


def mem_available_mb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return -1


def systemctl(*args):
    r = subprocess.run(["systemctl"] + list(args), capture_output=True, text=True)
    return r.returncode, (r.stdout or "").strip()


def check(apply_=True):
    st = load()
    now = int(time.time())
    running = systemctl("is-active", UNIT)[1] == "active"
    alive = mcp_alive() if running else False
    avail = mem_available_mb()

    if not running:
        if apply_:
            log("进程不在（systemd 应当已自动拉起；这里再兜一次）")
            systemctl("start", UNIT)
        return "未运行，已尝试启动"

    if alive:
        if st.get("fails"):
            log("恢复正常（此前连续失败 %d 次）" % st["fails"])
        st["fails"] = 0
        save(st)
        msg = "在线"
        if avail >= 0 and avail < MEM_WARN_MB:
            msg += "（可用内存仅 %d MB，注意）" % avail
            if st.get("mem_warned") != time.strftime("%F"):
                log("可用内存偏低：%d MB（不会杀它，交给内核按 oom 分数处理）" % avail)
                st["mem_warned"] = time.strftime("%F")
                save(st)
        return msg

    st["fails"] = int(st.get("fails") or 0) + 1
    if st["fails"] < FAIL_LIMIT:
        save(st)
        return "不应答（%d/%d）" % (st["fails"], FAIL_LIMIT)

    if now - int(st.get("last_restart") or 0) < 600:
        return "不应答，但 10 分钟内刚重启过，先不重复重启"

    if not apply_:
        return "不应答 %d 次（dry-run，不重启）" % st["fails"]

    log("MCP 连续 %d 次不应答，重启 %s" % (st["fails"], UNIT))
    systemctl("restart", UNIT)
    st["fails"] = 0
    st["last_restart"] = now
    save(st)
    time.sleep(10)
    return "已重启，" + ("恢复" if mcp_alive() else "仍未恢复（看 journalctl -u %s）" % UNIT)


if __name__ == "__main__":
    print(check(apply_="--dry-run" not in sys.argv))
