#!/bin/sh
# 记忆/日记服务自检
set -u
D=/opt/dsh/memory
echo "==================== 记忆服务自检 ===================="
fail=0
ok()  { printf '  [OK]   %s\n' "$1"; }
bad() { printf '  [FAIL] %s\n' "$1"; fail=1; }

[ -f /opt/dsh/memory_server.py ] && ok "服务脚本存在" || bad "缺 memory_server.py"
[ -d "$D/diary" ] && ok "日记目录存在" || bad "缺 $D/diary"
[ -f "$D/notes.md" ] && ok "长期笔记存在" || bad "缺 notes.md"

N=$(pgrep -fc memory_server.py 2>/dev/null || echo 0)
[ "$N" -ge 1 ] && ok "MCP 进程在跑（$N 个）" || bad "MCP 进程没起"

if [ -f "$D/server.log" ]; then
  ok "服务日志存在"
  echo "      最近: $(tail -1 "$D/server.log")"
else
  bad "没有 server.log（说明从没被拉起过）"
fi

CNT=$(ls "$D/diary"/*.md 2>/dev/null | wc -l)
echo "      日记文件: $CNT 个"
if [ "$CNT" -gt 0 ]; then
  ENT=$(grep -c '^### ' "$D/diary"/*.md 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
  ok "日记条目: $ENT 条"
fi

echo
echo "--- 长期笔记内容 ---"
sed -n '1,20p' "$D/notes.md" 2>/dev/null
echo
if [ "$fail" -eq 0 ]; then echo "结果: 记忆服务正常 ✓"; else echo "结果: 有问题 ✗"; fi
exit $fail
