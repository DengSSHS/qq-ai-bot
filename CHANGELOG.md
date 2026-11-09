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
## v0.9.44 — 2026-11-09 · 心情 + 省钱：OCR 砍 70%，报告压到 5%

用户圈定两件事：心情（不装插件、直接做进 context_pack）和子代理省钱。

### ① 先纠正我自己的账

我之前说「一集 OCR 198k token」——那是 **OCR 的成本**，不是主模型读稿的成本
（整集字幕只有 4k token）。所以该省的是 OCR 那一头。

实测两种省法（ep4，字幕密集段 21 帧）：

| 做法 | token/帧 | 文本一致 |
|---|---|---|
| 1280 宽（原来） | 274 | 基准 |
| **640 宽** | **94** | 15/17 一致 |
| 画面未变就跳过 | 省 4/21 帧（19%） | —— |

两项都落进 `bili_watch_subtitles.py`（`--width` / `--dup-diff`）。

ep7 整集复测：**199k → 60k（省 70%）**，跳过 78 帧。
代价如实记：字幕条 347 → 331，少了 16 条；文本差异多是「RiNG/RING」「もー/もう」这种。

### ② 报告：便宜模型读全文，主模型只读 600 字

新增 `bili_ep_report.py`（抄 dsh-bili-subagent 的"子代理去看、伙伴来说"思路，
但不装插件，用同一个网关）：

```
ep7 字幕 10891 字 → 报告 522 字（4.8%），7922 token，4 秒
```

报告结构：主线 3–5 条 / 名场面+时间点 2–3 条 / 情绪走向 / 值得跟他提的点。
接成她的工具 `bili_ep_report(ep)`：没有就现生成，有就直接取。

样例（ep7 第 3 条）：

> 登台后灯声音放不开、演出不顺，爱音一度说"抱歉请重来"，灯最终唱出《春日影》，情绪爆发完成演出。

### ③ 心情：每轮判一次，没人聊就回落

新增 `mood.py` + `mood.timer`（每 5 分钟）：
- 值域 0–1，0.5 平静；每次判定 = 一次很短调用（1270 token）
- 只用最近 1500 字的对话片段，判 JSON：`{delta, label, why}`
- 没人聊时每小时往 0.5 回落 6%——不会永远记着一次高兴或一次生气
- 存 `memory/mood.json`（带最近 20 条历史，能回看为什么涨跌）
- `context_pack` 注入：「你此刻的心情：疲惫（0.40/1，有点低，重复图片刷屏…）」，
  且低于 0.35 时加一句「别硬装开心」

首次判定就把今晚的事看对了：

```
心情 -0.10 → 0.40（无奈：重复图刷屏，略显疲惫）
```

## v0.9.43 — 2026-11-09 · 抄了 dsh-speak-up 的「命中线」
