# 开关面板（switches.py）

## 为什么要有

这套东西能调的行为散在三个地方：

```
systemd 定时器      启停要 systemctl enable/disable
cordis.patch.yml    改一个键还要 restart dsh-onebot
routines.json       改 JSON、认条目名
```

每次调整都要记住"改哪个文件、要不要重启"，改错了没有回滚。更麻烦的是
**记错地方就等于没改**——今天有一半时间是花在这类"改了但没生效"上。

`switches.py` 把它们收敛成一个清单。

## 用法

```bash
python3 switch.py                # 列出所有开关和当前状态
python3 switch.py off casual_dm  # 关掉
python3 switch.py on  casual_dm  # 打开
python3 switch.py --json         # 给程序读
```

输出：

```
开关                 状态       名称                 说明
casual_engage      开        群聊主动接话             未 @ 她的群消息按概率接一句（每小时最多 3 次）
casual_dm          开        私聊主动找人             空闲越久概率越高（每小时 +20%，封顶 80%）
diary_reminders    开        日记提醒               早上/中午/傍晚/夜里各提醒一次该写日记了
sticker_settle     开        收图结算               每天 03:00 判断群友发的图要不要收进表情库
memory_distill     开        记忆蒸馏               每天 03:10 从会话里提炼长期记忆
daily_report       开        每日日报               每天 23:55 发一封报告
qq_guard           开        QQ 掉线看护            每 2 分钟检查，掉线自动重启容器并告警
patch_doctor       开        插件补丁漂移检测           每周检查上游改版后 fork 是否还兼容
budget_breaker     关        预算熔断               token 超限时自动降级/停机（默认关闭）
content_screening  默认(开)    图片内容风险筛查           结算时判断图里有没有隐私/擦边
```

## 四种"插槽"

加新开关 = 往 `FEATURES` 里加一条 dict。

| kind | 映射到 | on/off 动作 | 要重启吗 |
|---|---|---|---|
| `timer` | systemd 定时器 | `systemctl enable/disable --now` | 不用 |
| `config` | `cordis.patch.yml` 的某个键 | 改值 + `restart dsh-onebot` | 要 |
| `routine` | `routines.json` 的作息条目 | 打 `off: true` 标记 | 不用（下次检查生效） |
| `budget` | `budget.json` 的键 | 改值 | 不用 |
| `env` | unit 的 `Environment=` override | 写 override 片段 + daemon-reload | 下次触发生效 |

## 安全措施

```
改文件前   备份到 memory/switch-backups/
改完       校验（JSON 能解析 / 那个键的值对得上）
校验失败   自动回滚
```

## 加一个开关的例子

```python
dict(id="my_feature", kind="timer", label="我的新功能",
     desc="它做什么", unit="my-feature.timer"),
```

## 踩过的坑：开关改了但行为没变

`routine` 和 `env` 两类最初只改了标记，**代码里没有读取标记的逻辑** ——
`casual_dm` 打了 `off: true`，`proactive.py` 照样按原逻辑跑。

所以补了两处：

```python
# proactive.py
if r.get("off"):
    continue

# stickerlib.py
def _screen_enabled():
    v = (os.environ.get("STICKER_SCREEN_SAFE") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")
```

**这是本项目最顽固的一类 bug**：开关面板如果只改配置不接行为，本身就是骗人的。
新增 kind 类型时，必须同时确认读取方存在。
