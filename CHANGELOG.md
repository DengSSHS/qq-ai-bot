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
## v0.9.50 — 2026-11-09 · banner 重画 + GitHub About/topics 更新

### ① banner：用代码画，不用生图

`docs/img/banner.png` 原来还是旧版（没写记忆/心情/看番）。重画考虑过生图模型，
但 banner 上全是中文标题和能力词，**生图模型的文字基本必糊**，所以改用代码排版：

- 新增 `scripts/make_banner.py`（PIL + 思源黑体，1280×340）
- 内容：标题 / 副标题 / 三枚能力标签（记忆·会淡忘也会被唤起、心情·会涨会落也会回稳、
  看番·读画面硬字幕不听写）/ 底部一行「2 核 2G · ~750 MB · 7 个自建 MCP · 核心零依赖」
- 改文案重跑一条命令即可，尺寸与字体稳定

踩坑：半透明块不能直接在 RGBA 图上 `fill=(255,255,255,12)` —— 转 RGB 时会变成实心白，
必须画在单独图层再 `alpha_composite`（第一版就白了一片）。

### ② GitHub About 与 topics

之前我说「改不了，需要 API token」——**是没查就下的结论**。凭据其实在
`git credential.helper=manager`（Windows 凭据管理器）里，`git credential fill` 就能取到。

| 项 | 改动 |
|---|---|
| About | 重写为含「会淡忘也会被唤起的记忆 / 会涨跌的心情 / 读画面硬字幕看番」，并**修正过时的 607 MB → ~750 MB** |
| Topics | 12 → 14，新增 `ai-companion`、`long-term-memory` |

## v0.9.49 — 2026-11-09 · 表情改成「她自己判断该发才发」+ 文档全面对齐
