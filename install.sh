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
cp -f "$STACK"/*.py "$STACK"/*.sh "$STACK"/*.json "$DSH_HOME"/ 2>/dev/null
rm -f "$DSH_HOME/profile-package.json"
chmod +x "$DSH_HOME"/*.sh 2>/dev/null
# 脚本里的占位符也要替换（checkall.py / audit.sh 里写死了 botQQ 和主人 QQ）
for f in "$DSH_HOME"/*.py "$DSH_HOME"/*.sh; do
  [ -f "$f" ] || continue
  sed -i -e "s|__BOT_QQ__|${BOT_QQ}|g" \
         -e "s|__OWNER_QQ__|${OWNER_QQ}|g" \
         -e "s|__WS_TOKEN__|${WS_TOKEN}|g" "$f"
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

# ── 7. 配置 ───────────────────────────────────────────────────────────
phase "7/9 写配置"

# 7.1 qwen.env（模型 key；变量名沿用 QWEN_API_KEY，配置里 apiKeyEnv 指向它）
umask 077
cat > "$DSH_HOME/qwen.env" <<ENVEOF
QWEN_API_KEY=${LLM_API_KEY}
ENVEOF
umask 022
ok "qwen.env 写好（600）"

# 7.2 cordis.patch.yml
sed -e "s|__LLM_BASE_URL__|${LLM_BASE_URL}|g" \
    -e "s|__LLM_MODEL__|${LLM_MODEL}|g" \
    -e "s|__WS_TOKEN__|${WS_TOKEN}|g" \
    -e "s|__BOT_QQ__|${BOT_QQ}|g" \
    -e "s|__OWNER_QQ__|${OWNER_QQ}|g" \
    "$STACK/cordis.patch.yml.tpl" > "$DSH_HOME/profiles/web/cordis.patch.yml"
grep -q "__" "$DSH_HOME/profiles/web/cordis.patch.yml" && die "配置文件里还有没替换的占位符"
ok "cordis.patch.yml 写好"

# 7.3 profile 依赖
cp -f "$STACK/profile-package.json" "$DSH_HOME/profiles/web/package.json"
sed -i 's|"file:./node_modules/local-memory"|"file:./local-memory"|' "$DSH_HOME/profiles/web/package.json"
rm -rf "$DSH_HOME/profiles/web/local-memory"
cp -a "$STACK/local-memory" "$DSH_HOME/profiles/web/local-memory"
# 看图端点：默认跟随模型线路
sed -i "s|__VISION_BASE__|${VISION_BASE:-$LLM_BASE_URL}|g"     "$DSH_HOME/profiles/web/local-memory/cordis.patch.yml"
cd "$DSH_HOME/profiles/web"
run "$NODE_DIR/bin/npm" install --registry="$NPM_REG" || die "装 profile 依赖失败（网络问题可换 NPM_REG）"
ok "profile 依赖装好"
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
    say ""
    say "  还没登录。人类扫完码后重新运行：  bash install.sh"
    exit 2
  fi
  ok "检测到登录成功，继续"
fi

# 登录后：写入反向 WS 客户端配置（让 NapCat 主动连宿主 8643）
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
sleep 8
systemctl is-active --quiet dsh-onebot && ok "dsh-onebot 运行中" || die "dsh-onebot 没起来：journalctl -u dsh-onebot -n 50"

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
   OneBot access token : ${WS_TOKEN}
   NapCat WebUI token  : ${NAPCAT_WEBUI_TOKEN}
------------------------------------------------------------------
TIPEOF