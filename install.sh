#!/usr/bin/env bash
# QQ 群机器人 · 一键部署
#
# 用法：
#   1) cp deploy.env.example deploy.env  然后填好里面 5 个必填项
#   2) bash install.sh
#   3) 如果提示扫码，扫完再跑一次 bash install.sh    （脚本是幂等的，可反复跑）
#
# 这个脚本只做“能自动化的事”。扫码登录和真实发消息验收必须人来。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENVF="$HERE/deploy.env"
STACK="$HERE/stack"

DSH_HOME="${DSH_HOME:-/opt/dsh}"
NAPCAT_DIR="${NAPCAT_DIR:-/opt/qqbot}"
NAPCAT_IMAGE="${NAPCAT_IMAGE:-mlikiowa/napcat-docker:latest}"
NODE_DIR="/opt/node"
NODE_VER="${NODE_VER:-v24.22.0}"
DSH_VER="${DSH_VER:-0.2.1-rc.2}"
NPM_REG="${NPM_REG:-https://registry.npmmirror.com}"
NODE_MIRROR="${NODE_MIRROR:-https://registry.npmmirror.com/-/binary/node}"
LOG="/var/log/qqbot-install.log"
(umask 077; touch "$LOG" 2>/dev/null; chmod 600 "$LOG" 2>/dev/null) || true

C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_0=$'\033[0m'
say()  { printf '%s\n' "$*"; }
phase(){ printf '\n%s\n%s\n%s\n' "==================================================================" \
         "$*" "=================================================================="; }
ok()   { printf '  %s[OK]%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '  %s[注意]%s %s\n' "$C_Y" "$C_0" "$*"; }
die()  { printf '  %s[失败]%s %s\n' "$C_R" "$C_0" "$*"; exit 1; }
run()  { "$@" >>"$LOG" 2>&1; }

exec > >(tee -a "$LOG") 2>&1
say "日志同时写入 $LOG"

# ── 读配置 ────────────────────────────────────────────────────────────
[ -f "$ENVF" ] || die "没有 deploy.env。先执行：cp deploy.env.example deploy.env 并填写。"
# shellcheck disable=SC1090
. "$ENVF"

BOT_QQ="${BOT_QQ:-}"; OWNER_QQ="${OWNER_QQ:-}"
LLM_BASE_URL="${LLM_BASE_URL:-}"; LLM_MODEL="${LLM_MODEL:-}"; LLM_API_KEY="${LLM_API_KEY:-}"
[ -n "$BOT_QQ" ]      || die "deploy.env 里 BOT_QQ 没填（机器人 QQ 号）"
[ -n "$OWNER_QQ" ]    || die "deploy.env 里 OWNER_QQ 没填（主人 QQ 号）"
[ -n "$LLM_BASE_URL" ]|| die "deploy.env 里 LLM_BASE_URL 没填"
[ -n "$LLM_MODEL" ]   || die "deploy.env 里 LLM_MODEL 没填"
[ -n "$LLM_API_KEY" ] || die "deploy.env 里 LLM_API_KEY 没填"

# 看图端点：没单独填就回落到模型线路
VISION_BASE="${VISION_BASE:-$LLM_BASE_URL}"
VISION_MODEL="${VISION_MODEL:-$LLM_MODEL}"
VISION_API_KEY="${VISION_API_KEY:-$LLM_API_KEY}"

# 可选：密码快速登录。填了就不用每次登录态失效都扫码。
# 优先用 MD5（明文不落盘）。两者都填时只取 MD5。
NAPCAT_QUICK_PASSWORD="${NAPCAT_QUICK_PASSWORD:-}"
NAPCAT_QUICK_PASSWORD_MD5="${NAPCAT_QUICK_PASSWORD_MD5:-}"
PWD_ENV=""
if [ -n "$NAPCAT_QUICK_PASSWORD_MD5" ]; then
  case "$NAPCAT_QUICK_PASSWORD_MD5" in
    *[!0-9a-fA-F]*)
      die "NAPCAT_QUICK_PASSWORD_MD5 应只有 0-9a-f" ;;
  esac
  if [ "${#NAPCAT_QUICK_PASSWORD_MD5}" -ne 32 ]; then
    die "NAPCAT_QUICK_PASSWORD_MD5 应该是 32 位，现在是 ${#NAPCAT_QUICK_PASSWORD_MD5} 位"
  fi
  PWD_ENV="      NAPCAT_QUICK_PASSWORD_MD5: \"$NAPCAT_QUICK_PASSWORD_MD5\""
  ok "NapCat 已配置密码重登（MD5）"
elif [ -n "$NAPCAT_QUICK_PASSWORD" ]; then
  PWD_ENV="      NAPCAT_QUICK_PASSWORD: \"$NAPCAT_QUICK_PASSWORD\""
  warn "NapCat 用明文密码登，密码会写进 /opt/qqbot/compose.yaml（建议改用 MD5）"
fi
# 纯文本接口看不了图，而且失败得很安静（她只会说"看不到"）。装之前就讲清楚。
case "$VISION_BASE" in
  *api.deepseek.com*)
    warn "看图端点指向 api.deepseek.com —— 这个接口不支持图片输入"
    warn "  -> 她能聊天，但看不见任何图片（read_image / 表情分类 / 隐私判断全失效）"
    warn "  -> 要修：在 deploy.env 里设 VISION_BASE 和 VISION_MODEL 指向多模态端点"
    ;;
esac
[ -d "$STACK" ]       || die "找不到 stack/ 目录，别把脚本单独拿走"

# 人设三个选项（默认第一个）：
#   builtin   = 用包里那份现成的人设（默认）
#   generated = 按提示词引导生成
#   custom    = 完全自己输入（人类给全文，或给一个文件路径 PERSONA_FILE）
case "${PERSONA_MODE:-builtin}" in
  builtin|generated|custom) PERSONA_MODE="${PERSONA_MODE:-builtin}" ;;
  *)  die "PERSONA_MODE 只能是 builtin / generated / custom（当前：${PERSONA_MODE}）" ;;
esac

rand() { openssl rand -hex 16 2>/dev/null || head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
# token 必须幂等：重跑 install 不能换掉它，否则 cordis 与 NapCat 两边会漂移、
# 8643 静默不响应（踩过两次）。存在就复用，否则生成并落盘。
TOKEN_FILE="$HERE/.ws_token"
WEBUI_FILE="$HERE/.napcat_webui_token"

if [ -z "${WS_TOKEN:-}" ]; then
  if [ -f "$TOKEN_FILE" ]; then
    WS_TOKEN=$(tr -d '[:space:]' < "$TOKEN_FILE")
  else
    WS_TOKEN=$(rand)
    echo "$WS_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE" 2>/dev/null || true
  fi
fi
if [ -z "${NAPCAT_WEBUI_TOKEN:-}" ]; then
  if [ -f "$WEBUI_FILE" ]; then
    NAPCAT_WEBUI_TOKEN=$(tr -d '[:space:]' < "$WEBUI_FILE")
  else
    NAPCAT_WEBUI_TOKEN=$(rand)
    echo "$NAPCAT_WEBUI_TOKEN" > "$WEBUI_FILE"
    chmod 600 "$WEBUI_FILE" 2>/dev/null || true
  fi
fi
ok "token 已固定（WS ${#WS_TOKEN} 位 / WebUI ${#NAPCAT_WEBUI_TOKEN} 位，存在 .ws_token 与 .napcat_webui_token）"

[ "$(id -u)" = "0" ] || die "请用 root 跑（sudo bash install.sh）"

# ── 1. 前置检查 ───────────────────────────────────────────────────────
phase "1/9 前置检查"
. /etc/os-release 2>/dev/null || true
say "  系统: ${PRETTY_NAME:-?}  $(uname -r)  $(uname -m)"
[ "$(uname -m)" = "x86_64" ] || die "只支持 x86_64（NapCat 镜像只有 amd64）"
case "${ID:-}" in ubuntu|debian) ok "发行版支持" ;; *) warn "非 Ubuntu/Debian，apt 命令可能不适用" ;; esac
mem_mb=$(awk '/MemTotal/{printf "%d",$2/1024}' /proc/meminfo)
[ "$mem_mb" -ge 1500 ] && ok "内存 ${mem_mb}MB" || warn "内存只有 ${mem_mb}MB，跑模型+容器可能吃紧"
disk_gb=$(df -P / | awk 'NR==2{printf "%d",$4/1048576}')
[ "$disk_gb" -ge 8 ] && ok "根分区剩余 ${disk_gb}GB" || die "根分区只剩 ${disk_gb}GB，不够（建议 ≥10GB）"

# ── 2. 系统依赖 ───────────────────────────────────────────────────────
phase "2/9 系统依赖"
export DEBIAN_FRONTEND=noninteractive
run apt-get update || warn "apt update 有报错，继续"
run apt-get install -y curl ca-certificates xz-utils tar git python3 \
    fonts-wqy-zenhei fonts-noto-cjk || die "apt 装依赖失败，看 $LOG"
ok "curl / xz / python3 / 中文字体 就位"

# ── 3. Docker ─────────────────────────────────────────────────────────
phase "3/9 Docker"
if command -v docker >/dev/null 2>&1; then
  ok "docker 已存在：$(docker --version)"
else
  run apt-get install -y docker.io docker-compose-v2 || \
    run apt-get install -y docker.io docker-compose-plugin || die "装 docker 失败"
  ok "docker 已装"
fi
systemctl enable --now docker >>"$LOG" 2>&1 || warn "docker 服务启动有异常"

# 国内机器拉 Docker Hub 会超时（i/o timeout），必须配镜像加速，
# 否则第 8 步拉 NapCat 镜像直接失败（2026-10-25 干净机实测）。
# 腾讯云机器用内网镜像最快；其它云依次回落。
if [ ! -f /etc/docker/daemon.json ]; then
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<'DOCKERMIRROR'
{
  "registry-mirrors": [
    "https://mirror.ccs.tencentyun.com",
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run"
  ]
}
DOCKERMIRROR
  systemctl restart docker >>"$LOG" 2>&1 || warn "docker 重启失败"
  sleep 3
  ok "docker 镜像加速已配置（腾讯云内网优先）"
else
  ok "docker 镜像配置已存在，保持不动"
fi
docker info >/dev/null 2>&1 || die "docker 守护进程没起来"
DC="docker compose"
$DC version >/dev/null 2>&1 || die "docker compose 插件不可用"
ok "docker compose 可用"

# ── 4. Node ───────────────────────────────────────────────────────────
phase "4/9 Node.js ${NODE_VER}"
if [ -x "$NODE_DIR/bin/node" ]; then
  ok "已存在：$("$NODE_DIR/bin/node" -v)"
else
  tmp=$(mktemp -d)
  url="$NODE_MIRROR/${NODE_VER}/node-${NODE_VER}-linux-x64.tar.xz"
  say "  下载 $url"
  curl -fsSL --retry 3 -o "$tmp/node.tar.xz" "$url" || die "下载 Node 失败（可改 deploy.env 的 NODE_MIRROR）"
  tar xJf "$tmp/node.tar.xz" -C "$tmp" || die "解压 Node 失败"
  mv "$tmp/node-${NODE_VER}-linux-x64" "$NODE_DIR" || die "放到 $NODE_DIR 失败"
  rm -rf "$tmp"
  ln -sf "$NODE_DIR/bin/node" /usr/local/bin/node
  ln -sf "$NODE_DIR/bin/npm"  /usr/local/bin/npm
  ln -sf "$NODE_DIR/bin/npx"  /usr/local/bin/npx
  ok "Node 装好：$("$NODE_DIR/bin/node" -v)"
fi

# ── 5. DSH ────────────────────────────────────────────────────────────
phase "5/9 DeepSeek Harness"
if [ -x "$NODE_DIR/bin/dsh" ]; then
  ok "已存在：$("$NODE_DIR/bin/dsh" --version 2>&1 | head -1)"
else
  run "$NODE_DIR/bin/npm" install -g "@deepseek-ai/dsh@${DSH_VER}" --registry="$NPM_REG" || die "装 dsh 失败"
  ok "dsh 装好：$("$NODE_DIR/bin/dsh" --version 2>&1 | head -1)"
fi

# ── 6. 代码与目录 ─────────────────────────────────────────────────────
phase "6/9 部署代码到 $DSH_HOME"
mkdir -p "$DSH_HOME"/{media/onebot,memory,workspace,sessions,storages,profiles/web}
# 只拷"目标不存在"的文件；已存在的走三方比对，绝不覆盖用户改过的脚本。
# （以前是 cp -f 无条件覆盖，重跑 install.sh 会把别人的改动冲掉）
if [ -f "$STACK/sync_scripts.py" ] && [ -d "$DSH_HOME" ]; then
  bash -c '
    set -e
    for f in "'"$STACK"'"/[a-zA-Z0-9_]*.py "'"$STACK"'"/[a-zA-Z0-9_]*.sh "'"$STACK"'"/[a-zA-Z0-9_]*.json; do
      [ -f "$f" ] || continue
      t="'"$DSH_HOME"'/$(basename "$f")"
      [ -e "$t" ] || cp -f "$f" "$t"
    done
  '
  # REPO_DIR 告诉它仓库在哪（安装时仓库可能在 ~/qq-ai-bot，不在固定路径里）
  REPO_DIR="$(cd "$STACK/.." && pwd)" python3 "$STACK/sync_scripts.py" 2>&1 | tail -6 || warn "脚本同步有问题（已保留你改过的文件）"
else
  cp -f "$STACK"/*.py "$STACK"/*.sh "$STACK"/*.json "$DSH_HOME"/ 2>/dev/null
fi
rm -f "$DSH_HOME/profile-package.json"
chmod +x "$DSH_HOME"/*.sh 2>/dev/null
# 记下基线（哈希），以后 update.sh 靠它区分「没人动过」和「使用者自己改过」。
# 不记的话，第一次更新就搞不清哪些文件被改过，只能保守地不更新。
if [ -f "$STACK/sync_scripts.py" ]; then
  # 注意：install.sh 里没有 $PY 变量（那是 update.sh 的），必须写死 python3，
  # 否则 set -u 下直接 unbound variable 崩掉（2026-10-25 干净机实测）。
  REPO_DIR="$(cd "$STACK/.." && pwd)" python3 "$STACK/sync_scripts.py" --adopt >/dev/null 2>&1 || warn "基线记录失败（不影响使用，更新时会保守处理）"
fi
# 脚本里的占位符也要替换（checkall.py / audit.sh 里写死了 botQQ 和主人 QQ）
for f in "$DSH_HOME"/*.py "$DSH_HOME"/*.sh; do
  [ -f "$f" ] || continue
  sed -i -e "s|__BOT_QQ__|${BOT_QQ}|g" \
         -e "s|__OWNER_QQ__|${OWNER_QQ}|g" "$f"
done
mkdir -p "$DSH_HOME/media/onebot"
# 人设：按 PERSONA_MODE 落地
PERSONA_DIR="$DSH_HOME/media/onebot"
mkdir -p "$PERSONA_DIR"
if [ ! -f "$PERSONA_DIR/persona.md" ]; then
  case "$PERSONA_MODE" in
    builtin)
      cp -f "$HERE/persona/example-jingjing.md" "$PERSONA_DIR/persona.md"
      ok "人设：用包里的现成那份（$(wc -l < "$PERSONA_DIR/persona.md") 行）"
      ;;
    generated)
      cp -f "$HERE/persona/TEMPLATE.md" "$PERSONA_DIR/persona.md"
      warn "人设：只放了骨架，还有 {{占位符}}！AI 必须按人类描述补完，否则 she 会像客服"
      ;;
    custom)
      if [ -n "${PERSONA_FILE:-}" ] && [ -f "$PERSONA_FILE" ]; then
        cp -f "$PERSONA_FILE" "$PERSONA_DIR/persona.md"
        ok "人设：已从 PERSONA_FILE 导入（$(wc -l < "$PERSONA_DIR/persona.md") 行）"
      else
        printf '## 人设

（还没写。这里是人类自己提供的人设全文，AI 应把人类给的内容原样写在这一行下面。）
' > "$PERSONA_DIR/persona.md"
        warn "人设：custom 模式但没有 PERSONA_FILE。请把人类给的人设写进 /opt/dsh/media/onebot/persona.md"
      fi
      ;;
  esac
else
  ok "人设已存在，保持不动"
fi
ok "脚本 $(ls "$DSH_HOME"/*.py 2>/dev/null | wc -l) 个、人设已就位"

# 6.5 主动行为日程（routines.json）
# 以前只存在于作者机器上，导致别人装完根本没有主动私聊/日记提醒/空间说说——
# proactive.py 读到的是空列表，安安静静什么都不做。
if [ ! -f "$DSH_HOME/routines.json" ]; then
  if [ -f "$STACK/config/routines.json.tpl" ]; then
    cp "$STACK/config/routines.json.tpl" "$DSH_HOME/routines.json"
    ok "routines.json 已装（主动私聊/日记/空间）"
  else
    warn "没有 stack/config/routines.json.tpl，主动行为不会启用"
  fi
else
  ok "routines.json 已存在，保持不动（用 update.sh 补新增的 routine）"
fi

# 6.6 每天/每小时限制（limits.json）
# 以前这些值写死在代码里（比如 B 站评论区 @ 主人每天 3 条），改不了。
# 现在抽成配置文件，改完立即生效。
if [ ! -f "$DSH_HOME/limits.json" ] && [ -f "$STACK/config/limits.json.tpl" ]; then
  cp "$STACK/config/limits.json.tpl" "$DSH_HOME/limits.json"
  ok "limits.json 已装（每天限制可改：switches.py limits）"
elif [ -f "$DSH_HOME/limits.json" ]; then
  ok "limits.json 已存在，保持不动"
fi

# ── 7. 配置 ───────────────────────────────────────────────────────────
phase "7/9 写配置"

# 7.1 qwen.env（模型 key；变量名沿用 QWEN_API_KEY，配置里 apiKeyEnv 指向它）
umask 077
cat > "$DSH_HOME/qwen.env" <<ENVEOF
QWEN_API_KEY=${LLM_API_KEY}
VISION_BASE=${VISION_BASE}
VISION_MODEL=${VISION_MODEL}
VISION_API_KEY=${VISION_API_KEY}
ENVEOF
umask 022
ok "qwen.env 写好（600）"

# 掉线告警邮箱（可选）：QQ 掉线时出站消息发不出去，邮箱是唯一还通的路
if [ -n "${MAIL_PASS:-}" ]; then
  cat > "$DSH_HOME/mail.env" <<MAILEOF
SMTP_HOST=${MAIL_HOST:-smtp.qq.com}
SMTP_PORT=${MAIL_PORT:-465}
SMTP_USER=${MAIL_USER:-${BOT_QQ}@qq.com}
SMTP_PASS=${MAIL_PASS}
MAIL_TO=${MAIL_TO:-${OWNER_QQ}@qq.com}
MAILEOF
  chmod 600 "$DSH_HOME/mail.env"
  ok "掉线告警邮件已配置：${MAIL_USER:-${BOT_QQ}@qq.com} -> ${MAIL_TO:-${OWNER_QQ}@qq.com}"
else
  warn "没配 MAIL_PASS，QQ 掉线时不能邮件通知（可选，不影响其它功能）"
fi

# 7.2 cordis.patch.yml（模板里所有占位符都必须被替换，漏一个就起不来）
CFG="$DSH_HOME/profiles/web/cordis.patch.yml"
mkdir -p "$(dirname "$CFG")"
TMPCFG="$(mktemp)"
sed -e "s|__LLM_BASE_URL__|${LLM_BASE_URL}|g" \
    -e "s|__LLM_MODEL__|${LLM_MODEL}|g" \
    -e "s|__WS_TOKEN__|${WS_TOKEN}|g" \
    -e "s|__BOT_QQ__|${BOT_QQ}|g" \
    -e "s|__OWNER_QQ__|${OWNER_QQ}|g" \
    "$STACK/cordis.patch.yml.tpl" > "$TMPCFG"
grep -q "__" "$TMPCFG" && die "模板里还有没替换的占位符"

if [ -f "$CFG" ]; then
  # 已存在：只补新版本新增的键，**用户调过的值一个字不改**
  # （作息时段、接话概率、开关……全在这个文件里，整份重写等于把设定清零）
  python3 - "$CFG" "$TMPCFG" <<'MERGEPY'
import io, re, sys
cfg_p, tpl_p = sys.argv[1], sys.argv[2]
cfg = io.open(cfg_p, encoding="utf-8").read().split("\n")
tpl = io.open(tpl_p, encoding="utf-8").read().split("\n")

def key_of(line):
    m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):", line)
    return (m.group(2), len(m.group(1))) if m else (None, None)

present = {key_of(l)[0] for l in cfg if key_of(l)[0]}
added = []
for i, line in enumerate(tpl):
    k, indent = key_of(line)
    if not k or k in present or line.lstrip().startswith("#"):
        continue
    block = [line]
    j = i + 1
    while j < len(tpl) and tpl[j].strip() and not key_of(tpl[j])[0]:
        block.append(tpl[j]); j += 1
    at = None
    for idx, cl in enumerate(cfg):
        ck, ci = key_of(cl)
        if ck and ci == indent:
            at = idx + 1
    if at is None:
        continue
    cfg[at:at] = block
    present.add(k)
    added.append(k)
io.open(cfg_p, "w", encoding="utf-8", newline="\n").write("\n".join(cfg))
print("  新增配置键：" + (", ".join(added) if added else "（无）"))
MERGEPY
  ok "cordis.patch.yml 已合并（保留你原有的设定，只补新键）"
else
  cp -f "$TMPCFG" "$CFG"
  ok "cordis.patch.yml 写好"
fi
rm -f "$TMPCFG"

# 7.3 profile 依赖
cp -f "$STACK/profile-package.json" "$DSH_HOME/profiles/web/package.json"
# 插件版本豁免（关键）：dsh 0.2.1-rc.2 会把 dsh-mcp-client@0.0.1-rc.1 判定为不兼容并
# **禁用全部 MCP**（记忆/看图/联网/生图/表情/管理全没了，只剩聊天）。
# 这个文件以前只存在于作者机器上，导致任何新装机器都是残废版（2026-10-25 干净机实测）。
if [ -f "$STACK/compatibility.json" ]; then
  cp -f "$STACK/compatibility.json" "$DSH_HOME/profiles/web/compatibility.json"
  ok "插件版本豁免已写入（否则所有 MCP 会被 dsh 禁用）"
else
  warn "没有 compatibility.json，MCP 插件可能被 dsh 判定不兼容而禁用"
fi
sed -i 's|"file:./node_modules/local-memory"|"file:./local-memory"|' "$DSH_HOME/profiles/web/package.json"
rm -rf "$DSH_HOME/profiles/web/local-memory"
cp -a "$STACK/local-memory" "$DSH_HOME/profiles/web/local-memory"
# 看图端点：默认跟随模型线路
sed -i "s|__VISION_BASE__|${VISION_BASE:-$LLM_BASE_URL}|g"     "$DSH_HOME/profiles/web/local-memory/cordis.patch.yml"
cd "$DSH_HOME/profiles/web"
# --legacy-peer-deps 是必须的，不是图省事：
#   dsh-onebot-qq 要求 @deepseek-ai/dsh-llm >=0.2.0-rc.1，
#   而 @deepseek-ai/dsh-mcp-client@0.0.1-rc.1 声明 peer ^0.0.1-rc.1 ——
#   npm 7+ 严格按 peer 解析会直接 ERESOLVE 拒装。线上这套组合
#   （dsh-llm 0.2.1-rc.2 + mcp-client 0.0.1-rc.1 + onebot 0.9.3）是能跑的，
#   所以按线上的实际解析方式装，而不是让 npm 判死刑。
run "$NODE_DIR/bin/npm" install --legacy-peer-deps --registry="$NPM_REG"     || die "装 profile 依赖失败（网络问题可换 NPM_REG）"
ok "profile 依赖装好"
# npm 会用 registry 原版覆盖 lib/，我们这套是 fork（约 500 行改动）。
# 部署包里带了 vendor 快照就必须重装回去，否则所有本地改动静默消失。
if [ -x "$STACK/vendor/install_fork.sh" ]; then
  VENDOR_DIR="$DSH_HOME/vendor"
  if [ -d "$VENDOR_DIR" ]; then
    bash "$STACK/vendor/install_fork.sh" || warn "fork 快照装入失败，插件可能是原版行为"
  else
    warn "没有 $VENDOR_DIR，用 registry 原版插件（本地 fork 功能不可用）"
  fi
fi
# npm 会把 local-memory 复制进 node_modules，**运行时读的是那一份**。
# 只改 profiles/web/local-memory 的话，占位符照样进 MCP 配置，
# 看图请求会发到 "__VISION_BASE__/chat/completions" —— 她从此看不见任何图片。
LM_CFG="$DSH_HOME/profiles/web/node_modules/local-memory/cordis.patch.yml"
if [ -f "$LM_CFG" ]; then
  sed -i "s|__VISION_BASE__|${VISION_BASE}|g" "$LM_CFG"
  if grep -q "__VISION_BASE__" "$LM_CFG"; then
    die "看图端点占位符没替换干净（$LM_CFG），她会看不见图片"
  fi
  ok "看图端点已写入 MCP 配置：${VISION_BASE}"
else
  warn "没找到 $LM_CFG，跳过看图端点检查"
fi
ls node_modules/dsh-onebot-qq >/dev/null 2>&1 && ok "onebot 插件在位" || die "dsh-onebot-qq 没装上"

# 7.4 LLM 转发层（强制关思考，可快 4~5 倍）
cat > /etc/systemd/system/llm-shim.service <<UNIT
[Unit]
Description=LLM shim: force-disable reasoning for providers that default to thinking
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=LLM_UPSTREAM=${LLM_BASE_URL}
Environment=LLM_LOCAL_BASE=/v1
ExecStart=/usr/bin/python3 ${DSH_HOME}/llm_shim.py --host 127.0.0.1 --port 8890
Restart=always
RestartSec=3
StandardOutput=append:/var/log/llm-shim.log
StandardError=append:/var/log/llm-shim.log

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now llm-shim.service >>"$LOG" 2>&1 || warn "llm-shim 启动失败"
sleep 2
ss -lntp 2>/dev/null | grep -q ':8890' && ok "LLM 转发层在 8890 监听（上游 ${LLM_BASE_URL}）" || warn "8890 未监听，检查 journalctl -u llm-shim"

# ── 8. NapCat ─────────────────────────────────────────────────────────
phase "8/9 NapCat（QQ 协议端）"
mkdir -p "$NAPCAT_DIR"/{data/qq,data/config,data/plugins,logs}
cat > "$NAPCAT_DIR/compose.yaml" <<COMPEOF
services:
  napcat:
    image: ${NAPCAT_IMAGE}
    container_name: napcat
    restart: unless-stopped
    environment:
      MODE: ws
      ACCOUNT: "${BOT_QQ}"
      WEBUI_TOKEN: "${NAPCAT_WEBUI_TOKEN}"
      TZ: Asia/Shanghai
${PWD_ENV}
    ports:
      - "127.0.0.1:6099:6099"
      - "127.0.0.1:3001:3001"
    volumes:
      - ./data/qq:/app/.config/QQ
      - ./data/config:/app/napcat/config
      - ./data/plugins:/app/napcat/plugins
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
COMPEOF

cd "$NAPCAT_DIR"
if ! docker ps --format '{{.Names}}' | grep -qx napcat; then
  say "  拉镜像并启动（首次可能要几分钟）…"
  $DC up -d || die "启动 NapCat 失败，看 $LOG 或 docker logs napcat"
fi
sleep 5
docker ps --format '{{.Names}}' | grep -qx napcat && ok "napcat 容器在跑" || die "napcat 容器没起来"
# 记录实际用的镜像 digest：上游 latest 变了你能发现
# .Image 是本地 image ID，拼不出可拉取的地址；RepoDigests 才是 registry 侧的 digest
repo_digest=$(docker inspect napcat --format '{{index .RepoDigests 0}}' 2>/dev/null || true)
if [ -n "$repo_digest" ]; then
  ok "NapCat 镜像: ${repo_digest}"
  ok "  想锁定版本：把这一整串写进 deploy.env 的 NAPCAT_IMAGE=，下次就不会随 latest 变"
  # 既然已经拿到了，就自动锁上——这个项目会被风控踢号、需要重装，
  # 可复现性比"追新"重要。想追新把 deploy.env 改回 :latest 即可。
  if [ -f "$HERE/deploy.env" ] && grep -qE '^NAPCAT_IMAGE=.*:latest' "$HERE/deploy.env"; then
    sed -i "s|^NAPCAT_IMAGE=.*|NAPCAT_IMAGE=${repo_digest}|" "$HERE/deploy.env"
    ok "  已自动锁进 deploy.env（想追新就改回 :latest）"
  fi
else
  warn "拿不到 RepoDigests（本地 build 的镜像没有），无法给可锁定的 digest"
fi

# 容器到宿主 docker 网关（NapCat 要连宿主上的 8643）
GW=$(docker network inspect qqbot_default -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)
[ -n "$GW" ] || GW=$(docker network inspect napcat_default -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)
[ -n "$GW" ] || die "拿不到 docker 网关 IP（网络名可能不叫 qqbot_default）"
ok "docker 网关 ${GW}"

OB="$NAPCAT_DIR/data/config/onebot11_${BOT_QQ}.json"
if [ ! -f "$OB" ]; then
  say ""
  say "  机器人 QQ 还没登录。下面会自动提取二维码信息。"
  say "  把输出原样转给人类去扫码；扫完脚本会自动继续，最多等 180 秒。"
  say ""
  python3 "$HERE/qr_wait.py" "$OB"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # 不中断：登录可以事后补，但服务/定时器必须装完，
    # 否则会出现"装完了但 dsh-onebot 服务不存在"（2026-10-25 干净机实测踩到）。
    say ""
    warn "QQ 还没登录 —— 先继续装服务，扫码后再执行一次 bash install.sh 补齐"
    PENDING_LOGIN=1
  else
    ok "检测到登录成功，继续"
  fi
fi
if [ "${PENDING_LOGIN:-0}" = "1" ] && [ ! -f "$OB" ]; then
  say "  跳过反向 WS 配置（需要先登录拿到 onebot 配置）"
fi

# 登录后：写入反向 WS 客户端配置（让 NapCat 主动连宿主 8643）
if [ -f "$OB" ]; then
cp -f "$OB" "$OB.bak-$(date +%s)"
python3 - "$OB" "$GW" "$BOT_QQ" "$WS_TOKEN" <<'PY'
import json, sys
path, gw, qq, token = sys.argv[1:5]
d = json.load(open(path, encoding="utf-8"))
net = d.setdefault("network", {})
clients = net.setdefault("websocketClients", [])
me = None
for c in clients:
    if c.get("name") == "dsh" or "dsh" in str(c.get("name", "")):
        me = c; break
if me is None:
    me = {"name": "dsh"}
    clients.append(me)
me.update({
    "enable": True,
    "url": "ws://%s:8643/ws" % gw,
    "messagePostFormat": "array",
    "reportSelfMessage": False,
    "token": token,
    "debug": False,
    "heartInterval": 30000,
    "reconnectInterval": 3000,
})
json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("  反向 WS 已写入:", me["url"])
PY
docker restart napcat >>"$LOG" 2>&1
else
  warn "没登录，暂时不写反向 WS（登录后重跑本脚本会补上）"
fi
ok "NapCat 已指向 ws://${GW}:8643/ws"

# ── 9. systemd ────────────────────────────────────────────────────────
phase "9/9 系统服务"
mkdir -p "$DSH_HOME/admin"
[ -f "$DSH_HOME/tokenhub.env" ] || echo "# 可选：备用线路 key" > "$DSH_HOME/tokenhub.env"
cp -f "$STACK/systemd"/*.service "$STACK/systemd"/*.timer "$STACK/systemd"/*.path /etc/systemd/system/
mkdir -p /etc/systemd/system/dsh-onebot.service.d
cp -f "$STACK/systemd/dsh-onebot.service.d"/*.conf /etc/systemd/system/dsh-onebot.service.d/
# unit 里写死的是默认路径，按实际参数替换（否则改了 DSH_HOME 服务起不来）
if [ "$DSH_HOME" != "/opt/dsh" ] || [ "$NODE_DIR" != "/opt/node" ]; then
  sed -i -e "s|/opt/dsh|${DSH_HOME}|g" -e "s|/opt/node|${NODE_DIR}|g"       /etc/systemd/system/*.service /etc/systemd/system/dsh-onebot.service.d/*.conf
fi
touch /var/log/dsh-onebot.log
systemctl daemon-reload
systemctl enable --now dsh-onebot.service   >>"$LOG" 2>&1 || warn "dsh-onebot 启动失败，看 journalctl"
systemctl enable --now proactive.timer      >>"$LOG" 2>&1 || warn "proactive.timer 启动失败"
systemctl enable --now body-state.timer     >>"$LOG" 2>&1 || warn "body-state.timer 启动失败"
systemctl enable --now toolmode.path        >>"$LOG" 2>&1 || warn "toolmode.path 启动失败"

# 这几个以前只在作者机器上，没进仓库 → 别人装完对应功能根本不跑。
# 缺任何一个都是静默失效（看起来装好了，其实什么也没做）。
for t in qqguard.timer memory-distill.timer sticker-settle.timer \
         usage-report.timer patch-doctor.timer; do
  systemctl enable --now "$t" >>"$LOG" 2>&1 || warn "$t 启动失败"
done
ok "定时任务已装：掉线看护 / 记忆蒸馏 / 收图结算 / 日报 / 漂移检测"
# 日记提醒由 proactive.py 的 routines（morning/noon/evening/night）负责，
# 这里**不要再装** diary-noon/midnight 定时器：那套 inject_diary.sh 会重复提醒。

# 自动更新：**默认不开**。
# 理由：升级会重启 dsh，正在进行的对话会被打断；开发者的机器本來就该手动控制升级时机。
# 想要就手动开：python3 /opt/dsh/switches.py on auto_update
#                （或 systemctl enable --now auto-update.timer）
if systemctl is-enabled --quiet auto-update.timer 2>/dev/null; then
  ok "自动更新已开启（之前开过，保持不动；关：switches.py off auto_update）"
else
  ok "自动更新未开启（默认；要开：switches.py on auto_update）"
fi

# 等服务真正就绪：dsh 要加载插件、起 6 个 MCP，冷启动常需 20 秒以上。
# 只 sleep 固定秒数会让 verify 误报"端口没监听 / 没有 bridge ready"（2026-10-25 干净机实测）。
ready=0
for i in $(seq 1 45); do
  systemctl is-active --quiet dsh-onebot || { sleep 2; continue; }
  if ss -lnt 2>/dev/null | grep -q ':8643 ' && ss -lnt 2>/dev/null | grep -q ':8090 '; then
    ready=1
    break
  fi
  sleep 2
done
if [ "$ready" = "1" ]; then
  ok "dsh-onebot 运行中（8643 与 8090 已监听）"
else
  systemctl is-active --quiet dsh-onebot \
    && warn "dsh-onebot 进程在，但 90 秒内端口未就绪（看 journalctl -u dsh-onebot -n 50）" \
    || die "dsh-onebot 没起来：journalctl -u dsh-onebot -n 50"
fi

# ── 日志轮转（不配这个，长跑几个月必被日志咬一次）────────────────────
# copytruncate 是必须的：dsh-onebot 和 llm-shim 都以 append 方式持有句柄，
# 直接 rename 会让它们继续写旧 inode。
cat > /etc/logrotate.d/qqbot <<'ROTEOF'
/var/log/dsh-onebot.log /var/log/qqbot-install.log /var/log/llm-shim.log {
    weekly
    rotate 4
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su root root
}
/opt/dsh/memory/*.log {
    weekly
    rotate 4
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su root root
}
ROTEOF
chmod 644 /etc/logrotate.d/qqbot
ok "日志轮转已配置（每周、留 4 份、压缩）"

# ── 验收 ──────────────────────────────────────────────────────────────
phase "部署完成 · 自检"
python3 "$DSH_HOME/checkall.py" 2>&1 | tail -25 || warn "checkall 有失败项，往上翻"

cat <<TIPEOF

------------------------------------------------------------------
接下来还要做的事（脚本做不了）：

1) 云服务器安全组：只放 22 端口。
   8643 是 OneBot 的 WebSocket，只应该在 docker 网桥内部用，
   千万不要对公网开放。

2) 真实验收：用主人 QQ 私聊机器人说一句话，看它回不回。
   然后拉进一个群，@ 它一次。

3) 常用命令：
   看日志   tail -f /var/log/dsh-onebot.log
   看服务   systemctl status dsh-onebot
   体检     python3 $DSH_HOME/checkall.py
   改人设   $DSH_HOME/media/onebot/persona.md（改完 systemctl restart dsh-onebot）
   改模型   $DSH_HOME/profiles/web/cordis.patch.yml 改完 systemctl restart dsh-onebot

本次生成的密钥（记下来）：
   OneBot access token : 见 ${HERE}/.ws_token（600）
   NapCat WebUI token  : 见 ${HERE}/.napcat_webui_token（600）
   （故意不打印明文，避免落进安装日志）
------------------------------------------------------------------
TIPEOF