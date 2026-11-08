#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 Tulpa 加「读弹幕」能力（2026-11-09）。

为什么加：
    现在 read_video 给她的是「字幕 + 几帧画面」，缺的正是 B 站最核心的信息——
    **观众在哪笑、在哪刷梗**。弹幕是社交信号，字幕是台词，两者不是一回事。

改动三处（都在 vendor 的 tulpa_bilibili 里）：
    net.py     API_PATHS 放行 /x/v1/dm/list.so（只读、仍是 api.bilibili.com）
    content.py 新增 Content.danmaku()：拉 XML、解析、按时间排序、限制条数
    tools.py   注册 get_video_danmaku 工具 + 分派（并把 assert 的 10 改成 11）

幂等：已打过就跳过；源文件各留一份 .bak-dm-<时间>。

用法（root）：
    python3 patch_tulpa_danmaku.py            # 打补丁
    python3 patch_tulpa_danmaku.py --check    # 只看打没打
"""
import io
import os
import subprocess
import sys
import time

ROOT = os.environ.get("TULPA_ROOT", "/opt/tulpa/tulpa_bilibili")
STAMP = time.strftime("%Y%m%d-%H%M%S")

NET_OLD = """API_PATHS = frozenset(('/x/web-interface/nav', '/x/web-interface/view',
    '/x/web-interface/wbi/search/type', '/x/web-interface/wbi/index/top/feed/rcmd',
    '/x/v2/reply/wbi/main', '/x/v2/reply/reply', '/x/player/wbi/v2', '/x/player/wbi/playurl'))"""
NET_NEW = """# 2026-11-09 本地补丁：放行弹幕 XML（只读，仍是 api.bilibili.com 的同一域名）
API_PATHS = frozenset(('/x/web-interface/nav', '/x/web-interface/view',
    '/x/web-interface/wbi/search/type', '/x/web-interface/wbi/index/top/feed/rcmd',
    '/x/v2/reply/wbi/main', '/x/v2/reply/reply', '/x/player/wbi/v2', '/x/player/wbi/playurl',
    '/x/v1/dm/list.so'))"""

DANMAKU_METHOD = '''
    # ---- 2026-11-09 本地补丁：读弹幕（观众在哪笑、刷什么梗）----
    def danmaku(self, video, page=None, limit=200):
        """拉一条视频的弹幕。返回 [{t: 秒, text: ...}]，按时间排序。

        弹幕是观众反应，不是视频内容：读它等于看「大家在哪一段笑了」。
        list.so 返回 XML，不走 JSON 通道，所以用 get_bytes 拿原文再解析。
        """
        import re as _re
        from html import unescape as _unescape

        bvid, _ = normalize_video(video, http=self.net)
        client = self.client()
        info = self.info(bvid, page=page, client=client)
        cid = info.get('cid')
        if not cid:
            raise Failure('danmaku_unavailable', '拿不到视频 cid，无法读弹幕。')
        url = 'https://api.bilibili.com/x/v1/dm/list.so?oid=%s&type=1' % cid
        raw = client.get_bytes(url)
        try:
            text = raw.decode('utf-8', 'replace')
        except Exception:
            text = str(raw)
        out = []
        for m in _re.finditer(r'<d p="([^"]*)">(.*?)</d>', text, _re.S):
            parts = m.group(1).split(',')
            try:
                t = float(parts[0])
            except Exception:
                continue
            out.append({'t': round(t, 2), 'text': _unescape(m.group(2))[:200]})
        out.sort(key=lambda x: x['t'])
        if limit and len(out) > limit:
            # 均匀采样，保留整段时间的分布，而不是只取前 N 条
            step = len(out) / float(limit)
            out = [out[int(i * step)] for i in range(limit)]
        return dict(bvid=bvid, count=len(out), items=out,
                    note='弹幕是观众反应，不是视频本身的内容')

'''

TOOL_SCHEMA = """    schema('get_video_danmaku','读一条视频的弹幕（观众反应：哪段在笑、在刷什么梗）。弹幕是观众观点，不是视频内容。',
           dict(video=VIDEO,page=PAGE,limit=dict(type='integer',minimum=1,maximum=500,default=200)),['video']),
"""

TOOL_DISPATCH_OLD = """            elif name=='get_video_comments': result=self.content.comments(**args)"""
TOOL_DISPATCH_NEW = """            elif name=='get_video_comments': result=self.content.comments(**args)
            elif name=='get_video_danmaku': result=self.content.danmaku(**args)"""


def apply_one(path, pairs, check_only):
    s = io.open(path, encoding="utf-8").read()
    changed, missing = [], []
    for label, old, new in pairs:
        if new in s:
            changed.append(label)
            continue
        if old not in s:
            missing.append(label)
            continue
        if check_only:
            missing.append(label + "(未打)")
            continue
        s = s.replace(old, new, 1)
        changed.append(label)
    if not check_only and changed and not missing:
        bak = path + ".bak-dm-" + STAMP
        if not os.path.exists(bak):
            io.open(bak, "w", encoding="utf-8", newline="\n").write(io.open(path, encoding="utf-8").read())
        io.open(path, "w", encoding="utf-8", newline="\n").write(s)
    return changed, missing


def main():
    check_only = "--check" in sys.argv
    plan = [
        (os.path.join(ROOT, "net.py"), [("net.API_PATHS", NET_OLD, NET_NEW)]),
        (os.path.join(ROOT, "content.py"), [("content.danmaku", "    def comments(self, video, sort='hot'",
                                             DANMAKU_METHOD + "    def comments(self, video, sort='hot'")]),
        (os.path.join(ROOT, "tools.py"), [
            ("tools.schema", "REGISTRY = [", "REGISTRY = [\n" + TOOL_SCHEMA),
            ("tools.dispatch", TOOL_DISPATCH_OLD, TOOL_DISPATCH_NEW),
            ("tools.assert", "assert len(NAMES)==10", "assert len(NAMES)==11"),
        ]),
    ]
    total_missing = []
    for path, pairs in plan:
        if not os.path.isfile(path):
            print("[跳过] 没有", path)
            continue
        changed, missing = apply_one(path, pairs, check_only)
        print("%-12s 已打: %s | 待打: %s" % (os.path.basename(path),
                                          ",".join(changed) or "-", ",".join(missing) or "-"))
        total_missing += missing

    if not check_only:
        for p in (os.path.join(ROOT, "net.py"), os.path.join(ROOT, "content.py"), os.path.join(ROOT, "tools.py")):
            r = subprocess.run([sys.executable, "-m", "py_compile", p], capture_output=True)
            if r.returncode != 0:
                print("[失败] 语法错误:", p, r.stderr.decode()[:200])
                return 3
        print("语法自检通过；重启生效：systemctl restart tulpa-mcp")
    return 0 if not total_missing else 1


if __name__ == "__main__":
    sys.exit(main())
