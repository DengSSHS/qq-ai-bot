# 让她刷 B 站（可选模块）

给她一个「自己去 B 站看视频、觉得有意思就在评论区 @ 主人」的能力。

**这个是可选模块**，不装不影响任何其它功能。它的依赖是一个第三方项目
[Tulpa](https://github.com/fumingyang2004/Tulpa)（读 B 站）+ 我们自己的三个脚本（时间窗、预算、写评论）。

---

## 为什么这么设计

### 1. 为什么不自己写 B 站接口

B 站的 `wbi` 签名每天轮换，还要 `buvid3`、SSRF 防护、字幕解析、视频抽帧。
Tulpa 这些都做完了，而且工程上比我们谨慎（DNS 固定、URL 白名单、拒回环/私网、媒体缓存配额）。
**站在它上面写，比自己从零写省两周，而且更安全。**

### 2. 为什么读和写要分开

Tulpa 是**明确只读**的（`net.py` 只放行 GET 路由）。发评论是 POST，它不提供。
所以分工：

```
Tulpa（读）      搜索 / 推荐流 / 视频详情 / 字幕+抽帧 / 评论列表
bili_at.py（写） 用同一个 cookie 发评论、@ 指定的人、可删自己刚发的
```

不改 Tulpa 的安全层，写入走独立进程——它的只读保证不被破坏。

### 3. 为什么中间还要一层代理（`bili_hub.py`）

一开始是把她直接连到 Tulpa 的 MCP，然后用「开关 MCP」来做时间窗。**实测失败**：

```
dsh 的 MCP 连接是启动时建立的。关掉再开，dsh 不会重连——
工具名还在她的工具表里，但每次调用都失败。
她的原话："窗口已经开了，但我手里好像没有能把视频内容读出来的那个工具。"
```

所以改成：**Tulpa 的 MCP 一直开着，时间窗的判断放在代理里**。
代理是 stdio 常驻（dsh 启动就连上，永不掉线），每次读工具调用前先问一句
「现在能不能刷」，不能就直接返回 `{"ok": false, "code": "window_closed"}`。

---

## 三层闸门

| 层 | 管什么 | 默认 |
|---|---|---|
| **时间窗** `bili_window.py` | 低谷期能自己刷、高峰期不许、手动授权开窗口 | 低谷 23:30-08:00，每天叫醒 3 次 |
| **token 预算** `bili_budget.py` | 每天浏览消耗的 token 上限 | **关闭**（`enabled: false`，不限制） |
| **分享配额** `bili_hub.py` | 评论区 @ 主人的频率 | 每天 3 条、间隔 20 分钟、同视频 7 天一次 |

### 时间窗怎么判断

```json
// bili_window.conf.json
{
  "allowed_windows": [["23:30", "08:00"]],
  "daily_auto_opens": 3,
  "auto_minutes": 25,
  "manual_per_day": 2,
  "manual_max_minutes": 60,
  "ramp_per_hour": 0.25,
  "ramp_cap": 0.9
}
```

- 低谷期**每多待一小时，「叫醒她」的概率抬一档**（+25%，封顶 90%）
- **同一小时内只掷一次骰**（小时桶哈希），换小时才重掷 —— 是累积随机，不是每分钟重试
- 掷中就注入一条消息给她：「（现在是你的时间）去 B 站刷刷吧，看到有意思的记得在评论区 @ 我」
- 主人在对话里说「给你玩 30 分钟」→ 她调 `bili_open(30)` 开窗口，到点自动关

### token 预算怎么算

不能复用 `usage.jsonl`（那里记的是整轮调用，**无法归因**到"因为刷 B 站")。
所以按**工具返回内容的体量**估算：中文约 2 字 = 1 token。

```json
// bili_budget.conf.json
{ "enabled": false, "daily_tokens": 150000, "token_per_char": 0.5 }
```

`enabled: false` 就是不限额（主人自用）。想限就改成 true 并设自己的额度。
她可以随时用 `bili_budget` 工具查「今天还剩多少」。

---

## 她拿到的工具

| 工具 | 作用 |
|---|---|
| `bili_now` | 现在能不能刷、还剩多久 |
| `bili_open(minutes)` | 主人说「给你玩 N 分钟」时开窗口 |
| `bili_budget` | 查 token 额度 |
| `bili_share_to_owner(bvid, note)` | 在评论区 @ 主人，他会收到 B 站通知 |
| `search_videos` / `get_recommended_videos` | 搜索 / 刷推荐流 |
| `get_video_info` / `get_video_comments` | 详情 / 评论 |
| `read_video` + `get_video_job` | 读视频（字幕优先，异步任务） |
| `get_video_frames` / `cancel_video_job` | 抽好的画面 / 取消任务 |

---

## 部署

### 1. 装 Tulpa（Linux 终端版）

```bash
# 需要 Python 3.13 或 uv
curl -sL https://ghfast.top/https://github.com/fumingyang2004/Tulpa/releases/download/v0.5.7/Tulpa-MCP-0.5.7-linux.tar.gz | tar xz
sudo mkdir -p /opt/tulpa && sudo tar xzf Tulpa-MCP-0.5.7-linux.tar.gz -C /opt/tulpa --strip-components=1
cd /opt/tulpa && sh setup-linux.sh          # 默认轻量安装，不装 OCR/docling
```

**已知的上游 bug（Linux 包）**：`chatlocal/cli_runtime.py` 里 `from app_mcp import create_app`
——`app_mcp` 在 Linux 包里已改名 `terminal_server`。改成
`from chatlocal.terminal_server import create_app` 即可。

### 2. 起服务并扫码登录

```bash
nohup .venv/bin/python -m chatlocal.mcp_cli serve > /tmp/tulpa.log 2>&1 &
# 用它的 API 生成二维码（返回 base64 PNG）
# POST /bilibili/api/action  {"action":"start_login","consent":true}
# 头部需要 X-Tulpa-Bilibili: 1
# 然后 POST {"action":"poll_login"} 直到 state=success
```

### 3. 开 MCP、建连接、记下 token

```bash
# 管理接口需要 x-chatweave-ui: 1 头
POST  /api/mcp                {"enabled":true,"port":18777}
POST  /api/mcp/connections    {"name":"bot-bili","platforms":[],"history_platforms":[],"bilibili_read":true}
# 返回里的 token 写进 /tmp/conn.json（bili_hub 从这里读）
```

### 4. 部署我们的脚本

```bash
cp stack/bili_hub.py stack/bili_budget.py stack/bili_window.py stack/bili_at.py /opt/dsh/
cp stack/config/bili_*.json /opt/dsh/           # 记得填 bili_share.conf.json 的 owner_mid
```

### 5. 注册 MCP（加到 local-memory 的 `- insert:` 列表下）

```yaml
  - id: mcp-bili-hub
    name: '@deepseek-ai/dsh-mcp-client'
    config:
      serverName: bili
      transport: stdio
      command: python3
      args:
        - /opt/dsh/bili_hub.py
```

**注意**：dsh 的 `cordis.patch.yml` 只能**修改已有条目**，要新增必须走
`local-memory/cordis.patch.yml` 的 `- insert:` 指令。写在 patch 里会报
`patch: entry "xxx" not found`。

### 6. 定时器

```bash
# 每分钟：按时间窗决定要不要叫醒她
systemctl enable --now bili-window.timer
```

---

## 踩过的坑（都是实测留下的）

**1. 评论区看着发出去了，但只有自己能看到**

新号第一次带 @ 的评论会进人工审核。**但 @ 通知照常送达**——所以"分享给主人"这个用途是可用的，
只是别指望陌生人马上看到。

**2. `-101 账号未登录` 不一定是代码问题**

同一个账号**同时只能在一处登录**。主人在手机上登了它，服务器这边的会话立刻失效，
之后所有写接口都返回 -101。先查 `bilibili/api/status` 是不是 `expired`，再看代码。

**3. 写接口不需要 `buvid3` / `bili_ticket`**

网上很多教程说 reply/add 要这两个。实测（2026-10）纯 Tulpa 的 cookie 就够——
带 `SESSDATA`、`bili_jct`、`DedeUserID` 即可。加多了反而容易出别的问题。

**4. 只写 `@昵称` 不传 `at_name_to_mid`，对方收不到通知**

```python
data["message"] = "@某人 这句话"
data["at_name_to_mid"] = json.dumps({"某人": 123456})   # 必须有
```

评论会正常显示，但**不会产生 @ 通知**。这是这类脚本最常见的失败点。

**5. `GenWebTicket` 返回 405**

那个接口要 POST（哪怕 body 是空的），不是 GET。

**6. Linux 包没做过真实登录验证**

作者在 release 里写得很清楚：「Linux 为离线回归，未重复真实扫码和媒体读取」。
我们装的时候撞上的 `app_mcp` 就是其中一处。遇到怪事先怀疑打包，再看自己代码。

---

## 成本参考

```
一个 25 分钟视频的完整 AI 中文字幕：295 条 / 3132 字 ≈ 1566 tokens
Tulpa 常驻内存：约 68 MB（systemd cgroup 实测，ps 的 RSS 会重复算共享库）
```

所以「学一个视频」的真实开销 ≈ 一个正常对话回合（字幕只占其中 1.5k）。
token 预算默认关闭，就是因为这个量级通常不需要限制。

---

## 安全边界

```
她能做    看视频、读字幕、搜东西、在评论区 @ 主人（一天最多 3 条）
她不能做  点赞、关注、发动态、剪视频、上传、翻别人的私信
硬约束    B 站登录态在 Tulpa 的加密库里，只有 bili_at 在发评论时读一次
          Tulpa 的 GET-only 白名单未改动，写入走独立进程
```

**一个要记住的运维约束**：主人在别处登录这个 B 站号 → 服务器这边立刻掉线。
要么给她单独的号，要么接受"你用了她就不能用"。
