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

## ③ 统一上下文装配器 🟡（部分）

**现状**：人设（persona.md）+ 平台规则（prompt.js 里 60 多行 `platform +=`）+ 记忆注入
（`mem_layers` 的摘要与 recall）+ 时间戳，分散在三处拼。缓存命中低、规则互相打架。

**已做的一半**：记忆注入已经收口到 `mem_layers.py`（摘要 + recall 一个入口），
稳定前缀/变化内容的顺序问题在 prompt.js 里还是散着。

**最小可做**：`buildContext()` 返回固定结构：

```
[稳定前缀] 人设 + 工具 schema + 平台规则（内容只随版本变）
[每轮变化] 时间戳 + 记忆摘要 + 检索片段 + 最近历史
```

**验收**：同一会话连续 3 轮，前 3 段的 hash 完全一致（缓存能命中）。

---

## ④ 能力/风险注册表 🟡（部分）

**现状**：`panel_api.py` 已有 28 个 def（四类：参数/开关/文本/操作），`toolmode.py` 有 chat/full 两档，
但没有"能力清单 + 风险分级"的统一表；新能力容易"写了但没接"。

**最小可做**：一张 `capabilities.toml`（或 `panel_api.py` 里的 REGISTRY 常量）：

```toml
[[cap]]
id = "qq.emoji_like"
kind = "write"          # read | write
risk = "low"            # low | medium | high
default = true
mode = "chat"           # chat | full
verify = "checkall:tool_qq_emoji_like"
```

**收益**：面板直接渲染；checkall 能校验"注册的能力是否真的可用"；
新增能力不会静默失效（`verify` 字段跑一遍）。

---

## ⑤ 统一调度器 🟡（方向已对，形态未到）

**现状**：`proactive.py` + 一堆 systemd timer + `qq-inject.jsonl` 混着用。
已经改掉的是**日记**：`diary_outband.py` 带外触发，不再伪装成主人消息。

**剩下的**：主动私聊、空间说说、B 站窗口仍走 `qq-inject.jsonl`，本质还是"任务变成一条用户消息"。

**最小可做**：`scheduler.py` 只产出**意图**：

```json
{"kind": "diary.write", "reason": "noon", "budget": "low"}
{"kind": "chat.initiate", "reason": "idle 3h", "chat": "private:1709392682"}
```

意图由执行器消费：`diary.write → diary_outband.py`、`chat.initiate → 生成内容 → send_gateway`。
**意图绝不直接变成用户消息。**

---

## ⑥ 统一备份与迁移 ⬜

**现状核实**：`/opt/dsh` 下 113 个备份痕迹、53 个 `.bak-*`，还有 `switch-backups/`、`dupes/`、
`vendor/lib-before-rebuild/`。迁服务器时极易漏掉某个 `index.json` → 她"失忆"。

**最小可做**：`backup.py`

```
数据（必须带）: memory/diary, memory/knowledge.json, memory/session_log,
                media/onebot/persona.md, media/onebot/stickers/, limits.json,
                workspace/AGENTS.md, profiles/web/cordis.patch.yml, limits 相关
可再生（不用带）: search.db, session_projcache, __pycache__, .bak-*, vendor/fork-*
输出: 一个 tar.gz + MANIFEST.txt（含文件数/校验和/生成时间）
```

**验收**：`python3 backup.py --verify <tar>` 能在干净目录里还原出三层记忆并跑通
`mem_layers.py status`。

---

## 建议顺序

1. ② 发送收口（消除一整族 bug，且现在只有一个出口，改起来最便宜）
2. ⑥ 备份清单（成本最低、风险最高的一项：迁机器丢数据不可逆）
3. ③ 上下文装配（缓存与规则冲突）
4. ④ 能力注册表（配合面板）
5. ⑤ 调度器（把主动私聊也搬出注入通道）

① 已完成，可以直接作为 ②⑤ 的地基：事件进来了，出口收口了，中间才有地方放意图。
