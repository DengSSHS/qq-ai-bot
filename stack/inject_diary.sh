#!/bin/sh
# 定时把一条"提醒"注入到她的入站管线，让她自己决定写不写日记。
# 用法: inject_diary.sh noon|midnight
set -u
SLOT="${1:-noon}"
Q=/opt/dsh/media/onebot/qq-inject.jsonl
OWNER=__OWNER_QQ__
BOT=__BOT_QQ__
NOW=$(date +%s)
MID=$((NOW + $$))

case "$SLOT" in
  noon)
    TEXT="（午间提醒）到中午了。上午有没有什么值得记一笔的？有就写两三句进日记，写清楚当时你什么感觉；没什么意思就别硬写。写完不用专门回我。"
    ;;
  midnight)
    TEXT="（零点提醒）一天过完了。把今天过一遍，挑一两件真值得记的写成日记，带上你当时的心情——高兴、丢脸、烦、被逗笑都行。要是今天确实没什么，那就不写。写完不用专门回我。"
    ;;
  *)
    TEXT="（提醒）看看今天有什么值得记的。"
    ;;
esac

mkdir -p "$(dirname "$Q")"
python3 - "$Q" "$OWNER" "$BOT" "$MID" "$NOW" "$TEXT" <<'PY'
import json, os, sys
q, owner, bot, mid, now, text = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]), sys.argv[6]
ev = {
    "post_type": "message",
    "message_type": "private",
    "sub_type": "friend",
    "self_id": bot,
    "user_id": owner,
    "time": now,
    "message_id": mid,
    "raw_message": text,
    "message": [{"type": "text", "data": {"text": text}}],
    "sender": {"user_id": owner, "nickname": os.environ.get("OWNER_NICK", "主人")},
}
with open(q, "a", encoding="utf-8") as f:
    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
print("injected %s at %s" % (mid, now))
PY
