# 三层记忆

2026-10-26 重做。之前三块各自都在，但**没有接线**，所以表现出来就是"她记不住事"。

## 三层各是什么

| 层 | 存在哪 | 谁写 | 何时注入 | 预算 |
|---|---|---|---|---|
| **临时记忆** | 会话本身 + `memory/session_summary.md` | `mem_layers.py idle`（空闲 ≥10 分钟蒸馏） | **每轮**注入摘要 | 摘要 ≤1200 字 |
| **大记忆库** | 日记（`memory/diary/*.md`）+ `knowledge.json`（189 条事实）+ FTS 索引 | 她写日记 / `memory_distill.py` 每天 03:10 / `mem_layers.py idle` | **每轮按当前消息检索**，命中才注入 | `recall_budget`（默认 1500 字） |
| **核心记忆** | `workspace/AGENTS.md` | `memory_distill.py` 定期重写 | **每轮全量**注入 | 文件本身 ≤3000 字 |

关键差异（相对旧版）：

- 旧版命中"回忆触发词"只会附一句 `MEM_HINT`：**提醒她"你可以去查日记"**。实测她基本不查，问过去的事靠印象编。
- 新版 `inbound.js` 直接把检索结果附在消息后面（`[记忆片段·…不是主人现在说的话]`），她不用再决定查不查。
- 旧版没有"临时层"，长会话被 compaction 压缩后就断片。新版每轮注入上一段摘要。

## 怎么调

改 `/opt/dsh/limits.json`，**不用改代码、不用重启**：

```json
{
  "recall_k": 5,          // 每轮从大记忆库里捞几条
  "recall_budget": 1500,  // 每轮最多注入多少字（觉得记性差就调高）
  "idle_minutes": 10      // 空闲多少分钟后开始蒸馏
}
```

## 手动验证

```bash
cd /opt/dsh
python3 mem_layers.py status                     # 三层各有多少东西
python3 mem_layers.py recall --text 'B站评论区'   # 看检索给她什么（这是每轮跑的那条命令）
python3 mem_layers.py idle --dry                 # 看空闲蒸馏会做什么（不写盘）
python3 mem_layers.py distill                    # 立即蒸馏一次
python3 mem_layers.py summary --set '…'          # 手动覆盖临时层摘要
```

## 已知边界

- **测试消息会进上下文**：`qq_test_inject.py` 会给消息加 `[测试]` 前缀，蒸馏时成对滤掉，
  否则测试里说过的话会被当成真事实写进库。（2026-10-26 实测踩过：蒸馏把"每轮只说一句话"记成了约定，已清理。）
- **蒸馏用的语言模型**是 llm-shim（8890），需要 `qwen.env` 里的 key，在 `mem-idle.service` 里通过 `EnvironmentFile` 注入。
- **核心记忆（AGENTS.md）只读不改**：写它的是 `memory_distill.py`，两个程序别抢同一个文件。
- `idle` 只看"最近一次活动时间"（会话文件 / 动作日志的 mtime），无法区分"她闲着"和"服务挂了"。
