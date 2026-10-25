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
  9. chat/inbound/bridge/tools/prompt  引用回复 + 表情回应 + 拍一拍（入站+出站）
 10. index.js + inject.js  发送审计（所有出站写记一行，假成功可见）
 11. bridge/events.js     统一事件适配层（notice 不再被丢）
 12. tools.js + inject.js 写操作统一过闸（表情回应/拍一拍/代理）

用法（在服务器上，以 root）：
    python3 apply_local_patches.py            # 打补丁
    python3 apply_local_patches.py --check    # 只检查打了哪些
"""
import io
import os
import shutil
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

    MARKERS = {
        "chat.js message_id": "MSG_ID_ATTR",
        "prompt.js 平台提示": "qq_emoji_like",
    }

    def sub(text, old, new, tag):
        mk = MARKERS.get(tag)
        if new in text or (mk is not None and mk in text):
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
    """气泡条数：默���不限（一句一个气泡），合并逻辑保留为可选闸门。

    历史：原来硬编码「超过 3 句合并回 3 条」，长回复被压成难读的长条。
    2026-10-26 用户明确「我不要限制，不过预留接口」→ 默认 0 = 不限，
    想收敛时改 limits.json 的 bubble_max_private / bubble_max_group（正整数）。
    超出闸门时合并（段间空行），**绝不静默丢弃**。
    """
    p = os.path.join(LIB, "bridge.js")
    s = rd(p)

    # 需要同步读文件
    if "readFileSync" not in s.split("export class")[0]:
        anchor = "import { realpath } from 'node:fs/promises';"
        if anchor not in s:
            return "跳过（找不到 fs import）"
        s = s.replace(anchor, anchor + chr(10) + "import { readFileSync } from 'node:fs';", 1)
        wr(p, s)

    old_a = """        if (parts.length > 3) {
            const per = Math.ceil(parts.length / 3);"""
    old_b = """        // local patch (BUBBLE_SPREAD): 上限 3 → 6，超出的才合并
        if (parts.length > 6) {
            const per = Math.ceil(parts.length / 6);"""
    old_c = """        let maxParts = 4;
        try {"""
    new = """        // local patch (BUBBLE_LIMIT_CFG): 默认 0 = 不限条数（一句一个气泡）。
        // 想收敛就改 limits.json 的 bubble_max_private / bubble_max_group；超出合并，不截断。
        let maxParts = 0;
        try {
            const lim = JSON.parse(readFileSync((process.env.DSH_HOME || '/opt/dsh') + '/limits.json', 'utf8'));
            const isGroup = params && params.group_id !== undefined && params.group_id !== null;
            const v = Number(isGroup ? lim.bubble_max_group : lim.bubble_max_private);
            if (Number.isFinite(v) && v >= 0)
                maxParts = v;
        }
        catch (e) { void e; }
        if (maxParts > 0 && parts.length > maxParts) {
            const per = Math.ceil(parts.length / maxParts);"""

    if "maxParts = 0;" in s and "bubble_max_private" in s:
        return "已打"
    if old_c in s:
        # 旧版配置化（默认 4）→ 改成默认 0
        body_start = s.index(old_c)
        tail_anchor = "        if (maxParts > 1 && parts.length > maxParts) {\n            const per = Math.ceil(parts.length / maxParts);"
        if tail_anchor in s:
            s = s[:body_start] + new + s[s.index(tail_anchor) + len(tail_anchor):]
        else:
            return "跳过（找不到合并闸门）"
    elif old_a in s:
        s = s.replace(old_a + "\n            const merged", new + "\n            const merged", 1)
    elif old_b in s:
        s = s.replace(old_b + "\n            const merged", new + "\n            const merged", 1)
    else:
        return "跳过（找不到分句上限）"
    if not apply_:
        return "未打"
    wr(p, s)
    return "已打"


def patch_reply_poke_emoji(apply_):
    """引用回复 / 表情回应 / 拍一拍（入站 + 出站）。

    1. message_id 进上下文：`<user_message … message_id="…">`。
       不带的话她拿不到 id，qq_reply / qq_emoji_like 全是摆设（实测踩过）。
    2. notice 入口：QQ 的「拍一拍」是 notice（notice_type=notify, sub_type=poke），
       原管线只认 message，这一拍在桥接层就被丢了 → 合成一条入站消息给她回应。
    3. 三个工具：qq_reply（引用回复）、qq_emoji_like（表情回应，零 token）、
       qq_poke（拍一拍，白名单 + 冷却，只对主人/熟人）。
    """
    br = os.path.join(LIB, "bridge.js")
    ch = os.path.join(LIB, "chat.js")
    ib = os.path.join(LIB, "inbound.js")
    tl = os.path.join(LIB, "tools.js")
    pr = os.path.join(LIB, "prompt.js")

    # 五处功能标记都在 = 已经打过了。线上文件可能被手工微调过，
    # 逐字比对会误判成「未打」从而重复插入（踩过一次），所以先用标记兜一层。
    have_bridge = ("POKE_IN" in rd(br)) or ("EVENT_ADAPTER" in rd(br))
    if ("MSG_ID_ATTR" in rd(ch) and "inbound.messageId" in rd(ib)
            and have_bridge and "QQ_REPLY_POKE_EMOJI" in rd(tl)
            and "qq_emoji_like" in rd(pr)):
        return "已打"

    MARK9 = {
        "chat.js message_id": "MSG_ID_ATTR",
        "prompt.js 平台提示": "qq_emoji_like",
        "bridge 拍一拍": "handlePokeNotice(event) {",
        "bridge notice 入口": "POKE_IN",
        "bridge 拍一拍": "POKE_IN",
        "tools.js 三个工具": "QQ_REPLY_POKE_EMOJI",
    }
    todo = []

    def sub(text, old, new, tag):
        mk = MARK9.get(tag)
        if new in text or (mk is not None and mk in text):
            return text
        # 事件适配层已经接管入站入口时，不再插旧的 poke 钩子
        if tag in ("bridge notice 入口", "bridge 拍一拍") and "EVENT_ADAPTER" in text:
            return text
        if old not in text:
            todo.append(tag + "（锚点没找到）")
            return text
        todo.append(tag)
        return text.replace(old, new, 1)

    # 两处线上文本可能被手工微调过，用功能标记判断「已打」
    MARK9 = {
        "chat.js message_id": "MSG_ID_ATTR",
        "prompt.js 平台提示": "qq_emoji_like",
        "bridge 拍一拍": "handlePokeNotice(event) {",
        "tools.js 三个工具": "QQ_REPLY_POKE_EMOJI",
    }
    _sub9 = sub

    def sub(text, old, new, tag):
        mk = MARK9.get(tag)
        if mk is not None and mk in text:
            return text
        return _sub9(text, old, new, tag)

    # ---- 1) message_id 进上下文 ----
    sc = rd(ch)
    if "MSG_ID_ATTR" in sc:
        todo.append("chat.js message_id")
    sc = sub(sc,
             "export function wrapUserMessage(content, userId, nickname) {"
             + chr(10) + "    return '<user_message qq=\"' + userId + '\" nickname=\"' + sanitizeNickname(nickname) + '\">"
             + chr(10) + "' + content + '" + chr(10) + "</user_message>';",
             "export function wrapUserMessage(content, userId, nickname, messageId) {"
             + chr(10) + "    // local patch (MSG_ID_ATTR): message_id 要带进上下文，"
             + chr(10) + "    // 否则 qq_reply / qq_emoji_like 没有目标可用。"
             + chr(10) + "    const mid = messageId === undefined || messageId === null || messageId === ''"
             + chr(10) + "        ? ''"
             + chr(10) + "        : ' message_id=\"' + String(messageId).replace(/[^0-9A-Za-z_-]/g, '') + '\"';"
             + chr(10) + "    return '<user_message qq=\"' + userId + '\" nickname=\"' + sanitizeNickname(nickname) + '\"' + mid + '>"
             + chr(10) + "' + content + '" + chr(10) + "</user_message>';",
             "chat.js message_id")
    si = rd(ib)
    si = sub(si,
             "        let final = wrapUserMessage(content, userId, nickname);",
             "        let final = wrapUserMessage(content, userId, nickname, inbound.messageId);",
             "inbound.js 传 messageId")

    # ---- 2) notice 入口 + 拍一拍 ----
    sb = rd(br)
    sb = sub(sb,
             "                const inbound = normalizeOneBot11(event);",
             "                // local patch (POKE_IN): 拍一拍是 notice 事件，原逻辑在这里被丢弃"
             + chr(10) + "                if (event && event.post_type === 'notice' && await this.handlePokeNotice(event)) {"
             + chr(10) + "                    return;"
             + chr(10) + "                }"
             + chr(10) + "                const inbound = normalizeOneBot11(event);",
             "bridge notice 入口")
    sb = sub(sb,
             "    /** Expand a quoted (reply) message into [引用] text via get_msg. */",
             "    /**"
             + chr(10) + "     * local patch (POKE_IN): 有人拍了拍她 → 合成一条入站消息，交给她回应。"
             + chr(10) + "     * QQ 的「拍一拍」是 notice 事件（notice_type='notify', sub_type='poke'），"
             + chr(10) + "     * 原管线只认 message，所以这一拍会被丢掉。只处理\"拍的是她\"；同人 3 秒内连拍算一次。"
             + chr(10) + "     */"
             + chr(10) + "    async handlePokeNotice(event) {"
             + chr(10) + "        const sub = String(event.notice_type === 'notify' ? event.sub_type : event.notice_type);"
             + chr(10) + "        if (sub !== 'poke')"
             + chr(10) + "            return false;"
             + chr(10) + "        const selfId = String(event.self_id ?? '');"
             + chr(10) + "        const target = String(event.target_id ?? '');"
             + chr(10) + "        if (selfId === '' || (target !== '' && target !== selfId))"
             + chr(10) + "            return false;"
             + chr(10) + "        const now = Date.now();"
             + chr(10) + "        this.pokeSeen = this.pokeSeen ?? new Map();"
             + chr(10) + "        const who = String(event.user_id ?? '');"
             + chr(10) + "        const isGroup = event.group_id !== undefined && event.group_id !== null;"
             + chr(10) + "        const key = (isGroup ? 'group:' + String(event.group_id) : 'private:') + who;"
             + chr(10) + "        if (now - (this.pokeSeen.get(key) ?? 0) < 3000)"
             + chr(10) + "            return true;"
             + chr(10) + "        this.pokeSeen.set(key, now);"
             + chr(10) + "        const sender = (event.sender ?? {});"
             + chr(10) + "        const label = (typeof sender.card === 'string' && sender.card !== '' ? sender.card"
             + chr(10) + "            : typeof sender.nickname === 'string' && sender.nickname !== '' ? sender.nickname : who);"
             + chr(10) + "        const text = '[拍一拍] ' + label + ' 拍了拍你（这是 QQ 的「拍一拍」动作，不是发消息）';"
             + chr(10) + "        const synthetic = {"
             + chr(10) + "            post_type: 'message',"
             + chr(10) + "            message_type: isGroup ? 'group' : 'private',"
             + chr(10) + "            sub_type: isGroup ? 'normal' : 'friend',"
             + chr(10) + "            self_id: event.self_id,"
             + chr(10) + "            user_id: event.user_id,"
             + chr(10) + "            ...(isGroup ? { group_id: event.group_id } : {}),"
             + chr(10) + "            message_id: now,"
             + chr(10) + "            time: event.time !== undefined ? event.time : Math.floor(now / 1000),"
             + chr(10) + "            raw_message: text,"
             + chr(10) + "            message: [{ type: 'text', data: { text } }],"
             + chr(10) + "            sender: event.sender !== undefined ? event.sender : { user_id: event.user_id, nickname: label },"
             + chr(10) + "        };"
             + chr(10) + "        try {"
             + chr(10) + "            this.deps.log('info', '[poke] 转成入站消息：' + text);"
             + chr(10) + "            await this.handleInbound(synthetic);"
             + chr(10) + "        }"
             + chr(10) + "        catch (error) {"
             + chr(10) + "            this.deps.log('warn', '拍一拍转入站失败: ' + describeError(error));"
             + chr(10) + "        }"
             + chr(10) + "        return true;"
             + chr(10) + "    }"
             + chr(10) + "    /** Expand a quoted (reply) message into [引用] text via get_msg. */",
             "bridge 拍一拍")

    # ---- 3) 三个工具 ----
    st = rd(tl)
    tool_block = (
        "    // ── local patch (QQ_REPLY_POKE_EMOJI): 引用回复 / 表情回应 / 拍一拍 ──"
        + chr(10) + "    const _cool = new Map();"
        + chr(10) + "    const _readLimits = () => {"
        + chr(10) + "        try { return JSON.parse(readFileSync((process.env.DSH_HOME || '/opt/dsh') + '/limits.json', 'utf8')); }"
        + chr(10) + "        catch (e) { return {}; }"
        + chr(10) + "    };"
        + chr(10) + "    const _tooSoon = (key, ms) => {"
        + chr(10) + "        const now = Date.now();"
        + chr(10) + "        const last = _cool.get(key) ?? 0;"
        + chr(10) + "        if (now - last < ms)"
        + chr(10) + "            return Math.ceil((ms - (now - last)) / 1000);"
        + chr(10) + "        _cool.set(key, now);"
        + chr(10) + "        return 0;"
        + chr(10) + "    };"
        + chr(10) + chr(10)
        + "    register(defineTool({"
        + chr(10) + "        name: 'qq_reply',"
        + chr(10) + "        description: '引用某条消息再回（QQ 的回复引用块）。想明确回应某一句话时用它，比直接发文字更像人。',"
        + chr(10) + "        parameters: {"
        + chr(10) + "            message_id: { type: 'string', description: '被引用的消息 id' },"
        + chr(10) + "            text: { type: 'string', description: '要说的话' },"
        + chr(10) + "            chat_id: { type: 'string', description: '目标会话；省略时默认当前会话' },"
        + chr(10) + "        },"
        + chr(10) + "        output: sendOutput(),"
        + chr(10) + "        isConcurrencySafe: () => true,"
        + chr(10) + "        execute: async (args, exec) => {"
        + chr(10) + "            const a = args;"
        + chr(10) + "            const chatId = resolveChat(bridge, exec, a.chat_id);"
        + chr(10) + "            await assertGroupMembership(connection, chatId, a.chat_id);"
        + chr(10) + "            const mid = String(a.message_id ?? '').trim();"
        + chr(10) + "            if (mid === '')"
        + chr(10) + "                throw new Error('qq_reply: message_id 必填');"
        + chr(10) + "            const text = String(a.text ?? '');"
        + chr(10) + "            if (text.trim() === '')"
        + chr(10) + "                throw new Error('qq_reply: text 不能为空');"
        + chr(10) + "            const messageId = await bridge.sendSegments(chatId, ["
        + chr(10) + "                { type: 'reply', data: { id: mid } },"
        + chr(10) + "                { type: 'text', data: { text } },"
        + chr(10) + "            ]);"
        + chr(10) + "            return { sent: true, messageId: messageId ?? null, quoted: mid };"
        + chr(10) + "        },"
        + chr(10) + "    }));"
        + chr(10) + chr(10)
        + "    register(defineTool({"
        + chr(10) + "        name: 'qq_emoji_like',"
        + chr(10) + "        description: '给某条消息贴一个 QQ 表情回应（默认 76 = 点赞）。比回一句话更轻、不花 token；同一话题最多一次。',"
        + chr(10) + "        parameters: {"
        + chr(10) + "            message_id: { type: 'string', description: '要点表情的消息 id' },"
        + chr(10) + "            emoji_id: { type: 'string', description: '表情 id，默认 76（点赞）' },"
        + chr(10) + "        },"
        + chr(10) + "        output: sendOutput(),"
        + chr(10) + "        isConcurrencySafe: () => true,"
        + chr(10) + "        execute: async (args) => {"
        + chr(10) + "            const a = args;"
        + chr(10) + "            const mid = String(a.message_id ?? '').trim();"
        + chr(10) + "            if (mid === '')"
        + chr(10) + "                throw new Error('qq_emoji_like: message_id 必填');"
        + chr(10) + "            const emoji = (String(a.emoji_id ?? '').trim() || '76');"
        + chr(10) + "            const left = _tooSoon('like:' + mid + ':' + emoji, 1500);"
        + chr(10) + "            if (left > 0)"
        + chr(10) + "                throw new Error('qq_emoji_like: 冷却中，' + left + ' 秒后再试');"
        + chr(10) + "            await connection.call('set_msg_emoji_like', { message_id: Number(mid), emoji_id: emoji });"
        + chr(10) + "            return { ok: true, messageId: mid, emoji };"
        + chr(10) + "        },"
        + chr(10) + "    }));"
        + chr(10) + chr(10)
        + "    register(defineTool({"
        + chr(10) + "        name: 'qq_poke',"
        + chr(10) + "        description: '拍一拍对方（QQ 的拍一拍）。只对主人或白名单里的人用；有冷却。',"
        + chr(10) + "        parameters: {"
        + chr(10) + "            user_id: { type: 'string', description: '要拍的人；私聊可省略' },"
        + chr(10) + "            chat_id: { type: 'string', description: '目标会话；省略时默认当前会话' },"
        + chr(10) + "        },"
        + chr(10) + "        output: sendOutput(),"
        + chr(10) + "        isConcurrencySafe: () => false,"
        + chr(10) + "        execute: async (args, exec) => {"
        + chr(10) + "            const a = args;"
        + chr(10) + "            const chatId = resolveChat(bridge, exec, a.chat_id);"
        + chr(10) + "            const info = splitChatId(chatId);"
        + chr(10) + "            const target = String(a.user_id ?? (info.kind === 'private' ? info.target : '')).trim();"
        + chr(10) + "            if (target === '')"
        + chr(10) + "                throw new Error('qq_poke: 群聊里必须给 user_id');"
        + chr(10) + "            const lim = _readLimits();"
        + chr(10) + "            // 管理员名单在 deps.policy.adminUsers（不是 config）；再并上 poke_allow 与 OWNER"
        + chr(10) + "            const allow = new Set(["
        + chr(10) + "                ...(bridge.deps.policy?.adminUsers ?? []),"
        + chr(10) + "                ...(bridge.deps.config?.adminUsers ?? []),"
        + chr(10) + "            ].map(String));"
        + chr(10) + "            const ownerEnv = String(process.env.OWNER_QQ ?? process.env.OWNER ?? '').trim();"
        + chr(10) + "            if (ownerEnv !== '')"
        + chr(10) + "                allow.add(ownerEnv);"
        + chr(10) + "            for (const x of String(lim.poke_allow ?? '').split(',')) {"
        + chr(10) + "                const v = x.trim();"
        + chr(10) + "                if (v !== '')"
        + chr(10) + "                    allow.add(v);"
        + chr(10) + "            }"
        + chr(10) + "            if (!allow.has(target))"
        + chr(10) + "                throw new Error('qq_poke: 只允许拍主人或白名单里的人（limits.json 的 poke_allow）');"
        + chr(10) + "            const cooldownS = Math.max(1, Number(lim.poke_cooldown_min ?? 10)) * 60;"
        + chr(10) + "            const left = _tooSoon('poke:' + target, cooldownS * 1000);"
        + chr(10) + "            if (left > 0)"
        + chr(10) + "                throw new Error('qq_poke: 刚拍过，' + Math.ceil(left / 60) + ' 分钟后再来');"
        + chr(10) + "            const params = info.kind === 'group'"
        + chr(10) + "                ? { group_id: Number(info.target), user_id: Number(target) }"
        + chr(10) + "                : { user_id: Number(target) };"
        + chr(10) + "            await connection.call(info.kind === 'group' ? 'group_poke' : 'friend_poke', params);"
        + chr(10) + "            return { ok: true, target, kind: info.kind };"
        + chr(10) + "        },"
        + chr(10) + "    }));"
        + chr(10) + chr(10)
    )
    anchor_tool = ("    return () => {"
                   + chr(10) + "        for (const dispose of disposers)"
                   + chr(10) + "            dispose();"
                   + chr(10) + "    };")
    st = sub(st, anchor_tool, tool_block + anchor_tool, "tools.js 三个工具")

    # ---- 4) 平台提示 ----
    sp = rd(pr)
    if "qq_emoji_like" in sp:
        todo.append("prompt.js 平台提示")
    sp = sub(sp,
             "    platform += '- 群聊消息带 [HH:MM 昵称(QQ)] 前缀，被 @ 时带 [@我]；私聊无前缀。" + chr(10) + "';",
             "    platform += '- 群聊消息带 [HH:MM 昵称(QQ)] 前缀，被 @ 时带 [@我]；私聊无前缀。" + chr(10) + "';"
             + chr(10) + "    platform += '- 每条 <user_message> 上有 message_id：要引用回复用 qq_reply(message_id, text)，"
             + "要贴表情回应用 qq_emoji_like(message_id)（更轻、不花 token）。" + chr(10) + "';"
             + chr(10) + "    platform += '- 拍一拍：只对主人用 qq_poke()，一天别超过两三次；"
             + "别人拍你时消息里会出现 [拍一拍]，可以自然回一句。" + chr(10) + "';",
             "prompt.js 平台提示")

    missing = [t for t in todo if "锚点" in t]
    if missing:
        return "跳过（%s）" % "、".join(missing)
    if not todo:
        return "已打"
    if not apply_:
        return "未打"
    wr(ch, sc)
    wr(ib, si)
    wr(br, sb)
    wr(tl, st)
    wr(pr, sp)
    return "已打"


def patch_send_audit(apply_):
    """统一发送审计：所有出站写记一行，只有真实 message_id 才算发出去了。

    为什么先做这个再谈"统一 gateway"：
      dry-run 假成功、"告警已告知"其实没发、限流拒发但调用方以为成功——
      这些都不会抛错，只会让人以为发出去了。先把"到底发没发"变成可查的事实，
      再去收口发送路径，风险小一半。

    做法：包一层 connection.call（所有出站最终都走它），并在 dry-run 拦截处
    单独记一笔（那条路不经过 connection.call，否则审计里看不出"其实没发"）。
    """
    src = os.path.join(BASE, "send-audit.js")
    dst = os.path.join(LIB, "send-audit.js")
    idx = os.path.join(LIB, "index.js")
    inj = os.path.join(LIB, "inject.js")

    if not os.path.isfile(src):
        return "跳过（没有 /opt/dsh/send-audit.js）"

    applied = 0
    if not os.path.isfile(dst):
        if not apply_:
            return "未打"
        shutil.copy2(src, dst)
        applied += 1

    s1 = rd(idx)
    if "installSendAudit" not in s1:
        if not apply_:
            return "未打"
        s1 = s1.replace("let OneBotConnection;", "let OneBotConnection;" + chr(10) + "let installSendAudit;", 1)
        s1 = s1.replace(
            "    ({ OneBotConnection } = await import('./connection.js'));",
            "    ({ OneBotConnection } = await import('./connection.js'));"
            + chr(10) + "    // local patch (SEND_AUDIT): 统一发送审计（真实 message_id 才算发出）"
            + chr(10) + "    ({ installSendAudit } = await import('./send-audit.js'));", 1)
        anchor = "    const media = new MediaStore(mediaDir,"
        i0 = s1.find(anchor)
        if i0 < 0:
            return "跳过（index.js 找不到 MediaStore 锚点）"
        s1 = s1[:i0] + ("    // local patch (SEND_AUDIT): 装在最靠近连接的地方"
                        + chr(10) + "    installSendAudit(connection, { dir: mediaDir, log });" + chr(10)) + s1[i0:]
        wr(idx, s1)
        applied += 1

    s2 = rd(inj)
    if "noteIntercept" not in s2:
        if not apply_:
            return "未打"
        s2 = s2.replace("import { newTraceId } from './trace.js';",
                        "import { newTraceId } from './trace.js';"
                        + chr(10) + "import { noteIntercept } from './send-audit.js';", 1)
        a2 = "    intercept(round, action, params) {" + chr(10)
        if a2 in s2:
            s2 = s2.replace(a2, a2 + ("        // local patch (SEND_AUDIT): 拦截也要记账"
                                      + chr(10) + "        try { noteIntercept(action, params); }"
                                      + chr(10) + "        catch (e) { void e; }" + chr(10)), 1)
        wr(inj, s2)
        applied += 1

    return "已打" if applied or (os.path.isfile(dst) and "installSendAudit" in rd(idx) and "noteIntercept" in rd(inj)) else "未打"


def patch_event_adapter(apply_):
    """统一事件适配层：所有 OneBot 事件先归一化，再由 ROUTES 决定喂不喂模型。

    原来只有 message 走管线，notice（拍一拍/撤回/进群/贴表情）在 normalizeOneBot11
    就返回 null 被丢掉了。加一种感知就要改一遍入站流程——引用、拍一拍、撤回都这么漏的。
    """
    br = os.path.join(LIB, "bridge.js")
    ev_src = os.path.join(BASE, "events.js")
    ev_dst = os.path.join(LIB, "events.js")

    if "EVENT_ADAPTER" in rd(br) and os.path.isfile(ev_dst):
        return "已打"
    if not os.path.isfile(ev_src):
        return "跳过（没有 /opt/dsh/events.js）"
    if not apply_:
        return "未打"

    if not os.path.isfile(ev_dst):
        shutil.copy2(ev_src, ev_dst)

    sb = rd(br)
    if "EVENT_ADAPTER" not in sb:
        sb = sb.replace("import { buildPromptParts } from './prompt.js';",
                        "import { buildPromptParts } from './prompt.js';"
                        + chr(10) + "// local patch (EVENT_ADAPTER): 统一事件适配层"
                        + chr(10) + "import { describeEvent, toInboundEvent, countEvent } from './events.js';", 1)

        # 旧形态 A：上游原版
        old_a = ("                const inbound = normalizeOneBot11(event);"
                 + chr(10) + "                if (inbound === null) {"
                 + chr(10) + "                    this.deps.log('debug', '非聊天消息事件，不处理（post_type=' + String(event.post_type) + '）');"
                 + chr(10) + "                    scope?.emit('normalize', { ok: false, reason: TRACE_REASONS.inboundNotChat });"
                 + chr(10) + "                    return;"
                 + chr(10) + "                }"
                 + chr(10) + "                await this.processInbound(inbound);")
        # 旧形态 B：v0.9.0 的 poke 钩子
        old_b = ("                // local patch (POKE_IN): 拍一拍是 notice 事件，原逻辑在这里被丢弃"
                 + chr(10) + "                if (event && event.post_type === 'notice' && await this.handlePokeNotice(event)) {"
                 + chr(10) + "                    return;"
                 + chr(10) + "                }"
                 + chr(10) + old_a)

        new_block = ("                // local patch (EVENT_ADAPTER): 所有事件先归一化，再由路由表决定喂不喂模型"
                     + chr(10) + "                const desc = describeEvent(event);"
                     + chr(10) + "                countEvent(desc);"
                     + chr(10) + "                const synthetic = toInboundEvent(event, desc);"
                     + chr(10) + "                if (synthetic === null) {"
                     + chr(10) + "                    this.deps.log(desc.kind === 'other' ? 'debug' : 'info',"
                     + chr(10) + "                        '事件只记录不下发：' + desc.key);"
                     + chr(10) + "                    scope?.emit('normalize', { ok: true, reason: 'event-recorded:' + desc.key });"
                     + chr(10) + "                    return;"
                     + chr(10) + "                }"
                     + chr(10) + "                const inbound = normalizeOneBot11(synthetic);"
                     + chr(10) + "                if (inbound === null) {"
                     + chr(10) + "                    this.deps.log('debug', '合成事件无法归一化：' + desc.key);"
                     + chr(10) + "                    scope?.emit('normalize', { ok: false, reason: TRACE_REASONS.inboundNotChat });"
                     + chr(10) + "                    return;"
                     + chr(10) + "                }"
                     + chr(10) + "                await this.processInbound(inbound);")

        if old_b in sb:
            sb = sb.replace(old_b, new_block, 1)
        elif old_a in sb:
            sb = sb.replace(old_a, new_block, 1)
        else:
            return "跳过（bridge 入口锚点没找到）"
        wr(br, sb)
    return "已打"


def patch_send_gateway(apply_):
    """写操作统一过闸：表情回应 / 拍一拍 / napcat 代理都走 guardedConnectionCall。

    为什么：这三处原来直接调 connection.call，绕过了 guard 的 dry-run 拦截——
    实测踩过：dry 测试时"拍一下"，**真的拍到了主人**（friend_poke 直接发出去）。
    顺带把 isWriteAction 扩到 poke / 表情回应，否则 dry-run 仍然拦不住。
    """
    tl = os.path.join(LIB, "tools.js")
    inj = os.path.join(LIB, "inject.js")
    st = rd(tl)
    si = rd(inj)
    todo = []

    def sub(text, old, new, tag):
        # 线上文本可能被手工微调过：按功能标记判断，四项各自独立
        if tag == "isWriteAction":
            if "poke / 表情回应也算写操作" in text or "friend_poke' || a === 'group_poke" in text:
                return text
        elif "SEND_GATEWAY" in text and tag in ("emoji", "poke", "proxy"):
            return text
        if new in text:
            return text
        if old not in text:
            todo.append(tag + "（锚点没找到）")
            return text
        todo.append(tag)
        return text.replace(old, new, 1)

    st = sub(st,
             "            await connection.call('set_msg_emoji_like', { message_id: Number(mid), emoji_id: emoji });",
             "            // local patch (SEND_GATEWAY): 走统一出口（guard 负责 dry-run 拦截/审计/限流）"
             + chr(10) + "            await bridge.guardedConnectionCall('set_msg_emoji_like', { message_id: Number(mid), emoji_id: emoji });",
             "emoji")

    st = sub(st,
             "            await connection.call(info.kind === 'group' ? 'group_poke' : 'friend_poke', params);",
             "            // local patch (SEND_GATEWAY): 走统一出口（否则测试时会真的拍人）"
             + chr(10) + "            await bridge.guardedConnectionCall(info.kind === 'group' ? 'group_poke' : 'friend_poke', params);",
             "poke")

    st = sub(st,
             "            return await connection.call(a.action, params);",
             "            // local patch (SEND_GATEWAY): 写操作统一过闸；只读的仍直连（少一层开销）"
             + chr(10) + "            if (/^(send_|set_|delete_|upload_|friend_poke|group_poke)/.test(String(a.action)))"
             + chr(10) + "                return await bridge.guardedConnectionCall(a.action, params);"
             + chr(10) + "            return await connection.call(a.action, params);",
             "proxy")

    si = sub(si,
             "export function isWriteAction(action) {"
             + chr(10) + "    return action.startsWith('send_') || action.startsWith('set_') || action === 'delete_msg';"
             + chr(10) + "}",
             "export function isWriteAction(action) {"
             + chr(10) + "    // local patch (SEND_GATEWAY): poke / 表情回应也算写操作，"
             + chr(10) + "    // 否则 dry-run 拦不住——测试时会真的拍人、真的贴表情（实测踩过）。"
             + chr(10) + "    const a = String(action);"
             + chr(10) + "    return a.startsWith('send_') || a.startsWith('set_') || a.startsWith('upload_')"
             + chr(10) + "        || a === 'delete_msg' || a === 'friend_poke' || a === 'group_poke'"
             + chr(10) + "        || a === 'set_msg_emoji_like';"
             + chr(10) + "}",
             "isWriteAction")

    missing = [t for t in todo if "锚点" in t]
    if missing:
        return "跳过（%s）" % "、".join(missing)
    if not todo:
        return "已打"
    if not apply_:
        return "未打"
    wr(tl, st)
    wr(inj, si)
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
    ("bridge.js 气泡默认不限（0=不限，接口保留）", patch_bubble_spread),
    ("引用回复 / 表情回应 / 拍一拍（入站+出站）", patch_reply_poke_emoji),
    ("发送审计（真实 message_id 才算发出）", patch_send_audit),
    ("事件适配层 events.js（notice 不再被丢）", patch_event_adapter),
    ("写操作统一过闸（表情回应/拍一拍/代理）", patch_send_gateway),
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
