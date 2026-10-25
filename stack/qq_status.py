#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 登录状态：问 NapCat 自己的 OneBot 接口，而不是猜日志。

为什么：今晚三次误判都来自"从日志推断状态"——
  · NapCat 有时不打「登录成功」，于是判成掉线（人明明在线）
  · 风控下线可能不写明显日志，于是判成在线（人明明掉线）
接口 `get_status` 返回 {"online": true/false}，这是唯一可信的来源。

用法：
    python3 qq_status.py            # 打印状态，在线退出码 0，掉线 1，未知 2
    python3 qq_status.py --json
"""
import io
import json
import os
import re
import subprocess
import sys
import urllib.request

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
CFG_DIR = "/opt/qqbot/data/config"
HOSTS = ["172.18.0.2", "127.0.0.1"]      # 容器 IP / 本机映射
PORTS = [3000, 3001]


def _token():
    import glob
    for p in glob.glob(os.path.join(CFG_DIR, "onebot11_*.json")) + glob.glob(os.path.join(CFG_DIR, "onebot11.json")):
        try:
            cfg = json.loads(io.open(p, encoding="utf-8").read())
        except Exception:
            continue
        for k in ("token", "accessToken", "access_token"):
            if cfg.get(k):
                return str(cfg[k])
        for hs in (cfg.get("network") or {}).get("httpServers") or []:
            if hs.get("token"):
                return str(hs["token"])
    return ""


def _call(action, token, timeout=8):
    body = json.dumps({}).encode()
    last = ""
    for host in HOSTS:
        for port in PORTS:
            req = urllib.request.Request("http://%s:%d/%s" % (host, port, action), data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            if token:
                req.add_header("Authorization", "Bearer " + token)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:
                last = str(e)[:80]
                continue
    return {"_err": last}


def status():
    tok = _token()
    st = _call("get_status", tok)
    if "_err" in st:
        # 接口不通：退一步看容器在不在
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}} {{.Status}}"], capture_output=True)
        up = "napcat Up" in (r.stdout or b"").decode("utf-8", "replace")
        return {"online": False, "reason": "接口不通（%s）" % st["_err"], "container": up}
    data = st.get("data") or {}
    online = bool(data.get("online"))
    out = {"online": online, "good": bool(data.get("good")), "raw": st.get("message", "")}
    if not online:
        r = subprocess.run(
            "docker logs --since 30m napcat 2>&1 | grep -a -c '请扫描\\|账号已离线\\|风险'",
            shell=True, capture_output=True)
        out["risk_hint"] = int((r.stdout or b"").decode().strip() or 0)
    else:
        info = _call("get_login_info", tok)
        out["uin"] = (info.get("data") or {}).get("user_id")
        out["nick"] = (info.get("data") or {}).get("nickname")
    return out


def main():
    s = status()
    if "--json" in sys.argv:
        print(json.dumps(s, ensure_ascii=False))
    else:
        if s.get("online"):
            print("[在线] %s (%s) good=%s" % (s.get("nick"), s.get("uin"), s.get("good")))
        else:
            print("[掉线] %s 容器在跑=%s 风险/扫码日志=%s"
                  % (s.get("reason"), s.get("container"), s.get("risk_hint")))
    return 0 if s.get("online") else 1


if __name__ == "__main__":
    sys.exit(main())
