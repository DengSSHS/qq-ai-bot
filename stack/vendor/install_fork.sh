#!/usr/bin/env bash
# 把 vendor 里的 fork 快照装进 node_modules，并在装完做语法自检。
#
# 为什么需要：这个插件本来就是个本地 fork（除了我们加的 5 处补丁，还叠着
# 约 500 行既有改动）。`npm install` 会用 registry 的原版覆盖 lib/，
# 所有改动静默消失——所以每次 npm 之后都要重装一遍 fork。
#
# 用法：
#   bash install_fork.sh              # 用最新快照
#   bash install_fork.sh fork-20261024
#   bash install_fork.sh --check      # 只对比，不动文件
set -uo pipefail

BASE="${DSH_HOME:-/opt/dsh}"
VENDOR="$BASE/vendor"
PKG="dsh-onebot-qq"
LIVE="$BASE/profiles/web/node_modules/$PKG"
NODE="${NODE_BIN:-/opt/node/bin/node}"

if [ ! -d "$VENDOR" ]; then
  echo "[skip] 没有 $VENDOR，这台机器用的是未 fork 的原版插件"
  exit 0
fi

SNAP="${1:-}"
case "$SNAP" in
  --check) CHECK_ONLY=1; SNAP="" ;;
  "") CHECK_ONLY=0 ;;
  *) CHECK_ONLY=0 ;;
esac

if [ -z "$SNAP" ]; then
  SNAP=$(ls -1d "$VENDOR"/fork-* 2>/dev/null | sort | tail -1)
fi
[ -n "$SNAP" ] || { echo "[fail] $VENDOR 下没有 fork-* 快照"; exit 1; }
SNAP_DIR="$VENDOR/$(basename "$SNAP")"

if [ "$CHECK_ONLY" = "1" ]; then
  echo "最新快照：$(basename "$SNAP_DIR")"
  if diff -rq "$SNAP_DIR/lib" "$LIVE/lib" >/dev/null 2>&1; then
    echo "[OK  ] node_modules 里的 lib 和快照一致"
  else
    echo "[DIFF] node_modules 已被覆盖或不一致："
    diff -rq "$SNAP_DIR/lib" "$LIVE/lib" 2>/dev/null | head -10
    exit 2
  fi
  exit 0
fi

[ -d "$LIVE" ] || { echo "[fail] 找不到 $LIVE（profile 依赖还没装？）"; exit 1; }

rm -rf "$LIVE/lib"
cp -a "$SNAP_DIR/lib" "$LIVE/lib"
[ -f "$SNAP_DIR/package.json" ] && cp -f "$SNAP_DIR/package.json" "$LIVE/package.json"
echo "[OK  ] 已装入 fork：$(basename "$SNAP_DIR")"

if [ -x "$NODE" ]; then
  bad=0
  for f in "$LIVE/lib"/*.js; do
    "$NODE" --check "$f" >/dev/null 2>&1 || { echo "[FAIL] 语法错误：$(basename $f)"; bad=1; }
  done
  [ "$bad" = "0" ] && echo "[OK  ] 语法自检通过"
  exit "$bad"
fi
echo "[warn] 找不到 node，跳过语法自检"
