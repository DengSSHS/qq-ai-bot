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
## v0.9.51 — 2026-11-09 · 给 B 站动作加风控护栏（这次是我把它玩坏了）

事情经过：为了核对一集字幕，我把同一个 `playurl` 反复重跑了五遍（改写 → 重跑 → 再重跑），
加上频繁重启服务，**撞上了 B 站风控**。主人只能关机、实名解除，重新登录。

这不是"运气不好"，是我的操作方式问题：**把线上账号当测试环境用**。

### 三道闸（`bili_hub` + `bili_watch_subtitles.py` 各一份）

| 闸 | 规则 | 为什么 |
|---|---|---|
| 同集冷却 | 同一集 **45 分钟**内只许动一次 | 重复 `playurl` 是最直接的风控触发点 |
| 失败退避 | 连续失败 3 次 → 这一集冷却 **3 小时** | 失败后连着重试就是在刷 |
| 开机宽限 | 开机 **15 分钟**内不动 B 站 | 刚登录就被刷请求，最容易被判异常 |

外层（`bili_hub.watch_episode`）与脚本内各写一份：脚本那份防的是"绕过工具直接跑脚本"。
被闸下来时给出的是**具体原因和还要等多久**，不是一句"不行"——这样她不会换个法子重试。

工具说明里也写死了：**「被拦下来就等着，不要换着法子重试。」**

### 我自己要改的习惯

- 验证改动时**不拿线上账号反复试**：能用本地/离线数据验的，就不去打真实接口
- 说"重跑一次"之前先看这条 45 分钟的闸——同一集一天最多动几次，心里有数
- 频繁重启服务本身也是可疑行为，能攒着一起重启就别零零碎碎重启

## v0.9.50 — 2026-11-09 · banner 重画 + GitHub About/topics 更新
