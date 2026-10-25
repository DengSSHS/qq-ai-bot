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
  5. cq.js       未知表情不再兜底成笑脸
  6. inject.js + bridge.js  注入事件带 dry_run 时只拦这一轮出站（测试不打扰主人）
  7. inbound.js + prompt.js 三层记忆接线：每轮检索大记忆库 + 注入临时层摘要
  8. bridge.js  气泡上限 3→6（长回复保持分散，不再合并成长条）

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

MEM_HINT_CONST = """
// Local patch (MEM_HINT): 问到过去的事时，提醒她先查记忆再回答。
const MEM_HINT = '（提醒：这条在问过去的事、或你自己经历过的事。先调 diary_recent '
    + '或 diary_search 查一下再答，别凭印象编；查不到就直说没记下来。）';
const MEM_TRIGGER = /(昨天|前天|上次|之前|记得|日记|笔记|你(今天|昨天|前天|之前)|看了什么|刷到|你说过)/;
"""

MEM_HINT_WIRE = """        // Local patch (MEM_HINT): 命中回忆触发词时提醒她查日记/笔记
        if (MEM_TRIGGER.test(final)) {
            final = final + String.fromCharCode(10) + MEM_HINT;
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
    s = s.replace(old, TIME_SUFFIX, 1)

    # 静音两条系统播报：排队提示 / 插话提示。
    # 连续对话时它们会把聊天刷满「⏳ 已排队」「✅ 已提交」这种机器套话。
    if "no-queue-notice" not in s:
        o1 = "        if (!this.queueNoticeChats.has(chatId)) {"
        n1 = ("        // local patch (no-queue-notice): 不再播报「已排队」\n"
              "        if (false && !this.queueNoticeChats.has(chatId)) {")
        if o1 in s:
            s = s.replace(o1, n1, 1)
    if "no-steer-notice" not in s:
        o2 = "            this.sendToChat(chatId, STEER_ACCEPTED_NOTICE).catch((error) => {"
        n2 = ("            // local patch (no-steer-notice): 不播报「已提交补充指令」\n"
              "            if (false) this.sendToChat(chatId, STEER_ACCEPTED_NOTICE).catch((error) => {")
        if o2 in s:
            s = s.replace(o2, n2, 1)

    wr(p, s)
    return "已打"


def patch_inject(apply_):
    """注入事件改标成「系统·内部提醒」，不要伪装成主人发的消息。"""
    p = os.path.join(LIB, "inject.js")
    s = rd(p)
    if "RELABEL_AS_SYSTEM" in s:
        return "已打"
    old = """    async consume(event) {
        const round = { traceId: newTraceId(), intercepted: 0 };"""
    new = """    async consume(event) {
        // local patch (RELABEL_AS_SYSTEM): 注入事件是系统内部提醒，不是主人说的话。
        try {
            const ev = event;
            if (ev && ev.post_type === 'message') {
                const tag = '[内部提醒·非用户消息] ';
                if (typeof ev.raw_message === 'string') ev.raw_message = tag + ev.raw_message;
                if (Array.isArray(ev.message)) {
                    for (const seg of ev.message) {
                        if (seg && seg.type === 'text' && seg.data && typeof seg.data.text === 'string') {
                            seg.data.text = tag + seg.data.text;
                            break;
                        }
                    }
                }
                if (ev.sender && typeof ev.sender === 'object') ev.sender.nickname = '系统·内部提醒';
            }
        }
        catch (e) { void e; }
        const round = { traceId: newTraceId(), intercepted: 0 };"""
    if old not in s:
        return "跳过（找不到 consume 插入点）"
    if not apply_:
        return "未打"
    wr(p, s.replace(old, new, 1))
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
    s = s.replace(imp, imp + chr(10) + chr(10) + CASUAL_CONST + chr(10) + STICKER_NUDGE_CONST + chr(10) + MEM_HINT_CONST, 1)
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
        s = s.replace(disp, MEM_HINT_WIRE + chr(10) + STICKER_NUDGE_WIRE + chr(10) + disp, 1)
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

def patch_inject_per_event_dry(apply_):
    """注入可「按条」dry-run：测试消息走完整管线，但一条都不发到 QQ。

    为什么需要：injectDryRun 是全局开关，可它同时是「主动消息投递」的唯一通道。
    打开它 → 主动消息发不出去；关掉它 → 测试注入的消息会真的发到主人 QQ
    （2026-10-26 实测：测试脚本注入 5 条假提问，她一条条回，全发出去了）。

    做法（三件小事，都不动原有分支）：
      1. 注入事件可带 "dry_run": true —— 消费时开一个 120 秒的拦截窗口；
         异步最终回复拿不到 ALS round，所以必须靠时间窗兜住。
      2. 拦截时往日志写一行原文：`[注入拦截] send_msg（dry_run 时间窗） :: 她本来说的话`。
         否则只能去面板看 trace，排障抓瞎。
      3. 注入事件可带 "as_user": true —— 不改成「系统·内部提醒」。
         默认要把注入标成内部提醒（免得她以为主人在说话），
         但本地测试时需要以主人身份进管线，否则她一律回「不接这条」。
         出站已经被拦，QQ 收不到，风险只落在上下文里。
    """
    inj = os.path.join(LIB, "inject.js")
    br = os.path.join(LIB, "bridge.js")
    si = rd(inj)
    sb = rd(br)
    # 功能级判断：这几处都在就算打过了（线上可能被手工微调过，别用精确文本比）
    if ("dryWindowUntil" in si and "inDryWindow" in si and "as_user" in si
            and "[注入拦截]" in si and "真实消息进来了" in si and "PER_EVENT_DRY" in sb):
        return "已打"
    todo = []

    def sub(text, old, new, tag, where):
        if new in text:
            return text
        if old not in text:
            todo.append(tag + "（锚点没找到）")
            return text
        todo.append(tag)
        return text.replace(old, new, 1)

    # 1) 字段
    si = sub(si,
             "    stats = { consumed: 0, skippedHistory: 0, intercepted: 0, parseErrors: 0, readFailures: 0 };",
             "    stats = { consumed: 0, skippedHistory: 0, intercepted: 0, parseErrors: 0, readFailures: 0 };"
             + chr(10)
             + "    // local patch (PER_EVENT_DRY): 自带 dry_run 的注入开一个短窗口，窗口内该会话出站全拦"
             + chr(10)
             + "    dryWindowUntil = 0;"
             + chr(10)
             + "    dryWindowChats = new Set();",
             "字段", "inject.js")

    # 2) shouldIntercept + inDryWindow
    si = sub(si,
             "    get intercepting() {"
             + chr(10) + "        return this.dryRun;"
             + chr(10) + "    }",
             "    get intercepting() {"
             + chr(10) + "        return this.dryRun;"
             + chr(10) + "    }"
             + chr(10) + "    /** local patch (PER_EVENT_DRY): 这一轮要不要拦（event 级 dry_run 优先）。 */"
             + chr(10) + "    shouldIntercept(round) {"
             + chr(10) + "        if (round !== undefined && round.dry === true)"
             + chr(10) + "            return true;"
             + chr(10) + "        return this.dryRun;"
             + chr(10) + "    }"
             + chr(10) + "    /** local patch (PER_EVENT_DRY): 是否处在「刚有 dry_run 注入」的拦截窗口内。 */"
             + chr(10) + "    inDryWindow(chatId) {"
             + chr(10) + "        if (!(this.dryWindowUntil > Date.now()))"
             + chr(10) + "            return false;"
             + chr(10) + "        if (this.dryWindowChats.size === 0)"
             + chr(10) + "            return true;"
             + chr(10) + "        return chatId === undefined || this.dryWindowChats.has(chatId);"
             + chr(10) + "    }",
             "shouldIntercept/inDryWindow", "inject.js")

    # 3) round 带 dry + 开窗
    si = sub(si,
             "        const round = { traceId: newTraceId(), intercepted: 0 };",
             "        const round = { traceId: newTraceId(), intercepted: 0,"
             + chr(10) + "            dry: Boolean(event && (event.dry_run === true || event.dryRun === true)) };"
             + chr(10) + "        if (round.dry) {"
             + chr(10) + "            this.dryWindowUntil = Date.now() + 120000;"
             + chr(10) + "            const ev0 = event || {};"
             + chr(10) + "            const cid0 = ev0.message_type === 'group'"
             + chr(10) + "                ? ('group:' + (ev0.group_id === undefined ? '' : ev0.group_id))"
             + chr(10) + "                : ('private:' + (ev0.user_id === undefined ? '' : ev0.user_id));"
             + chr(10) + "            this.dryWindowChats.add(cid0);"
             + chr(10) + "        }",
             "round.dry + 开窗", "inject.js")

    # 4) as_user
    si = sub(si,
             "            if (ev && (ev.post_type === 'message')) {",
             "            // local patch (PER_EVENT_DRY): as_user=true 时不改标（本地测试用）"
             + chr(10) + "            if (ev && (ev.post_type === 'message') && ev.as_user !== true) {",
             "as_user", "inject.js")

    # 5) 拦截日志（带原文）
    si = sub(si,
             "    intercept(round, action, params) {"
             + chr(10) + "        this.stats.intercepted += 1;",
             "    intercept(round, action, params) {"
             + chr(10) + "        // local patch (PER_EVENT_DRY): 日志写原文，方便验证「测试没发到 QQ」"
             + chr(10) + "        try {"
             + chr(10) + "            const why = round !== undefined && round.dry === true ? 'per-event dry_run'"
             + chr(10) + "                : (round === undefined ? 'dry_run 时间窗' : '全局 dry-run');"
             + chr(10) + "            let txt = '';"
             + chr(10) + "            try { txt = String(outboundTextOf(params) || '').replace(/\\s+/g, ' ').slice(0, 300); }"
             + chr(10) + "            catch (e) { void e; }"
             + chr(10) + "            this.opts.log('info', '[注入拦截] ' + action + '（' + why + '） :: ' + txt);"
             + chr(10) + "        }"
             + chr(10) + "        catch (e) { void e; }"
             + chr(10) + "        this.stats.intercepted += 1;",
             "拦截日志", "inject.js")

    # 6) bridge 闸门：非全局 dry-run 时按条/按窗拦截
    sb = sub(sb,
             "        if (inject === undefined || !inject.intercepting || !isWriteAction(action)) {"
             + chr(10) + "            return this.deps.connection.call(action, params);"
             + chr(10) + "        }"
             + chr(10) + "        const alsRound = currentInjectRound();",
             "        if (inject === undefined || !isWriteAction(action)) {"
             + chr(10) + "            return this.deps.connection.call(action, params);"
             + chr(10) + "        }"
             + chr(10) + "        const alsRound = currentInjectRound();"
             + chr(10) + "        // local patch (PER_EVENT_DRY): 全局 dry-run 没开时，单条注入仍可自带 dry_run"
             + chr(10) + "        if (!inject.intercepting) {"
             + chr(10) + "            if (alsRound !== undefined && inject.shouldIntercept(alsRound))"
             + chr(10) + "                return inject.intercept(alsRound, action, params);"
             + chr(10) + "            if (inject.inDryWindow(chatId))"
             + chr(10) + "                return inject.intercept(undefined, action, params);"
             + chr(10) + "            return this.deps.connection.call(action, params);"
             + chr(10) + "        }",
             "bridge 闸门", "bridge.js")

    # 7) 真实消息进来就关窗，免得测试期间把主人的对话也拦掉
    si = sub(si,
             "    noteDispatch(chatId) {"
             + chr(10) + "        const round = currentInjectRound();"
             + chr(10) + "        if (round !== undefined)"
             + chr(10) + "            this.activeRounds.set(chatId, round);"
             + chr(10) + "        else"
             + chr(10) + "            this.activeRounds.delete(chatId);"
             + chr(10) + "    }",
             "    noteDispatch(chatId) {"
             + chr(10) + "        const round = currentInjectRound();"
             + chr(10) + "        if (round !== undefined)"
             + chr(10) + "            this.activeRounds.set(chatId, round);"
             + chr(10) + "        else {"
             + chr(10) + "            this.activeRounds.delete(chatId);"
             + chr(10) + "            // local patch (PER_EVENT_DRY): 真实消息进来了 → 立刻关掉测试拦截窗口"
             + chr(10) + "            this.dryWindowChats.delete(chatId);"
             + chr(10) + "            if (this.dryWindowChats.size === 0)"
             + chr(10) + "                this.dryWindowUntil = 0;"
             + chr(10) + "        }"
             + chr(10) + "    }",
             "真实消息关窗", "inject.js")

    missing = [t for t in todo if "锚点" in t]
    if missing:
        return "跳过（%s）" % "、".join(missing)
    if not todo:
        return "已打"
    if not apply_:
        return "未打"
    wr(inj, si)
    wr(br, sb)
    return "已打"


def patch_mem_layers(apply_):
    """三层记忆接线：每轮真实检索 + 临时层摘要注入。

    2026-10-26 之前：MEM_HINT 只在命中触发词时提示她「你可以去查日记」，
    实测她几乎不查（问过去的事全靠印象编）。这里把"提醒"换成"直接捞给她"，
    并把临时层的滚动摘要（mem_layers.py 写的 session_summary.md）每轮注入。
    """
    ib = os.path.join(LIB, "inbound.js")
    pr = os.path.join(LIB, "prompt.js")
    si = rd(ib)
    sp = rd(pr)
    todo = []

    def sub(text, old, new, tag):
        if new in text:
            return text
        if old not in text:
            todo.append(tag + "（锚点没找到）")
            return text
        todo.append(tag)
        return text.replace(old, new, 1)

    # 1) inbound.js 需要 child_process
    si = sub(si,
             "import { writeFile } from 'node:fs/promises';",
             "import { writeFile } from 'node:fs/promises';"
             + chr(10) + "import { execFileSync } from 'node:child_process';",
             "child_process 导入")

    # 2) 把「提醒她查」换成「直接检索并附上」
    si = sub(si,
             "        if (MEM_TRIGGER.test(final)) {"
             + chr(10) + "            final = final + String.fromCharCode(10) + MEM_HINT;"
             + chr(10) + "        }",
             "        // local patch (MEM_LAYERS): 不再只提醒「你可以去查」，直接把相关的几条捞出来"
             + chr(10) + "        let recallBlock = '';"
             + chr(10) + "        if (typeof final === 'string' && final.length >= 4) {"
             + chr(10) + "            try {"
             + chr(10) + "                recallBlock = execFileSync('/usr/bin/python3',"
             + chr(10) + "                    ['/opt/dsh/mem_layers.py', 'recall', '--text', final.slice(0, 200)],"
             + chr(10) + "                    { encoding: 'utf8', timeout: 2500 }).trim();"
             + chr(10) + "            }"
             + chr(10) + "            catch (e) { recallBlock = ''; }"
             + chr(10) + "        }"
             + chr(10) + "        if (recallBlock) {"
             + chr(10) + "            final = final + String.fromCharCode(10) + recallBlock;"
             + chr(10) + "        }"
             + chr(10) + "        else if (MEM_TRIGGER.test(final)) {"
             + chr(10) + "            final = final + String.fromCharCode(10) + MEM_HINT;"
             + chr(10) + "        }",
             "recall 注入")

    # 3) prompt.js 注入临时层摘要
    sp = sub(sp,
             "    const mem = readAgentsMemory();",
             "    // local patch (MEM_LAYERS): 临时层滚动摘要（长会话被压缩后靠它接着聊）"
             + chr(10) + "    try {"
             + chr(10) + "        const sp = join(process.env.DSH_HOME || '/opt/dsh', 'memory', 'session_summary.md');"
             + chr(10) + "        let sm = readFileSync(sp, 'utf8').trim();"
             + chr(10) + "        if (sm.length > 1200)"
             + chr(10) + "            sm = sm.slice(0, 1200) + '…';"
             + chr(10) + "        if (sm)"
             + chr(10) + "            platform += String.fromCharCode(10) + '[上一段对话的摘要·接着聊，不要当新话题]'"
             + chr(10) + "                + String.fromCharCode(10) + sm + String.fromCharCode(10);"
             + chr(10) + "    }"
             + chr(10) + "    catch (e) { void e; }"
             + chr(10) + "    const mem = readAgentsMemory();",
             "摘要注入")

    missing = [t for t in todo if "锚点" in t]
    if missing:
        return "跳过（%s）" % "、".join(missing)
    if not todo:
        return "已打"
    if not apply_:
        return "未打"
    wr(ib, si)
    wr(pr, sp)
    return "已打"


def patch_bubble_spread(apply_):
    """长回复别压缩成长条：气泡上限 3 → 6，正常长度一句话一个气泡。

    原来超过 3 句就合并回 3 条，读起来断不开（用户反馈「合并并不完整，还是分散吧」）。
    """
    p = os.path.join(LIB, "bridge.js")
    s = rd(p)
    if "parts.length > 6" in s:
        return "已打"
    old = """        if (parts.length > 3) {
            const per = Math.ceil(parts.length / 3);"""
    new = """        // local patch (BUBBLE_SPREAD): 上限 3 → 6，超出的才合并
        if (parts.length > 6) {
            const per = Math.ceil(parts.length / 6);"""
    if old not in s:
        return "跳过（找不到分句上限）"
    if not apply_:
        return "未打"
    wr(p, s.replace(old, new, 1))
    return "已打"


PATCHES = [
    ("prompt.js  长期记忆注入", patch_prompt),
    ("bridge.js  时间后缀 + 静音系统播报", patch_bridge),
    ("inject.js  注入事件改标成系统提醒", patch_inject),
    ("inbound.js 主动接话概率门", patch_inbound),
    ("index.js   配置 schema + 透传", patch_index),
    ("cq.js      未知表情兜底成 [表情]", patch_cq),
    ("inject+bridge 注入按条 dry_run（测试不打扰主人）",
     patch_inject_per_event_dry),
    ("mem_layers 三层记忆接线（每轮检索 + 摘要注入）", patch_mem_layers),
    ("bridge.js 气泡上限 3→6（长回复保持分散）", patch_bubble_spread),
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
