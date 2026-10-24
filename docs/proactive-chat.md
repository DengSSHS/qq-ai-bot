# 主动说话（群聊接话 + 私聊找茬）

两个机制，逻辑完全不同，别混：

| | 触发方式 | 概率 | 上限 | 会不会延续 |
|---|---|---|---|---|
| **群聊接话** | 每一条没 @ 她的群消息 | 固定 20% | 每小时 3 次，均匀分布 | **不延续**，接完就结束 |
| **私聊主动** | 定时检查（每 10 分钟） | 随时间累积 | 每日受 dailyBudget | 正常对话 |

## 群聊接话

改在插件入站管线的 @ 门槛上（`inbound.js`）。原本未 @ 的群消息被直接丢弃，
现在多一道概率：

```
未 @ 她 + 不在激活窗口
  → 掷骰：20% 命中 → 当作"她想接一句"，继续走完管线
  → 80% 未命中 → 照旧丢弃
```

三个关键约束：

1. **不写入激活窗口**（`groupActiveUsers`）。接住的只有当前这一条 ——
   说完就结束，那个群友下次说话仍需 @ 她。这样她就不是"掺进对话里"，
   而是"插一句就走"。
2. **同群冷却 60 秒**。群里连着刷三条消息，不会三条都被接。
3. **主动插话轮会带一个标记**：

```
[主动插话] 这条没人 @ 你，是你自己看到想接一句。先看上面最近的群聊内容
（不够就调 qq_group_history 拉最近 10 条），再简短接一句就好，别追问、
别展开话题、别把话头接成长对话。
```

配置（`deploy.env` 不涉及，直接在 profile 的 `cordis.patch.yml` 里）：

```yaml
imageEngageRate: 0.3           # 图片消息单独的概率（未设时回落 casualEngageRate）
casualEngageRate: 0.2          # 0 = 关闭
casualEngageCooldownMs: 60000  # 两次接话的最小间隔
casualEngageMaxPerHour: 3      # 每小时上限
```

**图片为什么单独一个概率**：图片比文字贵得多——看一次要 3 秒、几千 token。
用户要的是“群里别人发的图也能有概率被接住”，但不想因此把文字接话也一起提频。
所以拆成两个旋钮：文字 20%，图片 30%（配额、冷却、时间格都共用）。
读不到 `imageEngageRate` 时会回落到 `casualEngageRate`，所以旧配置不会因此变成 0。

**为什么要有小时上限**：纯 20% 逐条掷在活跃群会扎堆——一天 100 条消息就是
约 20 次接话，而且可能集中在前半小时。所以把一小时切成 `maxPerHour` 个时间格，
**每格最多用一次**：配额是天花板，20% 决定这一格要不要用。

实测：同一格里连发 3 条 → 只接 1 次。

## 私聊主动

走 `proactive.py` 的累积型 routine（`routines.json` 里的 `casual_dm`）：

```
概率 = rampPerHour × (1 + 超出 minIdle 的小时数)，封顶 rampCap
默认：每小时 +20%，封顶 80%，要求空闲 ≥ 60 分钟
```

**这是"累积"不是"纯随机"**：同一小时内用小时桶哈希掷一次，
所以「1 小时 20%、2 小时 40%、3 小时 60%」是真的在涨；
不是每 10 分钟重投一次骰子。

窗口 08:30–23:30，静默时段 01:00–07:30 绝不打扰，每日总量受 `dailyBudget` 限制。

## 踩过的坑

### 1. 配置键被白名单吞掉

插件把配置传给 pipeline 时要经过 `gateConfig` 白名单：

```js
const gateConfig = {
    dedupWindowSeconds: config.dedupWindowSeconds,   // 只挑这几个
    actionRatePerMinute: config.actionRatePerMinute,
    ...
};
```

加了 schema 还不够——**不在这个对象里的键，pipeline 读到的永远是 undefined**。
表现是概率门静默失效（`rate=undefined` → 条件不成立 → 行为退回原样）。

### 2. 占位符没替换，服务静默失败 229 次

`proactive.py` 里 `OWNER = __OWNER_QQ__` 一直没被替换，于是
`proactive.service` 每 10 分钟 `NameError` 一次，**连续失败 229 次**。
影响：日记提醒（morning/noon/evening/night）从 10-22 之后全部没触发，
所以那两天她没有日记。

`install.sh` 本来有统一替换（遍历 `$DSH_HOME/*.py`），是这台机器更早的安装
没跑那段。教训：**新装完 `systemctl status` 看一眼有没有在失败**，
`list-timers` 显示 active 不代表它跑得通。

### 3. 四处改动都在 node_modules 里

`prompt.js` / `bridge.js` / `inbound.js` / `index.js` 全是插件文件，
`npm install` 或升级就丢，丢了不报错。

用 `stack/apply_local_patches.py` 一键重打（幂等，带 `node --check` 自检）：

```bash
python3 /opt/dsh/apply_local_patches.py          # 打
python3 /opt/dsh/apply_local_patches.py --check  # 只看状态
```

## 调参建议

20% 在活跃群会偏高：一天 100 条群消息 → 约 20 次接话。如果觉得吵，
先调 `casualEngageRate: 0.1`，或把冷却改成 180000（3 分钟）。

---

# 附：她能"看见"表情吗（实测）

## 两种表情，走两条路

| 类型 | 传输形式 | 她看到什么 |
|---|---|---|
| Unicode emoji（😄🥵🔥） | 文本的一部分 | 原样保留 |
| QQ 小黄脸（`face` 段） | 只有数字 id，没有字 | 插件按表映射成 emoji |

实测（@她 + 笑 + 赞 + 未知 id + Unicode）：

```
她收到的原文：
@3600398389 四个表情测试：😂👍[表情] 还有Unicode 🥵🔥
```

- `face id=2` → 😂
- `face id=121` → 👍
- `face id=99999`（表里没有）→ `[表情]`
- Unicode 原样保留

## 修掉的坑：未知 id 兜底成笑脸

原来：

```js
export function faceToEmoji(id) {
    return FACE_EMOJI[id] ?? '😀';    // 不认识的就当笑脸
}
```

`FACE_EMOJI` 只收了两百来个（QQ 实际有几百个），剩下的一律变 😀。
后果是**她会看到不存在的笑** —— 比如别人发个哭泣的表情，她读成笑脸，
然后回一句完全不对的话。

现在未知 id 返回 `[表情]`：宁可老实说"这里有个我认不出的表情"，
也不要猜错情绪。

## 映射表本身是准的

抽查过：`1=😁 2=😂 5=😄 6=😅 66=😡 74=🥳 121=👍 124=👏 110=❤️`
都对。所以日常小黄脸没问题，只有冷门表情会落到 `[表情]`。

---

# 附：补丁漂移检测（patch_doctor.py）

## 为什么需要

`node_modules` 里现在有 5 处本地补丁，全在 `dsh-onebot-qq` 里：

```
prompt.js   长期记忆每轮注入
bridge.js   时间挪到后缀（前缀缓存）
inbound.js  主动接话概率门
index.js    配置 schema 与 gateConfig 透传
cq.js       未知表情兜底成 [表情]
```

它们的锚点都是**上游源码里的具体字符串**。上游一改版，锚点就可能失配，
而失配是**静默**的——不报错，只是某个功能悄悄不工作（看图端点就这样坏过一整天）。

## 它做什么

```
读本地已装版本 → 查 registry 最新版 → 下载 tgz 解包
  → 在干净的新版上逐条打补丁 → node --check 自检
  → 有打不上的就告警（可发邮件）
```

```bash
python3 /opt/dsh/patch_doctor.py           # 看报告
python3 /opt/dsh/patch_doctor.py --mail    # 必要时发邮件
python3 /opt/dsh/patch_doctor.py --ci      # CI 模式：有漂移非零退出
```

退出码：`0` 正常 / `1` 环境问题 / `2` 有补丁打不上。

## 第一次运行就抓到真问题

```
本地已装：0.9.3
registry 最新：0.9.6

[DRIFT] prompt.js   跳过（结构已变，需人工处理）
[DRIFT] bridge.js   找不到目标行
[DRIFT] inbound.js  找不到插入点
[DRIFT] index.js    找不到插入点
[OK  ] cq.js        已打
```

核实过不是误报——0.9.6 里 `prompt.js` 从 96 行缩到 31 行，`readPersona()` 整个
搬走；`bridge.js` 的「当前时间」拼装也没了。**所以锁在 0.9.3 不是保守，是必要。**

## 两道防线

| 位置 | 频率 | 干什么 |
|---|---|---|
| `.github/workflows/patch-doctor.yml` | 每周一 12:00（北京时间） | 在 CI 里跑 `--ci`，失配就标红 |
| `/etc/systemd/system/patch-doctor.timer` | 每周日 04:00 | 在服务器上跑 `--mail`，失配就发邮件 |

## 分水岭：5 处该考虑 fork

补丁越多，升级成本越高。今天的账是：

```
升级到 0.9.6 的代价 = 重新锚定 4 处补丁
不升级的代价       = 拿不到上游修复
```

判断标准（自用）：

- **≤3 处**：补丁脚本够用，升级时跟着改
- **4-5 处**：必须上漂移检测（现在这里）
- **>5 处，或某处补丁开始伤到核心链路**：该 fork 了

fork 的好处不是"改起来自由"，而是**升级变成一次 rebase，而不是五次猜锚点**。
坏处是 upstream 的安全修复要手动挑。

更优的中间路线：把通用性强的补丁提 PR 给上游（`cq.js` 的表情兜底、
未知 id 不该假装笑，这条对任何人都有价值），留私有的（长期记忆注入、
主动接话）在本地。这样能压住补丁数量。
