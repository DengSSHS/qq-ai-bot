// send-retry 的行为测试（2026-11-09）
//
// 守的是一条很容易被"顺手改成都重试"的边界：
//   只有「帧根本没写出去」的错能重试（OneBot WebSocket not connected）。
//   "OneBot transport stopped" 是 stop() 里 failAllPending 抛的，
//   那时帧可能已经写进 socket 了，重试就是发两遍 —— 必须不重试。
//
// 用法（服务器上）：
//     /opt/node/bin/node tests/send_retry.mjs
import { isRetryableSendError, withSendRetry } from '/opt/dsh/send-retry.js';

class NotConnected extends Error {
    constructor(msg = 'OneBot WebSocket not connected') {
        super(msg);
        this.name = 'OneBotNotConnectedError';
    }
}

const results = [];
const check = (name, cond) => results.push((cond ? 'PASS ' : 'FAIL ') + name);

// ① 判据
check('not connected 可重试', isRetryableSendError(new NotConnected()));
check('transport stopped 不重试', !isRetryableSendError(new NotConnected('OneBot transport stopped')));
check('超时类错误不重试', !isRetryableSendError(
    Object.assign(new Error('OneBot action send_msg timed out'), { name: 'OneBotActionError' })));
check('null 不炸', isRetryableSendError(null) === false);

// ② 两次断连后成功
let calls = 0;
const value = await withSendRetry(async () => {
    calls += 1;
    if (calls < 3)
        throw new NotConnected();
    return { message_id: 42 };
}, { delays: [10, 10, 10] });
check('断 2 次后成功（共调用 3 次）', value.message_id === 42 && calls === 3);

// ③ 一直断连：重试用完必须抛错，不能假装成功
let calls2 = 0;
let threw = false;
try {
    await withSendRetry(async () => { calls2 += 1; throw new NotConnected(); }, { delays: [10, 10] });
}
catch (e) {
    threw = e instanceof NotConnected;
}
check('永远断连时抛错且只试 3 次', threw && calls2 === 3);

// ④ transport stopped：一次都不重试
let calls3 = 0;
threw = false;
try {
    await withSendRetry(async () => {
        calls3 += 1;
        throw new NotConnected('OneBot transport stopped');
    }, { delays: [10] });
}
catch (e) {
    threw = true;
}
check('transport stopped 不重试（只调用 1 次）', threw && calls3 === 1);

console.log(results.join('\n'));
const failed = results.filter(r => r.startsWith('FAIL')).length;
console.log(failed === 0 ? 'ALL PASS' : failed + ' FAILED');
process.exit(failed === 0 ? 0 : 1);
