#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看护哨兵：体检失败 / 她掉线 → 发邮件给主人。

为什么要有这个文件：
    今晚出现过多次"看起来正常其实没工作"（插件没激活、账号掉线 40 分钟没人发现）。
    人不在电脑前的时候，唯一能叫醒主人的渠道是邮件。所以把 checkall 的结论接上邮件。

规则：
    · 失败项集合（指纹）变了，或距上次发信超过 60 分钟 → 发信
    · 同样的故障不重复轰炸（一小时内只发一封）
    · 从"有失败"变成"全部通过" → 发一封恢复信（不静默）
    · 只在真有 FAIL 时发；WARN 不发（避免噪音）

用法：
    python3 health_mail.py            # 检查并按需发信
    python3 health_mail.py --force    # 强制发一封当前状态
    python3 health_mail.py --dry      # 只打印，不发信
"""
import io
import json
import os
import re
import subprocess
import sys
import time

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
sys.path.insert(0, BASE)
STATE = os.path.join(BASE, "memory", "health_state.json")
DEDUPE_SEC = 3600


def log(msg):
    try:
        with io.open(os.path.join(BASE, "memory", "health_mail.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def rdj(p, d):
    try:
        return json.loads(io.open(p, encoding="utf-8").read())
    except Exception:
        return d


def wrj(p, obj):
    tmp = p + ".tmp"
    io.open(tmp, "w", encoding="utf-8", newline="\n").write(json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def run_checkall():
    try:
        r = subprocess.run([sys.executable, os.path.join(BASE, "checkall.py")],
                           capture_output=True, timeout=300)
        out = (r.stdout or b"").decode("utf-8", "replace")
        return out, r.returncode
    except Exception as e:
        return "checkall 执行失败: %s" % e, 2


def parse(out):
    fails, warns, summary = [], [], ""
    in_fail = False
    for line in out.split("\n"):
        t = line.strip()
        if t.startswith("失败 ") and "项" in t:
            summary = t
        if t.startswith("需要修"):
            in_fail = True
            continue
        if t.startswith("提醒"):
            in_fail = False
            continue
        if in_fail and t.startswith("- "):
            fails.append(t[2:].strip())
        elif t.startswith("[WARN]"):
            warns.append(t.replace("[WARN]", "").strip())
    return fails, warns, summary


def main():
    force = "--force" in sys.argv
    dry = "--dry" in sys.argv
    out, rc = run_checkall()
    fails, warns, summary = parse(out)
    st = rdj(STATE, {})
    now = time.time()

    fingerprint = "|".join(sorted(f.strip()[:60] for f in fails))
    prev_fp = st.get("fingerprint", "")
    last_sent = float(st.get("last_sent") or 0)
    was_bad = bool(prev_fp)

    if not fails and not force:
        # 恢复信：之前有过故障，现在好了
        if was_bad:
            body = ("之前报过的问题已经恢复。\n\n" + (summary or "") + "\n\n"
                    "上次告警时间：" + time.strftime("%Y-%m-%d %H:%M", time.localtime(last_sent)) + "\n")
            if not dry:
                import mailer
                ok, msg = mailer.send("【恢复】鲸鲸系统检查已通过", body)
                log("恢复信: %s %s" % (ok, msg))
                wrj(STATE, {"fingerprint": "", "last_sent": 0})
            print("状态恢复，已发恢复信" if not dry else "（dry）状态恢复")
        else:
            print("全部通过（无失败项），不发信")
        return 0

    if not force:
        same = (fingerprint == prev_fp)
        if same and now - last_sent < DEDUPE_SEC:
            print("同样的故障，%.0f 分钟前刚发过，跳过" % ((now - last_sent) / 60))
            return 0

    body = ["体检失败，需要你看一眼。", "",
            summary or ("失败 %d 项" % len(fails)), ""]
    if fails:
        body.append("失败项：")
        for f in fails[:12]:
            body.append("  - " + f)
    if warns:
        body += ["", "提醒（不阻塞）："]
        for w in warns[:8]:
            body.append("  - " + w)
    body += ["", "排查入口：", "  ssh 到服务器后：cd /opt/dsh && python3 checkall.py",
             "  完整输出：/tmp/checkall.out（若存在）",
             "", "时间：" + time.strftime("%Y-%m-%d %H:%M:%S")]
    text = "\n".join(body)

    if dry:
        print("（dry）会发送：\n" + text[:800])
        return 0

    try:
        import mailer
        ok, msg = mailer.send("【告警】鲸鲸系统体检失败（%d 项）" % len(fails), text)
    except Exception as e:
        ok, msg = False, str(e)[:120]
    log("告警: %s %s" % (ok, msg))
    wrj(STATE, {"fingerprint": fingerprint, "last_sent": now, "fails": fails[:12]})
    print("已发告警邮件：%s" % msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
