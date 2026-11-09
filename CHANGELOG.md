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
## v0.9.42 — 2026-11-09 · 地区由她主动问、问到就实时更新；话题从记忆里找

用户补充两点：
> 应该在这里面写一个主动询问地区，在那边更新后，然后这也是实时的，同时也会根据记忆找话题。

### ① 地区：她问、她记、立刻生效

新增 `region.py` + 记忆服务的两个工具：

| 工具 | 作用 |
|---|---|
| `get_region` | 查现在记的是哪座城市 |
| `set_region(city)` | 他说「我在长沙」就记下来，天气立刻按新城市算 |

细节：
- **中文地名不走外部 geocoding**（实测「湘潭」会被解析到安徽「湘潭」），
  改成内置 45 个常用城市表，查不到才用 geocoding，且只接受 `country_code=CN`
- 存 `memory/region.json`；`set_region` 会顺手作废天气缓存，**下一次取就是新城市**
- 没设置地区时，素材包会明确写：「地区：还不知道他现在在哪个城市……
  **可以顺口问一句「你现在在哪个城市 / 回家了没」**，问到了用 set_region 记下来」
- 素材包的要求里也加了一条：**挑不出具体话题、又没问过地区，就问地区**

### ② 话题：从记忆里长出来

`memory_topics()` 从 `memory/diary/`（最近两天的 `-` 条目）+ `memory/notes.md`（小标题）
捞可接着聊的东西，去重后取最近 3 条，写进素材包：

```
记忆里能接着聊的：笔记：群里印象深的人 / 笔记：反复出现的梗 / 笔记：B站：MyGO 正片与二创的区别
```

### 实测

```
get_region  → 湘潭（27.83, 112.94）
set_region(长沙) → 记住了：你在 长沙
get_region  → 长沙（28.23, 112.94）        ← 实时生效
```

dry-run 生成的一句话：

```
外面体感才五度多，主人那边要是也这么冷，就别硬撑着开窗了。
```

"体感五度多"来自真实接口；「主人那边」这种说法是因为地区还是我问的时候记的——
他一说清自己在哪，就会变成「长沙那边」。

## v0.9.41 — 2026-11-09 · 主动搭话终于有真实依据
