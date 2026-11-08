#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Tulpa 自己的 venv 跑 B 站扫码登录（等价于设置页的「扫码登录」按钮）。

为什么不用设置页：那个页面挂在桌面端进程里，这台服务器上没有；但它底下的
Content.start_login / poll_login 就是同一个实现，所以这里直接调，凭据仍然写进
同一个加密库（data/bilibili/private/credentials.aead），不绕过它自己的存储。

二维码 180 秒过期，人手慢一点就废了，所以这里自动换码（默认最多 6 轮 ≈ 18 分钟），
每换一次都重写 /tmp/bili_qr.png 并打印 QR_READY#n。

用法（服务器上，后台跑）：
    nohup /opt/tulpa/.venv/bin/python /opt/dsh/bili_qr_login.py > /tmp/bili_login.log 2>&1 &
"""
import io
import json
import sys
import time

sys.path.insert(0, "/opt/tulpa")

from tulpa_bilibili.content import Content            # noqa: E402
from tulpa_bilibili.net import SafeHTTP               # noqa: E402
from tulpa_bilibili.settings import Settings          # noqa: E402

ROOT = "/opt/tulpa/data/bilibili"
QR_PNG = "/tmp/bili_qr.png"
ROUNDS = 6


def say(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def account(settings):
    """直接查 nav，拿到账号名 / 大会员状态，用来核对登录的是哪个号。"""
    try:
        raw = SafeHTTP().get("https://api.bilibili.com/x/web-interface/nav",
                             purpose="api", cookie=settings.cookie())
        body = json.loads(raw[0].decode("utf-8", "replace") if isinstance(raw, tuple)
                           else raw.decode("utf-8", "replace"))
    except Exception as e:
        return {"error": str(e)[:120]}
    d = body.get("data") or {}
    due = d.get("vipDueDate") or 0
    return {
        "code": body.get("code"),
        "uname": d.get("uname"),
        "mid": d.get("mid"),
        "vip": d.get("vipStatus"),
        "vip_due": time.strftime("%Y-%m-%d", time.localtime(due / 1000)) if due else None,
    }


def main():
    settings = Settings(ROOT)
    content = Content(settings, net=SafeHTTP())
    say("登录前状态：%s" % json.dumps(content.login_status(), ensure_ascii=False))
    say("登录前账号：%s" % json.dumps(account(settings), ensure_ascii=False))

    for rnd in range(1, ROUNDS + 1):
        url = content.start_login()
        import segno
        stream = io.BytesIO()
        segno.make(url).save(stream, kind="png", scale=7, border=3)
        with open(QR_PNG, "wb") as f:
            f.write(stream.getvalue())
        say("QR_READY#%d %s" % (rnd, QR_PNG))

        deadline = time.time() + 165
        last = None
        while time.time() < deadline:
            try:
                st = content.poll_login()
            except Exception as e:
                say("轮询出错：%s" % e)
                break
            state = st.get("state")
            if state != last:
                say("状态：%s" % state)
                last = state
            if state == "success":
                break
            if state == "expired":
                break
            time.sleep(3)
        if last == "success":
            say("LOGIN_OK %s" % json.dumps(account(settings), ensure_ascii=False))
            return 0
        if last is None:
            break
        say("这一轮没扫上（%s），换一张码继续等" % last)

    say("LOGIN_FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
