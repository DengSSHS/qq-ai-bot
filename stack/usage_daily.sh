#!/bin/bash
# 每天把「用量」和「排障」两份汇总追加到 memory/usage-daily.md
D=$(date +%F)
OUT=/opt/dsh/memory/usage-daily.md
cd /opt/dsh || exit 0
{
  echo "## $D"
  echo "### 用量"
  /usr/bin/python3 usage_log.py --days 1 2>/dev/null | head -n 14
  echo "### 排障（近 24 小时）"
  /usr/bin/python3 ops_report.py --hours 24 2>/dev/null | head -n 14
  echo
} >> "$OUT"

# 同一份内容再发一封邮件（QQ 掉线时邮件是唯一还通的通道）
/usr/bin/python3 /opt/dsh/report_mail.py >> "$OUT" 2>&1
