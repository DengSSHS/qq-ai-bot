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
## v0.9.41 — 2026-11-09 · 主动搭话终于有真实依据（天气/时间/最近聊的/看过什么）

用户的原话：

> 我认为现在做这个是有点问题的。主动搭话，他只会机械式地回复，而不会有自己的思考，
> 也不是今天真正的什么天气。他只是随便给你随手发一句机械式的话。

这是结构问题，不是措辞问题。查下来：`scheduler.decide()` 产出的 hint **全是写死的模板**，
`gen_line(hint)` 照着编，里面没有任何真实世界的数据——所以她只能说"今天也要好好过"这类空话。

### 新增 `context_pack.py`

每次主动搭话前，先把**真实素材**拼进提示：

| 素材 | 来源 |
|---|---|
| 真实时间、星期 | 本机时钟 |
| **湘潭真实天气**：现象/气温/体感/今日温差/降水概率/湿度 | open-meteo（免 key，实测 9.8°C、体感 5.8°C、晴） |
| 他最近说过什么 | 从 session 里取 user 角色最后 3 条；**没有就明说"他没跟你说什么"** |
| 最近看过什么 | `memory/bili-watched.jsonl` 的标题 |
| 今天日记写没写、上次落尾 | `memory/diary/` |
| 她自己的状态（困/饿） | `body_state` |

并且明确加了一句要求：**"只能用这里面的真实信息说话；没有具体依据就宁可不发，不要凑话。"**

### 接入与实测

`scheduler.run()` 的 `chat.initiate` 分支：`hint = context_pack.build() + hint`。

dry-run 生成的一句话：

```
外面体感才五度多，主人要是出门没加衣服，回头冻得直哆嗦可别怪鲸鲸没提过哦。
```

"体感五度多"来自真实接口（5.8°C），不是模板。

### 顺手修的

`weather_code=0`（晴）被 `int(x or -1)` 吃成 -1，显示"天气不明"。改成显式判 None。

## v0.9.40 — 2026-11-09 · 她每 90 秒回一次「不刷」——nudge 每分钟都在叫
