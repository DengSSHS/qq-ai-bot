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
import { appendFile, readFile } from 'node:fs/promises';
import { join } from 'node:path';

const FILE = 'qq-outbox.jsonl';
const RESULT = 'qq-outbox.result.jsonl';

export function startOutbox({ dir, bridge, log, intervalMs = 2000 }) {
    const path = join(dir, FILE);
    const resultPath = join(dir, RESULT);
    let consumed = 0;
    let busy = false;

    const appendResult = async (row) => {
        try {
            await appendFile(resultPath, JSON.stringify(row) + '\n');
        }
        catch (e) {
            void e;
        }
    };

    const sendOne = async (job) => {
        const chatId = String(job.chat_id ?? '');
        const text = String(job.text ?? '');
        const source = String(job.source ?? 'outbox');
        const started = Math.floor(Date.now() / 1000);
        const parts = chatId.split(':');
        const kind = parts[0];
        const target = Number(parts[1]);
        if ((kind !== 'private' && kind !== 'group') || !Number.isFinite(target) || text.trim() === '') {
            await appendResult({ ts: started, ok: false, source, error: 'bad job: chat_id/text', chat_id: chatId });
            return;
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
        }
        catch (error) {
            await appendResult({
                ts: started, ok: false, source, chat_id: chatId,
                error: String(error && error.message ? error.message : error).slice(0, 160),
            });
            if (log)
                log('warn', '[outbox] 发送失败 source=' + source + ' chat=' + chatId + ': '
                    + String(error && error.message ? error.message : error).slice(0, 120));
        }
    };

    const tick = async () => {
        if (busy)
            return;
        busy = true;
        try {
            let text;
            try {
                text = await readFile(path, 'utf8');
            }
            catch (e) {
                if (e && e.code === 'ENOENT')
                    return;
                return;
            }
            if (text.length < consumed)
                consumed = 0;                       // 文件被清空/轮转
            const fresh = text.slice(consumed);
            const nl = fresh.lastIndexOf('\n');
            if (nl < 0)
                return;                             // 还没写完整行
            const chunk = fresh.slice(0, nl + 1);
            consumed += chunk.length;
            for (const line of chunk.split('\n')) {
                const trimmed = line.trim();
                if (trimmed === '')
                    continue;
                let job;
                try {
                    job = JSON.parse(trimmed);
                }
                catch (e) {
                    if (log)
                        log('warn', '[outbox] 行解析失败，跳过：' + trimmed.slice(0, 80));
                    continue;
                }
                await sendOne(job);
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
