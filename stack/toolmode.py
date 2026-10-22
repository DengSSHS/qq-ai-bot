#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具模式切换器：chat（默认，精简）/ full（全量，含插件管理）

调用链：她在私聊被主人要求 → 调 MCP 工具 tool_mode → 写请求文件
        → systemd path 触发本脚本 → 改配置 + 校验 → 重启服务 → 生效
bash / fs / subagent 这类永远不给，跟模式无关。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

CFG = "/opt/dsh/profiles/web/cordis.patch.yml"
DEFAULT = "/tmp/preset-default.txt"
REQ = "/opt/dsh/toolmode.request"
STATE = "/opt/dsh/toolmode.state"
MARK = "# ==== TOOLMODE-SECTION ===="
NL = chr(10)
LOG = "/opt/dsh/memory/toolmode.log"

# 永远不给：终端、文件、子代理、工作流、目标规划
ALWAYS_OFF = {
    "tool-bash", "tool-pwsh", "tool-fs", "tool-fs-search", "tool-jobs",
    "tool-schedule", "skill-filesystem", "tool-skill", "command-goal", "tool-goal",
    "planning", "plan-mode", "delegation", "tool-subagent-control",
    "tool-subagent-list-agents", "tool-subagent", "tool-subagent-fork",
    "workflow-ptc", "tool-workflow", "tool-plugin-market",
}
# 看场合：chat 模式关掉，full 模式打开
SITUATIONAL = {
    "tool-ask-user", "tool-todo", "tool-web", "present", "tool-plugin-manager",
}
TOPLEVEL_SITUATIONAL = ["dream"]  # dream 那一组 9 个工具


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s" % (time.strftime("%F %T"), msg) + NL)
    except Exception:
        pass


def build_preset_block(mode):
    txt = open(DEFAULT, encoding="utf-8").read()
    i = txt.find("- id: preset-standard")
    if i < 0:
        raise SystemExit("默认配置里找不到 preset-standard")
    j = txt.find(NL + "- id: ", i + 10)
    block = txt[i:j if j > 0 else len(txt)]

    # 切成条目：head(带缩进) + 其余行
    entries = []
    prefix = []
    cur = None
    for ln in block.splitlines():
        m = re.match(r"^(\s+)- id: ([\w-]+)\s*$", ln)
        if m:
            cur = {"id": m.group(2), "head": ln, "indent": m.group(1), "lines": []}
            entries.append(cur)
        elif cur is not None:
            cur["lines"].append(ln)
        else:
            prefix.append(ln)

    out = list(prefix)
    for e in entries:
        out.append(e["head"])
        body = list(e["lines"])
        had_disabled = [b for b in body if re.match(r"^\s+disabled:", b)]
        body = [b for b in body if not re.match(r"^\s+disabled:", b)]
        prop = e["indent"] + "  "
        want = None
        if e["id"] in ALWAYS_OFF:
            want = "true"
        elif e["id"] in SITUATIONAL:
            want = "false" if mode == "full" else "true"
        # 去掉原有 disabled，按需要补一条（放在 name 之后）
        inserted = False
        for b in body:
            out.append(b)
            if want is not None and not inserted and b.strip().startswith("name:"):
                out.append(prop + "disabled: " + want)
                inserted = True
        if want is None:
            out.extend(had_disabled)
        elif not inserted:
            out.append(prop + "disabled: " + want)
    return NL.join(out)


def main():
    mode = "chat"
    if os.path.exists(REQ):
        try:
            data = json.loads(open(REQ, encoding="utf-8").read())
            mode = "full" if str(data.get("mode", "")).lower() == "full" else "chat"
        except Exception:
            pass
        os.unlink(REQ)
    elif len(sys.argv) > 1:
        mode = "full" if sys.argv[1].lower() == "full" else "chat"

    cfg = open(CFG, encoding="utf-8").read()
    cut = len(cfg)
    for marker in (MARK, "# ============ 关掉 preset 里的危险工具"):
        k = cfg.find(marker)
        if k >= 0:
            cut = min(cut, k)
    head = cfg[:cut].rstrip()

    block = build_preset_block(mode)
    extra = []
    for tid in TOPLEVEL_SITUATIONAL:
        extra.append("- id: " + tid)
        extra.append("  disabled: " + ("false" if mode == "full" else "true"))
    body = NL.join([MARK, "# 当前模式：" + mode + "（由 /opt/dsh/toolmode.py 生成，别手改这段）",
                    block, NL.join(extra)])
    new = head + NL + NL + body + NL

    shutil.copy(CFG, CFG + ".bak-mode-" + str(int(time.time())))
    open(CFG, "w", encoding="utf-8").write(new)

    env = dict(os.environ)
    env["PATH"] = "/opt/node/bin:/usr/local/bin:/usr/bin:/bin"
    env["DSH_HOME"] = "/opt/dsh"
    p = subprocess.run("cd /opt/dsh && dsh --profile web --dump-config",
                       shell=True, capture_output=True, text=True, timeout=150, env=env)
    if p.returncode != 0:
        bak = sorted(glob.glob(CFG + ".bak-mode-*"))[-1]
        shutil.copy(bak, CFG)
        log("mode=%s FAILED, rolled back: %s" % (mode, (p.stderr or "")[:200]))
        print("校验失败，已回滚")
        print((p.stderr or "")[:400])
        return 1

    d = p.stdout
    k = d.find("- id: preset-standard")
    seg = d[k:k + 7000]
    on = []
    for m in re.finditer(r"\s+- id: ([\w-]+)\s*$", seg, re.M):
        on.append(m.group(1))
    json.dump({"mode": mode, "at": int(time.time())}, open(STATE, "w", encoding="utf-8"))
    log("mode=%s applied ok" % mode)
    print("模式已切到 %s，preset 挂载 %d 项" % (mode, len(on)))
    subprocess.run("systemctl restart dsh-onebot", shell=True, timeout=120)
    return 0


if __name__ == "__main__":
    sys.exit(main())