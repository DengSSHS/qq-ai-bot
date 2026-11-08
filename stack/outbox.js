/**
 * 出站队列消费者（2026-10-26，架构欠账第 2/5 条）：
 * 让 Python 侧（scheduler / 告警 / 通知）也能走**同一个发送闸门**。
 *
 * 为什么要这个文件：
 *   Python 侧过去想发 QQ 只能往 qq-inject.jsonl 塞一条"假用户消息"——
 *   那是入站通道，语义完全错（她会当成主人说话），而且重启会丢。
 *   现在约定：Python 只往 qq-outbox.jsonl 追加 {chat_id, text, source}，
 *   由这里统一送进 bridge.guardedConnectionCall：
 *   限流 / dry-run 拦截 / 旁白剥离 / 气泡分合 / 审计，一个都不落。
 *
 * 每一行的结果追加到 qq-outbox.result.jsonl（含真实 message_id），
 * 所以调用方可以事后确认"到底发没发出去"。
 * @module dsh-onebot/outbox
 */
import { appendFile, readFile, rename, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const FILE = 'qq-outbox.jsonl';
const RESULT = 'qq-outbox.result.jsonl';
const OFFSET = 'qq-outbox.offset';
// 队列文件超过这个大小就压实一次（只保留未消费的尾巴）
const COMPACT_BYTES = 256 * 1024;

export function startOutbox({ dir, bridge, log, intervalMs = 2000 }) {
    const path = join(dir, FILE);
    const resultPath = join(dir, RESULT);
    const statePath = join(dir, OFFSET);
    let consumed = 0;
    // 消费到哪了要写盘。不写盘的后果（实测 2026-11-09）：
    //   每次重启 consumed 归零 → 整个文件从头重放
    //   → 旧任务被批量判“过期”（5 次重启刷出 190 条假失败，把体检报警了）
    //   → 更糟的是 30 分钟内的任务会被**重发一遍**（群里看到重复发言）
    let restored = false;
    let savedAt = -1;
    let busy = false;
    // 发送失败后退避：掉线时每 2 秒重试一次会把日志刷爆（实测）
    let backoffUntil = 0;

    const saveOffset = async (size) => {
        if (consumed === savedAt)
            return;
        savedAt = consumed;
        try {
            // 先写临时文件再改名：中途崩了也不会留下半个偏移量
            const tmp = statePath + '.tmp';
            await writeFile(tmp, JSON.stringify({ offset: consumed, size, ts: Math.floor(Date.now() / 1000) }));
            await rename(tmp, statePath);
        }
        catch (e) {
            void e;
        }
    };

    const restoreOffset = async (text) => {
        restored = true;
        try {
            const st = JSON.parse(await readFile(statePath, 'utf8'));
            const off = Number(st && st.offset);
            const old = Number(st && st.size);
            // 偏移量本身可用就用它（size 对不上只是文件长长了/压实过）
            if (Number.isFinite(off) && off <= text.length) {
                consumed = off;
                savedAt = off;
                return;
            }
            void old;
        }
        catch (e) {
            void e;
        }
        // 没状态 / 状态坏了：**不要从头重放**。
        // 从头的代价是两条实测过的：旧任务被判“过期”刷出一批假失败，
        // 30 分钟内的任务还会被重发一遍（对方看到重复消息）。
        // 改成：跳过那些已经超过 TTL 的旧行，能补发的只留下还没过期的。
        consumed = freshStartOffset(text);
        savedAt = -1;
    };

    /** 返回“第一条还没过期的任务”在文本里的位置；全都过期就返回末尾。 */
    const freshStartOffset = (text, ttlS = 1800) => {
        let at = 0;
        while (true) {
            const nl = text.indexOf('\n', at);
            if (nl < 0)
                return text.length;
            const line = text.slice(at, nl).trim();
            if (line !== '') {
                let job = null;
                try {
                    job = JSON.parse(line);
                }
                catch (e) {
                    void e;
                }
                const ts = Number(job && job.ts) || 0;
                if (!ts || Date.now() / 1000 - ts <= ttlS)
                    return at;          // 没时间戳的当新的，宁可补发也别默默丢
            }
            at = nl + 1;
        }
    };

    const appendResult = async (row) => {
        try {
            await appendFile(resultPath, JSON.stringify(row) + '\n');
        }
        catch (e) {
            void e;
        }
    };

    const sendOne = async (job) => {
        // 过期任务丢弃：掉线太久之后补发一堆「刚睡醒」没有意义（实测会一次轰出去）
        const ageS = Date.now() / 1000 - Number(job.ts || 0);
        if (job.ts && ageS > 1800) {
            await appendResult({ ts: Math.floor(Date.now() / 1000), ok: false, expired: true,
                                 source: String(job.source || 'outbox'), chat_id: String(job.chat_id || ''),
                                 error: 'expired: ' + Math.round(ageS / 60) + ' 分钟未发出' });
            if (log)
                log('info', '[outbox] 丢弃过期任务 source=' + String(job.source)
                    + '（' + Math.round(ageS / 60) + ' 分钟）');
            return true;
        }
        const chatId = String(job.chat_id ?? '');
        const text = String(job.text ?? '');
        const source = String(job.source ?? 'outbox');
        const started = Math.floor(Date.now() / 1000);
        const parts = chatId.split(':');
        const kind = parts[0];
        const target = Number(parts[1]);
        if ((kind !== 'private' && kind !== 'group') || !Number.isFinite(target) || text.trim() === '') {
            await appendResult({ ts: started, ok: false, source, error: 'bad job: chat_id/text', chat_id: chatId });
            return false;
        }
        const params = kind === 'group' ? { group_id: target } : { user_id: target };
        params.message = [{ type: 'text', data: { text } }];
        try {
            const res = await bridge.guardedConnectionCall('send_msg', params, chatId);
            const raw = res && (res.message_id !== undefined ? res.message_id
                : (res.data && res.data.message_id !== undefined ? res.data.message_id : undefined));
            const id = raw === undefined || raw === null ? null : Number(raw);
            await appendResult({
                ts: started, ok: true, source, chat_id: chatId,
                message_id: id,
                intercepted: id === -1,
            });
            if (log)
                log('info', '[outbox] 已发送 source=' + source + ' chat=' + chatId
                    + (id === -1 ? '（被 dry-run 拦截）' : ' id=' + String(id)));
            return true;
        }
        catch (error) {
            await appendResult({
                ts: started, ok: false, source, chat_id: chatId,
                error: String(error && error.message ? error.message : error).slice(0, 160),
            });
            const msg = String(error && error.message ? error.message : error);
            if (log)
                log('warn', '[outbox] 发送失败 source=' + source + ' chat=' + chatId + ': ' + msg.slice(0, 120));
            return msg;
        }
    };

    const tick = async () => {
        if (busy)
            return;
        if (Date.now() < backoffUntil)
            return;
        busy = true;
        try {
            let text;
            try {
                text = await readFile(path, 'utf8');
            }
            catch (e) {
                return;
            }
            if (!restored)
                await restoreOffset(text);
            if (text.length < consumed)
                consumed = 0;                       // 文件被清空/轮转
            let offset = consumed;
            while (true) {
                const nl = text.indexOf('\n', offset);
                if (nl < 0)
                    break;                          // 没有完整行
                const line = text.slice(offset, nl).trim();
                const lineBytes = nl + 1 - offset;
                if (line === '') {
                    offset = nl + 1;
                    consumed = offset;
                    continue;
                }
                let job;
                try {
                    job = JSON.parse(line);
                }
                catch (e) {
                    if (log)
                        log('warn', '[outbox] 行解析失败，跳过：' + line.slice(0, 80));
                    offset = nl + 1;
                    consumed = offset;
                    continue;
                }
                const ok = await sendOne(job);
                if (ok !== true) {
                    backoffUntil = Date.now() + 60000;   // 失败后退避 60 秒再试
                    break;
                }
                if (!ok && ok !== undefined && String(ok).indexOf('not connected') >= 0)
                    break;                          // 没连上：保留这一行，下轮重试
                if (ok === false)
                    break;                          // 其它失败也先停，避免连续撞墙
                offset = nl + 1;
                consumed = offset;
            }
            await saveOffset(text.length);
            // 压实：旧行不再需要，留在文件里只会让每次重启（万一丢了偏移量）重放一遍
            if (consumed >= COMPACT_BYTES) {
                try {
                    const nowText = await readFile(path, 'utf8');
                    const tail = nowText.length > consumed ? nowText.slice(consumed) : '';
                    const tmp = path + '.compact';
                    await writeFile(tmp, tail);
                    await rename(tmp, path);
                    consumed = 0;
                    savedAt = -1;
                    await saveOffset(tail.length);
                    if (log)
                        log('info', '[outbox] 队列已压实，保留未消费 ' + tail.length + ' 字节');
                }
                catch (e) {
                    if (log)
                        log('warn', '[outbox] 压实失败（不影响发送）：' + String(e && e.message || e).slice(0, 100));
                }
            }
        }
        finally {
            busy = false;
        }
    };

    const timer = setInterval(() => void tick(), intervalMs);
    timer.unref?.();
    return () => clearInterval(timer);
}
