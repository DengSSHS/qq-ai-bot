# 面板接口（`panel_api.py`）

将来要做图形面板时，**面板不要直接读写那些配置文件**——它们的格式各不相同，
一改代码面板就崩。统一走这个接口。

```
                        ┌──────────────────────────────┐
   面板（Web / Tk / CLI） │  list --json   拿全部控件描述  │
                        │  set <id> <值>  改一个         │
                        │  restart       需要时重启      │
                        └──────────────┬───────────────┘
                                       │
                            panel_api.py（注册表 + 读写 + 校验）
                                       │
        ┌──────────────┬───────────────┼──────────────┬────────────────┐
   limits.json   cordis.patch.yml  routines.json  bili_window.conf.json  bili-budget.json
   （每天限制）    （插件行为）      （主动行为）    （B站时间窗）         （token 预算）
```

## 三条命令

```bash
python3 panel_api.py list                 # 人看的表格（分组）
python3 panel_api.py list --json          # 面板用：全部控件 + 当前值 + 元数据
python3 panel_api.py set <id> <值>        # 改一个，返回 JSON
python3 panel_api.py get <id>             # 看一个的完整描述
python3 panel_api.py restart              # 重启 dsh-onebot（让 restart=true 的项生效）
```

## 控件描述长什么样

`list --json` 返回：

```json
{ "items": [
  {
    "id": "casualEngageRate",     // 稳定标识，写进代码里也不会变
    "group": "群聊行为",           // 分组，面板拿它做 tab / 折叠块
    "label": "主动接话概率",       // 中文名，直接当控件标题
    "type": "percent",            // 见下表
    "value": 0.2,                 // 当前值
    "default": 0.2,
    "min": 0, "max": 1,           // 数值范围（bool/string/enum 没有）
    "options": null,              // enum 的候选值
    "unit": "%",                  // 单位
    "desc": "未 @ 她的群消息，按此概率被接住并回一句",
    "restart": true,              // 改完要不要重启
    "storage": "cordis.patch.yml" // 存在哪（排障用，面板不必关心）
  }
]}
```

### `type` 怎么渲染

| type | 控件 | 说明 |
|---|---|---|
| `bool` | 开关 | 值就是 `true/false` |
| `int` | 数字框 + 步进 | 用 `min`/`max` 限制范围 |
| `percent` | 滑块 0~100 | **值域是 0~1**，面板显示时 ×100 加 `%`；写回时传 0~1 或 0~100 都行 |
| `enum` | 下拉框 | 候选值在 `options` |
| `schedule` | 两个时间选择器 | 值是 `[["18:00","08:00"]]`，**可以跨零点**（18:00 → 次日 08:00） |

## `set` 的返回

```json
{"ok": true,  "id": "bili_share_per_day", "value": 12,
 "need_restart": false, "message": "已生效"}

{"ok": true,  "id": "stickerNudgeRate", "value": 0.6,
 "need_restart": true,  "message": "已写入，需要重启 dsh-onebot 才生效"}

{"ok": false, "id": "newMessagePolicy",
 "error": "只能是：steer / queue"}
```

面板要做的就三件事：

1. 检查 `ok`，不 ok 就把 `error` 显示出来
2. `need_restart` 为真时，弹一个「重启后生效 → [现在重启]」的按钮（调 `restart`）
3. 别的什么都不用管——**不用知道值存在哪个文件、什么格式**

## 现在覆盖了哪些（24 项）

| 分组 | 项 |
|---|---|
| 群聊行为 | 只在被 @ 时说话 / 激活窗口 / 主动接话概率 / 接话每小时上限 / 思考时又来消息怎么办 / 表情提醒概率 |
| 每天限制 | 全局发送/天、发送/分钟、接收/分钟、B站@主人/天、两次@间隔、同视频不重复@、主动私聊/天、半夜叫醒/晚 |
| B 站 | 低谷时段（时间窗）、每天自动叫醒、凌晨上限、token 预算开关、每日 token 上限 |
| 主动行为 | 关掉主动私聊、主动私聊/天、基础冲动、关掉半夜失眠、失眠夜概率、失眠夜冲动 |

**没覆盖的**（面板要做的话得另外接）：

- 人设文件（`persona.md`）—— 本质是一篇长文本，面板应该给个编辑器直接写文件
- 开关面板的 11 个开关（`switches.py list --json` 已能读，改走 `switches.py on/off`）
- 模型与 key（`qwen.env`）—— 涉及密钥，建议只在面板里做"看/改"，不要回显
- 表情库管理、日记浏览 —— 这些是数据而非参数，属于面板的另外半部分

## 两条设计约束（给以后改这个文件的人）

1. **`id` 一旦发布就不能改**。面板可能把它写进前端配置、书签、甚至用户自己的脚本里。
   要改语义就新增一个 id，旧的标成 deprecated。
2. **写文件必须原子 + 备份**。面板会被连点、会被并发调用；
   `panel_api.py` 里所有写操作都是"写 `.tmp` → `os.replace`"，并且先存一份 `.bak-panel`。

## 一个已知的小坑

`percent` 类型的值，写 `5` 和写 `0.05` 都表示 5%（`>1` 时自动除以 100）。
所以**`min`/`max` 校验是在换算之后做的**——你写 `5` 想表达"500%"是做不到的。
面板做滑块时直接传 0~1 最省事。
