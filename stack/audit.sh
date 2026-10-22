#!/bin/sh
# QQ 群机器人整体审计
# 用法: sh /opt/dsh/audit.sh
export PATH=/opt/node/bin:/usr/local/bin:/usr/bin:/bin
export DSH_HOME=/opt/dsh

P=/opt/dsh/profiles/web/cordis.patch.yml
PER=/opt/dsh/media/onebot/persona.md
MEM=/opt/dsh/memory
STK=/opt/dsh/media/onebot/stickers
LOG=/var/log/dsh-onebot.log

FAIL=0; WARN=0
ok()   { printf '  \033[32m[OK]\033[0m   %s\n' "$1"; }
warn() { printf '  \033[33m[WARN]\033[0m %s\n' "$1"; WARN=$((WARN+1)); }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
sec()  { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

has()  { grep -qE "$1" "$P" 2>/dev/null && ok "$2" || bad "$2"; }
want() { # 期望值精确匹配
  if grep -qE "^[[:space:]]*$1:[[:space:]]*[\"']?$2[\"']?[[:space:]]*$" "$P" 2>/dev/null; then ok "$1 = $2"
  else bad "$1 期望 $2，实际: $(grep -E "^[[:space:]]*$1:" "$P" 2>/dev/null | head -1 | sed 's/^ *//')"; fi
}

echo "=========================================================="
echo " QQ 群机器人 · 全盘审计    $(date '+%F %T %Z')"
echo "=========================================================="

sec "1. 服务与进程"
A=$(systemctl is-active dsh-onebot 2>/dev/null); [ "$A" = active ] && ok "dsh-onebot: active" || bad "dsh-onebot: $A"
E=$(systemctl is-enabled dsh-onebot 2>/dev/null); [ "$E" = enabled ] && ok "开机自启: enabled" || warn "开机自启: $E"
D=$(systemctl is-active docker 2>/dev/null); [ "$D" = active ] && ok "docker: active" || bad "docker: $D"
docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | grep -q '^napcat' && ok "napcat 容器在跑" || bad "napcat 容器没跑"
N=$(systemctl is-active napcat 2>/dev/null); [ "$N" = inactive ] || true
SSHD=$(systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null)
[ "$SSHD" = active ] && ok "ssh: active" || bad "ssh: $SSHD"
MP=$(pgrep -fc memory_server.py 2>/dev/null); [ -z "$MP" ] && MP=0
[ "$MP" -ge 1 ] && ok "记忆服务进程: $MP 个" || bad "记忆服务没起"

sec "2. 配置文件完整性"
[ -f "$P" ] && ok "patch 文件存在" || bad "缺 $P"
cd /opt/dsh && dsh --profile web --dump-config >/tmp/au-dump.txt 2>/tmp/au-err.txt
RC=$?
[ "$RC" -eq 0 ] && ok "dump-config 装配成功" || bad "dump-config 失败 rc=$RC"
NF=$(grep -c 'not found' /tmp/au-err.txt 2>/dev/null); NF=$(echo "$NF" | head -1); [ -z "$NF" ] && NF=0
[ "$NF" -eq 0 ] && ok "无无效条目引用" || bad "有 $NF 个无效条目: $(grep -oE 'entry \"[^\"]+\"' /tmp/au-err.txt | head -3 | tr '\n' ' ')"

sec "3. 模型与推理"
want "model" "deepseek-v4.1-flash-1017"
want "provider" "platform"
want "reasoning" "off"
want "thinkingFormat" "deepseek"
want "contextWindow" "65536"
want "maxTokens" "800"
grep -q "api: openai-completions" "$P" && ok "api = openai-completions" || bad "缺 api 字段"
grep -q "127.0.0.1:8890" "$P" && ok "baseURL 指向转发层" || bad "baseURL 没指向转发层"
[ -f /opt/dsh/tokenhub.env ] && ok "API key 文件存在" || bad "缺 tokenhub.env"
SYS=$(systemctl show dsh-onebot -p EnvironmentFiles 2>/dev/null | grep -c tokenhub.env)
[ "$SYS" -ge 1 ] && ok "systemd 已注入 key 环境变量" || warn "systemd 未看到 EnvironmentFile"

sec "4. 上下文治理"
grep -A4 "id: compaction-basic" "$P" | grep -q "disabled: false" && ok "历史压缩: 启用" || bad "历史压缩未启用"
grep -A2 "id: image-offload" "$P" | grep -q "disabled: false" && ok "图片卸载: 启用" || bad "图片卸载未启用"
grep -A6 "id: tool-result-pruner" "$P" | grep -q "disabled: false" && ok "工具输出裁剪: 启用" || bad "工具输出裁剪未启用"
grep -q "headroomTokens: 8192" "$P" && ok "压缩触发线 headroom=8192" || warn "headroomTokens 不是 8192"

sec "5. 工具面（危险工具应为禁用或不存在）"
python3 - /tmp/au-dump.txt > /tmp/au-tools.txt <<'PY'
import re, sys
txt = open(sys.argv[1], encoding='utf-8', errors='replace').read()
blocks = {}
cur = None
for l in txt.splitlines():
    m = re.match(r'^- id: (\S+)', l)
    if m:
        cur = m.group(1)
        blocks[cur] = []          # 同名条目以最后一层为准
        continue
    if cur:
        blocks[cur].append(l)
want = ["tool-bash","tool-pwsh","tool-fs","tool-fs-search","tool-subagent",
        "tool-subagent-control","tool-subagent-fork","tool-workflow","tool-goal",
        "tool-todo","tool-skill","tool-cordis","tool-plugin-manager",
        "tool-plugin-market","tool-schedule","tool-ask-user","tool-presentation"]
for t in want:
    b = blocks.get(t)
    if b is None:
        print("ABSENT|%s" % t)
    elif any('disabled: true' in x for x in b):
        print("OK|%s" % t)
    else:
        print("ENABLED|%s" % t)
PY
DENY=0; LEAK=0
while IFS='|' read -r st name; do
  case "$st" in
    OK)     DENY=$((DENY+1)) ;;
    ABSENT) DENY=$((DENY+1)) ;;
    ENABLED) bad "$name 仍然启用"; LEAK=$((LEAK+1)) ;;
  esac
done < /tmp/au-tools.txt
[ "$LEAK" -eq 0 ] && ok "危险工具 $DENY 项全部禁用/不存在" || bad "有 $LEAK 个危险工具仍然可用"
grep -qE '^- id: tool-web' /tmp/au-dump.txt && ok "联网工具 tool-web 保留（她需要）" || warn "tool-web 不在树里（她就不能联网了）"

sec "6. QQ 侧策略"
want "requireMention" "true"
want "groupMentionAtOnly" "true"
want "groupActivationMinutes" "10"
want "groupMuteMinutes" "10"
want "dmPolicy" "allowlist"
want "rateLimitPerMinute" "8"
want "actionRatePerMinute" "6"
want "actionRatePerDay" "400"
want "inboundImageMaxPx" "1024"
want "sttEnabled" "false"
want "interimMessages" "false"
want "deferredTimeoutMs" "0"
want "injectEnabled" "true"
want "injectDryRun" "true"
want "chatIdleEvictDays" "5"
grep -qE "^\s*botQQ:\s*\"?[0-9]{5,12}" "$P" && ok "botQQ 格式正常" || bad "botQQ 缺失或格式不对"
grep -A3 "allowFrom:" "$P" | grep -qE '^\s*-\s*"?[0-9]{5,12}' && ok "私聊白名单已配置" || bad "私聊白名单为空"
grep -A3 "adminUsers:" "$P" | grep -qE '^\s*-\s*"?[0-9]{5,12}' && ok "管理员名单已配置" || bad "管理员名单为空"

sec "7. 人设"
[ -f "$PER" ] && ok "人设文件存在" || bad "缺 persona.md"
SIZE=$(wc -c < "$PER")
[ "$SIZE" -lt 20000 ] && ok "人设体积 ${SIZE}B（未超 20KB）" || warn "人设 ${SIZE}B 偏大，每轮都要带"
for s in "底线" "她是谁" "开口之前，四步" "什么时候开口" "脾气" "说话" "三条判尺" "工具（按需" "你的号" "日记"; do
  grep -q "$s" "$PER" && ok "人设含「$s」" || bad "人设缺「$s」"
done

sec "8. 表情库"
if [ -f "$STK/stickers.json" ]; then
  ST=$(python3 - "$STK" <<'PY'
import json,os,sys
d=sys.argv[1]
e=json.load(open(os.path.join(d,"stickers.json"),encoding="utf-8"))
have=sum(1 for i in e if os.path.exists(i.get("file","")))
print(len(e), have)
PY
)
  NE=$(echo "$ST" | awk '{print $1}'); NH=$(echo "$ST" | awk '{print $2}')
  ok "索引 $NE 条"
  [ "$NE" = "$NH" ] && ok "文件命中 $NH/$NE" || bad "有 $((NE-NH)) 条文件缺失"
else
  bad "缺 stickers.json 索引"
fi

sec "9. 记忆服务"
python3 -c "import ast;ast.parse(open('/opt/dsh/memory_server.py',encoding='utf-8').read())" 2>/dev/null && ok "脚本语法正确" || bad "memory_server.py 语法错"
[ -d "$MEM/diary" ] && ok "日记目录存在" || bad "缺 diary 目录"
[ -f "$MEM/notes.md" ] && ok "长期笔记存在" || bad "缺 notes.md"
for s in 主人 "常混的群" "群里印象深的人" "反复出现的梗"; do
  grep -q "## $s" "$MEM/notes.md" 2>/dev/null && ok "笔记含「$s」小节" || warn "笔记缺「$s」小节"
done
DE=$(cat "$MEM"/diary/*.md 2>/dev/null | grep -c '^### ')
[ "$DE" -ge 1 ] && ok "日记已有 $DE 条" || warn "日记还是空的"
PROBE=$(printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"audit","version":"0"}}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 | timeout 20 python3 /opt/dsh/memory_server.py 2>/dev/null | tail -1)
echo "$PROBE" | grep -q "diary_write" && ok "MCP 协议应答正常（工具可见）" || bad "MCP 协议无应答"
TOOLS=$(echo "$PROBE" | grep -o '"name": "[a-z_]*"' | wc -l)
[ "$TOOLS" -ge 5 ] && ok "MCP 工具数 $TOOLS" || warn "MCP 工具数 $TOOLS（期望 5）"

sec "10. 定时调度"
systemctl list-unit-files proactive.timer 2>/dev/null | grep -q "proactive.timer.*enabled" && ok "作息调度器已启用（开机自启）" || bad "作息调度器未启用"
[ -f /opt/dsh/routines.json ] && ok "作息表 routines.json 存在" || bad "缺 routines.json"
[ -x /opt/dsh/proactive.py ] && ok "调度脚本 proactive.py 可执行" || bad "调度脚本缺失"
NT=$(systemctl list-timers --no-pager 2>/dev/null | grep -c proactive)
[ "$NT" -ge 1 ] && ok "调度器在跑（$NT）" || bad "调度器没在跑"

sec "11. 网络与端口"
ss -ltn 2>/dev/null | grep -q ':8643' && ok "8643 监听（OneBot 反向 WS）" || bad "8643 未监听"
ss -ltn 2>/dev/null | grep -qE '127\.0\.0\.1:8090' && ok "8090 仅本地（控制台）" || warn "8090 绑定异常"
ss -ltn 2>/dev/null | grep -qE '127\.0\.0\.1:3001' && ok "3001 仅本地（NapCat）" || warn "3001 绑定异常"
ss -ltn 2>/dev/null | grep -qE '127\.0\.0\.1:6099' && ok "6099 仅本地（NapCat WebUI）" || warn "6099 绑定异常"
EC=$(ss -tn 2>/dev/null | grep -c '172\.18\.[0-9.]*:8643')
[ "$EC" -ge 1 ] && ok "NapCat 已连上（连接数 $EC）" || bad "NapCat 未连接 8643"

sec "12. TokenHub 连通性"
K=$(sed -nE 's/^TOKENHUB_API_KEY=//p' /opt/dsh/tokenhub.env 2>/dev/null)
if [ -z "$K" ]; then bad "读不到 API key"; else
  C=$(curl -s -o /dev/null -w '%{http_code}' -m 15 https://tokenhub.tencentmaas.com/v1/models -H "Authorization: Bearer $K")
  [ "$C" = 200 ] && ok "GET /v1/models -> 200" || bad "GET /v1/models -> $C"
  R=$(curl -s -m 60 -X POST https://tokenhub.tencentmaas.com/v1/chat/completions \
      -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
      -d '{"model":"deepseek-v4.1-flash-1017","thinking":{"type":"disabled"},"messages":[{"role":"user","content":"只回复两个字：通了"}],"max_tokens":20}' \
      | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    if "error" in d:
        e=d["error"]; print("ERR "+str(e.get("code"))+" "+(e.get("message_zh") or "")[:60])
    else:
        print(d["choices"][0]["message"]["content"].strip())
except Exception as e: print("ERR "+str(e))' 2>/dev/null)
  case "$R" in
    ERR*) bad "模型调用失败: $R" ;;
    *)    ok "模型调用返回: $R" ;;
  esac
fi

sec "13. 资源"
free -m | awk 'NR==2{printf "  内存  %sMB / %sMB（可用 %sMB）\n",$3,$2,$7}'
AVA=$(free -m | awk 'NR==2{print $7}')
[ "$AVA" -gt 300 ] && ok "可用内存充足（${AVA}MB）" || warn "可用内存偏低（${AVA}MB）"
free -m | awk 'NR==3{printf "  Swap  %sMB / %sMB\n",$3,$2}'
df -h / | awk 'NR==2{printf "  磁盘  %s / %s（%s）\n",$3,$2,$5}'
DU=$(df / | awk 'NR==2{print $5}' | tr -d '%')
[ "$DU" -lt 85 ] && ok "磁盘使用 ${DU}%" || warn "磁盘使用 ${DU}%"

sec "14. 日志健康"
ERRS=$(grep -ac "故障参考号" "$LOG" 2>/dev/null); ERRS=$(echo "$ERRS" | head -1); [ -z "$ERRS" ] && ERRS=0
TODAY=$(date '+%F')
echo "  历史故障参考号总数: $ERRS"
LAST=$(grep -a "故障参考号" "$LOG" 2>/dev/null | tail -1 | cut -c1-140)
[ -n "$LAST" ] && echo "  最近一条: $LAST"
START=$(grep -an "bridge ready" "$LOG" 2>/dev/null | tail -1 | cut -d: -f1)
if [ -n "${START:-}" ]; then
  CUR=$(tail -n +"$START" "$LOG" | grep -ac "401008"); CUR=$(echo "$CUR" | head -1); [ -z "$CUR" ] && CUR=0
  [ "$CUR" -eq 0 ] && ok "本次启动后无额度错误" || bad "本次启动后有 $CUR 次额度错误"
else
  warn "日志里没有启动标记，跳过本次错误检查"
fi
DP=$(pgrep -fc "dsh web" 2>/dev/null); [ -z "$DP" ] && DP=0
[ "$DP" -le 1 ] && ok "单一 dsh 实例（$DP 个）" || warn "有 $DP 个 dsh 实例在跑"

sec "15. 可回滚备份"
B=$(ls /opt/dsh/profiles/web/cordis.patch.yml.bak-* 2>/dev/null | wc -l)
[ "$B" -ge 3 ] && ok "配置备份 $B 份" || warn "配置备份只有 $B 份"
PB=$(ls /opt/dsh/media/onebot/persona.md.bak-* 2>/dev/null | wc -l)
[ "$PB" -ge 1 ] && ok "人设备份 $PB 份" || warn "人设没有备份"

echo
echo "=========================================================="
if [ "$FAIL" -eq 0 ] && [ "$WARN" -eq 0 ]; then
  printf ' 结果: \033[32m全部通过\033[0m（0 失败 0 警告）\n'
elif [ "$FAIL" -eq 0 ]; then
  printf ' 结果: \033[33m通过，但有 %d 个警告\033[0m\n' "$WARN"
else
  printf ' 结果: \033[31m%d 项失败\033[0m，%d 个警告\n' "$FAIL" "$WARN"
fi
echo "=========================================================="
[ "$FAIL" -eq 0 ]
