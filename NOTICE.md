# 第三方代码与素材说明 / Third-Party Notices

## 代码

| 位置 | 来源 | 说明 |
|---|---|---|
| `stack/search_index.py` | [fellowgeek/mcp-memory](https://github.com/fellowgeek/mcp-memory) | FTS5 + trigram 中文检索方案参考该项目的做法，按本项目场景重写。若原项目有许可证要求，请以原项目为准。 |
| `stack/mem_rank.py` | 无 | 遗忘曲线公式为通用工程近似（指数衰减 + 长尾地板），参考了记忆研究的公开结论，非特定项目代码。 |
| `stack/websurf.py` 的 SSRF 防护 | 无 | 判定思路参考了常见 SSRF 防护实践（公网白名单 + 连接钉 IP），代码为 Python 标准库自实现。 |

## 运行依赖（不随本仓库分发）

| 组件 | 许可 |
|---|---|
| [DeepSeek Harness](https://www.npmjs.com/package/@deepseek-ai/dsh) | 按上游项目 |
| [dsh-onebot-qq](https://www.npmjs.com/package/dsh-onebot-qq) | 按上游项目 |
| [NapCat](https://github.com/NapNeko/NapCatQQ) / `mlikiowa/napcat-docker` | 按上游项目 |
| Node.js | MIT |
| Docker | Apache-2.0 |

## 图片与角色

**本仓库不包含任何表情图片。**

默认人设是一位鲸鱼娘形象，灵感来自 DeepSeek 的社区拟人形象（社区常称"小鲸鱼"）。
人设文本是本项目自己的写作，但**角色形象的视觉版权不属于本项目**——如果要公开使用、商用或再分发形象素材，请自行确认授权，或换成自己绘制/生成的角色。

`persona/` 里的人设文件仅作**示例**，可以整个替换。
