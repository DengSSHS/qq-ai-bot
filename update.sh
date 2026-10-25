#!/usr/bin/env bash
# 更新已装好的机器：拉代码 → 补配置 → 补日程 → 重装 fork/补丁 → 重启 → 自检
#
#   bash update.sh                 # 只用当前仓库内容（先自己 git pull）
#   bash update.sh --pull          # 先 git pull 再更新
#   bash update.sh --dry-run       # 只打印会改什么，不动文件
#
# 设计原则：**只增不改**。已有配置项的值一律保留，只补新键；
# routines 只补缺失的 id，不覆盖你改过的 prompt。
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

PULL=0
DRY=0
for a in "$@"; do
  case "$a" in
    --pull) PULL=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  esac
done

# python：Linux 上是 python3，个别环境只有 python
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "print(1)" >/dev/null 2>&1; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then printf '[失败] 找不到 python3/python\n'; exit 1; fi

DSH_HOME="${DSH_HOME:-/opt/dsh}"
CFG="$DSH_HOME/profiles/web/cordis.patch.yml"
TPL="$PWD/stack/cordis.patch.yml.tpl"
ROU="$DSH_HOME/routines.json"
ROU_TPL="$PWD/stack/config/routines.json.tpl"

say() { printf '\n== %s ==\n' "$*"; }
ok()  { printf '  [OK] %s\n' "$*"; }
warn(){ printf '  [注意] %s\n' "$*"; }
die() { printf '  [失败] %s\n' "$*"; exit 1; }

[ -d "$DSH_HOME" ] || die "找不到 $DSH_HOME —— 这台机器还没装过，先跑 bash deploy.sh"
[ -f "$CFG" ] || die "找不到配置 $CFG"

# ── 1. 代码 ────────────────────────────────────────────────────────────
say "1/6 代码"
if [ "$PULL" = "1" ]; then
  if [ -d .git ]; then
    git pull --ff-only || warn "git pull 失败（本地有改动？），继续用当前代码"
  else
    warn "不是 git 仓库，跳过 pull"
  fi
fi
ok "版本 $(cat VERSION 2>/dev/null || echo '?')"

# ── 2. 配置：只补缺失的键 ─────────────────────────────────────────────
say "2/6 配置补键（不动你已有的值）"
"$PY" - "$CFG" "$TPL" "$DRY" <<'PY'
import io, re, sys

cfg_path, tpl_path, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
cfg = io.open(cfg_path, encoding="utf-8").read().split("\n")
tpl = io.open(tpl_path, encoding="utf-8").read().split("\n")

def key_of(line):
    m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):", line)
    return (m.group(2), len(m.group(1))) if m else (None, None)

present = {key_of(l)[0] for l in cfg if key_of(l)[0]}
added, skipped = [], []

# 在模板里找 cfg 缺失的键，插到 cfg 里"同缩进的最后一个键"之后
for i, line in enumerate(tpl):
    k, indent = key_of(line)
    if not k or k in present or line.lstrip().startswith("#"):
        continue
    # 模板里这个键属于哪个段（找它上面最近的 section 注释）
    block = [line]
    j = i + 1
    while j < len(tpl) and tpl[j].strip() and not key_of(tpl[j])[0]:
        block.append(tpl[j]); j += 1
    # 在 cfg 里找同缩进的插入点：最后一个同缩进键之后
    insert_at = None
    for idx, cl in enumerate(cfg):
        ck, ci = key_of(cl)
        if ck and ci == indent:
            insert_at = idx + 1
    if insert_at is None:
        skipped.append(k)
        continue
    cfg[insert_at:insert_at] = block
    present.add(k)
    added.append(k)

if added:
    print("  新增键：" + ", ".join(added))
else:
    print("  没有需要补的键")
if skipped:
    print("  跳过（找不到插入位置，手动确认）：" + ", ".join(skipped))
if added and not dry:
    io.open(cfg_path, "w", encoding="utf-8", newline="\n").write("\n".join(cfg))
PY
[ "$DRY" = "1" ] || ok "配置已更新"

# ── 3. 日程：补缺失的 routine，不覆盖已有的 ───────────────────────────
say "3/6 主动行为日程"
"$PY" - "$ROU" "$ROU_TPL" "$DRY" <<'PY'
import io, json, os, sys

rou_path, tpl_path, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
tpl = json.load(io.open(tpl_path, encoding="utf-8"))

if not os.path.exists(rou_path):
    if dry:
        print("  routines.json 不存在，将按模板创建（%d 条）" % len(tpl.get("routines", [])))
    else:
        io.open(rou_path, "w", encoding="utf-8", newline="\n").write(
            json.dumps(tpl, ensure_ascii=False, indent=2) + "\n")
        print("  routines.json 不存在 → 已按模板创建（%d 条）" % len(tpl.get("routines", [])))
    raise SystemExit(0)

cur = json.load(io.open(rou_path, encoding="utf-8"))
have = {r.get("id") for r in cur.get("routines", [])}
added = []
for r in tpl.get("routines", []):
    if r.get("id") not in have:
        cur.setdefault("routines", []).append(r)
        added.append(r["id"])

for k, v in tpl.items():
    if k != "routines" and k not in cur:
        cur[k] = v

if added:
    print("  新增 routine：" + ", ".join(added))
else:
    print("  没有需要补的 routine")
if not dry:
    io.open(rou_path, "w", encoding="utf-8", newline="\n").write(
        json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
PY
[ "$DRY" = "1" ] || ok "日程已更新"

# ── 3.5 脚本同步（三方比对，绝不覆盖别人改过的文件）─────────────
say "3.5/6 脚本同步（保护本地改动）"
if [ -f "$PWD/stack/sync_scripts.py" ]; then
  if [ "$DRY" = "1" ]; then
    REPO_DIR="$PWD" "$PY" "$PWD/stack/sync_scripts.py" --check || true
  else
    REPO_DIR="$PWD" "$PY" "$PWD/stack/sync_scripts.py" || warn "脚本同步有问题"
  fi
else
  warn "没有 sync_scripts.py，跳过脚本同步"
fi

# 3.6 limits.json（每天限制）：只补缺失的键，不改已有值
LIM="$DSH_HOME/limits.json"
LIM_TPL="$PWD/stack/config/limits.json.tpl"
if [ -f "$LIM_TPL" ]; then
  if [ "$DRY" = "1" ]; then
    [ -f "$LIM" ] || warn "limits.json 不存在（将按模板创建）"
  else
    "$PY" - "$LIM" "$LIM_TPL" <<'LIMPY'
import io, json, os, sys
lim_p, tpl_p = sys.argv[1], sys.argv[2]
tpl = json.load(io.open(tpl_p, encoding="utf-8"))
if os.path.exists(lim_p):
    cur = json.load(io.open(lim_p, encoding="utf-8"))
    added = [k for k in tpl if k not in cur]
    for k in added:
        cur[k] = tpl[k]
    if added:
        io.open(lim_p, "w", encoding="utf-8", newline="\n").write(
            json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
        print("  limits.json 补了新键：" + ", ".join(added))
    else:
        print("  limits.json 无新键（你改的值保留）")
else:
    io.open(lim_p, "w", encoding="utf-8", newline="\n").write(
        json.dumps(tpl, ensure_ascii=False, indent=2) + "\n")
    print("  limits.json 已创建")
LIMPY
  fi
fi

# ── 4. 插件：重装 fork + 重打补丁 ─────────────────────────────────────
say "4/6 插件与本地补丁"
if [ -x "$PWD/stack/vendor/install_fork.sh" ]; then
  if [ "$DRY" = "1" ]; then
    bash "$PWD/stack/vendor/install_fork.sh" --check || true
  else
    bash "$PWD/stack/vendor/install_fork.sh" || warn "fork 快照重装失败"
  fi
else
  warn "没有 fork 快照，跳过"
fi
if [ -f "$PWD/stack/apply_local_patches.py" ]; then
  if [ "$DRY" = "1" ]; then
    "$PY" "$PWD/stack/apply_local_patches.py" --check || true
  else
    "$PY" "$PWD/stack/apply_local_patches.py" || warn "补丁应用有问题"
  fi
fi

# ── 5. 重启 ────────────────────────────────────────────────────────────
say "5/6 重启服务"
if [ "$DRY" = "1" ]; then
  warn "dry-run：不重启"
else
  # 服务单位可能还没装（旧版安装若在"等扫码"超时就退出了，第 9 步没执行）。
  # 这种情况要先把 unit 装上并 enable，而不是直接报"重启失败"。
  if [ ! -f /etc/systemd/system/dsh-onebot.service ] && [ -d "$PWD/stack/systemd" ]; then
    warn "dsh-onebot.service 不存在 —— 先把 systemd 单位装上"
    cp -f "$PWD/stack/systemd"/*.service "$PWD/stack/systemd"/*.timer \
          "$PWD/stack/systemd"/*.path /etc/systemd/system/ 2>/dev/null
    mkdir -p /etc/systemd/system/dsh-onebot.service.d
    cp -f "$PWD/stack/systemd/dsh-onebot.service.d"/*.conf \
          /etc/systemd/system/dsh-onebot.service.d/ 2>/dev/null
    systemctl daemon-reload
    ok "systemd 单位已安装"
  fi
  # unit 里的 EnvironmentFile 指向这两个文件，缺任何一个服务都起不来。
  # 实测（2026-10-26）：全新机只有 qwen.env，tokenhub.env 缺失 →
  # "Failed to load environment files" → 服务无限 auto-restart。
  [ -f "$DSH_HOME/qwen.env" ] || warn "$DSH_HOME/qwen.env 不存在（模型 key 没写？）"
  [ -f "$DSH_HOME/tokenhub.env" ] || {
    printf '# 可选：备用线路 key\n' > "$DSH_HOME/tokenhub.env"
    chmod 600 "$DSH_HOME/tokenhub.env"
    ok "补建 tokenhub.env（unit 依赖它，没有会起不来）"
  }
  if systemctl restart dsh-onebot 2>/dev/null; then
    sleep 12
    systemctl is-active --quiet dsh-onebot && ok "dsh-onebot 已重启" \
      || warn "dsh-onebot 没起来：journalctl -u dsh-onebot -n 30"
  else
    warn "dsh-onebot 重启失败（配置与脚本已更新，服务需人工拉起）"
  fi
fi

# ── 6. 自检 ────────────────────────────────────────────────────────────
say "6/6 自检"
bash preflight.sh 2>&1 | tail -3
if [ "$DRY" != "1" ] && command -v python3 >/dev/null && [ -f "$DSH_HOME/qqguard.py" ]; then
  "$PY" "$DSH_HOME/qqguard.py" --status 2>/dev/null | head -3
fi

printf '\n------------------------------------------------------------------\n'
if [ "$DRY" = "1" ]; then
  printf '这是 dry-run，什么都没改。去掉 --dry-run 才会真更新。\n'
else
  printf '更新完成。若有 [注意]/[失败]，按提示处理后重跑 bash update.sh 即可（幂等）。\n'
fi
