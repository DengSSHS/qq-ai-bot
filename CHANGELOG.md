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
## v0.9.48 — 2026-11-09 · 她一张表情都没发：`stickerNudgeRate` 被设成 0

用户问「为什么她不准发表情包」。查下来不是权限、不是路径、不是工具缺失：

### 证据链

```
表情库          /opt/dsh/media/onebot/stickers  4202 个文件 / 4198 条目  ✅
路径白名单      media/onebot 下                  ✅（realpath 校验通过）
挑图            stickerlib.pick() 正常返回真实 id  ✅
工具            qq_send_sticker / qq_random_sticker 都在插件里  ✅
24h 发送审计    send_msg 1162 次、qq_send_sticker **0 次**  ❌
```

### 根因

`cordis.patch.yml` 里：

```
stickerNudgeRate: 0  # 2026-10-26 表情改成她自主决定，不再靠概率提示
```

这个值控制「每轮在消息尾部附一句『这条要不要配张表情』」的概率。设成 0 = 提醒永不出现。
所以 10-26 那次「改成她自主决定」的实际效果是：**软要求 + 没有提醒 = 从不发生**。
人设里那句「尽量先甩一张表情」拦不住她——她每轮都在忙别的。

### 改

`stickerNudgeRate: 0.5`（一半的轮次会提醒）。提醒文案本身写得不错：

> （顺手想一下：这条要不要配张表情？三成左右该配一张。拿不准就配——qq_random_sticker() 一次调用就能发出去。）

### 这类问题的通用教训

「让她自主决定」在没有**每轮可见的推力**时，等于「让她永远不做」。
和之前 nudge 刷屏是同一个硬币的两面：**同一个配置项，调 0 是静默失效，调大是刷屏**。
以后改这类"概率/开关"配置，两头都要验：既看它会不会吵，也看它是不是根本没动静。

## v0.9.47 — 2026-11-09 · 图扩散落地 + 日记自动入库
