#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地对 dsh-onebot 插件的改动重新打上（插件升级后会丢）。

为什么要这个脚本：这些改动都落在 node_modules 里，`npm install` 或插件升级
一覆盖就没了，而且丢了不会报错——只会表现为"某个功能悄悄不工作"。
（实测：看图端点就因此坏了一整天，没有任何日志提示。）

四处补丁，各自独立、幂等（已打过就跳过）：

  1. prompt.js   readAgentsMemory()：每轮注入 workspace/AGENTS.md 的长期记忆
  2. bridge.js   时间上下文从系统提示前缀挪到后缀（否则前缀缓存永远命中 0）
  3. inbound.js  主动接话概率门 + 主动插话提示
  4. index.js    上面两个配置键的 schema 与 gateConfig 透传

用法（在服务器上，以 root）：
    python3 apply_local_patches.py            # 打补丁
    python3 apply_local_patches.py --check    # 只检查打了哪些
"""
import io
import os
import subprocess
import sys

BASE = "/opt/dsh"
LIB = os.path.join(BASE, "profiles/web/node_modules/dsh-onebot-qq/lib")


def rd(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def wr(p, s):
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(s)


# ---------------------------------------------------------------- 1) prompt.js
MEM_FUNC = '''/**
 * 工作区长期记忆（workspace/AGENTS.md）。
 *
 * dsh 的 agent-instructions 插件在这个 profile 里是 disabled，所以 AGENTS.md
 * 本来只能靠她主动 read 才看得到（实测 21956 行会话里只出现 8 次）。
 * 记忆没有注入通道就等于没写，所以在最可靠的通道里补上。
 */
function agentsMemoryPath() {
    const home = process.env.DSH_HOME || '';
    if (!home)
        return null;
    return join(home, 'workspace', 'AGENTS.md');
}
function readAgentsMemory(maxChars = 6000) {
    const NL = String.fromCharCode(10);
    try {
        const p = agentsMemoryPath();
        if (!p)
            return '';
        let text = readFileSync(p, 'utf8').trim();
        if (!text)
            return '';
        if (text.length > maxChars)
            text = text.slice(0, maxChars) + NL + '（后面还有，太长省略了）';
        return ('## 长期记忆（workspace/AGENTS.md）' + NL
            + '下面是你从过去对话里学到的东西。标了「群/私聊」的只在那里用，标「通用」的到处能用；'
            + '（单次）表示只观察到一次，别当定论。把这些当背景知识，不要原文念出来。' + NL
            + text);
    }
    catch {
        return '';
    }
}
'''

MEM_WIRE = '''    const mem = readAgentsMemory();
    if (mem) {
        platform += String.fromCharCode(10) + mem + String.fromCharCode(10);
    }
'''

# ---------------------------------------------------------------- 2) bridge.js
TIME_SUFFIX = """                // 时间放到末尾：它每轮都变（精确到秒），放最前面会让后面
                // 12KB 系统提示 + 11KB 工具定义全部失去前缀缓存。
                return parts.complete + '\\n\\n' + header;"""

# ---------------------------------------------------------------- 3) inbound.js
CASUAL_CONST = '''// 主动接话标记：模型据此知道这一轮没人叫她，是自发插话。
const CASUAL_ENGAGE_HINT = '[主动插话] 这条没人 @ 你，是你自己看到想接一句。'
    + '先看上面最近的群聊内容（不够就调 qq_group_history 拉最近 10 条），'
    + '再简短接一句就好，别追问、别展开话题、别把话头接成长对话。' + String.fromCharCode(10);
'''

CASUAL_GATE = """        // 主动接话：每条未 @ 她的群消息都有一个小概率被她"接住"。
        // 只算这一条，不写进 groupActiveUsers —— 接完就结束，下次还得 @。
        // 每小时配额 + 均匀分布：把一小时切成 maxPerHour 个时间格，每格最多一次，
        // 所以既不会一小时刷十几次，也不会在开头几分钟把配额用光。
        let casualEngage = false;
        if (kind === 'group' && this.ctx.config.requireMention && !mentioned && !activationActive) {
            const rate = Number(this.ctx.config.casualEngageRate ?? 0);
            const cooldown = Number(this.ctx.config.casualEngageCooldownMs ?? 0);
            const maxPerHour = Math.max(1, Number(this.ctx.config.casualEngageMaxPerHour ?? 3));
            const last = this.casualEngageAt?.get(chatId) ?? 0;
            const cooling = cooldown > 0 && Date.now() - last < cooldown;
            const now = Date.now();
            const hourIdx = Math.floor(now / 3600000);
            const slotMs = Math.floor(3600000 / maxPerHour);
            const slotIdx = Math.floor(now / slotMs);
            if (!this.casualQuota)
                this.casualQuota = new Map();
            let q = this.casualQuota.get(chatId);
            if (!q || q.hour !== hourIdx) {
                q = { hour: hourIdx, used: new Set() };
                this.casualQuota.set(chatId, q);
            }
            if (q.used.size > maxPerHour * 4)
                q.used.clear();
            const slotFree = !q.used.has(slotIdx);
            if (rate > 0 && !cooling && slotFree && Math.random() < rate) {
                casualEngage = true;
                q.used.add(slotIdx);
                this.casualEngageAt.set(chatId, now);
                currentTrace()?.emit('mention', { ok: true,
                    reason: '主动接话（第 ' + q.used.size + '/' + maxPerHour + ' 格）', chatId });
            }
            else {
                this.ctx.log('debug', 'ignoring unmentioned group message in ' + groupId
                    + (slotFree ? '' : '（本格配额已用）'));
                currentTrace()?.emit('mention', { ok: false, reason: TRACE_REASONS.inboundUnmentioned });
                return;
            }
        }"""

CASUAL_HINT_WIRE = """            // 主动接话轮：这条不是你被叫到的，是你自己凑过去的。
            if (casualEngage) {
                final = CASUAL_ENGAGE_HINT + final;
            }"""

STICKER_NUDGE_CONST = """
// Local patch: 表情提醒。把她"要不要配张表情"的念头每轮递到眼前——
// 模型不会自发去调工具掷骰，光在人设里写一句等于没写。
const STICKER_NUDGE = '（顺手想一下：这条要不要配张表情？三成左右该配一张。'
    + '拿不准就配——qq_random_sticker() 一次调用就能发出去。）';
"""

STICKER_NUDGE_WIRE = """        // Local patch: 按概率附一句表情提醒（她自己决定用不用）
        const nudgeRate = Number(this.ctx.config.stickerNudgeRate ?? 0);
        if (nudgeRate > 0 && Math.random() < nudgeRate) {
            final = final + String.fromCharCode(10) + STICKER_NUDGE;
        }"""

# ---------------------------------------------------------------- 4) index.js
SCHEMA_KEYS = """    casualEngageRate: z.number().min(0).max(1).default(0)
        .description('主动接话概率：未 @ 她的群消息中按此概率被接住并回一句。只算当前这一条，'
            + '不写入激活窗口（接完就结束，下次仍需 @）；0 = 关闭'),
    casualEngageCooldownMs: z.number().default(60000)
        .description('同一群两次主动接话的最小间隔（毫秒）'),
    stickerNudgeRate: z.number().min(0).max(1).default(0)
        .description('表情提醒出现概率：每轮在消息尾部附一句极短的「要不要配张表情」，'
            + '由她自己决定用不用；0 = 关闭'),
    casualEngageMaxPerHour: z.number().min(1).default(3)
        .description('主动接话每小时上限：把一小时切成这么多时间格，每格最多接一次'),
"""

GATE_KEYS = """        casualEngageRate: config.casualEngageRate,
        casualEngageCooldownMs: config.casualEngageCooldownMs,
        casualEngageMaxPerHour: config.casualEngageMaxPerHour,
        stickerNudgeRate: config.stickerNudgeRate,
"""


def patch_prompt(apply_):
    p = os.path.join(LIB, "prompt.js")
    s = rd(p)
    if "readAgentsMemory" in s:
        return "已打"
    if "const persona = readPersona();" not in s:
        return "跳过（结构已变，需人工处理）"
    if not apply_:
        return "未打"
    anchor = "/** Assemble the three prompt parts contributed by this channel. */"
    if anchor not in s:
        return "跳过（找不到插入点）"
    s = s.replace(anchor, MEM_FUNC + anchor, 1)
    s = s.replace("    const persona = readPersona();",
                  MEM_WIRE + "    const persona = readPersona();", 1)
    wr(p, s)
    return "已打"


def patch_bridge(apply_):
    p = os.path.join(LIB, "bridge.js")
    s = rd(p)
    if "header 放到末尾" in s or "parts.complete + " in s:
        return "已打"
    old = "                return header + parts.complete;"
    if old not in s:
        return "跳过（找不到目标行）"
    if not apply_:
        return "未打"
    wr(p, s.replace(old, TIME_SUFFIX, 1))
    return "已打"


def patch_inbound(apply_):
    p = os.path.join(LIB, "inbound.js")
    s = rd(p)
    if "casualEngageRate" in s:
        return "已打"
    old_gate = """        if (kind === 'group' && this.ctx.config.requireMention && !mentioned && !activationActive) {
            this.ctx.log('debug', 'ignoring unmentioned group message in ' + groupId);
            currentTrace()?.emit('mention', { ok: false, reason: TRACE_REASONS.inboundUnmentioned });
            return;
        }"""
    imp = "import { currentTrace, TRACE_REASONS } from './trace.js';"
    if old_gate not in s or imp not in s:
        return "跳过（找不到插入点）"
    if not apply_:
        return "未打"
    s = s.replace(imp, imp + chr(10) + chr(10) + CASUAL_CONST + chr(10) + STICKER_NUDGE_CONST, 1)
    s = s.replace(old_gate, CASUAL_GATE, 1)
    # 概率门要用到两个 Map，必须声明成类字段（否则 this.casualQuota 是 undefined）
    field_anchor = "    groupActiveUsers = new Map();"
    if field_anchor in s and 'casualQuota = new Map();' not in s:
        s = s.replace(field_anchor, field_anchor + chr(10)
                      + "    // 主动接话状态：上次接话时间 / 本小时已用的时间格（casualQuota 字段）" + chr(10)
                      + "    casualEngageAt = new Map();" + chr(10)
                      + "    casualQuota = new Map();", 1)
    hook = """        if (kind === 'group') {
            final = buildGroupMessagePrefix(nickname, userId, mentioned) + final;"""
    if hook in s:
        s = s.replace(hook, hook + chr(10) + CASUAL_HINT_WIRE, 1)
    # 表情提醒：插在 dispatch 之前，群聊私聊都生效
    disp = "        await this.ctx.dispatchFollowup(chatId, final, role, nickname);"
    if disp in s and 'stickerNudgeRate' not in s:
        s = s.replace(disp, STICKER_NUDGE_WIRE + chr(10) + disp, 1)
    wr(p, s)
    return "已打"


def patch_index(apply_):
    p = os.path.join(LIB, "index.js")
    s = rd(p)
    if "casualEngageRate" in s:
        return "已打"
    a1 = "    groupActivationMinutes: z.number().default(0)"
    a2 = """    const gateConfig = {
        dedupWindowSeconds: config.dedupWindowSeconds,"""
    if a1 not in s or a2 not in s:
        return "跳过（找不到插入点）"
    if not apply_:
        return "未打"
    s = s.replace(a1, SCHEMA_KEYS + a1, 1)
    s = s.replace(a2, """    const gateConfig = {
        // 这两个键必须显式透传：gateConfig 是白名单，不加就进不了 pipeline
""" + GATE_KEYS + "        dedupWindowSeconds: config.dedupWindowSeconds,", 1)
    wr(p, s)
    return "已打"



# ---------------------------------------------------------------- 5) cq.js
FACE_OLD = """export function faceToEmoji(id) {
    return FACE_EMOJI[id] ?? '😀';
}"""

FACE_NEW = """export function faceToEmoji(id) {
    // 未知 id 不要兜底成笑脸：表里只收了两百来个，QQ 还有几百个，
    // 一律映射成 😀 会让她"看到不存在的笑"。宁可老实说认不出。
    return FACE_EMOJI[id] ?? '[表情]';
}"""


def patch_cq(apply_):
    p = os.path.join(LIB, "cq.js")
    s = rd(p)
    if "'[表情]'" in s and 'faceToEmoji' in s:
        return "已打"
    if FACE_OLD not in s:
        return "跳过（找不到目标函数）"
    if not apply_:
        return "未打"
    wr(p, s.replace(FACE_OLD, FACE_NEW, 1))
    return "已打"

PATCHES = [
    ("prompt.js  长期记忆注入", patch_prompt),
    ("bridge.js  时间挪到后缀（前缀缓存）", patch_bridge),
    ("inbound.js 主动接话概率门", patch_inbound),
    ("index.js   配置 schema + 透传", patch_index),
    ("cq.js      未知表情兜底成 [表情]", patch_cq),
]


def main():
    apply_ = "--check" not in sys.argv
    if not os.path.isdir(LIB):
        print("找不到插件目录：%s" % LIB)
        return 2
    print("目标：%s" % LIB)
    changed = 0
    for name, fn in PATCHES:
        r = fn(apply_)
        if r == "已打":
            pass
        elif r.startswith("跳过"):
            print("  [!] %-34s %s" % (name, r))
            continue
        else:
            changed += 1
        print("  [%s] %s" % ("+" if r == "已打" else " ", name) if False else "  %-36s %s" % (name, r))
    # 语法自检
    node = "/opt/node/bin/node"
    if os.path.exists(node):
        for f in ("prompt.js", "bridge.js", "inbound.js", "index.js", "cq.js"):
            r = subprocess.run([node, "--check", os.path.join(LIB, f)],
                               capture_output=True)
            if r.returncode != 0:
                print("  [语法错误] %s: %s" % (f, r.stderr.decode("utf-8", "replace")[:200]))
                return 1
        print("  语法自检通过")
    print("完成%s（%d 项新打上）" % ("（仅检查）" if not apply_ else "", changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
