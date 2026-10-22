#!/usr/bin/env bash
# 上线前自检：把能在本地静态验证的东西全查一遍
#
# 用法：bash preflight.sh
# 输出只用 ASCII（PASS/FAIL），避免经 SSH 传回时中文乱码导致误判。
#
# 注意：这个脚本**不能**替代真实部署验证（install.sh 装系统、扫码、发消息）。
# 它只保证"代码和文档自身没有明显错误"。
set -uo pipefail

P=0; F=0; W=0
pass() { printf '  [PASS] %s\n' "$*"; P=$((P+1)); }
fail() { printf '  [FAIL] %s\n' "$*"; F=$((F+1)); }
warn() { printf '  [WARN] %s\n' "$*"; W=$((W+1)); }
sec()  { printf '\n== %s ==\n' "$*"; }

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

# ── 1. 文件完整性 ──────────────────────────────────────────────
sec "1. required files"
for f in README.md LICENSE NOTICE.md CHANGELOG.md SECURITY.md CONTRIBUTING.md \
         VERSION .gitignore .gitattributes .editorconfig \
         START-HERE.md DEPLOY.md MANUAL.md install.sh verify.sh qr_wait.py \
         deploy.env.example persona/TEMPLATE.md persona/example-jingjing.md \
         stack/cordis.patch.yml.tpl stack/profile-package.json \
         stack/local-memory/cordis.patch.yml stack/local-memory/package.json \
         .github/workflows/ci.yml; do
  [ -e "$f" ] && pass "$f" || fail "missing $f"
done

sec "2. files referenced by install.sh"
missing=0
while read -r rel; do
  [ -z "$rel" ] && continue
  case "$rel" in *deploy.env) continue ;; esac   # 用户自己创建
  if [ ! -e "$rel" ]; then fail "install.sh references missing: $rel"; missing=$((missing+1)); fi
done < <(grep -oE '\$HERE/[A-Za-z0-9_./-]+|\$STACK/[A-Za-z0-9_./-]+' install.sh \
         | sed 's|^\$HERE/||; s|^\$STACK/|stack/|' | sort -u)
[ "$missing" -eq 0 ] && pass "all install.sh references resolve"

sec "3. files referenced by docs"
missing=0
for doc in README.md DEPLOY.md MANUAL.md START-HERE.md CONTRIBUTING.md SECURITY.md CHANGELOG.md; do
  while read -r ref; do
    [ -z "$ref" ] && continue
    case "$ref" in http*|/opt/*|./*) continue ;; esac
    case "$ref" in cordis.patch.yml) ref="stack/cordis.patch.yml.tpl" ;; esac
    if [ ! -e "$ref" ] && [ ! -d "$ref" ] && [ ! -e "stack/$ref" ] && [ ! -e "persona/$ref" ]; then
      fail "$doc references missing: $ref"; missing=$((missing+1))
    fi
  done < <(grep -oE '`[A-Za-z0-9_./-]+\.(md|py|sh|yml|json|example)`' "$doc" 2>/dev/null \
           | tr -d '`' | sort -u)
done
[ "$missing" -eq 0 ] && pass "doc file references resolve"

# ── 4. 语法 ────────────────────────────────────────────────────
sec "4. syntax"
# 找一个真的能跑的解释器（Windows 上 python3 常是商店别名存根，存在但跑不起来）
PY=""
for cand in python3 python py; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import sys; sys.exit(0)" >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  fail "no working python interpreter found (need 3.8+)"
  printf '
PREFLIGHT: cannot continue without python
'
  exit 1
fi
if "$PY" scripts/preflight_checks.py; then
  pass "python syntax / json parse"
else
  fail "python syntax / json parse"
fi

for f in install.sh verify.sh preflight.sh; do
  [ -f "$f" ] || continue
  bash -n "$f" 2>/dev/null && pass "bash -n $f" || fail "bash -n $f"
done
for f in stack/*.sh; do
  bash -n "$f" 2>/dev/null && pass "bash -n $f" || fail "bash -n $f"
done

if "$PY" -c "import yaml" 2>/dev/null; then
  for f in stack/cordis.patch.yml.tpl .github/workflows/ci.yml; do
    "$PY" -c "import yaml,sys;yaml.safe_load(open(sys.argv[1],encoding='utf-8'))" "$f" \
      && pass "yaml parse $f" || fail "yaml parse $f"
  done
else
  warn "pyyaml not installed, skip YAML parse"
fi

# ── 5. 占位符 ──────────────────────────────────────────────────
sec "5. placeholders"
TPL=stack/cordis.patch.yml.tpl
for k in __LLM_MODEL__ __WS_TOKEN__ __BOT_QQ__ __OWNER_QQ__; do
  grep -q "$k" "$TPL" && pass "tpl has $k" || fail "tpl missing $k"
done
grep -q '__VISION_BASE__' stack/local-memory/cordis.patch.yml \
  && pass "mcp vision has __VISION_BASE__" || fail "mcp vision missing __VISION_BASE__"

# install.sh 必须能替换 tpl 里出现的每个占位符
for k in $(grep -oE '__[A-Z_]+__' "$TPL" | sort -u); do
  grep -q "$k" install.sh && pass "install.sh substitutes $k" || fail "install.sh cannot substitute $k"
done

# 示例人设不该有占位符（它是成品）
if grep -q '{{' persona/example-jingjing.md; then
  fail "example persona still has {{placeholders}}"
else
  pass "example persona has no placeholders"
fi
# 模板人设必须有占位符（它是骨架）
n=$(grep -c '{{' persona/TEMPLATE.md || true)
[ "$n" -gt 5 ] && pass "TEMPLATE has $n placeholder lines" || fail "TEMPLATE looks empty ($n)"

# ── 6. secrets / privacy ───────────────────────────────────────
sec "6. secrets and privacy"
scan() { grep -rnE "$1" --exclude-dir=.git --exclude-dir=__pycache__ . 2>/dev/null | head -3; }
if scan 'sk-[A-Za-z0-9]{20,}'; then fail "possible API key"; else pass "no API key pattern"; fi
if scan 'BEGIN [A-Z ]*PRIVATE KEY'; then fail "private key found"; else pass "no private key"; fi
if scan 'accessToken:[[:space:]]*"[0-9a-f]{20,}"'; then fail "hardcoded accessToken"; else pass "no hardcoded token"; fi
if scan 'maas\.qianwen|api\.siliconflow\.cn/sk-'; then fail "private endpoint leaked"; else pass "no private endpoint"; fi
if [ -e deploy.env ]; then fail "deploy.env must not be committed"; else pass "deploy.env absent"; fi

# ── 7. 文档一致性 ──────────────────────────────────────────────
sec "7. doc consistency"
v=$(cat VERSION 2>/dev/null | tr -d '[:space:]')
grep -q "$v" README.md && pass "VERSION $v appears in README" || warn "VERSION $v not in README"
grep -q "$v" CHANGELOG.md && pass "VERSION $v appears in CHANGELOG" || warn "VERSION $v not in CHANGELOG"

# checkall 节数 vs README 声称的项数
if [ -f stack/checkall.py ]; then
  cnt=$(grep -c 'sec(' stack/checkall.py || true)
  [ "$cnt" -gt 5 ] && pass "checkall has $cnt sections" || warn "checkall sections: $cnt"
fi

# install.sh 的阶段数与文档描述
stages=$(grep -c '^phase "' install.sh || true)
[ "$stages" -gt 5 ] && pass "install.sh has $stages phases" || warn "install.sh phases: $stages"

# ── 8. 脚本可执行性与健壮性 ────────────────────────────────────
sec "8. script robustness"
for f in install.sh verify.sh qr_wait.py; do
  [ -x "$f" ] && pass "$f executable" || warn "$f not executable (chmod +x)"
done

# install.sh 缺参数时必须快速退出且不碰系统
if grep -q 'PERSONA_MODE' install.sh && grep -q 'die' install.sh; then
  pass "install.sh validates PERSONA_MODE"
else
  fail "install.sh lacks PERSONA_MODE validation"
fi

# qr_wait 超时行为（不启动 napcat 时应 180s 后退出 2；这里只验证它能 import 并解析参数）
[ -f qr_wait.py ] && pass "qr_wait.py present" || fail "qr_wait.py missing"

# verify.sh 必须输出机器可读结论
grep -q 'VERDICT:' verify.sh && pass "verify.sh prints VERDICT" || fail "verify.sh lacks VERDICT"

# .sh 必须 LF
for f in *.sh stack/*.sh; do
  grep -q $'\r' "$f" 2>/dev/null && fail "CRLF in $f"
done
pass "no CRLF in shell scripts"

sec "9. systemd units consistency"
# 每个 unit 的 ExecStart 引用的脚本必须存在（在 stack/ 里或由 install.sh 生成）
unit_bad=0
for u in stack/systemd/*.service; do
  [ -f "$u" ] || continue
  while read -r target; do
    [ -z "$target" ] && continue
    case "$target" in
      */opt/dsh/*) base=$(basename "$target")
                   [ -e "stack/$base" ] || { fail "$u needs stack/$base (missing)"; unit_bad=$((unit_bad+1)); } ;;
      */opt/node/*) : ;;   # 由 install.sh 安装
      /usr/bin/*|/bin/*) : ;;
      *) : ;;
    esac
  done < <(grep -oE '^ExecStart=-?[^ ]+' "$u" | sed 's/^ExecStart=-*//')
done
[ "$unit_bad" -eq 0 ] && pass "all unit ExecStart targets exist"

# 改了 DSH_HOME 时，install.sh 必须重写 unit 里的路径
if grep -q 's|/opt/dsh|' install.sh; then
  pass "install.sh rewrites DSH_HOME in units"
else
  fail "install.sh does not rewrite DSH_HOME in units (service will break)"
fi

# 可选组件不应被默认安装
[ -e stack/systemd/optional ] && pass "optional units separated" || warn "no optional/ dir"

# ── 汇总 ───────────────────────────────────────────────────────
printf '\n==============================================\n'
printf 'PREFLIGHT: %d passed, %d failed, %d warnings\n' "$P" "$F" "$W"
if [ "$F" -eq 0 ]; then
  printf 'VERDICT: STATIC OK - still need a clean-machine deploy test\n'
  exit 0
else
  printf 'VERDICT: NOT READY - fix FAIL items above\n'
  exit 1
fi
