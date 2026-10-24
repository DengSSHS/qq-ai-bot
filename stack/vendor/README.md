# vendor：本地 fork 的 dsh-onebot-qq

## 为什么有这个目录

先纠正一个容易搞错的认知：**这个插件不是"原版 + 5 处补丁"，它本身就是 fork。**

```
lib/bridge.js   +151 行   上游用 buildPlatformPrompt，fork 用 buildPromptParts
                          并且多传了 stickerStore
lib/prompt.js   +126 行   「Local patch: the QQ channel now owns the identity/persona」
```

`package.json` 里的版本号还写着 `0.9.3`，所以从外面看不出已经分叉——
**这正是问题所在**：`npm install` 只认版本号，会把 registry 的原版覆盖上来，
500 行改动全部静默消失，且没有任何报错。

相对 registry 0.9.3 的实际偏移：**+493 / -37 行**。

## 结构

```
vendor/
  fork-YYYYMMDD/          lib/ + package.json 的完整快照（我们的工作版本）
  patches/
    0001-local-changes.patch   相对 registry 0.9.3 的完整 diff
  install_fork.sh         把快照装回 node_modules，带语法自检
  lib-before-rebuild/     上次重建前的备份（出错可回滚）
```

## 用法

```bash
bash stack/vendor/install_fork.sh --check   # 对比 node_modules 是否被覆盖
bash stack/vendor/install_fork.sh           # 用最新快照重装
bash stack/vendor/install_fork.sh fork-20261024
```

`install.sh` 在 `npm install` 之后会自动调用它——**npm 会覆盖 lib/，所以这一步
不能省**。

## 升级上游的流程（这是 fork 的代价，也是它的价值）

```bash
# 1) 拉新版，看上游改了哪些文件
curl -sL https://registry.npmmirror.com/dsh-onebot-qq/-/dsh-onebot-qq-<VER>.tgz | tar xz
diff -rq package/lib vendor/fork-<date>/lib

# 2) 逐个文件合并（而不是"打补丁"——我们和上游都有改动）
#    冲突点通常在这几个：
#      prompt.js  人设/平台段拼装结构
#      bridge.js  系统提示 section 注册与时间上下文
#      inbound.js @ 门槛与主动接话概率门
#      index.js   Config schema 与 gateConfig 白名单

# 3) 合并完做语法自检 + 起服务验证
bash stack/vendor/install_fork.sh --check
systemctl restart dsh-onebot
bash /opt/dsh/verify.sh
```

## 已核实的上游漂移（2026-10-24）

```
本地 fork ≈ 0.9.3 + 我们 5 处补丁
上游已到  0.9.6

drift：prompt.js 96 行 → 31 行（readPersona 搬走）
       bridge.js 的「当前时间」拼装不存在了
       inbound.js requireMention 还在但代码块格式变了
       index.js groupActivationMinutes 改名/挪位
       cq.js 未变（我们那条表情兜底仍可打）
```

所以**升级是 fork 合并，不是打 5 个补丁**。这也是为什么要显式 vendor：
至少现在能一键对比、一键回滚，而不是靠记忆猜哪里改过。
