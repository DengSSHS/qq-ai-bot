#!/usr/bin/env bash
# 每天凌晨自动检查仓库更新，有就更新，更新完自检；坏了自动回滚。
#
#   bash auto_update.sh              # 正常跑（systemd 定时器用它）
#   bash auto_update.sh --force      # 不管有没有新提交都跑一遍 update.sh
#   bash auto_update.sh --check      # 只检查有没有更新，不动
#
# 关闭方式（三种都行）：
#   python3 /opt/dsh/switches.py off auto_update
#   systemctl disable --now auto-update.timer
#   touch /opt/dsh/memory/auto-update.disabled
set -uo pipefail

BASE="${DSH_HOME:-/opt/dsh}"
LOG="$BASE/memory/auto-update.log"
STATE="$BASE/memory/auto-update-state.json"
FLAG="$BASE/memory/auto-update.disabled"
PY="$(command -v python3 || command -v python || true)"

MODE="run"
case "${1:-}" in
  --force) MODE="force" ;;
  --check) MODE="check" ;;
esac

mkdir -p "$(dirname "$LOG")"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; printf '%s\n' "$*"; }
notify() { [ -n "$PY" ] && "$PY" "$BASE/notify.py" "$1" "$2" 2>/dev/null || true; }

# ── 找仓库：有 .git 且含 update.sh 的目录 ──────────────────────────────
find_repo() {
  if [ -n "${REPO_DIR:-}" ] && [ -d "$REPO_DIR/.git" ]; then echo "$REPO_DIR"; return; fi
  for d in "$BASE/repo" "/opt/qq-ai-bot" "/root/qq-ai-bot" "/opt/qqbot/repo" "$BASE"; do
    if [ -d "$d/.git" ] && [ -f "$d/update.sh" ]; then echo "$d"; return; fi
  done
  echo ""
}

# ── 开关检查 ───────────────────────────────────────────────────────────
if [ "$MODE" != "check" ]; then
  if [ -f "$FLAG" ]; then
    log "跳过：存在 $FLAG（自动更新被手动停用）"
    exit 0
  fi
  if [ -n "$PY" ] && [ -f "$BASE/switches.py" ]; then
    if "$PY" "$BASE/switches.py" 2>/dev/null | grep -q '^auto_update *关'; then
      log "跳过：switches.py 里 auto_update = 关"
      exit 0
    fi
  fi
fi

# ── 保护一：对话进行中不升级 ───────────────────────────────────────────
# 升级要重启 dsh，正在进行的回合会被打断。最近 15 分钟有人说过话就跳过。
ACTIVE="$(find "$BASE/sessions" -name 'session.v4.jsonl.zstd' -newermt '-15 minutes' 2>/dev/null | head -1)"
if [ -n "$ACTIVE" ]; then
  log "跳过：15 分钟内有对话活动（$(basename "$(dirname "$ACTIVE")")），不打断"
  exit 0
fi

# ── 保护二：别和后台任务撞 ─────────────────────────────────────────────
for u in memory-distill.service sticker-settle.service; do
  if systemctl is-active --quiet "$u" 2>/dev/null; then
    log "跳过：$u 正在运行（等它跑完再来）"
    exit 0
  fi
done

REPO="$(find_repo)"
if [ -z "$REPO" ]; then
  log "跳过：找不到 git 仓库（tarball 部署的机器没有自更新能力，请用 update.sh 手动更新）"
  exit 0
fi
cd "$REPO" || exit 1

CUR_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
CUR_VER="$(cat VERSION 2>/dev/null || echo '?')"

# ── 查更新 ─────────────────────────────────────────────────────────────
# 国内到 github 经常抽（实测：一次成功一次 timeout），必须重试；
# 实在不行用镜像（可用 REMOTE_URL 覆盖仓库 remote）。
FETCH_OK=0
for attempt in 1 2 3; do
  if git fetch --quiet --prune origin 2>>"$LOG"; then FETCH_OK=1; break; fi
  log "第 $attempt 次 git fetch 失败，10 秒后重试"
  sleep 10
done
if [ "$FETCH_OK" != "1" ]; then
  log "检查失败：连续 3 次 git fetch 不成功（网络问题？），保持现状不动"
  exit 0
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"

# detached HEAD（比如有人 git checkout 到某个 tag）→ pull 永远不能快进，
# 而脚本以前会误报成"本地有改动"。这里先回到远端分支。
if [ "$BRANCH" = "HEAD" ]; then
  DETACHED_AT="$(git rev-parse --short HEAD)"
  log "当前是 detached HEAD（停在 $DETACHED_AT），尝试切回 origin/main"
  if git checkout -q -B main "origin/main" 2>>"$LOG"; then
    BRANCH=main
    # 切分支会改变工作区内容，之前取的版本号/commit 已过期，必须重新取
    CUR_COMMIT="$(git rev-parse --short HEAD)"
    CUR_VER="$(cat VERSION 2>/dev/null || echo '?')"
    # 切分支已经把工作区代码换成新版，但 /opt/dsh 里部署的还是旧的，
    # 所以即使 BEHIND=0 也必须跑一次 update.sh，否则会误报“无更新”。
    NEED_DEPLOY=1
    log "已切回 main（$CUR_COMMIT / $CUR_VER）"
  else
    log "切回 main 失败（工作区可能有未提交改动），本次放弃。处理办法：git stash 或 git checkout main"
    exit 1
  fi
fi
BEHIND="$(git rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo 0)"

if [ "$BEHIND" = "0" ] && [ "$MODE" != "force" ]; then
  if [ "${NEED_DEPLOY:-0}" != "1" ]; then
    log "无更新（当前 $CUR_VER / $CUR_COMMIT）"
    exit 0
  fi
  log "代码已是最新（$CUR_VER），但刚切过分支，补一次部署"
fi

NEW_VER="$(git show "origin/$BRANCH:VERSION" 2>/dev/null || echo '?')"
log "发现 $BEHIND 个新提交：$CUR_VER($CUR_COMMIT) → $NEW_VER"

if [ "$MODE" = "check" ]; then
  log "check 模式，到此为止（要真更新请直接运行 bash auto_update.sh）"
  exit 0
fi

# ── 记录回滚点 ─────────────────────────────────────────────────────────
printf '{"from_commit":"%s","from_version":"%s","at":"%s"}\n' \
  "$CUR_COMMIT" "$CUR_VER" "$(date -Is)" > "$STATE"

# ── 更新 ───────────────────────────────────────────────────────────────
if ! git pull --ff-only --quiet 2>>"$LOG"; then
  log "失败：git pull 没能快进。常见原因：工作区有未提交改动，或本地有提交与远端分叉。"
  log "  处理办法：cd <仓库> && git stash（保留改动）或 git status 看清楚再决定。"
  exit 1
fi
NEW_COMMIT="$(git rev-parse --short HEAD)"
log "代码已更新：$NEW_COMMIT / $(cat VERSION 2>/dev/null || echo '?')"

if ! bash update.sh >> "$LOG" 2>&1; then
  log "update.sh 返回非零，回滚到 $CUR_COMMIT"
  git reset --hard "$CUR_COMMIT" >> "$LOG" 2>&1
  bash update.sh >> "$LOG" 2>&1 || log "回滚后 update.sh 仍失败，需人工介入"
  notify "自动更新失败并已回滚" "更新到 $NEW_COMMIT 时失败，已回滚到 $CUR_COMMIT（$CUR_VER）。详见 $LOG"
  exit 1
fi

# ── 更新后自检 ─────────────────────────────────────────────────────────
sleep 5
FAILED=""
WARN_ONLY=""
systemctl is-active --quiet dsh-onebot || FAILED="dsh-onebot 没起来"
if [ -z "$FAILED" ] && [ -n "$PY" ] && [ -f "$BASE/qqguard.py" ]; then
  out="$("$PY" "$BASE/qqguard.py" --status 2>/dev/null | head -1)"
  case "$out" in
    *断开*)
      # QQ 未连接分两种：没登录（环境状态，不怪代码）/ 登录了但掉了（真故障）。
      # 没登录时回滚毫无意义，反而会把刚升上去的好代码退回去。
      if ls /opt/qqbot/data/config/onebot11_*.json >/dev/null 2>&1; then
        FAILED="QQ 掉线了（$out）"
      else
        WARN_ONLY="QQ 还没登录（这台机器没扫码），不算更新失败"
      fi
      ;;
  esac
fi
if [ -n "$WARN_ONLY" ]; then
  log "自检：$WARN_ONLY"
fi

if [ -n "$FAILED" ]; then
  log "自检失败：$FAILED —— 回滚"
  git reset --hard "$CUR_COMMIT" >> "$LOG" 2>&1
  bash update.sh >> "$LOG" 2>&1 || log "回滚后 update.sh 仍失败"
  notify "自动更新后自检失败，已回滚" "$FAILED。已回滚到 $CUR_VER($CUR_COMMIT)。详见 $LOG"
  exit 1
fi

FINAL_VER="$(cat VERSION 2>/dev/null || echo '?')"
log "更新成功：$CUR_VER → $FINAL_VER"
notify "已自动更新到 $FINAL_VER" \
  "凌晨自动检查发现新版本并已更新：$CUR_VER → $FINAL_VER，自检通过。要关闭自动更新：python3 /opt/dsh/switches.py off auto_update"
exit 0
