#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 群机器人 · 全量体检
覆盖：服务/端口/配置/人设/补丁/MCP/记忆/表情/限流/定时/资源/日志/API/密钥权限/备份
只读为主，不修改任何东西（发现的问题单独修）。
"""
import json
import os
import re
import stat
import subprocess
import io
import sys
import urllib.request

OK, BAD, WARN, SKIP = [], [], [], []
SEC = []


# 「从未扫码登录」= 容器里还没有 onebot11_<QQ>.json。
# 这台机器还没到"能检查登录态"的阶段，相关项应该 skip 而不是 bad。
FRESH = not __import__("glob").glob("/opt/qqbot/data/config/onebot11_*.json")


def _count_write():
    try:
        rc, out, _err = sh("python3 /opt/dsh/capabilities.py --json", timeout=60)
        import json as _j
        rows = _j.loads(out or "[]")
        return sum(1 for r in rows if r.get("kind") == "write")
    except Exception:
        return 0


def sec(name):
    SEC.append(name)
    print()
    print("== %s ==" % name)


def ok(msg, extra=""):
    OK.append(msg)
    print("  [OK]   %s%s" % (msg, ("  " + extra) if extra else ""))


def bad(msg, extra=""):
    BAD.append(msg)
    print("  [FAIL] %s%s" % (msg, ("  " + extra) if extra else ""))


def warn(msg, extra=""):
    WARN.append(msg)
    print("  [WARN] %s%s" % (msg, ("  " + extra) if extra else ""))


def skip(msg, extra=""):
    """这一项次不检查（还没到能检查的状态），不算失败也不是警告。

    例：还没扫码登录，就没法检查 OneBot 连接/QQ 号/模型实测——
    以前这些会报 FAIL，让全新机器看起来"体检全红"，其实什么都没坏。
    """
    SKIP.append(msg)
    print("  [SKIP] %s%s" % (msg, ("  " + extra) if extra else ""))


def sh(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return -1, "", str(e)


def read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


CFG = "/opt/dsh/profiles/web/cordis.patch.yml"
PER = "/opt/dsh/media/onebot/persona.md"
BUNDLE = "/opt/dsh/profiles/web/node_modules/local-memory/cordis.patch.yml"
LIB = "/opt/dsh/profiles/web/node_modules/dsh-onebot-qq/lib"
LOG = "/var/log/dsh-onebot.log"

print("=" * 62)
print(" QQ 群机器人 · 全量体检   %s" % __import__("datetime").datetime.now().strftime("%F %T"))
print("=" * 62)

# ---------------- 1. 服务 ----------------
sec("1. 服务与进程")
for name in ("dsh-onebot", "docker", "ttyd"):
    rc, out, _ = sh("systemctl is-active %s" % name)
    (ok if out == "active" else bad)("%s: %s" % (name, out))
for name in ("dsh-onebot", "ttyd", "scheduler.timer", "body-state.timer", "diary-outband.timer"):
    rc, out, _ = sh("systemctl is-enabled %s" % name)
    (ok if out == "enabled" else bad)("%s 开机自启: %s" % (name, out))
rc, out, _ = sh("docker ps --format '{{.Names}}' | grep -x napcat")
(ok if rc == 0 else bad)("napcat 容器在跑")

# ---------------- 2. 端口 ----------------
sec("2. 端口暴露")
rc, out, _ = sh("ss -ltn")
listeners = out.splitlines()
def listen(addr, port):
    return any((addr in ln) and (":%d " % port in ln or ln.endswith(":%d" % port)) for ln in listeners)
if listen("0.0.0.0:8643", 8643):
    ok("8643 OneBot 反向 WS 在监听")
else:
    (skip if FRESH else bad)("8643 未监听（还没扫码登录，跳过）" if FRESH else "8643 未监听")

# 插件配置能不能加载：YAML 里值后面少一个空格接注释（`0.05# 说明`）就变成字符串，
# 校验失败后整块 entry 不激活——服务 is-active 仍是 active，端口也可能在，
# 但 QQ 完全不通（2026-10-26 实测：她离线约 40 分钟，体检全绿，没人发现）。
rc, out, _ = sh("awk '/mounted \\(mode=/{buf=\"\"} {buf=buf $0 \"\\n\"} END{print buf}' "
                "/var/log/dsh-onebot.log | grep -c 'ValidationError\\|did not activate'")
try:
    cfg_bad = int(out or 0)
    (bad if cfg_bad else ok)(
        "插件配置校验失败 %d 处（看 /var/log/dsh-onebot.log，她应该是掉线的）" % cfg_bad
        if cfg_bad else "插件配置校验通过（本次启动没有 ValidationError）")
except Exception:
    (skip if FRESH else warn)("读不到启动日志，跳过插件配置校验")

# LLM 转发层：强制关思考，缺了它会慢 4~5 倍
if listen("127.0.0.1:8890", 8890):
    ok("8890 LLM 转发层在监听")
else:
    bad("8890 LLM 转发层未监听（systemctl start llm-shim）")
baseurl = ""
try:
    for line in open(CFG, encoding="utf-8"):
        if "baseURL:" in line and "platform" not in line:
            baseurl = line.strip()
            break
except Exception:
    pass
if "127.0.0.1:8890" in baseurl:
    ok("模型线路已指向转发层")
else:
    bad("模型线路没指向转发层（当前：%s）" % (baseurl or "?"))
for port, label in ((7681, "网页终端 ttyd"), (8090, "DSH 控制台"), (3001, "NapCat WS"), (6099, "NapCat WebUI")):
    lo = any(("127.0.0.1:%d" % port) in ln for ln in listeners)
    pub = any(("0.0.0.0:%d" % port) in ln or ("[::]:%d" % port) in ln for ln in listeners)
    if lo and not pub:
        ok("%d %s 仅本地" % (port, label))
    elif pub:
        bad("%d %s 暴露到 0.0.0.0（不该）" % (port, label))
    else:
        warn("%d %s 没在监听" % (port, label))

# ---------------- 3. 配置装配 ----------------
sec("3. 配置装配")
env = dict(os.environ)
env["PATH"] = "/opt/node/bin:/usr/local/bin:/usr/bin:/bin"
env["DSH_HOME"] = "/opt/dsh"
try:
    p = subprocess.run("cd /opt/dsh && dsh --profile web --dump-config",
                       shell=True, capture_output=True, text=True, timeout=120, env=env)
    dump, derr = p.stdout, p.stderr
    if p.returncode == 0:
        ok("dump-config 通过")
    else:
        bad("dump-config 退出码 %d" % p.returncode)
    nf = derr.count("not found")
    (ok if nf == 0 else bad)("无效条目引用：%d" % nf)
except Exception as e:
    dump, derr = "", ""
    bad("dump-config 执行失败: %s" % e)

def onebot_field(key):
    m = re.search(r"^\s+%s:\s*(.+)$" % re.escape(key), dump, re.M)
    return m.group(1).strip().strip("'\"") if m else None

expect = {
    "provider": "platform",
    "model": "deepseek-v4.1-flash-1017",
    "reasoning": "off",
    "thinkingFormat": "deepseek",
    "contextWindow": "65536",
    "maxTokens": "800",
    "headroomTokens": "16384",
}
for k, v in expect.items():
    got = onebot_field(k)
    if got == v:
        ok("%s = %s" % (k, v))
    else:
        bad("%s 期望 %s，实际 %s" % (k, v, got))

qq = {
    "requireMention": "true", "groupMentionAtOnly": "true",
    "groupActivationMinutes": "10", "groupMuteMinutes": "10",
    "dmPolicy": "allowlist", "rateLimitPerMinute": "8",
    # 2000 是安全阀（实测每天 30~45 条）。曾写 400，撞上会表现为"完全发不出消息"。
    "actionRatePerMinute": "20", "actionRatePerDay": "2000",
    "inboundImageMaxPx": "1024", "sttEnabled": "false",
    "interimMessages": "false", "deferredTimeoutMs": "0",
    # injectDryRun 必须是 false：true 时注入轮次的出站全被拦成 dry-run，
    # 表现为"她生成了但一条都没发出去"（主动私聊/日记/空间都受影响）。
    "injectEnabled": "true", "injectDryRun": "false",
}
# botQQ 不写死（每套部署不同），只校验格式
_got = onebot_field("botQQ")
if _got and re.fullmatch(r"\d{5,12}", _got):
    ok("QQ botQQ = %s（格式正常）" % _got)
else:
    (skip if FRESH else bad)("QQ botQQ 未就绪（还没扫码）" if FRESH else "QQ botQQ 异常：%r" % _got)

for k, v in qq.items():
    got = onebot_field(k)
    if got == v:
        ok("QQ %s = %s" % (k, v))
    else:
        bad("QQ %s 期望 %s，实际 %s" % (k, v, got))

# ---------------- 4. 人设 ----------------
sec("4. 人设")
p = read(PER)
if p:
    ok("人设文件存在", "%d 字节" % len(p.encode()))
else:
    bad("缺 persona.md")
for s in ("## 底线", "## 她是谁", "## 开口之前，四步", "## 什么时候开口", "## 脾气",
          "## 说话", "## 三条判尺", "## 工具", "## 你的号", "## 日记"):
    (ok if s in p else bad)("含章节 %s" % s)
if "image_generate" in p:
    ok("已写画图工具")
else:
    warn("人设没提 image_generate")
if "web_search" in p:
    ok("已写联网工具")
else:
    warn("人设没提 web_search")
if "[语音]" in p:
    ok("已写语音占位符规则")
else:
    warn("缺 [语音] 占位符规则")
# 只检查"正面许可"：以 ❌ 开头的行是反面例子，不算违规
_positive = [ln for ln in p.splitlines() if not ln.strip().startswith("❌")]
for forbidden in ("（尾鳍", "尾巴动作", "旁白式动作"):
    if any(forbidden in ln for ln in _positive):
        bad("人设里还有旁白许可：%s" % forbidden)
    else:
        ok("不含旁白许可「%s」" % forbidden)

# ---------------- 5. 代码补丁 ----------------
sec("5. 代码补丁")
checks = [
    ("bridge.js", "stripNarration", "旁白拦截"),
    ("bridge.js", "splitBubbles", "气泡拆分"),
    ("tools.js", "assertGroupMembership", "群成员校验"),
    ("tools.js", "qzone-daily-limit", "空间一天一条"),
    ("tools.js", "send_qzone_msg", "空间发送白名单"),
    ("tools.js", "get_guild_list", "频道只读白名单"),
    ("inbound.js", "isAdminUser", "闭嘴分级（管理员触发）"),
]
for f, marker, label in checks:
    t = read(os.path.join(LIB, f))
    (ok if marker in t else bad)("%s: %s" % (label, f))
if "policy.adminUsers.includes(String((seg.data" in read(os.path.join(LIB, "inbound.js")):
    bad("owner-mention 触发代码还在（别人 @ 主人会触发）")
else:
    ok("owner-mention 触发代码已移除")

# ---------------- 5b. 工具档位与安全闸门 ----------------
sec("5b. 工具档位与安全闸门")
try:
    st = json.loads(read("/opt/dsh/toolmode.state"))
    mode = st.get("mode")
    (ok if mode in ("chat", "full") else bad)("工具档位：%s" % mode)
except Exception:
    warn("读不到 toolmode.state（还没切过档位，正常）")
rc, out, _ = sh("systemctl is-active toolmode.path")
(ok if out == "active" else bad)("toolmode.path 监听: %s" % out)
info = read("/opt/dsh/profiles/web/node_modules/local-memory/cordis.patch.yml")
for srv in ("memory", "websurf", "imagen", "admin"):
    (ok if ("serverName: " + srv) in info else bad)("MCP 已注册：%s" % srv)
sec_txt = read(CFG)
if "TOOLMODE-SECTION" in sec_txt:
    ok("档位段存在（由 toolmode.py 生成）")
else:
    bad("缺档位段")
lines_all = sec_txt.splitlines()
def hard_off(name):
    idx = None
    for i2, ln2 in enumerate(lines_all):
        if ln2.strip() == "- id: " + name:
            idx = i2
            break
    if idx is None:
        return False
    for ln2 in lines_all[idx + 1: idx + 6]:
        if ln2.strip().startswith("- id: "):
            break
        if ln2.strip() == "disabled: true":
            return True
    return False
for hard in ("tool-bash", "tool-fs", "tool-subagent", "tool-workflow"):
    (ok if hard_off(hard) else bad)("硬闸门 " + hard + " 已禁用")

# ---------------- 6. MCP ----------------
sec("6. MCP 服务")
for name, script in (("memory", "memory_server.py"), ("websurf", "websurf.py"), ("imagen", "imagen.py")):
    rc, out, _ = sh("pgrep -fc %s" % script)
    (ok if out and int(out) >= 1 else bad)("%s 进程：%s" % (name, out or "0"))

def mcp_probe(script, tool=None, args=None, timeout=60):
    payload = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "probe", "version": "0"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]
    if tool:
        payload.append(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": tool, "arguments": args or {}}}))
    try:
        p = subprocess.run(["python3", script], input="\n".join(payload) + "\n",
                           capture_output=True, text=True, timeout=timeout)
        lines = [json.loads(x) for x in p.stdout.splitlines() if x.strip().startswith("{")]
        tools = []
        for d in lines:
            if d.get("id") == 2:
                tools = [t["name"] for t in (d.get("result") or {}).get("tools", [])]
        call = ""
        for d in lines:
            if d.get("id") == 3:
                c = (d.get("result") or {}).get("content") or [{}]
                call = (c[0].get("text") or "")[:120]
        return tools, call
    except Exception as e:
        return [], "probe error: %s" % e

tools, call = mcp_probe("/opt/dsh/memory_server.py", "diary_recent", {"n": 1})
(ok if tools else bad)("memory 工具：%s" % ", ".join(tools))
tools2, _ = mcp_probe("/opt/dsh/websurf.py")
(ok if tools2 else bad)("websurf 工具：%s" % ", ".join(tools2))
tools3, _ = mcp_probe("/opt/dsh/imagen.py")
(ok if tools3 else bad)("imagen 工具：%s" % ", ".join(tools3))

# ---------------- 7. 记忆 ----------------
sec("7. 记忆与检索")
diary = "/opt/dsh/memory/diary"
files = [f for f in os.listdir(diary) if f.endswith(".md")] if os.path.isdir(diary) else []
entries = 0
for f in files:
    entries += len(re.findall(r"^### ", read(os.path.join(diary, f)), re.M))
(ok if files else warn)("日记文件 %d 个，条目 %d 条" % (len(files), entries))
notes = read("/opt/dsh/memory/notes.md")
for s in ("## 主人", "## 常混的群", "## 群里印象深的人", "## 反复出现的梗"):
    (ok if s in notes else warn)("笔记含 %s" % s)
if os.path.exists("/opt/dsh/memory/search.db"):
    ok("FTS 索引存在")
else:
    warn("FTS 索引未建（下次检索会自动建）")
if os.path.exists("/opt/dsh/memory/diary/index.json"):
    try:
        n = len(json.loads(read("/opt/dsh/memory/diary/index.json")))
        ok("遗忘曲线索引 %d 条" % n)
    except Exception:
        warn("遗忘曲线索引损坏")
else:
    warn("遗忘曲线索引未建")

# ---------------- 8. 表情库 ----------------
sec("8. 表情库")
stk = "/opt/dsh/media/onebot/stickers"
idx = os.path.join(stk, "stickers.json")
if os.path.exists(idx):
    try:
        ents = json.loads(read(idx))
        miss = sum(1 for e in ents if not os.path.exists(e.get("file", "")))
        if miss == 0:
            ok("索引 %d 条，文件全在" % len(ents))
        else:
            bad("索引 %d 条，缺 %d 个文件" % (len(ents), miss))
    except Exception as e:
        bad("表情索引解析失败: %s" % e)
else:
    (skip if FRESH else bad)("缺 stickers.json（表情库还没建，跳过）" if FRESH else "缺 stickers.json")

# ---------------- 9. 定时与作息 ----------------
sec("9. 定时器与作息")
rc, out, _ = sh("systemctl list-timers proactive.timer body-state.timer --no-pager")
if rc == 0 and out.count("timer") >= 2:
    for ln in out.splitlines()[1:5]:
        if ln.strip():
            print("   " + ln.strip()[:100])
    ok("proactive + body-state 都在")
else:
    bad("定时器异常")
try:
    cfg = json.loads(read("/opt/dsh/routines.json"))
    ids = [r["id"] for r in cfg.get("routines", [])]
    ok("作息 %d 条：%s（预算 %s）" % (len(ids), ",".join(ids), cfg.get("dailyBudget")))
    if "qzone_daily" not in ids:
        bad("缺 qzone_daily（零点发说说）")
except Exception as e:
    bad("routines.json 解析失败: %s" % e)

# ---------------- 10. 资源 ----------------
sec("10. 资源")
rc, out, _ = sh("free -m")
for ln in out.splitlines():
    if ln.startswith("Mem:") or ln.startswith("Swap:"):
        print("   " + re.sub(r"\s+", " ", ln))
        if ln.startswith("Mem:"):
            avail = int(ln.split()[6])
            (ok if avail > 300 else warn)("可用内存 %dMB" % avail)
rc, out, _ = sh("df -h / | tail -1")
print("   磁盘 " + re.sub(r"\s+", " ", out))
pct = int(re.search(r"(\d+)%", out).group(1)) if re.search(r"(\d+)%", out) else 0
(ok if pct < 85 else warn)("磁盘使用 %d%%" % pct)

# ---------------- 11. 日志健康 ----------------
sec("11. 日志健康")
logtxt = read(LOG)
marks = [m for m in re.finditer(r"bridge ready", logtxt)]
if marks:
    tail = logtxt[marks[-1].start():]
    errs = tail.count("故障参考号")
    sendfail = tail.count("final send failed")
    (ok if errs == 0 else bad)("本次启动后模型错误：%d" % errs)
    (ok if sendfail == 0 else bad)("本次启动后发送失败：%d" % sendfail)
else:
    warn("日志里没有启动标记")
total = logtxt.count("故障参考号")
print("   历史故障参考号总数（含旧）：%d" % total)

# ---------------- 12. API 与额度 ----------------
sec("12. 模型 API")
try:
    # 从配置里解析实际线路（现在指向本地转发层）
    baseurl = ""
    for _ln in read(CFG).splitlines():
        if "baseURL:" in _ln:
            baseurl = _ln.split("baseURL:", 1)[1].strip().strip('"').strip("'")
            break
    if not baseurl:
        bad("配置里找不到 baseURL")
    key = ""
    for ln in read("/opt/dsh/qwen.env").splitlines():
        if ln.startswith("QWEN_API_KEY="):
            key = ln.split("=", 1)[1].strip()
    if not key:
        bad("读不到 QWEN_API_KEY")
    else:
        req = urllib.request.Request(baseurl.rstrip("/") + "/models",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
        ids = [m["id"] for m in d.get("data", [])]
        ok("/models 200，%d 个模型" % len(ids))
        (ok if "deepseek-v4.1-flash-1017" in ids else bad)("含 deepseek-v4.1-flash-1017")
        body = json.dumps({"model": "deepseek-v4.1-flash-1017", "max_tokens": 12, "stream": False,
                           "thinking": {"type": "disabled"},
                           "messages": [{"role": "user", "content": "回两个字：在的"}]}).encode()
        req2 = urllib.request.Request(baseurl.rstrip("/") + "/chat/completions",
                                      data=body, headers={"Authorization": "Bearer " + key,
                                                          "Content-Type": "application/json"})
        with urllib.request.urlopen(req2, timeout=60) as r:
            d2 = json.loads(r.read().decode())
        txt = (d2["choices"][0]["message"].get("content") or "").strip()
        (ok if txt else bad)("实测调用返回：%r" % txt[:30])
        u = d2.get("usage", {})
        print("   用量：prompt=%s out=%s total=%s" % (u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens")))
except Exception as e:
    bad("API 调用异常：%s" % e)

# ---------------- 13. 密钥权限 ----------------
sec("13. 密钥文件权限")
for f in ("/opt/dsh/qwen.env", "/opt/dsh/tokenhub.env", "/opt/dsh/ttyd.pass",
          "/opt/dsh/.credentials.yaml"):
    if os.path.exists(f):
        mode = stat.S_IMODE(os.stat(f).st_mode)
        (ok if mode <= 0o600 else bad)("%s 权限 %o" % (f, mode))
    else:
        warn("不存在：%s" % f)
rc, out, _ = sh("ss -ltn | grep -c 0.0.0.0:22")
print("   SSH 22 对外：%s" % ("是（正常）" if out else "未监听？"))

# ---------------- 14. 备份 ----------------
sec("14. 备份")
for pat, label in (("profiles/web/cordis.patch.yml.bak-*", "配置"),
                   ("media/onebot/persona.md.bak-*", "人设"),
                   ("profiles/web/node_modules/dsh-onebot-qq/lib/*.bak-*", "插件代码")):
    rc, out, _ = sh("ls -1 /opt/dsh/%s 2>/dev/null | wc -l" % pat)
    (ok if out and int(out) > 0 else warn)("%s备份 %s 份" % (label, out or "0"))

# ---------------- 15. 发送审计 ----------------
sec("15. 发送审计")
AUDIT = "/opt/dsh/media/onebot/send_audit.jsonl"
try:
    import collections
    import json as _json
    import time as _time
    since = _time.time() - 86400
    c = collections.Counter()
    for line in io.open(AUDIT, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            row = _json.loads(line)
        except Exception:
            continue
        if float(row.get("ts") or 0) < since:
            continue
        if row.get("intercepted"):
            c["intercepted"] += 1
        elif row.get("fake"):
            c["fake"] += 1
        elif row.get("error"):
            c["fail"] += 1
        elif row.get("ok"):
            c["ok"] += 1
    total = sum(c.values())
    if total == 0:
        warn("最近 24h 没有发送记录（刚装或没在跑？）")
    else:
        (bad if c["fake"] else ok)(
            "真实发出 %d 条｜注入拦截 %d 条｜失败 %d 条｜假成功 %d 条"
            % (c["ok"], c["intercepted"], c["fail"], c["fake"]))
        if c["fake"]:
            bad("假成功：send_* 没拿到真实 message_id（看 %s）" % AUDIT)
        if c["fail"]:
            warn("发送失败 %d 条（重启瞬间的 transport stopped，或掉线期间）" % c["fail"])
except Exception as e:
    warn("读不到发送审计：%s" % e)

# ---------------- 16. 能力注册表 ----------------
sec("16. 能力注册表")
try:
    rc, out, _err = sh("python3 /opt/dsh/capabilities.py --check", timeout=60)
    miss = [ln for ln in (out or "").split(chr(10)) if ln.startswith("[MISS]")]
    total = [ln for ln in (out or "").split(chr(10)) if ln.startswith("[OK]") or ln.startswith("[MISS]")]
    if not total:
        warn("能力注册表没输出（capabilities.py 跑不起来？）")
    elif miss:
        bad("能力未接线 %d/%d 项" % (len(miss), len(total)))
        for ln in miss[:6]:
            bad(ln.replace("[MISS]", "").strip()[:110])
    else:
        ok("能力接线正常（%d 项，含写操作 %d 项）" % (len(total), _count_write()))
except Exception as e:
    warn("能力注册表校验失败：%s" % e)

# ---------------- 17. 上下文稳定性 ----------------
sec("17. 上下文稳定性（缓存）")
try:
    rc, out, _err = sh("python3 /opt/dsh/ctx_hash.py", timeout=90)
    lines = (out or "").split(chr(10))
    head = [ln for ln in lines if ln.startswith("[OK  ]") or ln.startswith("[FAIL]") or ln.startswith("[WARN]")]
    if not head:
        warn("ctx_hash.py 没给出结论")
    elif head[0].startswith("[OK"):
        ok(head[0].replace("[OK  ] ", "").strip()[:110])
    elif head[0].startswith("[FAIL"):
        bad("稳定前缀跨轮不一致（前缀缓存必然 miss）")
        for ln in head[:2]:
            bad(ln.replace("[FAIL]", "").strip()[:110])
    else:
        warn(head[0].replace("[WARN]", "").strip()[:110])
except Exception as e:
    warn("上下文稳定性检查失败：%s" % e)

# ---------------- 18. 调度器与出站队列 ----------------
sec("18. 调度器 / 出站队列")
try:
    import json as _j2
    import time as _t2
    # 调度器定时器
    rc, out, _e = sh("systemctl is-active scheduler.timer")
    (ok if out == "active" else bad)("scheduler.timer: %s" % out)
    rc, out, _e = sh("systemctl is-active proactive.timer")
    if out == "active":
        warn("旧的 proactive.timer 还在跑（会和调度器重复触发）")
    # 待办意图
    pend = 0
    try:
        for line in io.open("/opt/dsh/memory/intents.jsonl", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                if _j2.loads(line).get("state") == "new":
                    pend += 1
            except Exception:
                pass
    except Exception:
        pass
    (warn if pend > 5 else ok)("待办意图 %d 条" % pend)
    # outbox 结果
    okn = badn = 0
    since = _t2.time() - 86400
    try:
        for line in io.open("/opt/dsh/media/onebot/qq-outbox.result.jsonl", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                row = _j2.loads(line)
            except Exception:
                continue
            if float(row.get("ts") or 0) < since:
                continue
            if row.get("ok"):
                okn += 1
            else:
                badn += 1
    except Exception:
        pass
    (ok if badn == 0 else warn)("outbox 24h：成功 %d 条，失败 %d 条" % (okn, badn))
except Exception as e:
    warn("调度器检查失败：%s" % e)

# ---------------- 总结 ----------------
print()
print("=" * 62)
print(" 失败 %d 项，警告 %d 项，未验证 %d 项" % (len(BAD), len(WARN), len(SKIP)))
if BAD:
    print(" 需要修：")
    for b in BAD:
        print("   - " + b)
if WARN:
    print(" 提醒：")
    for w in WARN:
        print("   - " + w)
if SKIP:
    print(" 未验证（还没到能检查的阶段，不是故障）：")
    for k in SKIP:
        print("   - " + k)
print("=" * 62)
sys.exit(1 if BAD else 0)
