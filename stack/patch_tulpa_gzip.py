#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 Tulpa 的 HTTP 层加 gzip/deflate 解压（2026-11-09）。

为什么：
    net.py 请求时声明 `Accept-Encoding: identity`，但 B 站弹幕接口无视它，
    照样返回 gzip → 被 `if Content-Encoding not in ('identity','')` 拒绝，
    表现为「上游返回了不支持的压缩响应」，弹幕读不到。

改法（两处，都不动流式循环）：
    1) 拒绝逻辑：gzip/deflate 记下来放行，其它压缩编码仍拒绝
    2) 返回前：如果记了压缩编码，就对收集到的 body 解压
"""
import io
import os
import subprocess
import sys
import time

PATH = os.environ.get("TULPA_NET", "/opt/tulpa/tulpa_bilibili/net.py")
STAMP = time.strftime("%Y%m%d-%H%M%S")

OLD_CHECK = """                if response.getheader('Content-Encoding', 'identity') not in ('identity', ''):
                    raise Failure('protocol_error', '上游返回了不支持的压缩响应。')"""
NEW_CHECK = """                # 2026-11-09 本地补丁：弹幕等接口无视 identity，仍返回 gzip。
                # 这里只放行标准库能解的编码，其它（br/zstd）继续拒绝。
                _enc = (response.getheader('Content-Encoding') or 'identity').strip().lower()
                if _enc not in ('identity', ''):
                    if _enc not in ('gzip', 'x-gzip', 'deflate'):
                        raise Failure('protocol_error', '上游返回了不支持的压缩响应。')"""

OLD_RET = """                return b''.join(chunks), response.getheaders(), current"""
NEW_RET = """                _body = b''.join(chunks)
                if _enc in ('gzip', 'x-gzip', 'deflate'):
                    _body = _decompress(_body, _enc)
                return _body, response.getheaders(), current"""

HELPER = '''

def _decompress(raw, encoding):
    """解 gzip / deflate（标准库实现，无新依赖）。"""
    import zlib
    if encoding in ('gzip', 'x-gzip'):
        return zlib.decompress(raw, 16 + zlib.MAX_WBITS)
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return zlib.decompress(raw, -zlib.MAX_WBITS)

'''


def main():
    check = "--check" in sys.argv
    s = io.open(PATH, encoding="utf-8").read()
    if "_decompress" in s and "_enc" in s:
        print("已打（含 gzip 解压）")
        return 0
    missing = [n for n, o in (("拒绝逻辑", OLD_CHECK), ("返回语句", OLD_RET)) if o not in s]
    if missing:
        print("!! 锚点没找到:", ",".join(missing))
        return 2
    if check:
        print("未打")
        return 1
    s2 = s.replace(OLD_CHECK, NEW_CHECK, 1).replace(OLD_RET, NEW_RET, 1)
    anchor = "def validate_url("
    s2 = s2.replace(anchor, HELPER.strip("\n") + "\n\n\n" + anchor, 1) if anchor in s2 else s2 + HELPER
    io.open(PATH + ".bak-gz-" + STAMP, "w", encoding="utf-8", newline="\n").write(s)
    io.open(PATH, "w", encoding="utf-8", newline="\n").write(s2)
    r = subprocess.run([sys.executable, "-m", "py_compile", PATH], capture_output=True)
    print("语法:", "OK" if r.returncode == 0 else r.stderr.decode()[:300])
    return 0 if r.returncode == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
