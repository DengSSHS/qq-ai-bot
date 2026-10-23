# qq-ai-bot · 最轻的 QQ 群聊 AI 机器人

一个住在 QQ 群里的 AI 角色：被 @ 才说话，有长期记忆、会看图、会上网、有作息。
跑在 **2 核 2G 的入门服务器**上，进程占用 607 MB，单轮回复 1~2 秒。
代码纯 Python 标准库，**零第三方依赖**。

**部署不用你懂**：把仓库交给你的 AI，让它读 `START-HERE.md`。你只需回答几个问题、扫一次码。

---

## 它是什么

```
QQ 群 / 私聊
    │
    ▼
NapCat（Docker，QQ 协议端）
    │  OneBot v11 反向 WebSocket
    ▼
DSH（Node，systemd 常驻）
    ├── dsh-onebot-qq 插件：接消息、限流、白名单、注入
    ├── 模型（任意 OpenAI 兼容接口）
    └── 6 个 Python 服务（stdio，按需拉起）
         ├── memory_server  日记 / 长期笔记
         ├── websurf        联网搜索 + 读网页
         ├── seer           看图
         ├── imagen         生图
         ├── admin          工具档位切换
         └── llm_shim       转发层：强制关思考
```

不是"接个大模型回消息"的玩具。花时间的是那些让它在群里**不像机器人**的部分：
说话节奏、不做主持人、不汇报操作、有记忆分层、有作息、知道自己什么时候该闭嘴。

## 实测

在 **Ubuntu 24.04.4 LTS (Noble Numbat)** / 内核 6.8 / x86_64 / 2 核 2G 的腾讯云轻量上跑了几个月：

| 项目 | 实测值 |
|---|---|
| 进程内存 | **607 MB**（Node 宿主 296 + 6 个 Python 服务 106 + NapCat 容器 204） |
| 整机内存 | 933 / 1967 MB（47%，含系统） |
| CPU | 空载 **95% idle**，负载 0.00 |
| 磁盘（不含表情库） | 约 2.3 GB |
| 单轮回复延迟 | **1~2 秒**（优化前 5.6~9.3 秒） |
| 每次请求输入 | 12~18k tokens（随会话长度变化） |

> **这些数字哪些能自己验**：前五项部署后一条命令就能核（`free -m` / `docker stats` /
> `du -sh` / 看 `memory/llm_shim.log` 的耗时行）。「跑了几个月」和「群聊像不像人」
> **只能信**——没有第三方证据，单作者、0 star。要用在生产上，自己跑两周再判断。

## 快速开始

**Windows 先装 [Git for Windows](https://git-scm.com/download/win)**（提供 ssh/bash/scp），
Linux/macOS 自带。然后把 AI 的工作区指到这个文件夹。

```bash
cp deploy.env.example deploy.env    # 填 6 项：两个 QQ 号、模型地址/模型名/key、人设模式
bash install.sh                     # 幂等，中断了再跑一次
bash verify.sh                      # 最后一行 VERDICT: READY 才算装完
```

中途会停下来要你扫码登录 QQ（脚本会自动抓二维码并等你）。扫完自动继续。

**没有能执行命令的 AI**（网页版 ChatGPT 不行）就照 `MANUAL.md` 手动做，里面每步都写了。

## 系统要求

| | |
|---|---|
| 操作系统 | **Ubuntu 24.04 LTS**（实测 24.04.4，内核 6.8）。22.04 没验证 |
| 架构 | x86_64 —— NapCat 镜像只有 amd64 |
| CPU / 内存 | 2 核 / 2 GB。1 核 1 GB 未验证 |
| 磁盘 | ≥ 20 GB（表情库按需自备） |
| 其他 | Docker、systemd（安装脚本会自己装） |

**不支持**：Windows 服务器（容器只有 Linux amd64）、ARM。

## 成本

「零依赖」省的是内存和升级麻烦，**不省钱**。两项开销：

| 项目 | 量级 |
|---|---|
| 服务器 | ¥60~120 / 月（国内 2C2G；海外 IP 更容易触发 QQ 风控） |
| 模型 token | 视单价与轮次，见下 |

```
月成本 ≈ 每轮 input tokens × 单价 × 每天轮次 × 30
例：14,000 tokens × ¥2/百万 × 50 轮/天 × 30 天 ≈ ¥42/月
```

**缓存别指望**：实测这条聚合线路的 prefix cache **命中率 0.0%**（相隔 16 秒、前缀只差 40 token
也是 0）。`llm_shim` 会逐轮记 `USAGE` 行，**换线路后自己跑两天**再判断有没有折扣。

**能压**：换便宜模型、压缩历史、砍工具 schema、关技能。
**压不下**：服务器月租。判断标准——**一个月超过 1000 轮对话，这套常驻方案才划算**。

## 为什么能做到这么轻

- **零第三方依赖**：2200+ 行 Python 全是标准库，不用 pip、不会因编译包吃爆内存
- **按需拉起**：6 个服务是 stdio 子进程，每个 20 MB 上下，不调用不占 CPU
- **图片不进上下文**：看图由服务端代看、只回文字。否则几张 base64 图片就能把输入撑到 43k tokens
- **关掉隐藏思考**：宿主的 `reasoning: "off"` 语义是"什么都不发"，网关却默认开思考。
  `llm_shim` 把 `thinking: disabled` 注入请求体——单次从 9 秒降到 2 秒

## 表情库

机器人有自己的表情库（默认空，自己往里放）。这套逻辑的完整设计说明在
**[docs/sticker-library.md](docs/sticker-library.md)**，这里只说结论：

- **挑图按热度加权**，不是纯随机——常用多挑、刚发过的压权重，像人有惯用表情
- **去重分两层**：sha256 精确重复直接拦；pHash（感知哈希）只提示不拦，
  因为实测它在表情包这类图上误判率太高（距离 ≤6 时有 5.74% 的误判）
- **入库前让模型判断**「这张适不适合转发」，拿不到判断就不入库
- `sticker_stats` 能看库存 / 用过多少 / 热度榜 / 场景分布

三个工具：`sticker_pick`（挑）、`sticker_ingest`（入库过筛）、`sticker_stats`（统计）。

## 能改什么

| 想改 | 改这个 | 生效方式 |
|---|---|---|
| 说话的样子 | `persona/*.md` → `/opt/dsh/media/onebot/persona.md` | `systemctl restart dsh-onebot` |
| 作息 | `/opt/dsh/routines.json` | 不用重启 |
| 模型线路 | `/opt/dsh/profiles/web/cordis.patch.yml` 的 `llm-pi-ai` 段 | 重启 |
| 谁能指挥它 | 同上，`allowFrom` / `adminUsers` | 重启 |
| 工具档位 | `python3 /opt/dsh/toolmode.py chat\|full` | 自动 |
| 搜索后端 | `/opt/dsh/qwen.env` 里加 `SEARXNG_URL`（自建 SearXNG，比解析 HTML 稳） | 重启 |

## 已知限制

**架构性**

- 只支持 Linux x86_64；`/opt/dsh` 路径写死在 20 多处（install.sh 会重写 systemd 单元）
- NapCat 默认 `:latest`（install.sh 会把实际 digest 锁进 `deploy.env`，想追新改回 `:latest`）

**测试覆盖**

- `checkall.py` 是**配置自检**（约 100 项，项数随配置浮动），保证"配置没被改坏"，
  **不保证"逻辑正确"**——改了衰减公式它发现不了
- `tests/` 只有 38 个纯函数测试：遗忘曲线、SSRF 地址判定、看图路径白名单、转发层切换
- **业务逻辑 0 自动化覆盖**：群聊策略、工具调度、记忆写入、人设遵从度，只能人验

**账号风险**

- NapCat 是社区协议端，腾讯风控随时可能踢号（本项目实例被踢过一次，需重新扫码）。
  **用小号，别用主号**
- 第三方协议端的风险无法从代码层解决

## 项目结构

```
START-HERE.md       AI 入口：30 秒指令
DEPLOY.md           AI 执行手册：每阶段验证点、故障速查、回滚
MANUAL.md           人看的：准备清单、装 Git、指工作区、扫码、验收
CHANGELOG.md        改了什么、为什么——含踩过的坑和自己改坏的经历
SECURITY.md         权限模型、暴露面、SSRF 防护、密钥管理
CONTRIBUTING.md     怎么改、怎么验、不许碰的边界
install.sh          幂等安装脚本（含日志轮转、digest 锁定）
verify.sh           15 项部署验收，输出 VERDICT
preflight.sh        约 70 项静态自检（不需要服务器，项数随环境略有浮动）
qr_wait.py          扫码等待器：自动提取二维码，登录后自动继续
deploy.env.example  参数模板
persona/
  TEMPLATE.md       人设骨架（{{占位符}}，按描述填）
  example-jingjing.md  示例角色（151 行，可直接用）
stack/              运行时主体：12 个 Python 服务/工具 + 配置模板 + systemd
tests/              79 个单元测试（纯函数 + 安全边界 + 解析兜底）
scripts/            preflight 的 Python 侧检查
.github/workflows/  CI：语法、密钥扫描、换行、占位符、单元测试
```

## 开发

四层验证，改代码至少跑前两层：

```bash
# 1. 单元测试（秒级，不需要服务器）
python3 -m unittest discover tests -v

# 2. 静态自检（文档与配置一致性）
bash preflight.sh

# 3. 运行层（在真实部署上）
bash verify.sh                  # 15 项，看 VERDICT
python3 /opt/dsh/checkall.py    # 约 100 项（浮动），0 失败才算配置没坏

# 4. 人工验收
在群里让它回一句话，确认行为和语气没退化
```

Python **3.8+**，零第三方依赖（`requirements.txt` 只有说明）。
改动规矩、硬约束、不接受的方向见 `CONTRIBUTING.md`。

## 安全

这套东西跑在公网可达的服务器上、能被群友说话影响。**机器人默认没有 shell、没有文件权限**，
这是设计不是缺陷。改任何涉及网络 / 执行 / 权限的东西之前，先读 `SECURITY.md`。

## 版本与许可

当前 `0.1.0`（见 `VERSION`），改动记录在 `CHANGELOG.md`。
MIT，见 `LICENSE`；第三方出处与角色形象版权说明见 `NOTICE.md`。
