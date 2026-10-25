/**
 * 统一发送审计（2026-10-26，架构欠账第 2 条的第一步）。
 *
 * 为什么要有这个文件：
 *   发送这个动作散着三种"假成功"——dry-run 返回假 id、告警说"已告知"其实没发、
 *   限流拒发但调用方以为成功。它们都不会报错，只会让人以为发出去了。
 *
 * 做法：把 `connection.call` 包一层（所有出站最终都走它），每一次写操作都记一行：
 *   { ts, action, target, ok, message_id, intercepted, fake, error, ms }
 *
 * 判定规则：
 *   · send_* 且拿不到真实 message_id（>0）→ 记为 fake（"假成功"），并打 warn
 *   · message_id === -1 → 是注入 dry-run 的拦截标记，记为 intercepted（已知，不算故障）
 *   · 抛异常 → 记为 fail
 *
 * 这个文件只做"记账"，不改任何发送行为——所以它不会成为新的风险点。
 * @module dsh-onebot/send-audit
 */
import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';

/** 算"写操作"的动作前缀。 */
const WRITE_RE = /^(send_|set_|delete_|upload_|friend_poke|group_poke|set_msg_emoji_like)/;

const stats = { total: 0, ok: 0, intercepted: 0, fail: 0, fake: 0 };
let auditPath = '';
let logFn = null;

function ensureDir(p) {
    try {
        mkdirSync(dirname(p), { recursive: true });
    }
    catch (e) {
        void e;
    }
}

function realIdOf(result) {
    if (result === undefined || result === null)
        return null;
    const raw = result.message_id !== undefined ? result.message_id
        : (result.data && result.data.message_id !== undefined ? result.data.message_id : undefined);
    if (raw === undefined || raw === null || raw === '')
        return null;
    const n = Number(raw);
    if (!Number.isFinite(n))
        return null;
    return n;
}

function targetOf(params) {
    if (!params || typeof params !== 'object')
        return '';
    if (params.group_id !== undefined && params.group_id !== null)
        return 'group:' + String(params.group_id);
    if (params.user_id !== undefined && params.user_id !== null)
        return 'private:' + String(params.user_id);
    return '';
}

function write(row) {
    if (auditPath === '')
        return;
    try {
        appendFileSync(auditPath, JSON.stringify(row) + '\n');
    }
    catch (e) {
        void e;
    }
}

function record(action, params, result, error, startedAt) {
    const ms = Date.now() - startedAt;
    const target = targetOf(params);
    const base = { ts: Math.floor(startedAt / 1000), action: String(action), target };

    if (error) {
        stats.total += 1;
        stats.fail += 1;
        write({ ...base, ok: false, error: String(error && error.message ? error.message : error).slice(0, 180), ms });
        return;
    }

    const id = realIdOf(result);
    if (id !== null && id === -1) {
        // 注入 dry-run 的拦截标记：已知行为，不算故障
        stats.total += 1;
        stats.intercepted += 1;
        write({ ...base, ok: false, intercepted: true, ms });
        return;
    }

    const isSend = String(action).startsWith('send_');
    if (isSend && id === null) {
        // 最要命的一类：没报错、但也没有真实 message_id
        stats.total += 1;
        stats.fake += 1;
        write({ ...base, ok: false, fake: true, ms });
        if (logFn)
            logFn('warn', '[发送] 假成功：' + action + ' target=' + target + ' 没有返回真实 message_id');
        return;
    }

    stats.total += 1;
    stats.ok += 1;
    write({ ...base, ok: true, message_id: id, ms });
}

/**
 * 安装审计：包一层 connection.call。
 * @param connection OneBotConnection 实例
 * @param options {{dir: string, log?: Function}}
 */
export function installSendAudit(connection, options) {
    auditPath = join(options.dir, 'send_audit.jsonl');
    ensureDir(auditPath);
    logFn = options.log ?? null;
    if (connection.__sendAuditInstalled === true)
        return stats;
    connection.__sendAuditInstalled = true;

    const raw = connection.call.bind(connection);
    connection.call = (action, params) => {
        if (!WRITE_RE.test(String(action)))
            return raw(action, params);
        const t0 = Date.now();
        return Promise.resolve()
            .then(() => raw(action, params))
            .then((result) => {
                record(action, params, result, null, t0);
                return result;
            })
            .catch((error) => {
                record(action, params, null, error, t0);
                throw error;
            });
    };
    return stats;
}

/** 计数（面板 / checkall 可读）。 */
export function sendAuditStats() {
    return { ...stats, path: auditPath };
}
