#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一调参接口（面板后端）。

为什么要有这个文件
------------------
可调的东西散在五个地方，格式各不相同：

    limits.json          每天限制（JSON 平铺）
    cordis.patch.yml     插件行为（YAML，注释要保留）
    routines.json        主动行为（JSON 嵌套数组）
    bili_window.conf.json  B 站时间窗（JSON 嵌套数组）
    switches.py          systemd 定时器 / 环境变量开关

将来做面板时，如果每个控件都直接去读写这些文件，面板会被文件格式绑死。
所以这里把每一项都登记成一条**带元数据的记录**，面板只需要：

    python3 panel_api.py list --json        # 拿全部控件描述 + 当前值
    python3 panel_api.py set <id> <value>   # 改一个
    python3 panel_api.py restart            # 需要重启时，面板给个按钮

每条记录长这样（给面板渲染用）：

    {
      "id": "casualEngageRate",        # 稳定标识，永不变
      "group": "群聊行为",              # 分组（面板用它做 tab / 折叠块）
      "label": "主动接话概率",          # 中文名
      "type": "percent",               # bool/int/percent/string/enum/schedule
      "value": 0.2,                    # 当前值
      "default": 0.2,
      "min": 0, "max": 1,              # 数值范围（没有就省略）
      "options": [...],                # enum 的候选值
      "unit": "%",                     # 单位（面板拼在数字后面）
      "desc": "未 @ 她的群消息按此概率被接住",
      "restart": true,                 # 改完是否需要重启 dsh-onebot
      "storage": "cordis.patch.yml"    # 存在哪（排障用）
    }

命令
----
    python3 panel_api.py list               # 人看的表格
    python3 panel_api.py list --json        # 面板用
    python3 panel_api.py get <id>
    python3 panel_api.py set <id> <value>
    python3 panel_api.py restart            # 重启 dsh-onebot 让 restart=true 的项生效
"""
import io
import json
import os
import re
import subprocess
import sys

BASE = os.environ.get("DSH_HOME", "/opt/dsh")
REPO_PROFILE = os.path.join(BASE, "profiles/web")
CORDIS = os.path.join(REPO_PROFILE, "cordis.patch.yml")
COMPAT = os.path.join(REPO_PROFILE, "compatibility.json")
LIMITS = os.path.join(BASE, "limits.json")
ROUTINES = os.path.join(BASE, "routines.json")
BILI_WIN = os.path.join(BASE, "bili_window.conf.json")
BILI_BUDGET = os.path.join(BASE, "bili-budget.json")
SWITCHES = os.path.join(BASE, "switches.py")


# ---------------------------------------------------------------- 存储层
def _read_json(path, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else default
    except Exception:
        return default


def _write_json(path, data):
    """原子写 + 备份。面板可能被连点，不能写坏。"""
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    if os.path.exists(path):
        try:
            with io.open(path, "rb") as f:
                bak = f.read()
            with io.open(path + ".bak-panel", "wb") as f:
                f.write(bak)
        except Exception:
            pass
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


_YAML_LINE = r"^(?P<indent>\s{4})(?P<key>%s):(?P<sp>\s*)(?P<val>[^#\n]*)(?P<rest>.*)$"


def _yaml_get(key, default=None):
    """从 cordis.patch.yml 读一个 4 缩进键的值（不引入 pyyaml，服务器上没有）。"""
    try:
        with io.open(CORDIS, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return default
    m = re.search(_YAML_LINE % re.escape(key), text, re.M)
    if not m:
        return default
    raw = (m.group("val") or "").strip()
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except Exception:
        pass
    try:
        return float(raw)
    except Exception:
        pass
    return raw.strip("'\"")


def _yaml_set(key, value):
    """改 cordis.patch.yml 的值，**保留行尾注释**。"""
    try:
        with io.open(CORDIS, encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return False, "读不到 %s：%s" % (CORDIS, e)
    if isinstance(value, bool):
        new = "true" if value else "false"
    else:
        new = str(value)
    pat = re.compile(_YAML_LINE % re.escape(key), re.M)
    if not pat.search(text):
        return False, "配置里没有键 %s（可能版本不同）" % key
    with io.open(CORDIS + ".bak-panel", "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    text = pat.sub(lambda m: "%s%s:%s%s%s" % (m.group("indent"), m.group("key"),
                                              m.group("sp") or " ", new, m.group("rest")),
                   text, count=1)
    with io.open(CORDIS, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True, "ok"


def _num(v, kind):
    if kind == "bool":
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on", "开")
    if kind == "int":
        return int(float(str(v).strip()))
    if kind == "percent":
        f = float(str(v).strip().rstrip("%"))
        if f > 1.0:          # 面板可能传 20 表示 20%
            f = f / 100.0
        return f
    return str(v).strip()


# ---------------------------------------------------------------- 注册表
def T(item_id, group, label, kind, storage, key, default, desc,
      lo=None, hi=None, options=None, unit=None, restart=False):
    return dict(id=item_id, group=group, label=label, type=kind, storage=storage,
                key=key, default=default, desc=desc, min=lo, max=hi,
                options=options, unit=unit, restart=restart)


TUNABLES = [
    # ---- 群聊行为（cordis.patch.yml，改完要重启）-----------------------
    T("requireMention", "群聊行为", "只在被 @ 时说话", "bool", "yaml", "requireMention",
      True, "关掉后群里每条消息都可能触发她（慎用）", restart=True),
    T("groupActivationMinutes", "群聊行为", "激活窗口（分钟）", "int", "yaml",
      "groupActivationMinutes", 10, "被 @ 后这段时间内，同一个人不用再 @ 也会回",
      lo=0, hi=120, unit="分钟", restart=True),
    T("casualEngageRate", "群聊行为", "主动接话概率", "percent", "yaml",
      "casualEngageRate", 0.2, "未 @ 她的群消息，按此概率被接住并回一句", lo=0, hi=1, unit="%", restart=True),
    T("casualEngageMaxPerHour", "群聊行为", "主动接话/小时", "int", "yaml",
      "casualEngageMaxPerHour", 3, "把一小时切成 N 格，每格最多接一次", lo=1, hi=20, restart=True),
    T("newMessagePolicy", "群聊行为", "她思考时又来消息怎么办", "enum", "yaml",
      "newMessagePolicy", "steer", "steer=合并成一次回答；queue=各自排队各回一次",
      options=["steer", "queue"], restart=True),
    T("stickerNudgeRate", "群聊行为", "表情提醒概率", "percent", "yaml",
      "stickerNudgeRate", 0.6, "每轮按此概率提醒她「要不要配张表情」，她自己决定",
      lo=0, hi=1, unit="%", restart=True),

    # ---- 每天限制（limits.json，立即生效）-----------------------------
    T("qq_action_per_day", "每天限制", "全局发送次数/天", "int", "limits",
      "qq_action_per_day", 2000, "安全阀，撞上会表现为「她完全发不出消息」", lo=10, hi=100000),
    T("qq_action_per_minute", "每天限制", "发送次数/分钟", "int", "limits",
      "qq_action_per_minute", 20, "防刷屏", lo=1, hi=600),
    T("qq_rate_per_minute", "每天限制", "接收处理/分钟", "int", "limits",
      "qq_rate_per_minute", 8, "防消息洪峰", lo=1, hi=600),
    T("bili_share_per_day", "每天限制", "B 站 @ 主人/天", "int", "limits",
      "bili_share_per_day", 10, "评论区 @ 你，每天最多几条；超过就只自己看", lo=0, hi=100),
    T("bili_share_interval_min", "每天限制", "两次 @ 最小间隔", "int", "limits",
      "bili_share_interval_min", 8, "", lo=0, hi=1440, unit="分钟"),
    T("bili_share_same_video_days", "每天限制", "同视频不重复 @", "int", "limits",
      "bili_share_same_video_days", 7, "同一个视频几天内不重复 @", lo=0, hi=90, unit="天"),
    T("dm_per_day", "每天限制", "主动私聊/天", "int", "limits",
      "dm_per_day", 8, "她主动找你说话的次数上限", lo=0, hi=100),
    T("dm_night_per_night", "每天限制", "半夜叫醒/晚", "int", "limits",
      "dm_night_per_night", 1, "失眠那条路线", lo=0, hi=10),

    # ---- B 站（时间窗 + token 预算）-----------------------------------
    T("bili_window", "B 站", "低谷时段（能自己刷）", "schedule", "bili_window",
      "allowed_windows", [["18:00", "08:00"]],
      "跨零点写成 18:00→08:00；改完下一分钟生效"),
    T("bili_daily_auto_opens", "B 站", "每天自动叫醒次数", "int", "bili_window",
      "daily_auto_opens", 6, "低谷期她自己想起来去刷的次数", lo=0, hi=30),
    T("bili_night_max_auto", "B 站", "凌晨最多叫几次", "int", "bili_window",
      "night_max_auto", 2, "00:00-08:00 的上限，给晚上留额度", lo=0, hi=30),
    T("bili_budget_enabled", "B 站", "token 预算", "bool", "bili_budget",
      "enabled", False, "开启后每天浏览的 token 有上限", restart=False),
    T("bili_budget_daily", "B 站", "每日 token 上限", "int", "bili_budget",
      "daily_tokens", 150000, "仅在预算开启时生效", lo=1000, hi=10000000),

    # ---- 主动行为（routines.json）---------------------------------------
    T("routine_casual_dm_off", "主动行为", "关掉主动私聊", "bool", "routine",
      "casual_dm:off", False, "关掉后她不会主动找你说话"),
    T("routine_casual_dm_daily", "主动行为", "主动私聊/天", "int", "routine",
      "casual_dm:dailyBudget", 8, "每天最多主动找你几次", lo=0, hi=100),
    T("routine_casual_dm_ramp", "主动行为", "基础冲动（每小时）", "percent", "routine",
      "casual_dm:baseChance", 0.06, "她本来就想说话的冲动，与「多久没聊」无关",
      lo=0, hi=1, unit="%"),
    T("routine_night_off", "主动行为", "关掉半夜失眠", "bool", "routine",
      "casual_dm_night:off", False, "关掉后半夜不会来找你"),
    T("routine_night_insomnia", "主动行为", "失眠夜概率", "percent", "routine",
      "casual_dm_night:insomniaChance", 0.35, "约三成夜晚会失眠",
      lo=0, hi=1, unit="%"),
    T("routine_night_base", "主动行为", "失眠夜冲动", "percent", "routine",
      "casual_dm_night:baseChance", 0.015, "失眠夜每次检查的概率", lo=0, hi=1, unit="%"),
]


def _routines_doc():
    return _read_json(ROUTINES, {"routines": []})


def _routine_find(doc, rid):
    for r in doc.get("routines", []):
        if r.get("id") == rid:
            return r
    return None


def read_item(item):
    st = item["storage"]
    if st == "yaml":
        v = _yaml_get(item["key"], item["default"])
        # yaml 里 enum/bool 可能被读成字符串，按声明类型归一
        try:
            return _num(v, "bool") if item["type"] == "bool" else v
        except Exception:
            return item["default"]
    if st == "limits":
        return _read_json(LIMITS, {}).get(item["key"], item["default"])
    if st == "bili_window":
        return _read_json(BILI_WIN, {}).get(item["key"], item["default"])
    if st == "bili_budget":
        return _read_json(BILI_BUDGET, {}).get(item["key"], item["default"])
    if st == "routine":
        rid, field = item["key"].split(":", 1)
        r = _routine_find(_routines_doc(), rid)
        if r is None:
            return item["default"]
        return r.get(field, item["default"])
    return item["default"]


def write_item(item, value):
    st = item["storage"]
    if st == "yaml":
        return _yaml_set(item["key"], value)
    if st == "limits":
        d = _read_json(LIMITS, {})
        d[item["key"]] = value
        _write_json(LIMITS, d)
        return True, "ok"
    if st == "bili_window":
        d = _read_json(BILI_WIN, {})
        d[item["key"]] = value
        _write_json(BILI_WIN, d)
        return True, "ok"
    if st == "bili_budget":
        d = _read_json(BILI_BUDGET, {})
        d[item["key"]] = value
        _write_json(BILI_BUDGET, d)
        return True, "ok"
    if st == "routine":
        rid, field = item["key"].split(":", 1)
        doc = _routines_doc()
        r = _routine_find(doc, rid)
        if r is None:
            return False, "routines.json 里没有 %s（可能版本不同）" % rid
        r[field] = value
        _write_json(ROUTINES, doc)
        return True, "ok"
    return False, "未知存储类型 %s" % st


def describe(item):
    d = dict(item)
    d.pop("key", None)
    d["value"] = read_item(item)
    if d["type"] == "percent" and d.get("unit") == "%":
        pass  # 面板自己乘 100 显示
    return d


def validate(item, raw):
    kind = item["type"]
    if kind == "schedule":
        try:
            v = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return None, "时间窗要写成 [[\"18:00\",\"08:00\"]]"
        flat = v[0] if v and isinstance(v[0], list) else v
        if not (isinstance(flat, list) and len(flat) == 2):
            return None, "时间窗要两个时间点，如 [\"18:00\",\"08:00\"]"
        for t in flat:
            if not re.match(r"^\d{1,2}:\d{2}$", str(t)):
                return None, "时间要写成 HH:MM"
        return [list(flat)], "ok"
    if kind == "enum":
        s = str(raw).strip()
        if item.get("options") and s not in item["options"]:
            return None, "只能是：%s" % " / ".join(item["options"])
        return s, "ok"
    try:
        v = _num(raw, kind)
    except Exception:
        return None, "值格式不对（%s）" % kind
    if kind in ("int", "percent"):
        if item.get("min") is not None and v < item["min"]:
            return None, "不能小于 %s" % item["min"]
        if item.get("max") is not None and v > item["max"]:
            return None, "不能大于 %s" % item["max"]
    return v, "ok"


# ---------------------------------------------------------------- 命令
def cmd_list(as_json):
    if as_json:
        print(json.dumps({"items": [describe(i) for i in TUNABLES]},
                         ensure_ascii=False, indent=1))
        return 0
    groups = {}
    for i in TUNABLES:
        groups.setdefault(i["group"], []).append(i)
    for g, items in groups.items():
        print("\n== %s ==" % g)
        for i in items:
            v = read_item(i)
            if i["type"] == "percent" and i.get("unit") == "%":
                shown = "%.0f%%" % (float(v) * 100) if isinstance(v, (int, float)) else str(v)
            elif i["type"] == "bool":
                shown = "开" if v else "关"
            elif i["type"] == "schedule":
                try:
                    shown = "%s-%s" % (v[0][0], v[0][1])
                except Exception:
                    shown = str(v)
            else:
                shown = str(v)
            flag = "（需重启）" if i.get("restart") else ""
            print("  %-26s %-14s %s%s" % (i["label"], shown, i["desc"][:34], flag))
    print("\n改：python3 panel_api.py set <id> <值>     看 id：python3 panel_api.py list --json")
    return 0


def cmd_get(item_id):
    for i in TUNABLES:
        if i["id"] == item_id:
            print(json.dumps(describe(i), ensure_ascii=False, indent=1))
            return 0
    print("没有这个 id：%s" % item_id)
    return 2


def cmd_set(item_id, raw):
    for i in TUNABLES:
        if i["id"] != item_id:
            continue
        v, why = validate(i, raw)
        if v is None:
            print(json.dumps({"ok": False, "id": item_id, "error": why}, ensure_ascii=False))
            return 2
        ok, msg = write_item(i, v)
        print(json.dumps({"ok": ok, "id": item_id, "value": v,
                          "need_restart": bool(i.get("restart")),
                          "message": msg if not ok else "已生效" if not i.get("restart")
                          else "已写入，需要重启 dsh-onebot 才生效"},
                         ensure_ascii=False))
        return 0 if ok else 1
    print(json.dumps({"ok": False, "error": "没有这个 id：%s" % item_id}, ensure_ascii=False))
    return 2


def cmd_restart():
    r = subprocess.run(["systemctl", "restart", "dsh-onebot"],
                       capture_output=True, text=True, timeout=120)
    ok = r.returncode == 0
    print(json.dumps({"ok": ok, "message": "已重启" if ok else (r.stderr or "")[:200]},
                     ensure_ascii=False))
    return 0 if ok else 1


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("list", "ls"):
        return cmd_list("--json" in a)
    if a[0] == "get" and len(a) >= 2:
        return cmd_get(a[1])
    if a[0] == "set" and len(a) >= 3:
        return cmd_set(a[1], " ".join(a[2:]))
    if a[0] == "restart":
        return cmd_restart()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
