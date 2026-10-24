#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 NapCat 开一个 HTTP API，让 qqguard 能绕过机器人直接发私聊。

为什么需要：注入通道（qq-inject.jsonl）的消息不会自动回发到 QQ。
结果 qqguard 写完“已恢复，已告知主人”，主人手机上什么都没收到。
有了这个 HTTP 口，告警由服务器直接发，不经过模型。

用法（登录过一次之后跑一次即可，幂等）：

    python3 scripts/enable_napcat_http.py

它会：
  1. 在 /opt/qqbot/data/config/onebot11_<QQ号>.json 里加 httpServers（端口 3000）
  2. token 复用反向 WS 那个（不新增密钥）
  3. 绑 0.0.0.0，但只在 docker 内网可达；宿主机用容器 IP 访问，公网碰不到

改完必须重启 NapCat 容器才生效 —— 注意：重启通常要重新扫码登录。
"""
import glob
import io
import json
import os
import shutil
import sys

CFG_DIR = "/opt/qqbot/data/config"
PORT = 3000


def find_cfg():
    """挑出活动账号的 onebot 配置（有 websocketClients 指向 dsh 的那个）。"""
    cands = sorted(glob.glob(os.path.join(CFG_DIR, "onebot11_*.json")))
    if not cands:
        return None
    for path in cands:
        try:
            d = json.load(io.open(path, encoding="utf-8"))
        except Exception:
            continue
        if (d.get("network") or {}).get("websocketClients"):
            return path
    return cands[0]


def main():
    path = find_cfg()
    if not path:
        print("找不到 onebot 配置（%s/onebot11_*.json）。先让机器人登录一次。" % CFG_DIR)
        return 1

    raw = io.open(path, encoding="utf-8").read()
    d = json.loads(raw)
    net = d.setdefault("network", {})

    token = ""
    clients = net.get("websocketClients") or []
    if clients:
        token = clients[0].get("token") or ""

    entry = {
        "enable": True,
        "name": "http",
        "host": "0.0.0.0",
        "port": PORT,
        "enableCors": False,
        "enableWebsocket": False,
        "messagePostFormat": "array",
        "token": token,
        "debug": False,
    }

    servers = net.get("httpServers") or []
    for i, s in enumerate(servers):
        if s.get("name") == "http" or s.get("port") == PORT:
            servers[i] = entry
            break
    else:
        servers.append(entry)
    net["httpServers"] = servers

    shutil.copy2(path, path + ".bak")
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    os.chmod(path, 0o600)

    print("已写入: %s" % path)
    print("  端口 %d / token %s" % (PORT, (token[:6] + "…") if token else "(空)"))
    print("  ⚠ 需要重启 NapCat 容器才生效，重启后一般要重新扫码。")
    print("  验证：python3 /opt/dsh/qqguard.py --status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
