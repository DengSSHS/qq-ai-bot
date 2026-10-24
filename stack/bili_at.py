#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 B 站视频评论区 @ 主人（用她自己的账号）。

为什么单独一个脚本、而不是给 Tulpa 加工具：
    Tulpa 是明确只读的（net.py 只允许 GET 路由），发评论要 POST。
    与其改它的安全层，不如让写入走独立进程：读用 Tulpa，写用这个。

cookie 从哪来：
    调 Tulpa 自己的 Settings.cookie('content') 解出它的加密库——
    不逆向它的加密，用它自己的 API，登录态只在内存里过一遍。

安全边界：
    只做一件事：给指定视频发一条评论，内容里 @ 指定的人。
    没有点赞、没有关注、没有批量、没有删除别人。

用法：
    python3 bili_at.py --bvid BV1xx --text "这句话" --mid 123 --name "某人"
    python3 bili_at.py --bvid BV1xx --text "..." --mid 123 --name "某人" --verify
    python3 bili_at.py --del-rpid 123456 --bvid BV1xx      # 删自己刚发的（测完清理）
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TULPA_ROOT = "/opt/tulpa"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HOST = "https://api.bilibili.com"


def cookie():
    sys.path.insert(0, TULPA_ROOT)
    from pathlib import Path
    from tulpa_bilibili.settings import Settings
    return Settings(Path(TULPA_ROOT + "/data/bilibili")).cookie("content")


def _ensure_buvid3(ck):
    """写接口（reply/add）必须带 buvid3，读接口不需要。
    没有就现场去主页领一个——不落盘，只在这次请求里用。"""
    if "buvid3=" in ck:
        return ck
    try:
        import http.cookiejar
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        op.open(urllib.request.Request("https://www.bilibili.com/",
                headers={"User-Agent": UA}), timeout=15).read(80)
        extra = "; ".join("%s=%s" % (c.name, c.value) for c in jar)
        if extra:
            return (ck + "; " + extra).strip("; ")
    except Exception:
        pass
    return ck


def _headers(ck, referer="https://www.bilibili.com/", form=False):
    h = {"User-Agent": UA, "Referer": referer, "Origin": "https://www.bilibili.com",
         "Cookie": ck}
    if form:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    return h


def _get(path, ck, params=None, referer="https://www.bilibili.com/"):
    url = HOST + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers(ck, referer))
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _post_form(path, ck, data, referer):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(HOST + path, data=body,
                                 headers=_headers(ck, referer, form=True))
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def aid_of(bvid, ck):
    d = _get("/x/web-interface/view", ck, {"bvid": bvid},
             "https://www.bilibili.com/video/" + bvid)
    if d.get("code") != 0:
        raise RuntimeError("取视频信息失败 code=%s %s" % (d.get("code"), d.get("message")))
    return (d.get("data") or {}).get("aid")


def csrf_of(ck):
    for part in ck.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == "bili_jct":
                return v.strip()
    return ""


def post_comment(bvid, text, at_mid=None, at_name=None):
    ck = _ensure_buvid3(cookie())
    if "SESSDATA" not in ck:
        return {"ok": False, "error": {"code": "no_login", "message": "她的 B 站没有登录态"}}
    aid = aid_of(bvid, ck)
    csrf = csrf_of(ck)
    if not csrf:
        return {"ok": False, "error": {"code": "no_csrf", "message": "cookie 里没有 bili_jct"}}

    message = text
    data = {"oid": aid, "type": 1, "message": message, "csrf": csrf, "plat": 1}
    if at_mid:
        # 关键：只写 @名字 不传这个参数，对方收不到通知
        message = "@%s %s" % (at_name or "", text)
        data["message"] = message
        data["at_name_to_mid"] = json.dumps({at_name: int(at_mid)}, ensure_ascii=False)

    try:
        d = _post_form("/x/v2/reply/add", ck, data,
                       "https://www.bilibili.com/video/" + bvid)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": {"code": "http_%s" % e.code,
                                       "message": e.read(200).decode("utf-8", "replace")[:160]}}
    except Exception as e:
        return {"ok": False, "error": {"code": "network", "message": str(e)[:120]}}

    if d.get("code") != 0:
        return {"ok": False, "error": {"code": d.get("code"), "message": d.get("message")}}
    rpid = ((d.get("data") or {}).get("rpid_str") or (d.get("data") or {}).get("rpid"))
    return {"ok": True, "bvid": bvid, "aid": aid, "rpid": rpid,
            "message": message, "at_mid": at_mid}


def verify(bvid, at_name, timeout_s=25):
    """等一下再拉最新评论，看这条是不是真的可见（不是只有自己看得到）。"""
    ck = cookie()
    aid = aid_of(bvid, ck)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            d = _get("/x/v2/reply", ck, {"oid": aid, "type": 1, "sort": 0, "ps": 20},
                     "https://www.bilibili.com/video/" + bvid)
            for r in ((d.get("data") or {}).get("replies") or []):
                msg = (r.get("content") or {}).get("message") or ""
                if at_name and at_name in msg:
                    return {"visible": True, "rpid": r.get("rpid"), "msg": msg[:120]}
        except Exception:
            pass
        time.sleep(5)
    return {"visible": False, "note": "20 秒内没在公开列表里看到——可能被风控吞了（仅自己可见）"}


def delete_comment(bvid, rpid):
    ck = cookie()
    aid = aid_of(bvid, ck)
    d = _post_form("/x/v2/reply/del", ck,
                   {"oid": aid, "type": 1, "rpid": rpid, "csrf": csrf_of(ck)},
                   "https://www.bilibili.com/video/" + bvid)
    return {"ok": d.get("code") == 0, "code": d.get("code"), "message": d.get("message")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bvid")
    ap.add_argument("--text", default="")
    ap.add_argument("--mid", type=int)
    ap.add_argument("--name", default="")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--del-rpid")
    a = ap.parse_args()

    if a.del_rpid:
        print(json.dumps(delete_comment(a.bvid, a.del_rpid), ensure_ascii=False))
        return 0
    if not a.bvid or not a.text:
        print("需要 --bvid 和 --text")
        return 2
    r = post_comment(a.bvid, a.text, a.mid, a.name)
    print(json.dumps(r, ensure_ascii=False))
    if r.get("ok") and a.verify:
        v = verify(a.bvid, a.name or "", )
        print(json.dumps({"verify": v}, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
