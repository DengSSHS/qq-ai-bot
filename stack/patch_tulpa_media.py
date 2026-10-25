#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 Tulpa 的媒体选流打一处本地补丁（重装后会丢，所以做成脚本）。

问题（2026-10-26 实测）：
    B站开始下发 PCDN 地址 `https://<随机>.edge.mountaintoys.cn:4483/...`，
    而 tulpa_bilibili/net.py 的 validate_url(purpose='media') 只允许
    白名单域名 + 端口 443/空。于是这类地址被判 unsafe_url，媒体下载整体跳过，
    但 job 仍返回 state=succeeded、evidence_level=metadata_only、字幕为空
    —— 娃娃拿到 1KB 空壳，还以为自己"看过"了（10 次读视频里 5 次是这种）。

修法（不放松安全边界）：
    同一份 playurl 响应的 backupUrl 通常还有标准域名
    （upos-sz-mirrorbd.bilivideo.com，443）。所以选流时按
    base → backup 的顺序，挑第一个能通过 validate_url 的地址。
    https / 端口 / 域名白名单 / DNS 钉死 全部照旧。

用法（root）：
    python3 patch_tulpa_media.py            # 打补丁（幂等）
    python3 patch_tulpa_media.py --check     # 只看打没打
"""
import io
import os
import subprocess
import sys

PATH = os.environ.get("TULPA_CONTENT", "/opt/tulpa/tulpa_bilibili/content.py")

HELPER = '''def _media_candidates(stream):
    """一个 dash/durl 流的候选地址：base 优先，其次 backup。

    2026-10-26：B站开始下发 PCDN 地址（*.edge.mountaintoys.cn:4483，非 443 端口），
    validate_url 会拒绝，而同一响应的 backupUrl 里通常还有标准域名的标准端口地址。
    所以把候选都收起来，交给 _pick_media 挑第一个合规的。
    """
    urls = []
    for key in ('baseUrl', 'base_url', 'url'):
        v = stream.get(key) if isinstance(stream, dict) else None
        if isinstance(v, str) and v:
            urls.append(v)
    for key in ('backupUrl', 'backup_url'):
        v = stream.get(key) if isinstance(stream, dict) else None
        if isinstance(v, list):
            urls.extend([u for u in v if isinstance(u, str) and u])
        elif isinstance(v, str) and v:
            urls.append(v)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _pick_media(stream):
    """挑第一个能通过只读白名单的地址；都不行就退回第一个（让下游照常报错）。"""
    cands = _media_candidates(stream)
    for u in cands:
        try:
            validate_url(u, 'media')
            return u
        except Failure:
            continue
    return cands[0] if cands else None


'''

PAIRS = [
    ("MARK", "class Content:", HELPER + "class Content:"),
    (
        "dash",
        """            return dict(video=video.get('baseUrl') or video.get('base_url'),
                        audio=(audio.get('baseUrl') or audio.get('base_url')) if audio else None,
                        duration_seconds=dash.get('duration'), has_audio=bool(audio))""",
        """            return dict(video=_pick_media(video),
                        audio=_pick_media(audio) if audio else None,
                        duration_seconds=dash.get('duration'), has_audio=bool(audio))""",
    ),
    (
        "durl",
        """            return dict(video=segments[0].get('url'), audio=None, has_audio=True,
                        duration_seconds=data.get('timelength', 0)/1000)""",
        """            return dict(video=_pick_media(segments[0]), audio=None, has_audio=True,
                        duration_seconds=data.get('timelength', 0)/1000)""",
    ),
]


def main():
    check_only = "--check" in sys.argv
    if not os.path.isfile(PATH):
        print("[跳过] 没找到 %s（这台机器没装 Tulpa）" % PATH)
        return 0
    s = io.open(PATH, encoding="utf-8").read()

    applied, missing = [], []
    for label, old, new in PAIRS:
        if new in s:
            applied.append(label)
        elif old in s:
            missing.append(label)
        else:
            print("[注意] %s 的锚点没找到（上游改过？）：%s" % (label, old.strip().splitlines()[0][:60]))
            return 2

    if check_only:
        print("Tulpa 媒体选流补丁：%d/%d 处已打（%s）"
              % (len(applied), len(PAIRS), ", ".join(applied) or "-"))
        return 0 if not missing else 1

    if not missing:
        print("已经打过，跳过（%s）" % ", ".join(applied))
        return 0

    bak = PATH + ".bak-tulpa-" + __import__("time").strftime("%Y%m%d-%H%M%S")
    io.open(bak, "w", encoding="utf-8", newline="\n").write(s)
    for label, old, new in PAIRS:
        if new in s:
            continue
        s = s.replace(old, new, 1)
    io.open(PATH, "w", encoding="utf-8", newline="\n").write(s)

    r = subprocess.run([sys.executable, "-m", "py_compile", PATH], capture_output=True)
    if r.returncode != 0:
        io.open(PATH, "w", encoding="utf-8", newline="\n").write(io.open(bak, encoding="utf-8").read())
        print("[失败] 打完后语法不过，已回滚：%s" % r.stderr.decode()[:300])
        return 3

    print("已打补丁：%s（备份 %s）" % (", ".join(missing), bak))
    print("生效需要重启 Tulpa：systemctl restart tulpa-mcp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
