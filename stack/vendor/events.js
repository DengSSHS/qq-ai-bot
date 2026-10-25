/**
 * 统一事件适配层（2026-10-26）。
 *
 * 为什么要有这个文件：
 *   原来入站只有 message 走管线，notice（拍一拍/撤回/进群/贴表情）走到
 *   `normalizeOneBot11` 返回 null 就被丢了——每加一种「感知」都要改一遍入站代码，
 *   引用、拍一拍、撤回都是这么漏掉的。
 *
 * 现在的形状：
 *   OneBot 事件 → describeEvent() 归一化成「系统里发生了一件事」
 *                → 路由表决定喂不喂给模型（feed）
 *                → 需要喂的再用 toInboundEvent() 转成一条合成消息
 *
 * 加新感知只需要在 ROUTES 里加一行，不用碰桥接的入站流程。
 * @module dsh-onebot/events
 */

/** 事件大类。 */
export const KIND = {
    MESSAGE: 'message',
    POKE: 'poke',
    RECALL: 'recall',
    MEMBER: 'member',
    REACTION: 'reaction',
    OTHER: 'other',
};

/**
 * 路由表：`<post_type>[.<notice_type>][.<sub_type>]` → 规则。
 *
 * 字段：
 *   feed    要不要交给模型（true = 合成一条消息进管线）
 *   kind    归类，日志/统计用
 *   note    给模型看的一句话（feed=true 且需要自定义文案时）
 */
export const ROUTES = {
    // ---- 消息：全部进管线 ----
    'message.private': { feed: true, kind: KIND.MESSAGE },
    'message.group': { feed: true, kind: KIND.MESSAGE },

    // ---- 拍一拍：进管线（她会看到「有人拍了你」）----
    'notice.notify.poke': { feed: true, kind: KIND.POKE, note: '拍了拍你（这是 QQ 的「拍一拍」动作，不是发消息）' },

    // ---- 撤回：只记录，不打断她 ----
    'notice.group_recall': { feed: false, kind: KIND.RECALL },
    'notice.friend_recall': { feed: false, kind: KIND.RECALL },

    // ---- 群成员变动：只记录 ----
    'notice.group_increase': { feed: false, kind: KIND.MEMBER },
    'notice.group_decrease': { feed: false, kind: KIND.MEMBER },
    'notice.group_admin': { feed: false, kind: KIND.MEMBER },

    // ---- 表情回应（set_msg_emoji_like 的回执）：只记录 ----
    'notice.notify.emoji_like': { feed: false, kind: KIND.REACTION },

    // ---- 其它 notify：输入状态、群名变更等，静默 ----
    'notice.notify.input_status': { feed: false, kind: KIND.OTHER },
    'notice.notify.group_name': { feed: false, kind: KIND.MEMBER },
    'notice.notify.title': { feed: false, kind: KIND.OTHER },
};

/** 路由表里没登记的：静默丢弃（但会被计数，checkall 能看到）。 */
const FALLBACK = { feed: false, kind: KIND.OTHER };

/** 拍一拍去抖：同一个人 3 秒内连拍只算一次。 */
const pokeSeen = new Map();

function keyOf(event) {
    const post = String(event?.post_type ?? '');
    if (post === 'message') {
        const mt = String(event.message_type ?? '');
        return mt === 'group' ? 'message.group' : 'message.private';
    }
    if (post === 'notice') {
        const nt = String(event.notice_type ?? '');
        const st = String(event.sub_type ?? '');
        if (st !== '')
            return 'notice.' + nt + '.' + st;
        return 'notice.' + nt;
    }
    return post;
}

function chatIdOf(event) {
    const isGroup = event?.message_type === 'group' || (event?.group_id !== undefined && event?.group_id !== null);
    const kind = isGroup ? 'group' : 'private';
    const target = isGroup ? event?.group_id : event?.user_id;
    if (target === undefined || target === null || target === '')
        return '';
    return kind + ':' + String(target);
}

/**
 * 归一化：这条路不关心事件原本长什么样，只回答四个问题。
 * @returns {{key, kind, feed, chatId, userId, note, event}}
 */
export function describeEvent(event) {
    const key = keyOf(event);
    const rule = ROUTES[key] ?? FALLBACK;
    return {
        key,
        kind: rule.kind,
        feed: rule.feed === true,
        chatId: chatIdOf(event),
        userId: String(event?.user_id ?? event?.target_id ?? ''),
        note: rule.note ?? '',
    };
}

function senderLabel(event) {
    const s = event?.sender ?? {};
    if (typeof s.card === 'string' && s.card !== '')
        return s.card;
    if (typeof s.nickname === 'string' && s.nickname !== '')
        return s.nickname;
    return String(event?.user_id ?? '');
}

/**
 * 把「要喂给模型」的事件转成一条合成消息（OneBot message 事件的形状）。
 * 不需要喂的返回 null。
 *
 * @param event 原始事件
 * @param desc  describeEvent(event)
 * @param now   毫秒时间戳（测试可注入）
 */
export function toInboundEvent(event, desc, now = Date.now()) {
    if (!desc.feed)
        return null;

    if (desc.kind === KIND.MESSAGE)
        return event;

    if (desc.kind === KIND.POKE) {
        const selfId = String(event?.self_id ?? '');
        const target = String(event?.target_id ?? '');
        // 拍的不是她 → 不进管线
        if (selfId === '' || (target !== '' && target !== selfId))
            return null;
        const isGroup = event?.group_id !== undefined && event?.group_id !== null;
        const who = String(event?.user_id ?? '');
        const key = (isGroup ? 'group:' + String(event.group_id) : 'private:') + who;
        if (now - (pokeSeen.get(key) ?? 0) < 3000)
            return null;
        pokeSeen.set(key, now);
        const label = senderLabel(event);
        const text = '[拍一拍] ' + label + ' ' + (desc.note || '拍了拍你');
        return {
            post_type: 'message',
            message_type: isGroup ? 'group' : 'private',
            sub_type: isGroup ? 'normal' : 'friend',
            self_id: event?.self_id,
            user_id: event?.user_id,
            ...(isGroup ? { group_id: event.group_id } : {}),
            message_id: now,
            time: event?.time !== undefined ? event.time : Math.floor(now / 1000),
            raw_message: text,
            message: [{ type: 'text', data: { text } }],
            sender: event?.sender !== undefined ? event.sender : { user_id: event?.user_id, nickname: label },
        };
    }

    return null;
}

/** 统计：每种事件来过多少次（checkall / 面板可以读）。 */
const counters = new Map();

export function countEvent(desc) {
    counters.set(desc.key, (counters.get(desc.key) ?? 0) + 1);
}

export function eventStats() {
    return Object.fromEntries(counters);
}
