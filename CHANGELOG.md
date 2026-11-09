# 更新日志

本项目按「改了什么 + 为什么」记录，不按版本号堆功能。所有日期为实际改动日期。

## v0.9.39 — 2026-11-09 · 找到闸门源码：workspace 只是「管理员回合」才能读

她一直报同一句，我这次去读了断言本身的实现，而不是继续搬目录。

`dsh-onebot-qq/lib/bridge.js → mediaSendRoots(sessionId)`：

```js
const candidates = [this.deps.config.mediaDir];          // 永远允许
if (chatId !== undefined && isTurnAdmin)
    candidates.push(this.effectiveCwd(chatId));          // 仅当本回合发起者是管理员
```

也就是说：

| 根 | 什么时候允许 |
|---|---|
| `mediaDir`（= `/opt/dsh/media/onebot`） | **永远** |
| `workspace` | **只有管理员回合**，且会话能映射到 chatId |

我前几轮一直让她读 `workspace` —— 她的回合只要不是管理员身份，就必然被拒，
所以每次都是"一模一样"的错。**不是文件没放进去，是那条路径本身就带条件。**

### 改

抽帧输出**主副对调**：

```
主输出（返回给她的路径）  /opt/dsh/media/onebot/bili-frames/ss<season>/ep<N>/
副本                     /opt/dsh/workspace/bili-frames/ss<season>/ep<N>/
```

### 按闸门算法自检

照 `resolveContainedPath` 的 realpath 前缀匹配，用**唯一无条件根** `mediaDir` 验：

```
media/onebot/bili-frames/ss73077/ep2/t00851.jpg  通过
media/onebot/bili-frames/ss73077/ep2/t00877.jpg  通过
```

## v0.9.38 — 2026-11-09 · 她说图还在 /opt/tulpa/.../grab/：那是重启前的旧文件
## v0.9.46 — 2026-11-09 · 记忆按「妹居物语」那套逻辑重做（不做图）

用户看了 BV17mWJ6LEYc（妹居物语的记忆系统）后说：**主要逻辑是那个就行，不用输出图片**。
所以只实现逻辑，不碰任何可视化。

### 那套逻辑是什么（视频原话拆解）

| 环节 | 做法 |
|---|---|
| 写入 | 每次聊完**用自己的话**写一条记忆，并打**重要性分数** |
| 检索 | 一开口按**语义**找最接近的 20 个点 |
| 排序 | 按**相关 / 新鲜 / 重要**三项打分，取前 **5** 条注入 |
| 淡忘 | 不重要的小事慢慢淡忘 |
| 加鲜 | **每被想起一次，那段记忆就重新变清晰** |

### 为什么用 mem0 而不是自己写

搜了一圈成熟方案，按我们的机器（2 核 1.9G、无 GPU、要中文）筛：

| 方案 | 判断 |
|---|---|
| **mem0**（68k★，Python） | ✅ 装上了，193MB（没拉 torch），独立目录 `/opt/mem0lib` |
| graphiti（32k★） | ❌ 要 Neo4j |
| LightRAG（40k★） | ❌ 是文档 RAG，不是聊天记忆 |
| cognee（33k★） | ❌ 要 lancedb + transformers，2 核跑不动 |
| dsh-layered-memory | ❌ TS 插件，和我们 fork 过的 onebot 冲突 |

### 实测（都是真跑出来的）

```
写入   193MB 依赖，初始化 0.5 秒
抽事实  中文正确：「主人喜欢玩《钢铁雄心4》（钢4），并且经常熬夜」
检索   「他玩什么游戏」→ 钢4 排第一
存储   48–88KB（qdrant 本地）+ 12KB（记账）
```

**两个坑**：
1. 默认抽出来是**英文**（"User loves playing..."）；必须用 `add(prompt=中文模板)` 覆盖 —— mem0 的 `custom_fact_extraction_prompt` 配了也没用
2. 默认往 **PostHog 发遥测**；`MEM0_TELEMETRY=False` 关掉

### 加鲜是怎么实现的

视频里那条「被想起就变清晰」mem0 没有，我在自己的 SQLite 里记账：

```
final = 0.55×语义 + 0.22×重要性 + 0.18×新鲜度 + min(0.12, 0.05×ln(1+次数))
新鲜度 = 0.5^(天数/30)      # 30 天半衰期
```

实测复现效应：

```
第一次召回 0.594 → 二次 0.629（被想起 1 次）
「他玩什么游戏」→ 钢4 那条 0.690（被想起 2 次，升到第一）
```

### 她多了两个工具

| 工具 | 作用 |
|---|---|
| `mem_remember(text, importance)` | 值得记的事 → 自动抽成中文短句存起来 |
| `mem_recall(query, n)` | 按意思回忆，返回排序后的事实，并记账 |

### 仍然没有的（如实说）

**图结构**（人/物/事件/概念连成网、沿图走几步）——mem0 的 graph 要 Neo4j，我们扛不住。
所以「你只说了面试和下雨，我却想起火锅」那一跳，现在还是做不到，只有同义/近义召回。

## v0.9.45 — 2026-11-09 · 撤掉「600 字报告」
