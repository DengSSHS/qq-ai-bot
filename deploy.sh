#!/usr/bin/env bash
# 一条命令走完部署：查配置 -> 安装 -> 验收
#
#   bash deploy.sh
#
# 幂等：中途失败，改好 deploy.env 再跑一次即可，install.sh 会接着装。
# 退出码：0 = 验收通过（VERDICT: READY）；非 0 = 看最后一条 FAIL/NOT READY。
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

# 自动提权：云服务器默认账号常是 ubuntu/centos（不是 root），
# 而 install.sh 必须 root。以前会跑到一半才报"请用 root"，像脚本 bug。
if [ "$(id -u)" != "0" ]; then
  printf '[提示] 需要 root 权限，自动用 sudo 重新执行…\n'
  exec sudo -E bash "$0" "$@"
fi

say() { printf '\n== %s ==\n' "$*"; }
die() { printf '[FAIL] %s\n' "$*"; exit 1; }

# ── 1. 配置 ────────────────────────────────────────────────────
say "1/3 deploy.env"

if [ ! -f deploy.env ]; then
  die "deploy.env 不存在。先执行：cp deploy.env.example deploy.env 然后填 5 项"
fi

# 在 Windows 上编辑过会带 CRLF，bash 读到的值会多一个 \r
if grep -q $'\r' deploy.env 2>/dev/null; then
  sed -i 's/\r$//' deploy.env
  printf '  [FIX] deploy.env 有 CRLF，已转成 LF\n'
fi

missing=""
for k in BOT_QQ OWNER_QQ LLM_BASE_URL LLM_MODEL LLM_API_KEY; do
  v=$(grep -E "^${k}=" deploy.env | head -1 | cut -d= -f2-)
  v="${v%\"}"; v="${v#\"}"
  if [ -z "$v" ]; then missing="$missing $k"; fi
done
[ -n "$missing" ] && die "deploy.env 里这些键还是空的:$missing"

for k in BOT_QQ OWNER_QQ; do
  v=$(grep -E "^${k}=" deploy.env | head -1 | cut -d= -f2-)
  if ! printf '%s' "$v" | grep -qE '^[0-9]{5,12}$'; then
    die "$k 不是合法的 QQ 号：$v"
  fi
done

mode=$(grep -E "^PERSONA_MODE=" deploy.env | head -1 | cut -d= -f2-)
case "${mode:-builtin}" in
  builtin) printf '  [OK] 人设：包里现成的那份\n' ;;
  generated)
    if grep -qE '\{\{[^}]+\}\}' persona/persona.md 2>/dev/null; then
      die "PERSONA_MODE=generated，但 persona/persona.md 里还有 {{占位符}} 没替换"
    fi
    printf '  [OK] 人设：已生成\n' ;;
  custom)
    pf=$(grep -E "^PERSONA_FILE=" deploy.env | head -1 | cut -d= -f2-)
    [ -f "${pf:-/nonexistent}" ] || die "PERSONA_MODE=custom，但 PERSONA_FILE 指向的文件不存在：$pf"
    printf '  [OK] 人设：自定义 %s\n' "$pf" ;;
  *) die "PERSONA_MODE 只能是 builtin|generated|custom，现在是：$mode" ;;
esac

perm=$(stat -c '%a' deploy.env 2>/dev/null || echo '?')
case "$perm" in
  600|400) printf '  [OK] deploy.env 权限 %s\n' "$perm" ;;
  *) warn="deploy.env 权限是 $perm，里面有 API Key。稍后会 chmod 600"
     printf '  [WARN] %s\n' "$warn"; chmod 600 deploy.env 2>/dev/null || true ;;
esac

printf '  [OK] 必填项齐全，开始装\n'

# ── 2. 安装 ────────────────────────────────────────────────────
say "2/3 install.sh（可重入，失败就改完再跑）"
if ! bash install.sh; then
  die "install.sh 中途失败。上面的报错就是原因；修好后重跑 bash deploy.sh 即可接着装"
fi

# ── 3. 验收 ────────────────────────────────────────────────────
say "3/3 verify.sh（实况检查，不看它就不算装完）"
bash verify.sh
rc=$?

printf '\n------------------------------------------------------------------\n'
if [ "$rc" -eq 0 ]; then
  printf '部署完成。下一步：让群里 @ 它一句，看它回不回。\n'
else
  printf '验收没过。上面的 [FAIL] 逐条修，然后重跑 bash deploy.sh。\n'
fi
exit "$rc"
