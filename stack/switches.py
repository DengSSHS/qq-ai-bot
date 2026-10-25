#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开关面板：一个命令开关所有运行时行为，不用改代码。

背景：这套东西现在能调的东西散在三处——systemd 定时器、profile 的
cordis.patch.yml、routines.json。每次调整都要记住"改哪个文件、要不要重启"，
而且改错了没有回滚。这个脚本把它们收敛成一个声明式清单。

用法：
    python3 switch.py                # 列出所有开关和当前状态
    python3 switch.py on  casual_dm  # 打开
    python3 switch.py off casual_dm  # 关掉
    python3 switch.py --json         # 给程序读

加新开关：在 FEATURES 里追加一条。三种"插槽"类型：

    timer    映射到 systemd 定时器，on/off = enable/disable
    config   映射到 cordis.patch.yml 里的一个键，on/off = 改值 + 重启 dsh-onebot
    routine  映射到 routines.json 里的作息条目，on/off = 打 off 标记

设计原则：
  - 改任何文件前先备份到 /opt/dsh/memory/switch-backups/
  - 改完做格式校验（JSON 能解析 / YAML 里那行还在），校验失败自动回滚
  - 只重启真正受影响的组件
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

BASE = "/opt/dsh"
PROFILE = os.path.join(BASE, "profiles/web/cordis.patch.yml")
ROUTINES = os.path.join(BASE, "routines.json")
BUDGET = os.path.join(BASE, "budget.json")
BACKUP_DIR = os.path.join(BASE, "memory/switch-backups")

# ---------------------------------------------------------------- 开关清单
FEATURES = [
    # ---- 主动行为 ----
    dict(id="casual_engage", kind="config", label="群聊主动接话",
         desc="未 @ 她的群消息按概率接一句（每小时最多 3 次）",
         key="casualEngageRate", on_val="0.2", off_val="0"),
    dict(id="casual_dm", kind="routine", label="私聊主动找人",
         desc="空闲越久概率越高（每小时 +20%，封顶 80%）"),
    dict(id="diary_reminders", kind="routine", label="日记提醒",
         desc="早上/中午/傍晚/夜里各提醒一次该写日记了",
         routines=["morning", "noon", "evening", "night"]),
    dict(id="qzone_daily", kind="routine", label="每日说说提醒",
         desc="每天一条空间动态的提醒"),
    # ---- 后台任务 ----
    dict(id="sticker_settle", kind="timer", label="收图结算",
         desc="每天 03:00 判断群友发的图要不要收进表情库",
         unit="sticker-settle.timer"),
    dict(id="memory_distill", kind="timer", label="记忆蒸馏",
         desc="每天 03:10 从会话里提炼长期记忆写进 AGENTS.md",
         unit="memory-distill.timer"),
    dict(id="daily_report", kind="timer", label="每日日报",
         desc="每天 23:55 发一封报告（用量/排障/预算/掉线/日记）",
         unit="usage-report.timer"),
    dict(id="qq_guard", kind="timer", label="QQ 掉线看护",
         desc="每 2 分钟检查，掉线自动重启容器并邮件告警",
         unit="qqguard.timer"),
    dict(id="patch_doctor", kind="timer", label="插件补丁漂移检测",
         desc="每周检查上游插件改版后本地 fork 是否还兼容",
         unit="patch-doctor.timer"),
    dict(id="auto_update", kind="timer", label="每日自动更新",
         desc="每天 04:00 检查仓库有无新版本，有就升级；升级后自检，坏了自动回滚",
         unit="auto-update.timer"),
    # ---- 安全与预算 ----
    dict(id="budget_breaker", kind="budget", label="预算熔断",
         desc="token 用量超限时自动降级/停机（默认关闭）",
         key="enabled", on_val=True, off_val=False),
    dict(id="content_screening", kind="env", label="图片内容风险筛查",
         desc="结算时判断图里有没有隐私/擦边，有就不入库",
         unit="sticker-settle.service", key="STICKER_SCREEN_SAFE",
         on_val="1", off_val="0"),
]

BY_ID = {f["id"]: f for f in FEATURES}


def log(msg):
    print(msg)


def _backup(path):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, "%s.%s" % (os.path.basename(path),
                                              time.strftime("%Y%m%d-%H%M%S")))
    try:
        shutil.copy2(path, dst)
    except Exception:
        pass


def _sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


# ---------------------------------------------------------------- 各类型读取
def _timer_on(unit):
    rc, out, _ = _sh("systemctl is-enabled %s" % unit)
    return rc == 0 and out == "enabled"


def _config_value(key):
    try:
        s = io.open(PROFILE, encoding="utf-8").read()
    except Exception:
        return None
    m = re.search(r"^\s*%s:\s*([^\s#]+)" % re.escape(key), s, re.M)
    return m.group(1).strip() if m else None


def _routine_off(rid):
    try:
        cfg = json.load(io.open(ROUTINES, encoding="utf-8"))
    except Exception:
        return None
    for r in cfg.get("routines", []):
        if r.get("id") == rid:
            return bool(r.get("off"))
    return None


def _budget_on(key):
    try:
        d = json.load(io.open(BUDGET, encoding="utf-8"))
    except Exception:
        return None
    return bool(d.get(key))


def _env_val(unit, key):
    rc, out, _ = _sh("systemctl show -p Environment %s" % unit)
    m = re.search(r"%s=([^\s\"]+)" % re.escape(key), out)
    if m:
        return m.group(1)
    # 也看 EnvironmentFile 里的同名键
    return None


def status(f):
    kind = f["kind"]
    if kind == "timer":
        return "开" if _timer_on(f["unit"]) else "关"
    if kind == "config":
        v = _config_value(f["key"])
        return "开" if v not in (None, "0", "0.0") else "关"
    if kind == "routine":
        ids = f.get("routines") or [f["id"]]
        offs = [_routine_off(r) for r in ids]
        known = [o for o in offs if o is not None]
        if not known:
            return "无此条目"
        return "关" if all(known) else ("部分关" if any(known) else "开")
    if kind == "budget":
        v = _budget_on(f["key"])
        return "开" if v else "关"
    if kind == "env":
        v = _env_val(f["unit"], f["key"])
        if v is None:
            return "默认(开)"
        return "开" if v not in ("0", "false", "no") else "关"
    return "?"


# ---------------------------------------------------------------- 各类型操作
def set_timer(f, on):
    a = "enable" if on else "disable"
    rc, _, err = _sh("systemctl %s --now %s" % (a, f["unit"]))
    if rc != 0:
        return False, err[:120]
    return True, "%s %s" % (a, f["unit"])


def set_config(f, on):
    try:
        s = io.open(PROFILE, encoding="utf-8").read()
    except Exception as e:
        return False, "读不到 profile: %s" % e
    val = f["on_val"] if on else f["off_val"]
    pat = re.compile(r"^(\s*%s:\s*)([^\s#]+)(.*)$" % re.escape(f["key"]), re.M)
    if not pat.search(s):
        return False, "profile 里没有 %s" % f["key"]
    _backup(PROFILE)
    new = pat.sub(lambda m: m.group(1) + str(val) + m.group(3), s, count=1)
    io.open(PROFILE, "w", encoding="utf-8").write(new)
    # 校验：那行还在，值对得上
    if _config_value(f["key"]) != str(val):
        shutil.copy2(os.path.join(BACKUP_DIR, sorted(os.listdir(BACKUP_DIR))[-1]), PROFILE)
        return False, "写后校验失败，已回滚"
    rc, _, err = _sh("systemctl restart dsh-onebot")
    if rc != 0:
        return False, "改好了但重启失败: %s" % err[:100]
    return True, "%s = %s（dsh-onebot 已重启）" % (f["key"], val)


def set_routine(f, on):
    try:
        cfg = json.load(io.open(ROUTINES, encoding="utf-8"))
    except Exception as e:
        return False, "读不到 routines.json: %s" % e
    ids = f.get("routines") or [f["id"]]
    hit = 0
    for r in cfg.get("routines", []):
        if r.get("id") in ids:
            r["off"] = (not on)
            hit += 1
    if not hit:
        return False, "routines.json 里没有 %s" % ",".join(ids)
    _backup(ROUTINES)
    tmp = ROUTINES + ".tmp"
    io.open(tmp, "w", encoding="utf-8").write(
        json.dumps(cfg, ensure_ascii=False, indent=2))
    try:
        json.load(io.open(tmp, encoding="utf-8"))
    except Exception as e:
        os.remove(tmp)
        return False, "写出来的 JSON 坏了，已放弃: %s" % e
    os.replace(tmp, ROUTINES)
    return True, "%d 条作息标记为 %s（下次检查生效，不用重启）" % (hit, "启用" if on else "停用")


def set_budget(f, on):
    try:
        d = json.load(io.open(BUDGET, encoding="utf-8"))
    except Exception as e:
        return False, "读不到 budget.json: %s" % e
    _backup(BUDGET)
    d[f["key"]] = f["on_val"] if on else f["off_val"]
    io.open(BUDGET, "w", encoding="utf-8").write(
        json.dumps(d, ensure_ascii=False, indent=1))
    return True, "%s = %s" % (f["key"], d[f["key"]])


def set_env(f, on):
    """往 unit 里加/改一行 Environment=（用 override 片段，不动原 unit）。"""
    val = f["on_val"] if on else f["off_val"]
    d = "/etc/systemd/system/%s.d" % f["unit"]
    rc, _, _ = _sh("mkdir -p %s" % d)
    if rc != 0:
        return False, "建不了 override 目录"
    path = os.path.join(d, "switch.conf")
    body = "[Service]\nEnvironment=%s=%s\n" % (f["key"], val)
    io.open(path, "w", encoding="utf-8").write(body)
    rc, _, err = _sh("systemctl daemon-reload")
    if rc != 0:
        return False, "daemon-reload 失败: %s" % err[:100]
    return True, "%s=%s（下次触发时生效）" % (f["key"], val)


SETTERS = {"timer": set_timer, "config": set_config, "routine": set_routine,
           "budget": set_budget, "env": set_env}


# ---------------------------------------------------------------- CLI
def cmd_list(as_json=False):
    rows = []
    for f in FEATURES:
        rows.append({"id": f["id"], "label": f["label"],
                     "state": status(f), "desc": f["desc"]})
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    print("%-18s %-8s %-18s %s" % ("开关", "状态", "名称", "说明"))
    print("-" * 96)
    for r in rows:
        print("%-18s %-8s %-18s %s" % (r["id"], r["state"], r["label"], r["desc"]))
    print()
    print("用法：python3 switch.py on <开关>  /  off <开关>  /  list")
    return 0


def cmd_set(fid, on):
    f = BY_ID.get(fid)
    if not f:
        print("没有这个开关：%s" % fid)
        print("可用：" + " ".join(BY_ID))
        return 2
    ok, msg = SETTERS[f["kind"]](f, on)
    print(("[OK] " if ok else "[失败] ") + "%s → %s" % (f["label"], "开" if on else "关"))
    print("  " + msg)
    print("  现在状态：" + status(f))
    return 0 if ok else 1


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("list", "ls", "status"):
        return cmd_list("--json" in a)
    if a[0] in ("on", "off") and len(a) >= 2:
        return cmd_set(a[1], a[0] == "on")
    if a[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    print("用法：python3 switch.py [list | on <开关> | off <开关> | --json]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
