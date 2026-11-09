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
## v0.9.45 — 2026-11-09 · 撤掉「600 字报告」：太薄，而且省错了地方

用户看到报告样例（ep7 第 3 条主线）后判断：

> 这太简洁太少了吧，那还是没必要了

我认。两条理由都成立：

1. **600 字对"她真的看过这集"来说太薄**——主线压缩掉了对白细节、语气、具体说了什么，
   而这些正是她评论时该用的东西。
2. **我算错了账**：当初拿报告当省钱手段，但整集字幕只有 **4k token**，
   真正贵的是 OCR（198k）。报告只省了主模型一点点上下文，代价是丢细节，不划算。

### 撤掉的东西

| 项 | 处理 |
|---|---|
| `bili_ep_report` 工具 | 从 `bili_hub` 删除（工具数回到 19） |
| `bili_ep_report.py` | 删文件 |
| 已生成的 `*.report.md` | 删除 |

### 保留的东西

**OCR 的 70% 省钱留着**，因为它不损内容：只是把字幕带缩放从 1280 降到 640
（94 vs 274 token/帧，文本 15/17 一致）+ 画面未变跳过（19% 帧）。
ep7 实测 199k → 60k。

代价仍然如实记着：字幕条 347 → 331，少的 16 条多是极短行。

**心情系统**也留着（和报告无关）。

## v0.9.44 — 2026-11-09 · 心情 + 省钱：OCR 砍 70%，报告压到 5%
