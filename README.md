# qq-ai-bot · 最轻的 QQ 群聊 AI 机器人

> **一台 2 核 2G 的入门云服务器就够。** 整机进程占用 607 MB，CPU 空载 95% 空闲，
> 单轮回复 1~2 秒。纯 Python 标准库，**零第三方依赖**。

## 实测（不是估算）

在 **Ubuntu 24.04.4 LTS (Noble Numbat)** / 内核 6.8.0 / x86_64 / 2 核 2G 的腾讯云轻量上，跑了几个月的真实数据：

| 项目 | 实测值 |
|---|---|
| **进程内存** | **607 MB**（dsh/Node 296 + Python MCP×6 106 + NapCat 容器 204） |
| 整机内存 | 933 / 1967 MB（47%，含系统与其他） |
| **CPU** | 空载 **95% idle**，负载 0.00 |
| 磁盘（不含表情库） | 约 2.3 GB（Node 运行时 1.1 + NapCat 349M + 代码与数据 800M） |
| **单轮回复延迟** | **1~2 秒**（优化前 5.6~9.3 秒） |
| 每次请求输入 | 12~14k tokens |

跑起来之后的常驻开销只有三块：Node 宿主、6 个按需拉起的 Python MCP、NapCat 容器。
没有数据库、没有 Redis、没有消息队列。

## 系统要求

| | |
|---|---|
| **操作系统** | **Ubuntu 24.04 LTS**（实测 24.04.4 LTS Noble Numbat，内核 6.8）。22.04 理论上也可以，但没验证 |
| **架构** | x86_64 —— NapCat 的 Docker 镜像只有 amd64 |
| **CPU / 内存** | 2 核 / 2 GB（实测配置）。1 核 1 GB 未验证，不建议 |
| **磁盘** | ≥ 20 GB（Node 运行时 + NapCat + 代码 + 表情库；表情库按需自备） |
| **其他** | Docker、systemd。安装脚本会自己装 Docker 和 Node，不需要预装 |

**不支持**：Windows（NapCat 容器只有 Linux amd64）、ARM。服务器必须是干净的 Linux。

## 为什么能做到这么轻

- **零第三方依赖**：2200+ 行 Python 全是标准库，不用 pip、不会因为编译包把内存打满
- **MCP 按需拉起**：6 个 Python 服务是 stdio 子进程，每个 20 MB 上下，不调用就不占 CPU
- **图片不进上下文**：看图由服务端代看、只回文字描述。否则几张 base64 图片就能把输入撑到 43k tokens（这是实测过的坑）
- **关掉隐藏思考**：配置里的 `reasoning: "off"` 在宿主语义里是"什么都不发"，网关却默认开思考。`llm_shim.py` 把 `thinking: disabled` 注入请求体，单次从 9 秒降到 2 秒

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
    └── 5 个本地 MCP 子进程（stdio，按需拉起）
         ├── memory_server  日记 / 长期笔记
         ├── websurf        联网搜索 + 读网页
         ├── seer           看图
         ├── imagen         生图
         └── admin          工具档位切换
```

不是"接个大模型回消息"的玩具。真正花时间的是那些让它在群里**不像机器人**的部分：

- **说话节奏**：可以晚回、可以不回、刚说完不必补一条；被 @ 才理，被 @ 的人 10 分钟内免 @
- **不做主持人**：不总结话题、不劝架、不给每个人挨个回应
- **不汇报操作**：不会说"我已经发给你了"这种旁白
- **有记忆分层**：日记 + 长期笔记 + 按遗忘曲线排序的检索
- **有作息**：什么时候可能主动说话、什么时候安静
- **工具档位**：默认只给聊天用得上的工具，需要运维能力时再切

## 几个值得看的设计

**1. 看图：服务端代看，图不进上下文**

不只是为了省 token——DSH 的 MCP 客户端会丢弃 image 内容段，模型只收到
`[image: image/jpeg, content discarded]`，所以直接返回图片根本行不通。
`seer.py` 让服务端自己调多模态模型看，只把"看到了什么"用文字返回。
对群聊反而更好用：可以直接问"这图什么梗""写的什么字"。

**2. 记忆不是简单的时间倒序**

`mem_rank.py` 用混合遗忘曲线给检索结果排序：

```
R(t) = floor + (1 - floor) × exp(−ln2 × (t / halfLife)^β)
```

按记忆类型给不同半衰期（事实类 240 天、经历类 60 天、待办类 28 天），重要性和复述次数会拉长半衰期。
**只影响排序，不删除任何东西。**

**3. 联网是带防护的**

`websurf.py` 的 `fetch()` 只允许公网 http/https：域名解析后逐个 IP 校验（白名单 `is_global`，显式排除私网/回环/链路本地/CGNAT/组播）、连接时钉死已校验的 IP 防 DNS rebinding、重定向手动跟且每跳重新校验、不吃环境代理。

这不是洁癖——机器人能被群友一句话支使去抓网址，云服务器上 `169.254.169.254` 是实例元数据。

**4. 自带体检**

`checkall.py` 15 节 103 项，覆盖服务、端口、配置、插件、MCP、记忆文件、权限。
改完任何东西跑一遍，0 失败才算没搞坏。**它同时是这套代码的验收标准。**

## 快速开始

**先看看你的 AI 有没有干活的手**：它要能执行命令（终端 / bash）。
Windows 上装 [Git for Windows](https://git-scm.com/download/win)（提供 `ssh.exe` / `bash.exe` / `scp.exe`）；Linux / macOS 自带。
并且把 AI 的**工作区设成这个文件夹**，否则它找不到 `install.sh`。

```bash
# 1. 复制参数模板，填 6 项（两个 QQ 号 + 模型地址/模型名/key + 可选你的称呼）
cp deploy.env.example deploy.env

# 2. 跑
bash install.sh

# 3. 如果提示扫码，扫完再跑一次（脚本是幂等的）
bash install.sh
```

或者更省事：**把整个文件夹丢给 AI，让它读 `DEPLOY.md`**。
那份文件是写给 AI 的执行手册，含每阶段验证点、故障速查表、验收标准。

只有两件事人必须做：**扫码登录**、**最后发一句话验证**。

## 改什么，改哪里

| 想改 | 改这个 | 生效方式 |
|---|---|---|
| 说话的样子 | `persona/*.md` → `/opt/dsh/media/onebot/persona.md` | `systemctl restart dsh-onebot` |
| 作息 | `/opt/dsh/routines.json` | 不用重启 |
| 模型线路 | `/opt/dsh/profiles/web/cordis.patch.yml` 的 `llm-pi-ai` 段 | 重启 |
| 谁能让它说话 | 同上，`dsh-onebot.allowFrom` / `adminUsers` | 重启 |
| 工具档位 | `python3 /opt/dsh/toolmode.py chat\|full` | 自动 |

## 已知限制

- **只支持 Linux x86_64**（NapCat 的 Docker 镜像只有 amd64）
- **`/opt/dsh` 路径写死**：脚本里 20 多处硬编码这个路径，换目录要自己改
- **体检期望值写死了一部分**：`checkall.py` 里对端口、文件名有具体断言，是给标准部署用的；改了部署结构就得同步改断言
- **示例角色是别人的形象**：`example-jingjing.md` 的灵感来自 DeepSeek 社区拟人形象，视觉版权不属于本项目。公开分发或商用请换成自己写/自己画的角色——部署时选 `PERSONA_MODE=generated` 就不会用到它
- **不含表情图片**：表情库接口留着，图片自己放（版权原因）

## 项目结构

```
START-HERE.md       AI 入口：30 秒指令，先问什么、按什么顺序、怎么验收
DEPLOY.md           AI 执行手册：每阶段验证命令、故障速查、回滚
MANUAL.md           人看的：准备清单、装 Git、指工作区、扫码、验收
README.md           本文件：项目说明 + 设计取舍
CHANGELOG.md        改了什么、为什么——包括踩过的坑和两次踩坏的经历
SECURITY.md         权限模型、暴露面、SSRF 防护、密钥管理
CONTRIBUTING.md     怎么改、怎么验、不许碰的边界
VERSION             当前版本
install.sh          幂等安装脚本
verify.sh           15 项部署验收，输出 VERDICT
preflight.sh        64 项静态自检（上线前跑，不需要服务器）
scripts/            preflight 的 Python 侧检查
qr_wait.py          扫码等待器（自动提取二维码，登录后自动继续）
deploy.env.example  参数模板
persona/
  TEMPLATE.md       人设骨架（{{占位符}}，按描述填）
  example-jingjing.md  示例角色（136 行，可直接用）
stack/              运行时主体：12 个 Python 服务/工具 + 配置模板 + systemd
.github/workflows/  CI：语法、密钥扫描、换行、占位符完整性
```

## 开发

```bash
# 第一层：静态自检（不需要服务器，64 项）
bash preflight.sh               # 看最后一行 VERDICT: STATIC OK

# 第二层：语法（preflight 已包含，也可单独跑）
python3 -m py_compile stack/*.py qr_wait.py
bash -n install.sh verify.sh

# 第三层：运行层（在真实部署上）
bash verify.sh                  # 15 项，看最后一行 VERDICT
python3 /opt/dsh/checkall.py    # 106 项，0 失败才算过
```

**注意**：`preflight.sh` 只能证明"代码和文档自身没明显错误"，
不能替代干净机器上的真实部署验证。

Python **3.8+**，零第三方依赖（`requirements.txt` 里只有说明）。
改动规矩、硬约束、不接受的方向见 `CONTRIBUTING.md`。

## 安全

这套东西跑在公网可达的服务器上、能被群友说话影响。**机器人默认没有 shell、没有文件权限**，
这是设计不是缺陷。改任何涉及网络 / 执行 / 权限的东西之前，先读 `SECURITY.md`。

## 版本

当前 `0.1.0`（见 `VERSION`）。改动记录在 `CHANGELOG.md`。

## 许可

MIT。见 `LICENSE`。第三方代码的出处与角色形象版权说明见 `NOTICE.md`。
