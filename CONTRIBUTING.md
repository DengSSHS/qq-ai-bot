# 参与贡献

欢迎改。但这是一个**跑在别人服务器上、能被公网用户影响**的项目，所以有几条硬规矩。

## 一、环境

- Python **3.8+**（部署环境是 3.12），零第三方依赖，只用标准库
- Bash 4+（部署环境是 Ubuntu 24.04 的 `/bin/bash`）
- 不需要 Node 环境就能跑 Python 部分

## 二、改之前先读

| 想改 | 读这个 |
|---|---|
| 部署流程 | `DEPLOY.md` |
| 安全边界 | `SECURITY.md`（**改任何涉及网络/执行/权限的东西前必须看**） |
| 为什么代码长这样 | `CHANGELOG.md`（记录了踩过的坑） |
| 人设怎么写 | `persona/TEMPLATE.md` |

## 三、验证方式

四层检查（改完至少跑前两层）：

```bash
# 1. 单元测试：纯函数，秒级，不需要服务器
python3 -m unittest discover tests -v

# 2. 静态自检：文档与配置一致性
bash preflight.sh

# 3~4. 需要真实部署
# 语法层：所有 Python + Shell
python3 -m py_compile stack/*.py qr_wait.py
bash -n install.sh verify.sh stack/*.sh

# 运行层：真实部署上跑
bash verify.sh                    # 15 项，最后一行 VERDICT
python3 /opt/dsh/checkall.py      # 约 100 项（浮动），0 失败才算配置没坏
```

**改了任何东西，`checkall.py` 必须 0 失败。** 但别把它当验收标准：
它逐项比对「配置期望值 vs 实际值」，属于配置自检。**改了业务逻辑它发现不了**——
那要靠 `tests/` 的单元测试和最后一层人验。

## 四、硬规矩

1. **改配置前先备份**，并在改动记录里写清备份名和回滚命令
2. **不要在代码里硬编码账号、密钥、IP、群号**。用 `deploy.env` 或环境变量
3. **新增任何「按用户/模型给的地址去抓取」的功能，必须复用 `websurf.py` 的校验逻辑**
4. **不要给机器人加 `bash` / `fs` 工具**。真要排查，临时开、用完关，见 `SECURITY.md`
5. **不要把备份放进 `sessions/` 目录**。workspace 插件会扫描它，非标准布局会让整条
   插件链 pending、8643 不监听（踩过两次）
6. **`.sh` 文件必须 LF 换行**。CRLF 会导致 `syntax error: word unexpected`。
   仓库根目录有 `.gitattributes` 强制 `eol=lf`，别删它——Windows 用户 clone 后
   如果换行符被转成 CRLF，部署脚本会直接语法错误。
7. **不要提交 `deploy.env`**、任何 `.bak-*` 备份、`__pycache__`

## 四之二、改了这些必须跑单元测试

```python
stack/mem_rank.py     # 遗忘曲线 → tests/test_mem_rank.py
stack/websurf.py      # SSRF 判定 → tests/test_websurf_ipguard.py
stack/seer.py         # 路径白名单 → tests/test_seer_paths.py
stack/llm_shim.py     # 候选切换 → tests/test_llm_shim.py
```

这四处是**纯函数 + 安全边界**，`checkall.py` 完全覆盖不到。改错了只有跑测试能发现。
新增同类纯函数时，顺手加一组测试——不需要框架，标准库 `unittest` 就行。

## 五、代码风格

- Python：标准库、无类型标注（保持轻量）、中文注释可以（受众主要是中文用户）
- 错误处理：失败要给出可读原因，不要吞异常
- 输出：**面向 AI/脚本的输出只用 ASCII**（`PASS`/`FAIL`），中文留给人类界面——
  经 SSH 传回中文容易乱码，会让调用方误判

## 六、提交信息

```
<范围>: <做了什么>

为什么这么做（一句话）。如果改变了行为，写清旧行为。
```

例：

```
websurf: 重定向改为逐跳校验

原来只校验首个 URL，公网地址 302 到 127.0.0.1 可以绕过。
```

## 七、不接受的方向

- 代跑运动数据、伪造成绩类功能
- 自动加群、批量私聊、养号
- 把机器人做成「有 shell 的通用助手」（安全边界会被废掉）
- 任何需要在提示词里写「忽略安全规则」才能工作的设计

## 八、报告问题

开 issue，附：

- `bash verify.sh` 的输出（脱敏）
- `/opt/dsh/memory/` 下相关日志的片段（**删掉账号、密钥、群号**）
- 复现步骤

安全漏洞不要贴利用细节，见 `SECURITY.md`。
