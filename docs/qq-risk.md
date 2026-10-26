# QQ 风控与重新登录（2026-10-26 实战记录）

## 一句话

NapCat 里的 Linux QQ 被安全中心判为「风险设备」踢下线时，**重装 QQ 数据目录**（换新设备指纹）
比反复扫码有效。但顺序上应先解限制、再扫码。

## 发生了什么

```
07:11  正常收发（最后一次成功发送）
07:24  NapCat: 账号已离线，正在重启 Worker 以重新创建 QQ 登录
07:24  这之后手机 QQ 里她就显示离线；安全中心推送「Linux 版 QQ 风险设备已下线」
07:47  扫码恢复，正常收发
07:57  再次被踢：send_msg → retcode 1200 / result 1006514「网络连接异常!」
07:59  重装 QQ 数据（新设备指纹）→ 扫码 → 恢复正常
```

**根因**：为改代码在 1 小时内重启服务十几次、扫码三次。
在 QQ 看来是「Linux 设备在机房 IP 上反复登录」——风控最敏感的模式。

## 关键判据（别再从日志猜）

| 现象 | 含义 |
|---|---|
| `get_status` 返回 `online:true / good:true` | **不可信**：被踢后仍报在线（假在线） |
| `send_msg` → `retcode 1200` / `1006514 网络连接异常` | 确诊：已被踢，客户端拒绝发送 |
| 重启容器后不肯快速登录、只给二维码 | 确诊：设备/账号被安全中心限制 |
| `get_friend_list` 返回 0 条但 `get_group_list` 正常 | 可疑（会话异常时的表现） |

`qq_status.py` 现在把「接口在线 + 最近发送全被拒」判为掉线，不再假在线。

## 处置步骤

### 1. 先解限制（顺序很重要）

手机 QQ → 设置 → 账号安全 → 安全中心 → 找到那条「Linux 设备」风险 → 解除
（通常有「本次是我本人操作」）

**先扫码后解除 = 大概率再被踢一次**，昨晚已经因此多扫了三次。

### 2. 重装 QQ 数据（换设备指纹）

```bash
# 备份（约 289MB）
tar -czf /opt/dsh/backups/qq-data-$(date +%Y%m%d-%H%M%S).tar.gz -C /opt/qqbot/data qq

# 停容器 → 移走旧数据 → 建空目录 → 启动
cd /opt/qqbot && docker compose stop napcat
mv /opt/qqbot/data/qq /opt/qqbot/data/qq.old-$(date +%H%M%S)
mkdir -m 700 /opt/qqbot/data/qq
docker compose up -d napcat
```

容器镜像不用动，只重装里面的 QQ 客户端数据。

### 3. 扫码

```bash
cd /opt/dsh && python3 qr_now.py      # 打印最新链接 + 导出 /tmp/qr-now.png
```

NapCat 在扫码模式下每约 2 分钟自动换码，所以随时取都是新鲜的。

### 4. 验证（必须做，别只看日志）

```bash
cd /opt/dsh && python3 qq_status.py                 # 只能说"在线"，不代表能发
# 真发送才算数：往 qq-outbox.jsonl 投一条，看 result 里有没有真实 message_id
```

## 附：常见坑

- **`ACCOUNT` 环境变量会导致快速登录卡死**（不成功也不出码）。真需要扫码时，
  临时把 compose 里的 `ACCOUNT` 注释掉，登录后恢复。
- **频繁重启是风控的主要诱因**。改代码时优先用 `apply_local_patches --check`，
  只做必要的一次重启。
- NapCat 有时不打「登录成功」这行日志，所以**不能用它判断在线**。

## 追加：连着被踢三次 = 账号级限制（2026-10-26 当天实录）

重装设备指纹后正常跑了 2.5 小时（08:00 → 10:29），**在没有任何操作的情况下又被踢**。
这说明限制已经从「设备」升级到「账号」。

判据：

| 现象 | 含义 |
|---|---|
| 重装后能跑一段时间，然后无操作被踢 | 账号被标记，不是设备 |
| 11:59–12:15 每 2 分钟自动出一张新码 | 容器在扫码模式下持续请求登录，**会加重风控** |
| 扫了立刻又被踢 | 账号限制没解除，先处理安全中心 |

处置顺序（**先解限制，再扫码**）：

1. 停容器，别让它一直刷登录请求
2. 手机 QQ → 设置 → 账号安全 → 安全中心 → 解除那条 Linux 设备/异常登录的限制
   （可能要短信验证 / 人脸 / 改密码，走完才算清干净）
3. `python3 qq_login.py` 起容器拿码 → **2 分钟内扫掉**
4. 真发一条验证（`get_status` 会假报在线）
5. `systemctl start qqguard.timer health-mail.timer` 恢复托管

停容器时要一起停的东西（否则会被自动拉起或反复告警）：

```bash
cd /opt/qqbot && docker compose stop napcat
systemctl stop qqguard.timer      # 它会在检测到踢下线时 docker restart napcat
systemctl stop health-mail.timer  # 否则每 10 分钟发"容器没跑"
: > /opt/dsh/media/onebot/qq-outbox.jsonl   # 清掉积压，避免登录后一次性轰出去
```

## 关键：谁在「一直请求登录」，以及怎么关

风控期间最忌讳的就是"持续登录尝试"。这套系统里有两个来源：

| 来源 | 行为 | 处理 |
|---|---|---|
| `qqguard.timer` | 检测到被踢 → `docker restart napcat` → **每次重启都是一次登录** | **已停用**（2026-10-26） |
| NapCat 扫码模式 | 停在扫码界面时**每 2 分钟自动换一张码** = 持续登录请求 | `qq-churn-guard.timer` 兜底 |

`qq-churn-guard`（每 5 分钟）规则：

```
容器在跑 且 未登录 且 已超 15 分钟  →  docker compose stop napcat
登录正常                          →  什么都不做
```

效果：被踢之后最多再请求 15 分钟，然后彻底安静，不再自己加重风控。

注意 `compose stop` 是"主动停"，`restart: unless-stopped` 不会把它拉起来——
所以这个保险不会被容器重启策略抵消。

需要登录时：

```bash
cd /opt/dsh && python3 qq_login.py     # 起容器 + 打印二维码（2 分钟内扫）
```

**不要**让容器长时间挂在扫码界面。
