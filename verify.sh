#!/usr/bin/env bash
# 部署验收：跑完 install.sh 之后执行，AI 用它判断"到底成没成"
# 输出故意只用 ASCII（避免经 SSH 回来乱码），AI 直接读 PASS/FAIL
set -uo pipefail

DSH_HOME="${DSH_HOME:-/opt/dsh}"
NAP_PASS=0; NAP_FAIL=0
pass() { printf '  [PASS] %s\n' "$*"; NAP_PASS=$((NAP_PASS+1)); }
fail() { printf '  [FAIL] %s\n' "$*"; NAP_FAIL=$((NAP_FAIL+1)); }

chk_port() {  # port label
  if ss -lnt 2>/dev/null | awk '{print $4}' | grep -q ":$1\$"; then pass "$1 $2 listening"; else fail "$1 $2 NOT listening"; fi
}
chk_svc() {   # unit label
  if systemctl is-active --quiet "$1" 2>/dev/null; then pass "service $1 active"; else fail "service $1 inactive"; fi
}

echo "== 1. services =="
chk_svc dsh-onebot
chk_svc llm-shim

echo "== 2. ports =="
chk_port 8643 "onebot ws"
chk_port 8890 "llm shim"
chk_port 8090 "dsh web"

echo "== 3. mcp children =="
n=$(pgrep -cf 'memory_server\.py|websurf\.py|seer\.py|imagen\.py|admin\.py' 2>/dev/null || true)
n=${n:-0}
if [ "$n" -ge 5 ]; then pass "mcp processes = $n"; else fail "mcp processes = $n (expect >=5)"; fi

echo "== 4. onebot bridge =="
if grep -q 'bridge ready' /var/log/dsh-onebot.log 2>/dev/null; then
  pass "bridge ready found"
else
  fail "no 'bridge ready' in /var/log/dsh-onebot.log"
fi
if grep -q 'OneBot connected' /var/log/dsh-onebot.log 2>/dev/null; then
  pass "OneBot connected"
else
  fail "OneBot not connected (check napcat: docker logs --tail 50 napcat)"
fi

echo "== 5. napcat container =="
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx napcat; then
  pass "container napcat running"
else
  fail "container napcat not running"
fi

echo "== 6. config sanity =="
CFG="$DSH_HOME/profiles/web/cordis.patch.yml"
if [ -f "$CFG" ]; then
  pass "cordis.patch.yml exists"
  grep -q '127.0.0.1:8890' "$CFG" && pass "model route -> shim" || fail "model route not pointing to shim"
  grep -qE 'botQQ:\s*"?[0-9]{5,12}' "$CFG" && pass "botQQ set" || fail "botQQ missing"
  grep -qE 'accessToken:\s*"?[0-9a-f]{16,}' "$CFG" && pass "accessToken set" || fail "accessToken missing/short"
else
  fail "cordis.patch.yml missing"
fi
[ -s "$DSH_HOME/qwen.env" ] && pass "qwen.env non-empty" || fail "qwen.env missing/empty"

echo "== 6.5 persona =="
PER="$DSH_HOME/media/onebot/persona.md"
if [ -f "$PER" ]; then
  lines=$(wc -l < "$PER")
  if [ "$lines" -ge 20 ]; then pass "persona.md $lines lines"; else fail "persona.md only $lines lines (too thin)"; fi
  if grep -q '{{' "$PER"; then fail "persona.md still has {{placeholders}} - AI must fill them"; else pass "no placeholders left"; fi
else
  fail "persona.md missing"
fi

echo "== 7. full healthcheck =="
if [ -f "$DSH_HOME/checkall.py" ]; then
  timeout 120 python3 "$DSH_HOME/checkall.py" > /tmp/_verify_checkall.txt 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    pass "checkall.py exit 0"
  else
    fail "checkall.py exit $rc (details: /tmp/_verify_checkall.txt)"
  fi
else
  fail "checkall.py missing"
fi

echo
echo "=============================================="
printf "RESULT: %d passed, %d failed\n" "$NAP_PASS" "$NAP_FAIL"
if [ "$NAP_FAIL" -eq 0 ]; then
  echo "VERDICT: READY - ask the human to send one QQ message to confirm."
  exit 0
else
  echo "VERDICT: NOT READY - fix the FAIL items above before reporting success."
  exit 1
fi