#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 Tulpa 加三个「自主找内容」的入口（2026-11-09）。

    get_ranking_videos   全站排行榜（最近什么火，不依赖个性化推荐）
    get_up_videos        某个 UP 主的投稿列表（现在只能按单个 BV 号读）
    get_following_feed   关注动态里的视频（她关注的 UP 更新了什么）

实现方式：新增 vendor 文件 `_vendor/polylens/api/_extra_feed.py`（源码在
/opt/dsh/tulpa_extra_feed.py，本脚本负责拷进去），然后三处接线：
    net.py     白名单放行 ranking / arc/search / dynamic feed
    content.py 加 ranking / up_videos / following 三个方法（复用 _page 分页）
    tools.py   注册三个工具 + 分派 + assert 计数

幂等；留 .bak-extra-<时间>。用法：python3 patch_tulpa_extra.py [--check]
"""
import io
import os
import shutil
import subprocess
import sys
import time

ROOT = os.environ.get("TULPA_ROOT", "/opt/tulpa/tulpa_bilibili")
VENDOR = os.path.join(ROOT, "_vendor/polylens/api")
SRC = os.environ.get("EXTRA_SRC", "/opt/dsh/tulpa_extra_feed.py")
STAMP = time.strftime("%Y%m%d-%H%M%S")

NET_OLD = "'/x/v1/dm/list.so'))"
NET_NEW = ("'/x/v1/dm/list.so',\n"
           "    '/x/web-interface/ranking/v2', '/x/space/wbi/arc/search',\n"
           "    '/x/polymer/web-dynamic/v1/feed/all'))")

CONTENT_METHODS = '''
    # ---- 2026-11-09 本地补丁：自主找内容的三个入口 ----
    def ranking(self, limit=10, cursor=None):
        """全站排行榜。不需要登录也能用（新号不会一片空白）。"""
        client = self.client()
        binding = (self.settings.load()['generation'], 'ranking')
        def load(remote):
            page = fetch_ranking(client, limit=20, cursor=remote)
            return [public_item(x) for x in page.items], page.next_cursor
        return self._page(limit, cursor, binding, load)

    def up_videos(self, mid, limit=10, cursor=None):
        """某个 UP 主的投稿（按发布时间倒序）。mid 是他的 UID。"""
        client = self.client()
        binding = (self.settings.load()['generation'], 'up', int(mid))
        def load(remote):
            page = fetch_up_videos(client, mid=int(mid), limit=20, cursor=remote)
            return [public_item(x) for x in page.items], page.next_cursor
        return self._page(limit, cursor, binding, load)

    def following(self, limit=10, cursor=None):
        """关注动态里的视频投稿（需要登录）。"""
        client = self.client()
        binding = (self.settings.load()['generation'], 'following')
        def load(remote):
            page = fetch_following(client, limit=20, cursor=remote)
            return [public_item(x) for x in page.items], page.next_cursor
        return self._page(limit, cursor, binding, load)

'''

IMPORT_OLD = "from ._vendor.polylens.api._feed import fetch_feed"
IMPORT_NEW = ("from ._vendor.polylens.api._feed import fetch_feed\n"
              "from ._vendor.polylens.api._extra_feed import (fetch_ranking, fetch_up_videos,\n"
              "                                              fetch_following)")

TOOL_SCHEMAS = """    schema('get_ranking_videos','读全站排行榜：最近什么火。不依赖个性化推荐，新号也能用。',
           dict(limit=dict(type='integer',minimum=1,maximum=20,default=10),cursor=CURSOR)),
    schema('get_up_videos','读某个 UP 主的投稿列表（按发布时间倒序）。mid 是他的 UID。',
           dict(mid=dict(type='integer',minimum=1,maximum=99999999999999999),limit=dict(type='integer',minimum=1,maximum=20,default=10),cursor=CURSOR),['mid']),
    schema('get_following_feed','读关注动态里的视频投稿（她关注的 UP 更新了什么）。需要登录。',
           dict(limit=dict(type='integer',minimum=1,maximum=20,default=10),cursor=CURSOR)),
"""

DISPATCH_OLD = "            elif name=='get_video_danmaku': result=self.content.danmaku(**args)"
DISPATCH_NEW = (DISPATCH_OLD + "\n"
                "            elif name=='get_ranking_videos': result=self.content.ranking(**args)\n"
                "            elif name=='get_up_videos': result=self.content.up_videos(**args)\n"
                "            elif name=='get_following_feed': result=self.content.following(**args)")


def patch(path, pairs, check_only):
    s = io.open(path, encoding="utf-8").read()
    done, todo = [], []
    for label, old, new in pairs:
        if new in s:
            done.append(label)
        elif old in s:
            todo.append((label, old, new))
        else:
            return done, [label + "(锚点没找到)"], s
    if check_only or not todo:
        return done, [t[0] for t in todo], s
    for _label, old, new in todo:
        s = s.replace(old, new, 1)
    bak = path + ".bak-extra-" + STAMP
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    io.open(path, "w", encoding="utf-8", newline="\n").write(s)
    return done + [t[0] for t in todo], [], s


def main():
    check = "--check" in sys.argv
    if not os.path.isfile(SRC):
        print("!! 找不到源码", SRC)
        return 2

    target = os.path.join(VENDOR, "_extra_feed.py")
    if os.path.isfile(target) and io.open(target, encoding="utf-8").read() == io.open(SRC, encoding="utf-8").read():
        print("vendor 文件已是最新")
    elif check:
        print("vendor 文件待更新")
    else:
        shutil.copy2(SRC, target)
        print("已拷入 vendor/_extra_feed.py")

    plan = [
        (os.path.join(ROOT, "net.py"), [("net.白名单", NET_OLD, NET_NEW)]),
        (os.path.join(ROOT, "content.py"), [
            ("content.import", IMPORT_OLD, IMPORT_NEW),
            ("content.方法", "    def comments(self, video, sort='hot'", CONTENT_METHODS + "    def comments(self, video, sort='hot'"),
        ]),
        (os.path.join(ROOT, "tools.py"), [
            ("tools.schema", "REGISTRY = [", "REGISTRY = [\n" + TOOL_SCHEMAS),
            ("tools.dispatch", DISPATCH_OLD, DISPATCH_NEW),
            ("tools.assert", "assert len(NAMES)==11", "assert len(NAMES)==14"),
        ]),
    ]
    missing = []
    for path, pairs in plan:
        done, todo, _ = patch(path, pairs, check)
        print("%-11s 已打: %s | 待打: %s" % (os.path.basename(path),
                                          ",".join(done) or "-", ",".join(todo) or "-"))
        missing += todo

    if not check:
        for p in (os.path.join(ROOT, "net.py"), os.path.join(ROOT, "content.py"),
                  os.path.join(ROOT, "tools.py"), target):
            r = subprocess.run([sys.executable, "-m", "py_compile", p], capture_output=True)
            if r.returncode != 0:
                print("[失败] 语法错误:", p, r.stderr.decode()[:300])
                return 3
        print("语法自检通过；systemctl restart tulpa-mcp 生效")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
