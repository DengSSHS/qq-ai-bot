# 主动说话（群聊接话 + 私聊找茬）

两个机制，逻辑完全不同，别混：

| | 触发方式 | 概率 | 上限 | 会不会延续 |
|---|---|---|---|---|
| **群聊接话** | 每一条没 @ 她的群消息 | 固定 20% | 每小时 3 次，均匀分布 | **不延续**，接完就结束 |
| **私聊主动** | 定时检查（每 10 分钟） | 随时间累积 | 每日受 dailyBudget | 正常对话 |

## 群聊接话

改在插件入站管线的 @ 门槛上（`inbound.js`）。原本未 @ 的群消息被直接丢弃，
现在多一道概率：

```
未 @ 她 + 不在激活窗口
  → 掷骰：20% 命中 → 当作"她想接一句"，继续走完管线
  → 80% 未命中 → 照旧丢弃
```

三个关键约束：

1. **不写入激活窗口**（`groupActiveUsers`）。接住的只有当前这一条 ——
   说完就结束，那个群友下次说话仍需 @ 她。这样她就不是"掺进对话里"，
   而是"插一句就走"。
2. **同群冷却 60 秒**。群里连着刷三条消息，不会三条都被接。
3. **主动插话轮会带一个标记**：

```
[主动插话] 这条没人 @ 你，是你自己看到想接一句。先看上面最近的群聊内容
（不够就调 qq_group_history 拉最近 10 条），再简短接一句就好，别追问、
别展开话题、别把话头接成长对话。
```

配置（`deploy.env` 不涉及，直接在 profile 的 `cordis.patch.yml` 里）：

```yaml
casualEngageRate: 0.2          # 0 = 关闭
casualEngageCooldownMs: 60000  # 两次接话的最小间隔
casualEngageMaxPerHour: 3      # 每小时上限
```

**为什么要有小时上限**：纯 20% 逐条掷在活跃群会扎堆——一天 100 条消息就是
约 20 次接话，而且可能集中在前半小时。所以把一小时切成 `maxPerHour` 个时间格，
**每格最多用一次**：配额是天花板，20% 决定这一格要不要用。

实测：同一格里连发 3 条 → 只接 1 次。

## 私聊主动

走 `proactive.py` 的累积型 routine（`routines.json` 里的 `casual_dm`）：

```
概率 = rampPerHour × (1 + 超出 minIdle 的小时数)，封顶 rampCap
默认：每小时 +20%，封顶 80%，要求空闲 ≥ 60 分钟
```

**这是"累积"不是"纯随机"**：同一小时内用小时桶哈希掷一次，
所以「1 小时 20%、2 小时 40%、3 小时 60%」是真的在涨；
不是每 10 分钟重投一次骰子。

窗口 08:30–23:30，静默时段 01:00–07:30 绝不打扰，每日总量受 `dailyBudget` 限制。

## 踩过的坑

### 1. 配置键被白名单吞掉

插件把配置传给 pipeline 时要经过 `gateConfig` 白名单：

```js
const gateConfig = {
    dedupWindowSeconds: config.dedupWindowSeconds,   // 只挑这几个
    actionRatePerMinute: config.actionRatePerMinute,
    ...
};
```

加了 schema 还不够——**不在这个对象里的键，pipeline 读到的永远是 undefined**。
表现是概率门静默失效（`rate=undefined` → 条件不成立 → 行为退回原样）。

### 2. 占位符没替换，服务静默失败 229 次

`proactive.py` 里 `OWNER = __OWNER_QQ__` 一直没被替换，于是
`proactive.service` 每 10 分钟 `NameError` 一次，**连续失败 229 次**。
影响：日记提醒（morning/noon/evening/night）从 10-22 之后全部没触发，
所以那两天她没有日记。

`install.sh` 本来有统一替换（遍历 `$DSH_HOME/*.py`），是这台机器更早的安装
没跑那段。教训：**新装完 `systemctl status` 看一眼有没有在失败**，
`list-timers` 显示 active 不代表它跑得通。

### 3. 四处改动都在 node_modules 里

`prompt.js` / `bridge.js` / `inbound.js` / `index.js` 全是插件文件，
`npm install` 或升级就丢，丢了不报错。

用 `stack/apply_local_patches.py` 一键重打（幂等，带 `node --check` 自检）：

```bash
python3 /opt/dsh/apply_local_patches.py          # 打
python3 /opt/dsh/apply_local_patches.py --check  # 只看状态
```

## 调参建议

20% 在活跃群会偏高：一天 100 条群消息 → 约 20 次接话。如果觉得吵，
先调 `casualEngageRate: 0.1`，或把冷却改成 180000（3 分钟）。

---

# 附：她能"看见"表情吗（实测）

## 两种表情，走两条路

| 类型 | 传输形式 | 她看到什么 |
|---|---|---|
| Unicode emoji（😄🥵🔥） | 文本的一部分 | 原样保留 |
| QQ 小黄脸（`face` 段） | 只有数字 id，没有字 | 插件按表映射成 emoji |

实测（@她 + 笑 + 赞 + 未知 id + Unicode）：

```
她收到的原文：
@3600398389 四个表情测试：😂👍[表情] 还有Unicode 🥵🔥
```

- `face id=2` → 😂
- `face id=121` → 👍
- `face id=99999`（表里没有）→ `[表情]`
- Unicode 原样保留

## 修掉的坑：未知 id 兜底成笑脸

原来：

```js
export function faceToEmoji(id) {
    return FACE_EMOJI[id] ?? '😀';    // 不认识的就当笑脸
}
```

`FACE_EMOJI` 只收了两百来个（QQ 实际有几百个），剩下的一律变 😀。
后果是**她会看到不存在的笑** —— 比如别人发个哭泣的表情，她读成笑脸，
然后回一句完全不对的话。

现在未知 id 返回 `[表情]`：宁可老实说"这里有个我认不出的表情"，
也不要猜错情绪。

## 映射表本身是准的

抽查过：`1=😁 2=😂 5=😄 6=😅 66=😡 74=🥳 121=👍 124=👏 110=❤️`
都对。所以日常小黄脸没问题，只有冷门表情会落到 `[表情]`。
