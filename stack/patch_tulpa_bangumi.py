#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 Tulpa 的 B 站只读能力加上「番剧（PGC）」——2026-11-09

背景：她的白名单里只有 /x/web-interface、/x/v2/reply、/x/player、弹幕和 CDN，
**没有 /pgc/**。于是「MyGO」这种番剧她进不去（正片是 /bangumi/play/epXXXX，
走 pgc 接口）；剧场上传版是普通投稿 BV 号，所以之前她能看那个。

改动（三处，都幂等）：
  1. net.py      白名单 + /pgc/view/web/season、/pgc/player/web/playurl
  2. content.py  read_video 接受 ep/ss 号或番剧链接：
                 info() 认出番剧 → 走 season 接口拿分集表
                 play() 认出 ep_id → 走 pgc playurl（用登录 cookie，大会员才解锁）
  3. media.py    不用改（info()/play() 是它唯一入口）

刻意不做的事：
  · 不加新工具、不改任何工具的 schema —— 她原来的 read_video → get_video_job 流程直接可用
  · 不放开 pgc 的其它接口（收藏、追番、投币一律不放，仍然是只读浏览）

用法（服务器上）：
    python3 patch_tulpa_bangumi.py           # 打补丁
    python3 patch_tulpa_bangumi.py --check    # 只看状态
"""
import io
import os
import shutil
import sys

BASE = "/opt/tulpa/tulpa_bilibili"
MARK = "BANGUMI_LOCAL"

ALLOW = ("    '/x/polymer/web-dynamic/v1/feed/all'",
         "    '/x/polymer/web-dynamic/v1/feed/all',\n"
         "    # 2026-11-09 本地补丁（BANGUMI_LOCAL）：番剧详情 + 番剧取流\n"
         "    '/pgc/view/web/season', '/pgc/player/web/playurl'")

HELPERS = '''

# ==== 2026-11-09 本地补丁（BANGUMI_LOCAL）：番剧入口 ====
BANGUMI_EP = re.compile(r'^(?:ep)?[:\\( ]*(\\d{4,12})[)\\) ]*$', re.I)


def parse_bangumi(value):
    """把 'ep12345' / 'ss73077' / 番剧链接 解析成 ('ep'|'ss', 数字)；不是番剧返回 None。"""
    if not isinstance(value, str):
        return None
    v = value.strip()
    m = re.fullmatch(r'(ep)(\\d{4,12})', v, re.I)
    if m:
        return 'ep', int(m.group(2))
    m = re.fullmatch(r'(ss)(\\d{1,12})', v, re.I)
    if m:
        return 'ss', int(m.group(2))
    m = re.search(r'bilibili\\.com/bangumi/play/(ep|ss)(\\d{1,12})', v)
    if m:
        return m.group(1).lower(), int(m.group(2))
    return None


def _pgc_json(client, path, params):
    """pgc 接口的载荷在 result 里，而 ReadHTTP.get_json 只返回 data 字段，
    所以这里自己取原始响应。域名/路径仍然过白名单校验（get_bytes 内部会校验）。"""
    from urllib.parse import urlencode
    url = client.api_base + path + (('?' + urlencode(params)) if params else '')
    raw = client.get_bytes(url, referer='https://www.bilibili.com')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise Failure('protocol_error', 'B站响应格式不兼容。')
    code = data.get('code')
    if code in (-101, -111):
        raise Failure('login_required', 'B站登录已失效，请重新扫码。', status=code)
    if code not in (0, None):
        raise Failure('upstream_rejected', 'B站未提供所请求的内容。', status=code)
    return data


def _pick_streams(data):
    """从 playurl 的返回里挑一路能读的视频/音频（和原来 play() 一样的规则）。"""
    dash = data.get('dash') or {}
    videos = [v for v in dash.get('video', [])
              if v.get('codecs', '').startswith('avc') and v.get('height', 9999) <= 720]
    audios = dash.get('audio') or []
    if videos:
        video = min(videos, key=lambda v: v.get('bandwidth', 10 ** 12))
        audio = min(audios, key=lambda v: v.get('bandwidth', 10 ** 12)) if audios else None
        return dict(video=_pick_media(video),
                    audio=_pick_media(audio) if audio else None,
                    duration_seconds=dash.get('duration'), has_audio=bool(audio))
    segments = data.get('durl') or []
    if len(segments) == 1:
        return dict(video=_pick_media(segments[0]), audio=None, has_audio=True,
                    duration_seconds=data.get('timelength', 0) / 1000)
    return None
'''

INFO_BRANCH = '''    def info(self, video, page=None, *, client=None):
        _bk = parse_bangumi(video)
        if _bk:
            return self._bangumi_info(_bk, page, client=client)
        bvid, selected = normalize_video(video, page, self.net)'''

INFO_METHOD = '''
    # ==== 2026-11-09 本地补丁（BANGUMI_LOCAL）：番剧详情 ====
    def _bangumi_info(self, kind, page=None, *, client=None):
        """番剧的一集，伪装成 info() 的形状，好让 read_video 那条链路直接复用。"""
        client = client or self.client()
        params = {'season_id': page if False else 0}
        params = {'season_id': kind[1]} if kind[0] == 'ss' else {'ep_id': kind[1]}
        data = _pgc_json(client, '/pgc/view/web/season', params)
        r = data.get('result') or {}
        if not r:
            raise Failure('not_found', '没找到这部番剧，可能是地区或版权限制。')
        eps = [e for e in (r.get('episodes') or []) if e.get('id')]
        if not eps:
            raise Failure('not_found', '这部番剧没有可读的分集。')
        if kind[0] == 'ep':
            ep = next((e for e in eps if e.get('id') == kind[1]), None)
        else:
            idx = (page or 1) - 1
            if not 0 <= idx < len(eps):
                raise Failure('invalid_part', '集数超出范围。')
            ep = eps[idx]
        if ep is None:
            raise Failure('not_found', '这一集不在该番剧的分集表里。')
        if type(ep.get('cid')) is not int or ep['cid'] <= 0:
            raise Failure('part_unavailable', '这一集缺少 cid，无法确认内容。')
        duration = float(ep.get('duration') or 0) / 1000.0
        title = str(r.get('title') or '')
        sub = str(ep.get('long_title') or ep.get('title') or '')
        return dict(
            bvid='ep%d' % ep['id'], page=1, cid=ep['cid'], aid=ep.get('aid') or 0,
            title=('%s · %s' % (title, sub)) if sub else title,
            author=str(r.get('season_title') or title), duration_seconds=duration,
            cover=ep.get('cover') or r.get('cover') or '', ep_id=ep['id'],
            season_id=r.get('season_id'), badge=ep.get('badge') or '',
            parts=[dict(page=i + 1, cid=e.get('cid'), duration_seconds=float(e.get('duration') or 0) / 1000.0,
                        title=str(e.get('long_title') or e.get('title') or '')[:300])
                   for i, e in enumerate(eps[:1000])],
            part_count=len(eps), fetched_at=time.time(), source='bilibili_pgc',
            content_trust='untrusted_data', is_preview=bool(ep.get('badge') == '预告'))

    # ==== 2026-11-09 本地补丁（BANGUMI_LOCAL）：番剧取流 ====
    def play_episode(self, info, *, cancel=None, deadline=None):
        """番剧取流走 pgc 接口；用**登录凭据**（大会员才解锁正片，匿名只能试看）。"""
        if not info.get('ep_id'):
            raise Failure('invalid_video', '这不是番剧分集。')
        client = ReadHTTP(self.settings.cookie('content'), net=self.net,
                          deadline=deadline or time.monotonic() + 30, cancel=cancel)
        params = {'ep_id': info['ep_id'], 'qn': 32, 'fnval': 16, 'fourk': 0}
        data = _pgc_json(client, '/pgc/player/web/playurl', params)
        if not isinstance(data, dict):
            raise Failure('media_unavailable', '未取得可读取的番剧资源。')
        body = data.get('result') or data.get('data') or {}
        if body.get('is_preview') or data.get('is_preview'):
            raise Failure('preview_only', '平台只提供试看（这一集需要大会员）。')
        streams = _pick_streams(body)
        if not streams:
            raise Failure('media_unavailable', '没有可读的番剧流。')
        if not streams.get('duration_seconds'):
            streams['duration_seconds'] = info.get('duration_seconds') or 0
        return streams
'''

PLAY_BRANCH = '''    def play(self, info, *, cancel=None, deadline=None):
        # 2026-11-09 本地补丁（BANGUMI_LOCAL）：番剧走 pgc 接口
        if info.get('ep_id'):
            return self.play_episode(info, cancel=cancel, deadline=deadline)
        # Separate media credential, anonymous unless explicitly configured in UI.'''


def rd(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def wr(p, s):
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)


def patch_net(apply_):
    p = os.path.join(BASE, "net.py")
    s = rd(p)
    if "/pgc/view/web/season" in s:
        return "已打"
    if not apply_:
        return "未打"
    if ALLOW[0] not in s:
        return "跳过（net.py 白名单锚点找不到）"
    shutil.copy2(p, p + ".bak-bangumi")
    wr(p, s.replace(ALLOW[0], ALLOW[1], 1))
    return "已打"


def patch_content(apply_):
    p = os.path.join(BASE, "content.py")
    s = rd(p)
    if MARK in s:
        return "已打"
    if not apply_:
        return "未打"
    if "def _pick_media(stream):" not in s or "def play(self, info" not in s:
        return "跳过（content.py 锚点找不到）"
    shutil.copy2(p, p + ".bak-bangumi")
    # ① 辅助函数放到 _pick_media 之前
    s = s.replace("def _pick_media(stream):", HELPERS.strip() + "\n\n\ndef _pick_media(stream):", 1)
    # ② info() 开头分叉（番剧先认，认出来就不走 view 接口）
    head = ("    def info(self, video, page=None, *, client=None):\n"
            "        bvid, selected = normalize_video(video, page, self.net)")
    if head not in s:
        return "跳过（info() 锚点找不到）"
    s = s.replace(head, INFO_BRANCH, 1)
    # ③ 番剧方法插在 play() 之前
    s = s.replace("    def play(self, info, *, cancel=None, deadline=None):",
                  INFO_METHOD.strip("\n") + "\n\n    def play(self, info, *, cancel=None, deadline=None):", 1)
    # ④ play() 分叉
    s = s.replace("    def play(self, info, *, cancel=None, deadline=None):\n"
                  "        # Separate media credential, anonymous unless explicitly configured in UI.",
                  PLAY_BRANCH, 1)
    wr(p, s)
    return "已打"



def patch_tools(apply_):
    """让工具说明提到番剧，否则她不知道 read_video 能吃 ep/ss。"""
    p = os.path.join(BASE, "tools.py")
    s = rd(p)
    if "番剧" in s and "ep1183102" not in s and "ep/ss" in s:
        return "已打"
    if "ep/ss" in s:
        return "已打"
    if not apply_:
        return "未打"
    old = "VIDEO = dict(type='string',minLength=3,maxLength=2048,description='标准 BV 或 HTTPS B站视频/短链；禁止任意 URL。')"
    if old not in s:
        return "跳过（tools.py 里找不到 VIDEO 定义）"
    shutil.copy2(p, p + ".bak-bangumi")
    new = ("VIDEO = dict(type='string',minLength=3,maxLength=2048,"
           "description='标准 BV / 番剧 ep 或 ss 号 / HTTPS B站视频或番剧链接；禁止任意 URL。"
           "番剧直接传 ep1183102 这样的一集（或 ss73077 加 page=第几集）即可。')")
    s = s.replace(old, new, 1)
    wr(p, s)
    return "已打"

def main():
    apply_ = "--check" not in sys.argv
    for name, fn in (("net.py 白名单 +/pgc/", patch_net),
                     ("content.py 番剧详情与取流", patch_content),
                     ("tools.py 说明加番剧", patch_tools)):
        print("  %-28s %s" % (name, fn(apply_)))
    # 语法自检
    venv = "/opt/tulpa/.venv/bin/python"
    if os.path.exists(venv):
        rc = os.system("%s -c 'import sys; sys.path.insert(0, \"/opt/tulpa\"); "
                       "import tulpa_bilibili.content, tulpa_bilibili.net' 2>/dev/null" % venv)
        print("  导入自检:", "通过" if rc == 0 else "失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
