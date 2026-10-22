# 部署指令（写给 AI，不是写给人看的）

> 你（AI）的任务：在一台**全新的 Linux 服务器**上，把一套 QQ 群聊机器人部署到能用。
> 环境里有 `install.sh`、`verify.sh`、`deploy.env.example`、`stack/`、`persona/`。
> 你有 shell 权限。**每一个阶段都有验证点，验证不过不要往下走。**

---

## 一、先向人类要这 3 件事（一次问清，别反复打断）

| 需要的 | 说明 | 例子 |
|---|---|---|
| 服务器登录方式 | IP + root 密码，或私钥路径 | `1.2.3.4` / `ssh root@1.2.3.4` |
| 两个 QQ 号 | ①**机器人号**（要能扫码登录，建议小号）②**主人号**（只有它能私聊下指令） | `360xxxxxxx` / `170xxxxxxx` |
| 模型 API | Base URL + Key + 模型名，任意 OpenAI 兼容接口 | `https://api.deepseek.com` / `sk-xxx` / `deepseek-chat` |

如果人类一次性给了，就别再问第二次。**以下不要问**（有默认值或自动生成）：端口、路径、Node 版本、Docker、token。

---

## 二、这套东西长什么样（先理解，再动手）

```
QQ 用户
  │
  ▼
NapCat（Docker 容器，跑 QQ 协议）
  │  OneBot v11 反向 WebSocket，主动连 ws://<docker网关>:8643/ws
  ▼
DSH（Node 进程，systemd: dsh-onebot）
  │  ⑵ 插件 dsh-onebot-qq 接住消息
  │  ⑶ 模型（OpenAI 兼容）
  │  ⑷ 5 个本地 MCP 子进程（stdio，按需拉起）
  ▼
回复发回 QQ
```

### 端口

| 端口 | 谁在听 | 要不要对公网开 |
|---|---|---|
| 22 | sshd | 要 |
| 8643 | DSH 的 OneBot WebSocket 服务端 | **绝对不要**，只在 docker 网桥内部用 |
| 8890 | LLM 转发层（127.0.0.1），给上游请求强制关思考 | 不要 |
| 8090 | DSH web 控制台（127.0.0.1） | 不要 |
| 6099 | NapCat WebUI（127.0.0.1） | 不要，需要时走 SSH 隧道 |
| 7681 | ttyd 网页终端（可选，未默认安装，见 `stack/systemd/optional/`） | 不要 |

### 目录

| 路径 | 内容 |
|---|---|
| `/opt/node` | Node 24 运行时（自带，不动系统 node） |
| `/opt/qqbot` | NapCat：`compose.yaml`、`data/`（QQ 登录态、onebot 配置） |
| `/opt/dsh` | 主程序：人设、记忆、MCP 脚本、profile 配置 |
| `/opt/dsh/media/onebot/persona.md` | **人设**，改完要重启 |
| `/opt/dsh/profiles/web/cordis.patch.yml` | **主配置**（模型线路 + QQ 策略 + 工具档位） |
| `/opt/dsh/memory/` | 日记、笔记、日志 |
| `/var/log/dsh-onebot.log` | 主服务日志 |

### stack/ 里都是什么

| 文件 | 作用 |
|---|---|
| `memory_server.py` | MCP：日记写/翻/搜、长期笔记读写 |
| `websurf.py` | MCP：联网搜索 + 读网页（**已带 SSRF 防护**，别改坏） |
| `imagen.py` | MCP：生图 |
| `seer.py` | MCP：**看图**（服务端调多模态，只把文字描述给模型） |
| `admin.py` | MCP：切工具档位 chat/full |
| `llm_shim.py` | LLM 转发层：把 `thinking: disabled` 注入请求体，**别关它，关了会慢 4~5 倍** |
| `checkall.py` | 全量体检，**验收就靠它** |
| `proactive.py` + `routines.json` | 作息：什么时候可能主动说话 |
| `body_state.py` | 定时刷新「现在的状态」 |
| `toolmode.py` | 生成工具档位段（AI 不需要手动改那段） |
| `search_index.py` / `mem_rank.py` | 中文检索 / 遗忘曲线排序 |
| `local-memory/` | 把上面 5 个 MCP 注册进 DSH 的桥接包 |
| `cordis.patch.yml.tpl` | 主配置模板（占位符会被 install.sh 替换） |
| `persona/TEMPLATE.md` | 人设骨架，带 `{{占位符}}`，按用户描述填 |
| `persona/example-jingjing.md` | 示例角色（鲸鱼娘），`PERSONA_MODE=example` 时使用 |
| `systemd/` | 服务单元（主服务 + 3 个定时器 + ttyd） |

---

## 二之二、怎么让 AI 接管服务器（重要）

先把三种「控制」分清楚，混了会出事故：

| | 谁在控制 | 用什么 | 默认状态 |
|---|---|---|---|
| **① 你的 AI** | 部署者自己的 AI（Claude Code / Cherry Studio / Cursor） | SSH 客户端的 bash | 看你的工具 |
| **② 服务器上的机器人** | QQ 群里的她 | —— 没有 | **bash / fs 全禁用** |
| **③ 人类** | 你 | SSH / 网页终端 | 有 root |

### ① 你的 AI 怎么接管服务器

**Windows 前提**：需要 `ssh.exe` 和 `bash.exe`。装 **Git for Windows**（提供 `C:\Program Files\Git\usr\bin\ssh.exe`、`bash.exe`、`scp.exe`）。Linux / macOS 自带，跳过。

**工作区（cwd）必须对**：AI 执行命令时的当前目录决定相对路径。
部署时把 AI 的工作区设成**部署包所在目录**（有 `install.sh` 的那一层），否则 `bash install.sh` 找不到文件。
`scp -r qq-bot-stack root@<IP>:/root/` 之后，服务器上的工作目录应是 `/root/qq-bot-stack`。

**三个实测坑**（都会让 AI 卡住，不是它笨）：

1. **Windows 用户名带单引号会炸**。路径如 `C:\Users\d'c\.ssh\key` 写进 `ssh -o UserKnownHostsFile="..."`，会被 ssh 自己当成引号解析，报 `command-line line 0: invalid quotes`。
   绕法：把私钥复制到无引号的路径再用（`cp key "$TMP/k" && ssh -i "$TMP/k" ...`）。

2. **中文输出经 SSH 回来容易乱码**。批量检查时不要靠肉眼看输出，改成：
   ```bash
   ssh ... 'cd /opt/dsh && python3 checkall.py > /tmp/ca.txt 2>&1'
   scp ... root@<IP>:/tmp/ca.txt ./ca.txt     # 取回本地再解析
   ```
   或者让远端脚本**只输出 ASCII**（`mem=854/1967MB` 这种），把中文留给本地渲染。

3. **`ssh host '命令'` 里的 `$(...)` 和引号会被本地先展开**。复杂命令写成本地脚本文件，再 `ssh host 'bash -s' < script.sh`，别硬拼成一行。

### ② 机器人自己能控制服务器吗

**不能，这是刻意的。**

```yaml
- id: tool-bash      disabled: true    # 终端
- id: tool-fs        disabled: true    # 读写文件
- id: tool-fs-search disabled: true    # 搜索文件
```

原因是它公网可达、能被群友说话影响。给它 bash = 把整台服务器交给任何一个能让它「打开这个网址」的人。

它的能力边界是**故意收窄**的：只用 5 个 MCP（记忆 / 联网 / 看图 / 生图 / 档位），加上 QQ 相关工具。

**如果你确实要临时给它终端**（比如让它自己查日志）：

```bash
# 1. 先备份
cp -a /opt/dsh/profiles/web/cordis.patch.yml /tmp/cfg.bak

# 2. 把 tool-bash 从 ALWAYS_OFF 里去掉，再切 full 档
sed -i 's/"tool-bash", //' /opt/dsh/toolmode.py
python3 /opt/dsh/toolmode.py full

# 3. 用完立刻撤回
python3 /opt/dsh/toolmode.py chat
```

**同时必须做**：云安全组只开 22；`bash` 开启期间别让它读网页（`websurf` 有 SSRF 防护，但 `bash` 没有）；盯 `media/onebot/qq-actions.log`。

风险自评：**开着 bash 的 QQ 机器人 ≈ 把 root shell 挂在公网上**。除非做一次性排查，否则别开。

### ③ 人类怎么进

```bash
ssh root@<IP>                          # 常规
ssh -L 7681:127.0.0.1:7681 root@<IP>   # 需要网页终端时，然后浏览器开 127.0.0.1:7681
```

---

## 三、开始部署

### 阶段 0：把包传上去，填参数

```bash
# 本地（你手上）
scp -r qq-bot-stack root@<服务器IP>:/root/
ssh root@<服务器IP>

# 服务器上
cd /root/qq-bot-stack
cp deploy.env.example deploy.env
# 填 5 个必填项：BOT_QQ / OWNER_QQ / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
```

**人设三选项**（默认第一个，但仍要问）：

| 选项 | PERSONA_MODE | 说明 |
|---|---|---|
| A（默认） | `builtin` | 用包里现成那份（136 行，鲸鱼娘），装完直接能用 |
| B | `generated` | 按人类描述生成：问清名字、性格、怎么称呼、口癖/特别在意的事，用 `persona/TEMPLATE.md` 填满，**不许留 `{{占位符}}`** |
| C | `custom` | 完全自己输入：人类给全文或给文件路径（`PERSONA_FILE=<路径>`），原样写入 |

**验证点**（两条都要过）：
- `grep '^PERSONA_MODE=' deploy.env` 必须是 builtin / generated / custom
- `grep -c '=$' deploy.env` 里剩下的空项只能是 `WS_TOKEN`、`NAPCAT_WEBUI_TOKEN`、`OWNER_NICK`、`BOT_NAME`（这四个留空有默认处理）

### 阶段 1~9：跑脚本

```bash
bash install.sh
```

脚本会顺次完成：前置检查 → apt 依赖 → Docker → Node 24 → DSH → 代码落地 → 写配置 → NapCat → systemd → 体检。

**它是幂等的**：中途断了、扫码没做完，直接再跑一次即可，不会重复装。

### 阶段 8 会停下来（这是唯一需要人的地方）

脚本以退出码 **2** 结束，并打印：

```
现在需要人做一件事：扫码登录
1) docker logs -f napcat
2) 日志里会出现二维码，用手机 QQ 扫它
3) 看到「登录成功」后再跑一次 bash install.sh
```

**你要做的**：

1. 把这条指令**原样转告人类**，让他扫码。你无法代替他扫码，不要尝试。
2. 扫码完成后，等他回复，再执行一次 `bash install.sh`。
3. 判断依据：`ls /opt/qqbot/data/config/onebot11_<BOT_QQ>.json` 存在 = 登录成功。

**如果二维码一直不出来**，按这个顺序查：

```bash
docker ps -a | grep napcat          # 容器在不在、有没有反复重启
docker logs --tail 80 napcat        # 有没有报错、二维码链接
ss -lntp | grep 6099                # WebUI 起来没有
```

### 阶段 9 之后的验收

```bash
python3 /opt/dsh/checkall.py        # 期望：失败 0，警告 0
systemctl is-active dsh-onebot      # 期望：active
grep 'bridge ready' /var/log/dsh-onebot.log | tail -1
ss -lntp | grep 8643                # 期望有监听
```

必须四项全过。**任何一项不过，不要报告“部署完成”。**

---

## 四、验收标准（这是唯一算数的东西）

| # | 检查 | 命令 | 通过条件 |
|---|---|---|---|
| 1 | 体检 | `python3 /opt/dsh/checkall.py` | `失败 0` |
| 2 | 服务 | `systemctl is-active dsh-onebot` | `active` |
| 3 | OneBot 已连 | `grep -c 'bridge ready' /var/log/dsh-onebot.log` | ≥1 |
| 4 | 5 个 MCP 在跑 | `pgrep -af 'memory_server|websurf|imagen|seer|admin' \| wc -l` | ≥5 |
| 5 | **真实回话** | 让主人 QQ 私聊机器人说「在吗」 | 它回话 |

第 5 项**必须由人类完成**，你做不到。让人类发一句，然后把 `tail -40 /var/log/dsh-onebot.log` 贴给你，你确认有没有 `turn/end ... completed`。

---

## 五、故障速查（按症状找）

| 症状 | 原因 | 处理 |
|---|---|---|
| `dsh-onebot` 起不来，日志说 EnvironmentFile 不存在 | `qwen.env` 或 `tokenhub.env` 缺失 | `install.sh` 会建；手动则 `echo 'QWEN_API_KEY=sk-x' > /opt/dsh/qwen.env && chmod 600` |
| 服务在跑但没有 `bridge ready` | NapCat 没连上来 | 查 `onebot11_<BOT_QQ>.json` 里 `websocketClients[0].url` 的 IP 是不是当前 docker 网关：`docker network inspect qqbot_default -f '{{(index .IPAM.Config 0).Gateway}}'` |
| NapCat 连上又断，反复重连 | token 不一致 | 配置文件里 `websocketClients[0].token` 必须等于 `cordis.patch.yml` 的 `dsh-onebot.accessToken` |
| 她收到消息但永远不回 | 没 @ 她（默认只理 @ 的人） | 这是设计，不是 bug。见「群聊规则」一节 |
| 回复很慢（5 秒以上） | 思考没关，或历史里堆了图片 | `systemctl is-active llm-shim`；再 `grep BODY /opt/dsh/memory/llm_shim.log` 看 user 占比，超过 50% 就清会话 |
| 8643 不监听、插件全 pending | `sessions/` 里放了非标准目录（如备份） | 把备份移出 `sessions/`，`systemctl restart dsh-onebot` |
| 她回了但发送失败 | QQ 风控 / 号被限制 | `docker logs napcat | tail -30` 看 send 报错；换号或降低频率 |
| 模型报 401/404 | key 或 baseURL 错 | 先单独验证：`curl -s <LLM_BASE_URL>/models -H "Authorization: Bearer <key>"` |
| 体检里「模型线路」失败 | `qwen.env` 里变量名不对 | 必须是 `QWEN_API_KEY=`，配置里 `apiKeyEnv` 指向它 |
| 网页读不出正文 | 没装 bs4（只影响整洁度） | `pip3 install beautifulsoup4` 可选 |
| 生图很慢 / 报错 | 走的是千问平台的图像接口 | `imagen.py` 里 `BASE` 和模型名按实际线路改 |

---

## 六、部署完要知道的三条设计（别当成故障去修）

1. **被 @ 才理**。`requireMention: true` + `groupActivationMinutes: 10`：被 @ 的人 10 分钟内不带 @ 也能触发。这是刻意的。
2. **看图不是把图塞给模型**。DSH 的 MCP 客户端会丢弃 image 内容段，所以 `seer.py` 是"服务端代看、返回文字描述"。别改回直接返回图片。
3. **工具档位是 chat（24 个）**。想开全部工具，私聊她说「打开全部工具」（她调 `tool_mode`）。不要手改 `cordis.patch.yml` 里 `preset-standard` 那一段——那段由 `toolmode.py` 生成。

---

## 七、回滚与重装

```bash
# 只重装代码（保留 QQ 登录态、日记、人设）
systemctl stop dsh-onebot
cd /root/qq-bot-stack && bash install.sh

# 完全重来（连 QQ 登录态一起清）
systemctl stop dsh-onebot proactive.timer body-state.timer toolmode.path
cd /opt/qqbot && docker compose down
rm -rf /opt/qqbot/data /opt/dsh/profiles /opt/dsh/memory
cd /root/qq-bot-stack && bash install.sh
```

---

## 八、交付给人类时说什么

部署完成后，用这几句汇报，别堆术语：

> 装好了，现在能在 QQ 里用了。
> 私聊它说一句话试试；把它拉进群，@ 它一次也行。
> 想改它的性格，改 `/opt/dsh/media/onebot/persona.md`，然后重启服务。
> 想换模型，改 `/opt/dsh/profiles/web/cordis.patch.yml` 里那两行。
> 每天自动体检：`python3 /opt/dsh/checkall.py`。

如果第 5 项（真实回话）还没验过，**必须**说"还差最后一步没验证"，不要说"部署完成"。