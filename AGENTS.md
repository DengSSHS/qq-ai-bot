# AGENTS.md · 给 AI 的机器入口

> 人类可以忽略这个文件。**AI 请从这一页开始，不要先读 README。**
> 本文件是 `START-HERE.md` 的索引，权威内容在后者；冲突时以 `START-HERE.md` 为准。

## 你的任务

把一台全新的 Linux 服务器变成能用的 QQ 群聊机器人。你有 shell 权限。

## 三条命令，按顺序

```bash
cp deploy.env.example deploy.env     # 填 5 项，见下表
bash deploy.sh                       # 查配置 -> install.sh -> verify.sh，一条走完
# 失败就改 deploy.env 再跑一次，install.sh 可重入
```

## 已装好的机器要更新（这是一条独立命令，不要让人类回去问作者）

```bash
git pull && bash update.sh
```

`update.sh` 的原则是**只增不改**：

- 配置：只补新版本新增的键，**人类改过的值一字不动**；
- 日程：只添加新的 routine，不覆盖已有 prompt；
- 插件：重装 fork 快照 + 重打本地补丁；
- 最后重启 dsh 并跑 preflight。

`bash update.sh --dry-run` 可先看会改什么。它幂等，失败修好重跑即可。

**也有全自动的**：`auto-update.timer` 每天 04:00 检查仓库，有新版就升级、
升级后自检、失败自动回滚（`git reset --hard` 回旧 commit 后再跑一次 update.sh）。
关闭：`python3 /opt/dsh/switches.py off auto_update`。前提是**用 git clone 部署的机器**。

**绝对不要用重跑 `install.sh` 来“更新”** —— 它会用模板整体覆盖
`cordis.patch.yml`，人类手调过的每一项都会没。

`deploy.sh` 最后一行是结论：**`VERDICT: READY`** 才算装完。出现
`NOT READY` 或任何 `[FAIL]` 都**不要**向人类汇报成功。

## 必须问人类的问题（一次问完，别反复打断）

| # | 要问的 | 填进 deploy.env |
|---|---|---|
| 1 | 服务器怎么登 | IP + root 密码，或私钥路径 |
| 2 | 机器人 QQ 号（建议小号，要扫码） | `BOT_QQ` |
| 3 | 主人 QQ 号（只有这个号能私聊下指令） | `OWNER_QQ` |
| 4 | 模型 | `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` |
| 5 | **人设要哪种**（必问，见下） | `PERSONA_MODE` |

人设三选一，默认 `builtin`（包里现成的鲸鱼娘）：

- `builtin` —— 直接用，装完就能说话
- `generated` —— 人类给名字/性格/称呼/口癖，你照 `persona/TEMPLATE.md` 写，
  **`{{占位符}}` 必须全部替换**（verify.sh 会检查残留和少于 20 行）
- `custom` —— 人类给全文或文件路径，填 `PERSONA_FILE`

## 装完之后你会得到

| 东西 | 位置 |
|---|---|
| 服务 | `systemd` 单元 `dsh-onebot`（`systemctl status dsh-onebot`） |
| 代码 | `/opt/dsh` |
| 日志 | `/var/log/dsh-onebot.log` |
| 密钥 | 仓库目录下 `.ws_token`、`.napcat_webui_token`（600，**别打印、别提交**） |
| 运行时开关 | `python3 /opt/dsh/switches.py` 看清单，`... off <名字>` 关掉某项 |
| 日报 | 配了 `MAIL_*` 就每天 23:55 发一封 |

## 红线

1. **不要跳过 `verify.sh`**。它做的事（真发一条消息、真让模型看图）静态检查替代不了。
2. **不要把 `deploy.env`、`.ws_token`、`.napcat_webui_token` 提交或贴进对话**。
3. **不要给人设留占位符**，也不要为了"跑通"随手删 `verify.sh` 里的检查项。
4. 机器人**默认没有 shell、没有文件读写权限**。它是设计如此，不要"顺手"把
   `agent` / `admin` 工具档位开到最大。
5. 装完**不要**自己登录 QQ 主号去测；测消息用主人号 @ 一次即可。

## 常见坑

| 症状 | 原因 |
|---|---|
| `install.sh` 卡住 | 多半在等 `docker pull`，别 Ctrl-C，看 `docker stats` |
| `verify.sh` 到"发消息"这步失败 | 扫码没成功，或 NapCat 没连回来：`docker logs --tail 50 <napcat 容器>` |
| 回复正常但不懂图 | `VISION_*` 没填。填了要 `systemctl restart dsh-onebot` |
| 中文乱码 | 环境不是 UTF-8：`export LANG=C.UTF-8` 后重跑 |
| `deploy.env` 改了不生效 | 值带 CRLF，`deploy.sh` 会自动修；别用 Windows 记事本改 |

## 你要交付给人类的一句话

装完只说结论 + 两件事：`VERDICT: READY`、他在哪个群 @ 它测试、日报邮件的开关状态。
不要复述日志。
