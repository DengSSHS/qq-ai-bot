# 架构欠账清单（六条）

2026-10-26。这六条不是功能，是**结构**。按「先止血、再收口」排序，
每条都写了现状核实结果、最小可做版本、以及怎么验证"接对了"。

状态标记：✅ 已做 · 🟡 部分 · ⬜ 未做

---

## ① 统一事件适配层 ✅（2026-10-26 已上线）

**现状核实**：入站只处理 `message`，`notice`（拍一拍/撤回/进群/贴表情）走到
`normalizeOneBot11` 返回 null 就被丢了。加一种感知就要改一遍入站代码——引用、拍一拍、撤回都这么漏的。

**已实现** `lib/events.js`：

```js
ROUTES = {
  'message.private'        : { feed: true },
  'message.group'          : { feed: true },
  'notice.notify.poke'     : { feed: true, kind: 'poke', note: '拍了拍你…' },
  'notice.group_recall'    : { feed: false, kind: 'recall' },   // 只记录
  'notice.group_increase'  : { feed: false, kind: 'member' },
  'notice.notify.emoji_like': { feed: false, kind: 'reaction' },
  ...
}
describeEvent(event) → {key, kind, feed, chatId, userId, note}
toInboundEvent(event, desc) → 合成消息（只有 feed=true 才转）
countEvent(desc) → 每种事件来过多少次
```

bridge 入口只做三件事：归一化 → 计数 → 喂或不喂。**加新感知只改 ROUTES 一行。**

**验证**（注入 4 类事件）：

```
notice.notify.poke      → 进管线（[拍一拍] …拍了拍你）
notice.group_recall     → 事件只记录不下发：notice.group_recall
notice.group_increase   → 事件只记录不下发：notice.group_increase
notice.notify.emoji_like→ 事件只记录不下发：notice.notify.emoji_like
```

下一步：把 `countEvent` 暴露给面板/checkall（"这周有多少次撤回被她记下了"）。

---

## ② 统一发送收口 🟡（部分）

**现状核实**：QQ 出站目前**只有 bridge 一个真出口**（Python 侧不发 QQ，只写 `qq-inject.jsonl`）。
但"一个出口"只在 JS 内成立，缺点是：

- 限流/条数/审计/确认散在 `guardedConnectionCall` 里，Python 侧看不到
- 注入通道（`qq-inject.jsonl`）是旁路，重启会丢、也不区分测试流量
- 三个历史 bug（dry-run 假成功、告警"已告知"其实没发、条数不一致）都源于"发送这个动作被多处各自实现"

**最小可做**：bridge 内建一个 `send_gateway`（同一个函数出口），返回**真实 message_id**：

```js
sendGateway({ chatId, segments, source, dryRun }) →
  { ok, messageId, intercepted, dropped, reason }
```

所有内部发送（最终回复、interim、工具、主动消息、通知）都调它；Python 侧要发就用
`POST 127.0.0.1:<port>/send`（同机、无鉴权、只监听 loopback）。

**验收**：`grep -c "connection.call('send_msg'" lib/` 只剩 1 处（gateway 内部）。

---

## ③ 统一上下文装配器 ✅（2026-10-26 已上线）（部分）

**现状**：人设（persona.md）+ 平台规则（prompt.js 里 60 多行 `platform +=`）+ 记忆注入
（`mem_layers` 的摘要与 recall）+ 时间戳，分散在三处拼。缓存命中低、规则互相打架。

**已做的一半**：记忆注入已经收口到 `mem_layers.py`（摘要 + recall 一个入口），
稳定前缀/变化内容的顺序问题在 prompt.js 里还是散着。

**已实现**：

1. `prompt.js` 顶部加「规则优先级」，**冲突时的判定只在这里定义一次**：
   `通道安全 > 人设 > 平台规则 > 动态内容`
2. 动态内容前插分界线：`[以下为动态内容·每轮可能变化，冲突时以上面为准]`
   （上面只随版本变；下面是摘要 + 核心记忆 + 时间戳）
3. `ctx_hash.py`：读最近几轮 system 提示，取分界线之前的哈希比对 —— **可测，不靠感觉**

实测（打补丁后连续 3 轮）：

```
轮  总长    稳定段   动态段   前缀哈希        分界线
4   13429  12009   1420    445f8c0b6a69   有
5   13429  12009   1420    445f8c0b6a69   有
6   13429  12009   1420    445f8c0b6a69   有
[OK] 稳定前缀跨 3 轮完全一致，缓存可以命中
```

顺带查清一个事实：**时间戳早就在末尾**（bridge 里 `parts.complete + header`），
所以"命中率 24%"不是它造成的。真正的问题是**没有任何东西在验证前缀是否稳定** ——
现在有了，checkall 第 17 段会直接报 FAIL。

---

## ④ 能力/风险注册表 ✅（2026-10-26 已上线）（部分）

**现状**：`panel_api.py` 已有 28 个 def（四类：参数/开关/文本/操作），`toolmode.py` 有 chat/full 两档，
但没有"能力清单 + 风险分级"的统一表；新能力容易"写了但没接"。

**已实现** `capabilities.py`：31 项能力，每项登记

```
id / kind(read|write) / risk(low|medium|high) / mode(chat|full) / default / file+marker
```

`file+marker` 就是"接对了没有"的判据——能在那个文件里找到那个标记，才算接线成功。
（"代码在 ≠ 接对了"：这个项目里已经踩过两次，补丁写了但锚点不匹配。）

```bash
python3 capabilities.py            # 表格 + 统计
python3 capabilities.py --check    # 只校验（未接线返回 1）
python3 capabilities.py --json     # 给面板用
```

实测：**31 项：read=11 write=20｜high=2 low=22 medium=7｜未接线 0**

checkall 第 16 段直接调它，未接线项当场 FAIL。

---

## ⑤ 统一调度器 ✅（2026-10-26 已上线，真机验证待登录）

**现状**：`proactive.py` + 一堆 systemd timer + `qq-inject.jsonl` 混着用。
已经改掉的是**日记**：`diary_outband.py` 带外触发，不再伪装成主人消息。

**剩下的**：主动私聊、空间说说、B 站窗口仍走 `qq-inject.jsonl`，本质还是"任务变成一条用户消息"。

**已实现**：

```
scheduler.py（每 10 分钟，systemd timer）
   ├─ decide()  按 routines.json 的时间窗/配额/空闲产出意图 → memory/intents.jsonl
   └─ run()     消费 state=new 的意图：
                  chat.initiate → 生成一句话 → qq-outbox.jsonl
                  diary.write   → diary_outband.py
                  qzone.post    → 空间脚本

qq-outbox.jsonl → bridge 的 startOutbox() → guardedConnectionCall
                  → 结果写 qq-outbox.result.jsonl（含真实 message_id）
```

三条约束都落地了：
1. **意图不变成用户消息**（走 outbox，不走 qq-inject）
2. **Python 也过闸门**（限流 / dry-run / 审计 / 真实 id）
3. **失败可重试**（发送失败回退读位 + 60 秒退避，不会"消费掉但没发出去"）

旧的 `proactive.timer` 已停用，`scheduler.timer` 接管。

踩到的两个坑（都写进代码注释了）：
- **TDZ**：`startOutbox` 接在 `const bridge` 之前 → `Cannot access 'bridge' before initialization`，插件静默失效
- **启动即消费**：WS 还没连上就读走队列 → 消息丢失；改成"失败回退读位"

checkall 第 18 段：scheduler 是否在跑、旧 timer 是否残留、待办意图数、outbox 24h 成败。

---

## ⑥ 统一备份与迁移 ✅（2026-10-26 已上线）

**现状核实**：`/opt/dsh` 下 113 个备份痕迹、53 个 `.bak-*`，还有 `switch-backups/`、`dupes/`、
`vendor/lib-before-rebuild/`。迁服务器时极易漏掉某个 `index.json` → 她"失忆"。

**已实现** `backup.py`：

```
DATA（默认打包，12 项 ≈ 30KB）
  memory/diary/           日记正文 + index.json（遗忘曲线的 hits 在这里）
  memory/knowledge.json   事实库
  memory/session_log/     历史会话摘要
  memory/session_summary.md, notes.md, layers_state.json
  memory/bili-watched.jsonl
  limits.json, media/onebot/persona.md, workspace/AGENTS.md,
  profiles/web/cordis.patch.yml, qwen.env
可选
  --with-sessions   会话原文（37M）
  --with-stickers   贴纸库（2.8G，可重下）
可再生（明确不带）
  search.db / vision_cache / *.log / schedule-state / qqguard.json /
  node_modules / vendor/fork-* / __pycache__ / .bak-* / backups
```

- 每次输出 `backups/backup-<时间戳>.tar.gz` + `MANIFEST-<时间戳>.txt`（每个文件带 sha256）
- 自动只留最近 20 份
- `backup.timer` 每天 04:10 跑

**验收（已过）**：

```
打包 12 项 → 30K tar
--verify → 清单里 11 个文件 / 1 个目录，[OK] 全部校验通过
```

---

## 建议顺序

1. ② 发送收口（消除一整族 bug，且现在只有一个出口，改起来最便宜）
2. ⑥ 备份清单（成本最低、风险最高的一项：迁机器丢数据不可逆）
3. ③ 上下文装配（缓存与规则冲突）
4. ④ 能力注册表（配合面板）
5. ⑤ 调度器（把主动私聊也搬出注入通道）

① 已完成，可以直接作为 ②⑤ 的地基：事件进来了，出口收口了，中间才有地方放意图。
