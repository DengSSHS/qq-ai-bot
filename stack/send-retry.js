/**
 * 发送重试（local patch SEND_RETRY，2026-11-09）
 *
 * 要解决的问题：服务重启 / NapCat 重连的那几秒，socket 还没 OPEN。
 * connection.call() 这时**在写出任何字节之前**就抛 OneBotNotConnectedError，
 * 于是一条刚好在这几秒里生成的话就这么没了（审计里 24 小时 9 条
 * "OneBot WebSocket not connected" 全是这么来的）。
 *
 * 为什么可以安全重试：
 *   call() 的实现是「先查 socket 状态，不 OK 就直接 reject」，
 *   帧根本没发出去 → 重试不可能造成重复消息。
 *
 * 为什么只重试这一种：
 *   "OneBot transport stopped" 是 stop() 时 failAllPending 抛的，
 *   那时帧**可能已经写进 socket 了**，只是没等到回包。
 *   这种情况重试 = 有可能发两遍，所以一律不重试，老老实实算失败。
 */

/** 这个错误是否「确定没发出去」——只有它才允许重试 */
export function isRetryableSendError(err) {
    if (!err)
        return false;
    const name = String(err.name || '');
    const msg = String(err.message || err);
    if (name === 'OneBotNotConnectedError' && !/transport stopped/i.test(msg))
        return true;
    // 名字丢了但错误文本能认出来的情况（跨版本升级时会遇到）
    return /OneBot WebSocket not connected/i.test(msg);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * 包一层有限重试。
 * @param {() => Promise<any>} fn 真正干活的函数
 * @param {object} [opts]
 * @param {number[]} [opts.delays] 每次重试前等多久，默认 1.5s / 4s / 8s（合计约 13.5 秒）
 * @param {Function} [opts.log] 日志出口
 */
export async function withSendRetry(fn, opts = {}) {
    const delays = opts.delays || [1500, 4000, 8000];
    const log = opts.log;
    for (let attempt = 0;; attempt += 1) {
        try {
            return await fn();
        }
        catch (err) {
            if (attempt >= delays.length || !isRetryableSendError(err)) {
                if (attempt > 0 && log)
                    log('warn', '[send-retry] 重试 ' + attempt + ' 次仍未成功：'
                        + String(err && err.message || err).slice(0, 100));
                throw err;
            }
            if (log)
                log('warn', '[send-retry] 传输还没连上，' + delays[attempt]
                    + 'ms 后重试（第 ' + (attempt + 1) + ' 次）：'
                    + String(err && err.message || err).slice(0, 80));
            await sleep(delays[attempt]);
        }
    }
}
