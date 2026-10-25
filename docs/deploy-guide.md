# 部署指南

从一台空服务器到"她在群里说话"，全程一条命令。这份文档把每一步会看到什么、
出错怎么办都写清楚——内容来自 2026-10-25 在干净 Ubuntu 24.04 上的完整实测。

---

## 0. 你要准备什么

| 项 | 说明 |
|---|---|
| 一台服务器 | **Ubuntu 24.04 LTS · x86_64**（ARM 不行，NapCat 镜像只有 amd64） |
| 配置 | **2 核 2G** 起（1核1G 未验证）；磁盘 **≥ 20 GB**，建议 40 |
| 地域 | **国内**优先（腾讯/阿里机房到 QQ 最稳，别选海外） |
| 机器人 QQ | 建议**小号**。这个号会被当成登录设备，别和你主号混用 |
| 你的 QQ | 用来私聊下指令、接收日报和告警 |
| 模型 API | 任意 OpenAI 兼容接口：Base URL + 模型名 + Key |
| 登录方式 | root 密码 / 私钥都行。**云厂商默认的 `ubuntu` 账号也能直接用** |

> ⚠️ 机器人号在服务器上登录后，**别在手机/电脑 QQ 上再登同一个号**——
> 那会把服务器这份登录踢掉，表现为"她突然不回消息"。

---

## 1. 三条命令

```bash
git clone https://github.com/DengSSHS/qq-ai-bot.git && cd qq-ai-bot
cp deploy.env.example deploy.env
bash deploy.sh
```

如果用云厂商的 `ubuntu` 账号（不是 root），照样 `bash deploy.sh`——脚本会自己 `sudo` 提权。

### 填 `deploy.env`

只有 5 项必填：

```ini
BOT_QQ=3600398389                 # 机器人号
OWNER_QQ=1709392682               # 你的号（只有这个号能私聊下指令）
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-xxxx
PERSONA_MODE=builtin              # builtin=包里现成的人设；generated/custom 见 START-HERE.md
```

可选但建议：

```ini
# 看图：不填就回落到模型线路（模型不支持图片时会失败）
VISION_BASE=
VISION_MODEL=
VISION_API_KEY=

# 掉线邮件告警：不填就没有邮件通知（QQ 侧发不出去时，邮件是唯一通道）
MAIL_PASS=你的QQ邮箱授权码
MAIL_USER=你的QQ邮箱
MAIL_TO=接收告警的邮箱

# 让容器能用密码自动重登，省掉扫码（填 MD5 更安全）
NAPCAT_QUICK_PASSWORD_MD5=
```

算 MD5：`echo -n '你的QQ密码' | md5sum`

改完权限收紧一下（脚本也会做）：`chmod 600 deploy.env`

---

## 2. `deploy.sh` 会依次做什么

```
1/3 查 deploy.env    必填项、QQ 号格式、人设模式、文件权限
2/3 install.sh       九步安装（见下表）
3/3 verify.sh        16 项实况验收，最后打印 VERDICT
```

`install.sh` 的九步，以及每步你能看到的输出：

| 步 | 做什么 | 正常输出 |
|---|---|---|
| 1 | 环境检查 | `发行版支持` / `内存 1935MB` / `根分区剩余 38GB` |
| 2 | 系统依赖 | `curl / xz / python3 / 中文字体 就位` |
| 3 | Docker + **镜像加速** | `docker 镜像加速已配置（腾讯云内网优先）` |
| 4 | Node.js | `Node 装好：v24.22.0` |
| 5 | DeepSeek Harness | `dsh 装好：0.2.1-rc.2` |
| 6 | 部署代码到 `/opt/dsh` | `脚本 30 个、人设已就位` |
| 7 | 写配置 | `cordis.patch.yml 写好` + **`插件版本豁免已写入`** |
| 8 | NapCat（QQ 协议端） | 拉镜像 → 起容器 → **等扫码** |
| 9 | 系统服务 | `dsh-onebot 运行中（8643 与 8090 已监听）` + 定时器就位 |

### 第 8 步会停下来等你扫码

首次部署（容器里还没有登录态）会打印：

```
  ---------- 二维码链接 ----------
    http://127.0.0.1:6099/webui?token=xxxx
  （在浏览器打开链接，再用手机 QQ 扫屏幕上的二维码）
  等待扫码中...（剩余 179 秒）
```

**最省事的做法**：在你自己的电脑上开一条隧道，然后浏览器打开那个链接：

```bash
ssh -L 6099:127.0.0.1:6099 root@<服务器IP>
# 然后在本地浏览器打开 http://127.0.0.1:6099/webui?token=xxxx
```

手机 QQ 登录**机器人号**扫码 → 手机上点允许 → 脚本检测到登录会自动继续。

**没扫上也不要紧**：脚本会继续把服务装完（不会中断），最后提示你
"扫码后再执行一次 `bash install.sh`"。

---

## 3. 验收：什么叫装好了

最后一行必须是：

```
RESULT: 16 passed, 0 failed
VERDICT: READY
```

`VERDICT: READY` 之外的结果都不算装完。

### 常见 FAIL 与处理

| 输出 | 原因 | 处理 |
|---|---|---|
| `OneBot not connected` | 还没扫码登录 | 扫码（见上），或 `bash install.sh` 补 |
| `8643 / 8090 NOT listening` | 服务刚起、端口还没就绪 | 等 20 秒重跑 `bash verify.sh` |
| `mcp processes = 0` | 插件被 dsh 判定不兼容而禁用 | 检查 `/opt/dsh/profiles/web/compatibility.json` 是否存在 |
| `checkall.py exit 1` | 全新机器还没有日记/索引/备份 | **正常**，用一段时间后自动消失 |
| `docker pull` 超时 | `i/o timeout` 拉不到 Docker Hub | 配 `/etc/docker/daemon.json` 镜像加速后重跑 |

---

## 4. 装完之后

```bash
# 看状态
python3 /opt/dsh/qqguard.py --status      # 连接 / 重启次数 / 最近事件
python3 /opt/dsh/switches.py              # 11 个运行时开关一览

# 到群里 @ 她一句，或私聊发个消息
# 想看她今天写了什么
tail -40 /opt/dsh/memory/diary/$(date +%Y-%m).md
```

服务与定时器：

```bash
systemctl status dsh-onebot          # 主服务
systemctl list-timers | grep -E "proactive|qqguard|memory-distill|sticker-settle|usage-report"
journalctl -u dsh-onebot -n 50       # 出问题先看这个
```

---

## 5. 更新

```bash
git pull && bash update.sh
```

原则是**只增不改**：配置只补新键、日程只加新项、脚本走三方比对（你改过就保留 + `.new`）。
先看会改什么：`bash update.sh --dry-run`。

想全自动（每天 04:00）：

```bash
python3 /opt/dsh/switches.py on auto_update
```

自带保护：15 分钟内有对话就跳过；记忆蒸馏在跑也跳过；升级失败自动回滚并通知。

---

## 6. 重装 / 迁移

`deploy.sh` 和 `install.sh` 都是**幂等**的：中途失败改完再跑一次即可。

如果你要**从零重装到同一台机器**（比如换机器、或者上次装乱了）：

```bash
# 先把要留的东西拷走
scp -r root@old:/opt/dsh/memory/diary .      # 日记
scp -r root@old:/opt/dsh/media/onebot/stickers .   # 表情库
scp root@old:/opt/dsh/routines.json .        # 你调过的日程

# 新机器上正常装完，再把上面三样放回 /opt/dsh 对应位置
```

**不要**直接删 `/opt/dsh` 就想重装：里面有你的人设、日记、表情库、长期记忆。

---

## 7. 排障速查

| 症状 | 先查什么 |
|---|---|
| 她不回消息 | `python3 /opt/dsh/qqguard.py --status`；再看 `docker logs --tail 50 napcat` |
| 只看得到"有图片" | `grep VISION /opt/qqlink 2>/dev/null`；实际查 `/opt/dsh/qwen.env` 有没有 `VISION_*` |
| 她说"看不到图" | 跑 `python3 /opt/dsh/probe_vision.py` 看视觉端点通不通 |
| 她想不起之前的事 | `python3 /opt/dsh/memory_server.py` 手工发个 `diary_recent` 看有没有数据 |
| 日志里 `disabling profile plugin` | `compatibility.json` 丢了 → 从仓库 `stack/compatibility.json` 拷回去 |
| 服务无限重启 | `journalctl -u dsh-onebot`；常见是 `Failed to load environment files`（缺 `tokenhub.env`） |

---

## 8. 关于它的边界

- 它**不代跑、不上传伪造数据**，只做聊天与记录。
- 它默认**没有 shell、没有文件读写权限**（设计如此，见 `SECURITY.md`）。
- 整套东西用的是第三方协议端（NapCat），**不是腾讯官方接口**——
  使用前请读 [`DISCLAIMER.md`](DISCLAIMER.md)。
