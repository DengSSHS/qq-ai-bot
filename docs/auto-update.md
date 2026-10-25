# 自动更新

**默认关闭。** 开启后每天 **04:00**（±10 分钟随机延迟）检查仓库有没有新提交，
有就更新；更新完自检，失败自动回滚并通知主人。

## 为什么默认关

升级要重启 dsh，正在进行的对话会被打断；开发者的机器本来就该自己控制升级时机。
所以装机不会自动开启，想要就手动开（见下）。

## 两道保护（即使开着也不会乱动）

每次运行前先查：

1. **最近 15 分钟有对话活动 → 跳过**，不打断正在进行的交谈；
2. **`memory-distill` / `sticker-settle` 正在跑 → 跳过**，不和文件写入撞。

实测：模拟刚聊过再跑 → `跳过：15 分钟内有对话活动（onebot-private-…），不打断`

## 不会碰的东西

`update.sh` 全程只写三个地方：

```
cordis.patch.yml     （只补缺失的键）
routines.json        （只添加新 routine）
node_modules/…       （重装 fork + 重打补丁）
```

**完全不碰** `sessions/`（对话历史）、`memory/`（日记与状态）、
`workspace/`（AGENTS.md 长期记忆）、`persona/`（人设）、`media/`（表情与附件）。
重启不会丢会话——会话落在磁盘上，dsh 起来会自己 attached。

## 它怎么工作

```
04:00  auto-update.timer  ──>  auto-update.service  ──>  /opt/dsh/auto_update.sh
                                                          ├─ 查开关（关了就退出）
                                                          ├─ git fetch，比较 HEAD..origin/main
                                                          ├─ 没更新 → 记一行日志，结束
                                                          ├─ 有更新 → 记回滚点 → git pull → update.sh
                                                          ├─ 自检：服务起来了吗 / QQ 在线吗
                                                          └─ 失败 → git reset --hard 回滚点 → 再 update.sh → 通知
```

`update.sh` 本身是"只增不改"的（见该脚本注释），所以自动更新不会动你的配置值和
你改过的 prompt。

## 三种关闭方式

```bash
python3 /opt/dsh/switches.py off auto_update    # 推荐：和别的一起看/关
systemctl disable --now auto-update.timer       # 直接关定时器
touch /opt/dsh/memory/auto-update.disabled      # 兜底标记文件
```

打开：

```bash
python3 /opt/dsh/switches.py on auto_update
```

脚本每次运行都会先查前两种；`.disabled` 标记文件优先级最高。

## 前提：必须是 git 仓库

自动更新靠 `git fetch` / `git pull`。用 `git clone` 部署的机器天然满足；
如果是把 tarball 拷过去解压的，脚本会打印：

```
跳过：找不到 git 仓库（tarball 部署的机器没有自更新能力，请用 update.sh 手动更新）
```

这时手动更新仍然是 `git pull && bash update.sh`（或直接把新 tarball 覆盖上去再跑 `update.sh`）。

仓库位置按顺序找：`$REPO_DIR` → `/opt/dsh/repo` → `/opt/qq-ai-bot` → `/root/qq-ai-bot` → `/opt/qqbot/repo` → `/opt/dsh`。
要求目录里同时有 `.git` 和 `update.sh`。

## 日志与状态

| 文件 | 内容 |
|---|---|
| `/opt/dsh/memory/auto-update.log` | 每次运行的记录（无更新也会记一行） |
| `/opt/dsh/memory/auto-update-state.json` | 最近一次的回滚点（from_commit / from_version / at） |

## 失败时会怎样

| 情况 | 处理 |
|---|---|
| `git fetch` 失败（网络） | 保持现状，记日志，退出 0 |
| `git pull` 不能快进（本地有改动） | 放弃本次更新，记日志，退出 1 |
| `update.sh` 失败 | `git reset --hard` 回滚到旧 commit，再跑一次 `update.sh`，通知主人 |
| 更新后服务没起来 / QQ 离线 | 同上，回滚 + 通知 |

通知优先走 qqguard 的直连通道（真发到 QQ），失败退回邮件。

## 为什么凌晨 4 点

- 她的作息里 01:00–07:30 是睡眠时段，主动消息基本不在这时发；
- 收图结算是 03:00、记忆蒸馏是 03:10，4 点更新不会和它们撞；
- 真出问题，早上你醒来能看到通知，而不是半夜被吵。

## 已知限制

- **只对 git 部署生效**（见上）。
- 自动更新只覆盖"代码 + 配置补键 + 补丁 + 重启"，**不覆盖**需要人工的操作：
  例如 QQ 掉线需要扫码、模型 key 变更、上游插件大版本需要重新移植补丁。
  这些情况 `update.sh` 会报 `[注意]`，但不会替你解决。
