#!/bin/sh
# DSH QQ bot self-check: one command, tells you exactly what is broken.
export PATH=/opt/node/bin:$PATH
export DSH_HOME=/opt/dsh
P=/opt/dsh/profiles/web/cordis.patch.yml
fail=0
ok()   { printf '  [OK]   %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; fail=1; }

echo "==================== DSH QQ bot 自检 ===================="
echo "时间: $(date '+%F %T')"

echo
echo "1. 配置文件"
if [ -f "$P" ]; then ok "存在 $P"; else bad "缺少 $P"; fi
grep -qE '^\s+provider:\s*tokenhub' "$P" && ok "模型路由指向 tokenhub" || bad "模型路由不是 tokenhub"
grep -qE 'thinkingFormat:\s*deepseek' "$P" && ok "thinkingFormat: deepseek" || bad "缺 thinkingFormat"
grep -qE '^\s+- image' "$P" && ok "已开启图片输入" || bad "未开启图片输入"
if grep -qE '^\s+reasoning:\s*.?off' "$P"; then ok "推理已关闭"; else echo "  [WARN] 推理未关闭（会更慢更贵）"; fi
grep -qE 'id: compaction-basic' "$P" && grep -qE 'disabled: false' "$P" && ok "历史压缩已启用" || bad "历史压缩未启用（历史会越滚越大）"
[ -f /opt/dsh/tokenhub.env ] && ok "API key 文件存在" || bad "缺 /opt/dsh/tokenhub.env"

echo
echo "2. 表情库"
S=/opt/dsh/media/onebot/stickers
if [ -f "$S/stickers.json" ]; then
  ok "索引 stickers.json 存在"
  python3 - "$S" <<'PY'
import json, os, sys
d = sys.argv[1]
try:
    e = json.load(open(os.path.join(d, "stickers.json"), encoding="utf-8"))
except Exception as x:
    print("  [FAIL] 索引解析失败:", x); raise SystemExit(1)
have = sum(1 for i in e if os.path.exists(i.get("file", "")))
print("  索引 %d 条，文件命中 %d 条" % (len(e), have))
if len(e) == 0:
    print("  [FAIL] 索引是空的，表情工具会全部报错"); raise SystemExit(1)
if have != len(e):
    print("  [WARN] 有 %d 条文件缺失" % (len(e) - have))
else:
    print("  [OK]   全部命中")
PY
else
  bad "缺少表情索引 stickers.json（表情工具会全部失败）"
fi

echo
echo "3. 服务状态"
A=$(systemctl is-active dsh-onebot 2>/dev/null)
[ "$A" = "active" ] && ok "dsh-onebot: active" || bad "dsh-onebot: $A"
N=$(systemctl is-active docker 2>/dev/null)
[ "$N" = "active" ] && ok "docker: active" || bad "docker: $N"
docker ps --format '{{.Names}}' 2>/dev/null | grep -qx napcat && ok "napcat 容器在跑" || bad "napcat 容器没跑"

echo
echo "3. 端口与连接"
ss -ltn 2>/dev/null | grep -q ':8643' && ok "8643 在监听" || bad "8643 未监听"
ss -tn 2>/dev/null | grep -q '172\.18\.[0-9.]*:8643' && ok "NapCat 已连上反向 WS" || bad "NapCat 未连接 8643"

echo
echo "4. 配置能否装配"
if cd /opt/dsh && timeout 90 dsh --profile web --dump-config >/tmp/sc-dump.txt 2>&1; then
  ok "dump-config 通过"
else
  bad "dump-config 失败："; tail -3 /tmp/sc-dump.txt
fi

echo
echo "5. TokenHub 连通性"
K=$(sed -nE 's/^TOKENHUB_API_KEY=//p' /opt/dsh/tokenhub.env 2>/dev/null)
if [ -z "$K" ]; then
  bad "读不到 API key"
else
  C=$(curl -s -o /dev/null -w '%{http_code}' -m 15 https://tokenhub.tencentmaas.com/v1/models -H "Authorization: Bearer $K")
  [ "$C" = "200" ] && ok "GET /v1/models -> 200" || bad "GET /v1/models -> $C"
  R=$(curl -s -m 60 -X POST https://tokenhub.tencentmaas.com/v1/chat/completions \
      -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
      -d "{\"model\":\"$(p=$(grep -A3 'id: agent-default-model' "$P" | sed -nE 's/^[[:space:]]+model:[[:space:]]*//p' | head -1); echo ${p:-ep-jc70682p})\",\"thinking\":{\"type\":\"disabled\"},\"messages\":[{\"role\":\"user\",\"content\":\"只回复两个字：通了\"}],\"max_tokens\":20}" \
      | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    if "error" in d:
        e=d["error"]
        print("ERR "+str(e.get("code"))+" "+str(e.get("message"))[:80])
    else:
        print(d["choices"][0]["message"]["content"].strip())
except Exception as e:
    print("ERR "+str(e))' 2>/dev/null)
  case "$R" in
    401008*) bad "免费额度已耗尽（换服务 ID 或充值）: $R" ;;
    ERR*)    bad "模型调用失败: $R" ;;
    *)       ok "模型调用返回: $R" ;;
  esac
fi

echo
echo "6. 资源"
free -m | awk 'NR==2{printf "  内存 已用 %sMB / 共 %sMB（可用 %sMB）\n",$3,$2,$7}'
free -m | awk 'NR==3{printf "  Swap 已用 %sMB / 共 %sMB\n",$3,$2}'
df -h / | awk 'NR==2{printf "  磁盘 已用 %s / 共 %s（%s）\n",$3,$2,$5}'

echo
echo "7. 最近故障"
if [ -f /var/log/dsh-onebot.log ]; then
  grep -a "故障参考号" /var/log/dsh-onebot.log | tail -3 | sed 's/^/  /'
  [ -z "$(grep -a '故障参考号' /var/log/dsh-onebot.log | tail -1)" ] && echo "  无"
else
  echo "  无日志"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "结果: 全部关键项通过 ✓"
else
  echo "结果: 有项目失败 ✗（见上面 [FAIL]）"
fi
exit $fail
