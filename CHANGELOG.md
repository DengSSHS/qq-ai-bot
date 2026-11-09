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
## v0.9.43 — 2026-11-09 · 抄了 dsh-speak-up 的「命中线」，替掉我那个会刷屏的掷骰子

用户贴来一整个 DSH 插件生态（Yuuyuko-uu），让看看有没有能用的。逐仓读了 README。

最该抄的一条：**主动搭话用「命中线」而不是「每天固定掷一次骰子」**。

| | 我原来的 | speak-up 的 | 抄过来之后 |
|---|---|---|---|
| 判据 | `roll(按天固定) < prob` | 随机数 vs 累积命中线 | 随机数 vs 命中线 |
| 没中时 | 什么都不变（今天恒为 0.11 → 一直中）| 命中线 +0.1（越来越容易开口）| +0.1，封顶 0.95 |
| 命中后 | 不变（所以每分钟都中）| 回到 0.5 | 回到 0.5 |
| 久不说话 | 无 | >6h 命中线 ×1.5 | ×1.5，封顶 0.98 |

我那个版本的根本缺陷是「roll 一天一个值」+「命中后不重置」，所以一次命中就等于永远命中——
19:41–19:59 连叫 63 次就是这么来的。命中线模型天然自限：中了就回到初始线，没中才慢慢变容易。

### 其它仓库的评估（都读了 README）

| 仓库 | 能不能用 | 判断 |
|---|---|---|
| dsh-speak-up 主动搭话 | **抄设计，不装** | 它要 Web UI 设置页；而且会和我们的 scheduler 抢着说话 |
| dsh-bili-subagent 刷B站子代理 | **抄思路** | 「子代理 + 便宜模型看，伙伴用自己的口吻说」很对；我们的 `tool-subagent` 是禁用的，要用得先开 |
| dsh-feiyu-mood 心情 | 可抄 | 每轮用短调用判断心情涨跌、会回落；我们现在只有按时间算的 body_state |
| dsh-fish-memory 鱼的记忆 | 不需要 | 和我们的 memory_server + mem_rank 衰减重复 |
| dsh-qq-link 通到QQ | **不要装** | 和我们的 onebot fork 重复，会双发 |
| dsh-xingxi 星图 | 现在没用 | 是记忆可视化 UI，我们没跑 Web UI |
| dsh-tts-bridge 朗读桥 | **有真价值** | 但要你电脑上开着 DeepSeek 页面 + Chrome 扩展，声音才从你音箱出来 |
| dsh-fish-buddy 鱼搭子 | 值得看 | 桌面 tkinter 挂件（语音/陪看字幕/游戏弹幕/截图），和你那个 Python 环境合得来 |

环境事实：我们跑的是 **DSH 0.2.1-rc.2**，没有 Web UI 在跑（只有 ttyd 和 NapCat 端口），
已装插件是 `dsh-onebot-qq`（我们打了 ~500 行补丁的 fork）、`local-memory`、`dsh-dream`。
所以依赖设置页的插件不是装上就能改配置的。

## v0.9.42 — 2026-11-09 · 地区由她主动问、问到就实时更新
