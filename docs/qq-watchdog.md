# QQ 掉线看护（qqguard.py）

## 为什么需要它

2026-10-24 的真实事故：

```
07:25:15  [KickedOffLine] 你的账号当前登录已失效，请重新登录
07:25:24  没有 -q 指令指定快速登录，将使用二维码登录方式
19:18     有人上去查 —— 中间 12 小时，一条消息都没回
```

掉线本身不可怕，可怕的是**没人知道**。

## 根因：entrypoint 与内部重启的区别

`/app/entrypoint.sh`：

```bash
if [ -n "${ACCOUNT}" ]; then
  gosu napcat /opt/QQ/qq --no-sandbox -q $ACCOUNT   # 带 -q → 可快速登录
else
  gosu napcat /opt/QQ/qq --no-sandbox                # 不带 → 只能扫码
fi
```

| 场景 | 走哪里 | 带 `-q` | 结果 |
|---|---|---|---|
| 容器重启 | entrypoint | ✅ | 用保存的 token 自动登录 |
| 被踢后 | NapCat 内部重启 worker | ❌ | 只能扫码等人 |

所以**掉线后重启容器**是一个可用的自愈手段：token 还有效就能免扫码恢复。

## 状态机

```
在线 ──断线──> 等新事件 ──看到 KickedOffLine──> 重启容器
                                  ├─ 连上了 → 回「在线」，若掉线 >5 分钟就告知主人
                                  └─ 没连上但有二维码 → 「等人工」，发邮件，停手
```

三条防呆：

1. **只认 checkpoint 之后的新事件**。否则每轮都看到旧的 KickedOffLine 就重启，
   会把主人正在扫的二维码刷掉。
2. **一小时最多 3 次**。和风控硬碰没有意义。
3. **进入「等人工」后不再重启**，让二维码留在那里。

## 为什么告警走邮件

QQ 掉线时出站通道也断，`qq_send_*` 发不出去。邮箱是唯一必然还通的渠道
（而且 QQ 号自带 QQ 邮箱）。

配置 `mail.env`：

```
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=<BOT_QQ>@qq.com       # 发信方，默认从 BOT_QQ 推导
SMTP_PASS=<16 位授权码>          # 不是 QQ 密码
MAIL_TO=<OWNER_QQ>@qq.com       # 收信方，默认从 OWNER_QQ 推导
```

## 踩过的坑

**授权码配错时的报错极具误导性。**

填 QQ 密码（而不是授权码）得到的是：

```
发送失败: Connection unexpectedly closed
```

完全不提认证失败，看起来像网络问题或 TLS 问题。实测同一把钥匙分别试两个邮箱，
只有配对的那个能登录——所以遇到这个报错，先怀疑「钥匙和账号不配对」。

获取授权码：QQ 邮箱网页版 → 设置 → 账号 → POP3/IMAP/SMTP 服务 → 开启 → 生成。
**只显示一次**，关掉就没了。

## 日常查看

```bash
python3 /opt/dsh/qqguard.py --status    # 连接状态 / 本小时重启次数 / 最近事件
cat /opt/dsh/memory/qq-incidents.log    # 完整事件流水
```

QQ 恢复后 `--status` 里的「本小时重启次数」会重置——它是按成功连接来清的，
不是按时间窗。
