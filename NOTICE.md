# 致谢与第三方代码说明 / Acknowledgements & Third-Party Notices

这个项目没有别人的代码和文档它跑不起来。下面把来源一条条列清楚，
包括"参考了思路但代码是自己写的"那种——**用别人的东西要说，这是底线。**

---

## 一、核心运行依赖（不随本仓库分发，部署时安装）

| 组件 | 用途 | 许可 |
|---|---|---|
| [DeepSeek Harness](https://www.npmjs.com/package/@deepseek-ai/dsh) | 宿主：会话、工具、MCP 调度 | 按上游项目 |
| [dsh-onebot-qq](https://www.npmjs.com/package/dsh-onebot-qq) | OneBot v11 通道插件（本地有改动，见下） | 按上游项目 |
| [NapCat](https://github.com/NapNeko/NapCatQQ)（镜像 `mlikiowa/napcat-docker`） | QQ 协议端 | 按上游项目 |
| [Node.js](https://nodejs.org/) | 运行时 | MIT |
| [Docker](https://www.docker.com/) | 容器 | Apache-2.0 |

## 二、可选模块

| 组件 | 用途 | 说明 |
|---|---|---|
| [Tulpa](https://github.com/fumingyang2004/Tulpa) | B 站读取（搜索 / 推荐流 / 详情 / 字幕 + 抽帧 / 评论列表） | **本项目不重写这部分**。B 站的 wbi 签名每天轮换、还要 buvid3、SSRF 防护、字幕解析、视频抽帧；Tulpa 全做完了且更谨慎（DNS 固定、URL 白名单、拒回环/私网）。我们只写"写"的那一半（发评论、@ 人），不改它的只读安全层。 |

## 三、参考了思路、代码由本项目自己写的

| 位置 | 参考来源 | 说明 |
|---|---|---|
| `stack/search_index.py` | [fellowgeek/mcp-memory](https://github.com/fellowgeek/mcp-memory) | FTS5 + trigram 中文检索的做法参考该项目，按本项目场景重写。 |
| `stack/mem_rank.py` | 记忆研究公开结论 | **遗忘曲线不是本项目的原创**：指数衰减 + 长尾地板是通用工程近似。我们的贡献是把它接进检索、配上原子写与防错，不是发明公式。 |
| `stack/websurf.py` 的 SSRF 防护 | 常见 SSRF 防护实践 | 思路：公网白名单 + 连接时钉住已解析 IP。代码为 Python 标准库自实现。 |
| `stack/llm_shim.py` | 各家"关闭思考"的通用做法 | 通过注入 `thinking` 字段关掉推理链，实测回复快 4~5 倍。字段名按上游实际支持情况自适应。 |
| 主动行为的时间窗 / 离散配额 | 调度系统里常见的"时间格 + 配额"模式 | 把一小时切成 N 格、每格最多触发一次——不是特殊算法，就是避免"一小时内扎堆"的常规做法。 |

## 四、对上游插件的本地改动

`dsh-onebot-qq` 在本项目里是**带改动的**（fork）。所有改动都记在
`stack/apply_local_patches.py` 里，可随时检查、随时重打：

| 文件 | 改了什么 | 为什么 |
|---|---|---|
| `prompt.js` | 每轮注入长期记忆 | 上游没有跨会话记忆 |
| `bridge.js` | 时间戳挪到消息尾部；静音"已排队/已提交"两条系统播报 | 前者保住前缀缓存（省 token），后者防止刷屏 |
| `inbound.js` | 主动接话概率门；表情提醒；回忆提示 | 上游只有"被 @"一条路径 |
| `inject.js` | 注入事件改标成「系统·内部提醒」 | 否则会伪装成"主人发的消息"，她分不清 |
| `index.js` | 配置 schema + **白名单透传** | 新配置项不加进白名单会静默失效 |
| `cq.js` | 未知表情兜底成 `[表情]` | 否则渲染成乱码 |

这些改动的许可证跟随上游。如果上游作者不希望被 fork，请联系，我们会改成纯插件外挂。

## 五、图片与角色形象

**本仓库不包含任何表情图片素材。** 表情库需要你自己准备（`media/onebot/stickers/`）。

默认人设是一位鲸鱼娘，灵感来自 DeepSeek 社区对模型形象的拟人化（社区常称"小鲸鱼"）。
**人设文本是本项目自己写的，但角色形象本身的版权不属于本项目。**
如果要公开传播、商用或再分发形象素材，请自行确认授权，或者换成你自己绘制/生成的角色。

`persona/TEMPLATE.md` 和 `persona/example-jingjing.md` 都只是**示例**，可以整份替换。

## 六、文档与工具链

- 徽章：[shields.io](https://shields.io/)
- 图片生成：Pillow（`scripts/gen_images.py` 用它画 README 配图）
- CI：GitHub Actions + shellcheck

---

## 如果你发现漏了

这份清单靠人工维护，很可能漏。发现漏标来源、或者哪个改动的许可证有问题，
欢迎开 issue——**这类问题会优先处理**，因为用别人的东西不写明，比功能有 bug 更严重。
