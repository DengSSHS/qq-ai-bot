// 出站队列的「重启不重放」回归测试。
//
// 背景（2026-11-09）：消费位置只存在内存里，每次重启 consumed 归零 →
// 整个队列文件从头重放：旧任务被批量判“过期”（5 次重启刷出 190 条假失败），
// 而 30 分钟内的任务会被**重发一遍**（对方看到重复消息）。
//
// 用法（服务器上，需要 /opt/dsh/outbox.js）：
//     /opt/node/bin/node tests/outbox_restart.mjs
// 输出 JSON，最后一行 "没有重复发送": true 即通过。
import { startOutbox } from '/opt/dsh/outbox.js';
import { appendFile, mkdir, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';

const dir = '/tmp/outbox_restart_' + Date.now();
await mkdir(dir, { recursive: true });

const sent = [];
const logs = [];
const bridge = {
    guardedConnectionCall: async (_act, p, chatId) => {
        sent.push(chatId + '|' + (p.message?.[0]?.data?.text ?? ''));
        return { message_id: 1000 + sent.length };
    },
};
const cfg = { dir, bridge, log: (lvl, msg) => logs.push(lvl + ' ' + msg) };

const now = Math.floor(Date.now() / 1000);
for (const job of [
    { ts: now - 10 * 86400, chat_id: 'private:1', text: '十天前的旧消息', source: 't' },
    { ts: now - 60, chat_id: 'private:1', text: '一分钟前的新消息', source: 't' },
])
    await appendFile(join(dir, 'qq-outbox.jsonl'), JSON.stringify(job) + '\n');

const stop1 = startOutbox(cfg);
await new Promise(r => setTimeout(r, 2600));
stop1();
const afterFirst = sent.slice();

// 模拟重启：同一目录起一个新的消费者
const stop2 = startOutbox(cfg);
await new Promise(r => setTimeout(r, 2600));
stop2();

const offset = JSON.parse(await readFile(join(dir, 'qq-outbox.offset'), 'utf8'));
const pass = afterFirst.length === 1 && sent.length === 1;

// —— 场景 2：状态文件坏了（offset 指向文件外）→ 也不能从头重放 ——
const sent2 = [];
const dir2 = '/tmp/outbox_bad_state_' + Date.now();
await mkdir(dir2, { recursive: true });
for (const job of [
    { ts: now - 5 * 86400, chat_id: 'private:2', text: '五天前的旧消息', source: 't' },
    { ts: now - 30, chat_id: 'private:2', text: '刚排的新消息', source: 't' },
])
    await appendFile(join(dir2, 'qq-outbox.jsonl'), JSON.stringify(job) + '\n');
await appendFile(join(dir2, 'qq-outbox.offset'), JSON.stringify({ offset: 999999, size: 999999 }));
const stop3 = startOutbox({ dir: dir2, bridge: {
    guardedConnectionCall: async (_a, p, chatId) => {
        sent2.push(chatId + '|' + (p.message?.[0]?.data?.text ?? ''));
        return { message_id: 2000 + sent2.length };
    },
}, log: () => {} });
await new Promise(r => setTimeout(r, 2600));
stop3();

console.log(JSON.stringify({
    场景1_第一次运行发送: afterFirst,
    场景1_重启后新增发送: sent.slice(afterFirst.length),
    场景1_偏移量已持久化: offset,
    场景1_没有重复发送: pass,
    场景2_状态损坏时发送: sent2,
    场景2_只发了新消息: sent2.length === 1 && sent2[0].endsWith('刚排的新消息'),
    日志: logs,
}, null, 2));
await rm(dir, { recursive: true, force: true });
await rm(dir2, { recursive: true, force: true });
process.exit(pass && sent2.length === 1 ? 0 : 1);
