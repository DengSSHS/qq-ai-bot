"""排行榜 / UP 主投稿 / 关注动态（2026-11-09 本地新增）。

关键约定（踩过才写下来）：`HttpClient.get_json()` **已经解包了平台的 {code,data} 信封**，
返回的就是 data 本体。所以这里直接读 data 内的字段，不要再套一层 "data"。

  · ranking   —— 最近什么火（不依赖个性化推荐，新号也不会一片空白）
  · up_videos —— 追一个 UP 主的全部视频（现在只能按单个 BV 号读）
  · following —— 她关注的 UP 主更新了什么（新号没关注 → 返回空页，不是报错）
"""

from __future__ import annotations

import html
import re
from typing import Any

from ..errors import BilibiliError
from ..models import Page, SearchItem, space_url, to_local_time
from ._signing import fetch_nav, sign_params

_EM_RE = re.compile(r"</?em[^>]*>", re.IGNORECASE)


def _clean(text: Any) -> str:
    return html.unescape(_EM_RE.sub("", str(text or "")))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _secs(value: Any) -> float | None:
    """时长：排行榜/投稿给的是秒数，动态给的是 "12:34" 文本。"""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        nums = [int(x) for x in text.split(":")]
    except ValueError:
        return None
    total = 0
    for n in nums:
        total = total * 60 + n
    return float(total)


def _item(bvid: str, title: Any, author: Any, mid: Any,
          duration: Any, summary: Any, published: Any) -> SearchItem:
    """SearchItem 是 slots dataclass，字段必须给全（可空的显式 None）。"""
    author = str(author or "") or None
    mid_i = _int_or_none(mid)
    return SearchItem(
        title=_clean(title) or "(无标题)",
        url="https://www.bilibili.com/video/" + bvid if bvid else "",
        author=author,
        author_url=(space_url(mid_i) if mid_i else None),
        published_at=to_local_time(published),
        duration_sec=_secs(duration),
        category=None,
        tags=None,
        summary=(_clean(summary)[:3000] or None),
        view_count=None,
        danmaku_count=None,
        comment_count=None,
        like_count=None,
        favorite_count=None,
    )


def _page(items, offset: int, limit: int) -> Page:
    chunk = items[offset: offset + limit]
    more = offset + limit < len(items)
    return Page(items=chunk, has_more=more,
                next_cursor=(str(offset + limit) if more else None))


def fetch_ranking(client, *, limit: int = 20, cursor: str | None = None) -> Page:
    """全站排行榜（get_json 已解包，直接读 list）。"""
    offset = int(cursor) if cursor else 0
    body = client.get_json("/x/web-interface/ranking/v2", {"rid": 0, "type": "all"}) or {}
    raw = body.get("list") or []
    if not raw:
        raise BilibiliError("排行榜暂时拿不到（平台返回空）")
    items = []
    for v in raw:
        bvid = str(v.get("bvid") or "")
        if not bvid:
            continue
        owner = v.get("owner") if isinstance(v.get("owner"), dict) else {}
        items.append(_item(bvid, v.get("title"), owner.get("name"), owner.get("mid"),
                           v.get("duration"), v.get("desc"), v.get("pubdate")))
    return _page(items, offset, limit)


def fetch_up_videos(client, *, mid: int, limit: int = 20, cursor: str | None = None) -> Page:
    """某个 UP 主的投稿（WBI 签名，按发布时间倒序）。"""
    if not isinstance(mid, int) or mid <= 0:
        raise BilibiliError("mid 必须是正整数（UP 主的 UID）")
    pn = (int(cursor) if cursor else 0) + 1
    nav = fetch_nav(client)
    params = sign_params(
        {"mid": mid, "ps": min(limit, 30), "pn": pn, "order": "pubdate", "platform": "web"},
        nav.img_key, nav.sub_key,
    )
    body = client.get_json("/x/space/wbi/arc/search", params) or {}
    vlist = (body.get("list") or {}).get("vlist") or []
    total = _int_or_none((body.get("page") or {}).get("count")) or 0
    if not vlist:
        # 可能真没投稿、可能设了隐私；都如实说，别静默当空
        raise BilibiliError("这个 UP 主没有可读的公开投稿（共 0 条）")
    items = []
    for v in vlist[: min(limit, 30)]:
        bvid = str(v.get("bvid") or "")
        if not bvid:
            continue
        owner = v.get("owner") if isinstance(v.get("owner"), dict) else {}
        items.append(_item(bvid, v.get("title"), owner.get("name") or v.get("author"),
                           mid, v.get("length") or v.get("duration"),
                           v.get("description"), v.get("created") or v.get("pubdate")))
    has_more = pn * min(limit, 30) < total
    return Page(items=items, has_more=has_more,
                next_cursor=(str(pn) if has_more else None))


def fetch_following(client, *, limit: int = 20, cursor: str | None = None) -> Page:
    """关注动态里的视频投稿。新号没关注任何人 → 空页（不是错误）。"""
    offset = int(cursor) if cursor else 0
    body = client.get_json("/x/polymer/web-dynamic/v1/feed/all",
                           {"type": "video", "page": 1, "offset": ""}) or {}
    raw = body.get("items") or []
    items = []
    for it in raw:
        modules = it.get("modules") if isinstance(it.get("modules"), dict) else {}
        major = ((modules.get("module_dynamic") or {}).get("major")) or {}
        arc = major.get("archive") or {}
        bvid = str(arc.get("bvid") or "")
        if not bvid:
            continue
        author = modules.get("module_author") or {}
        items.append(_item(bvid, arc.get("title"), author.get("name"), author.get("mid"),
                           arc.get("duration_text"), arc.get("desc"), author.get("pub_ts")))
    return _page(items, offset, limit)
