# ============ 模型 ============
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: platform
    model: __LLM_MODEL__
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      platform:
        displayName: 主线路
        apiKeyEnv: QWEN_API_KEY
        baseURL: http://127.0.0.1:8890/v1
        api: openai-completions
        reasoning: "off"
        compat:
          thinkingFormat: deepseek
        defaultContextWindow: 32768
        defaultMaxTokens: 800
        models:
          - id: __LLM_MODEL__
            name: __LLM_MODEL__
            contextWindow: 65536
            maxTokens: 800
            input:
              - text
              - image
      deepseek:
        displayName: DeepSeek
        apiKeyEnv: DEEPSEEK_API_KEY
        baseURL: https://api.deepseek.com
        api: openai-completions
        reasoning: "off"
        compat:
          thinkingFormat: deepseek
        defaultContextWindow: 32768
        defaultMaxTokens: 800
        models:
          - id: deepseek-flash
            name: DeepSeek-V4.1-Flash
            contextWindow: 65536
            maxTokens: 800
            input:
              - text
              - image

# ============ 上下文治理 ============
- id: compaction-basic
  name: "@deepseek-ai/dsh-compaction-basic"
  disabled: false
  config:
    auto: true
    headroomTokens: 16384
- id: image-offload
  name: "@deepseek-ai/dsh-compaction-image-offload"
  disabled: false
- id: tool-result-pruner
  name: "@deepseek-ai/dsh-compaction-tool-result-pruner"
  disabled: false
  config:
    thresholdChars: 8192
    headChars: 1024
    tailChars: 256

# ============ 工具面收窄（群机器人不需要这些）============
- id: tool-bash
  disabled: true
- id: tool-pwsh
  disabled: true
- id: tool-fs
  disabled: true
- id: tool-fs-search
  disabled: true
- id: tool-jobs
  disabled: true
- id: tool-subagent
  disabled: true
- id: tool-subagent-control
  disabled: true
- id: tool-subagent-list-agents
  disabled: true
- id: tool-subagent-fork
  disabled: true
- id: tool-workflow
  disabled: true
- id: tool-goal
  disabled: true
- id: tool-todo
  disabled: true
- id: tool-skill
  disabled: true
- id: tool-plugin-manager
  disabled: true

# ============ 记忆 / 日记（MCP，按需调用，不常驻注入）============
- id: mcp-memory
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: memory
    transport: stdio
    command: python3
    args:
      - /opt/dsh/memory_server.py
    env:
      MEMORY_DIR: /opt/dsh/memory

# ============ QQ 侧策略 ============
- id: dsh-onebot
  name: dsh-onebot-qq
  config:
    mode: reverse
    host: 0.0.0.0
    port: 8643
    accessToken: "__WS_TOKEN__"
    workspacePath: "/opt/dsh/workspace"
    botQQ: "__BOT_QQ__"

    # --- 谁能跟她说话 ---
    requireMention: true
    groupMentionAtOnly: true
    dmPolicy: allowlist
    allowFrom:
      - "__OWNER_QQ__"
    groupPolicy: open
    allowAllUsers: false
    autoSteerPolicy: admin
    restrictedMemberPrefix: true

    # --- 群内行为 ---
    groupActivationMinutes: 10
    groupMuteMinutes: 10
    dedupWindowSeconds: 300
    newMessagePolicy: queue
    interimMessages: false
    interimRecall: true
    deferredTimeoutMs: 0

    # --- 频率与预算 ---
    rateLimitPerMinute: 8
    actionRatePerMinute: 20
    actionRatePerDay: 400

    # --- 主动接话（未 @ 她的群消息里，挑一小部分「接住」）---
    # 接住只算这一条，不进激活窗口：说完就结束，下次还得 @
    casualEngageRate: 0.2         # 群里随机抽消息回一句：两成概率（每小时最多 3 次）
    casualEngageCooldownMs: 60000 # 两次接话的最小间隔
    stickerNudgeRate: 0.6         # 每轮按此概率附一句「要不要配张表情」提醒（她自选，0=关）
    casualEngageMaxPerHour: 3     # 把一小时切成 3 格，每格最多接一次

    # --- 媒体 ---
    inboundImageMaxPx: 1024
    outboundImageMaxBytes: 8388608
    maxVoiceBytes: 15728640
    inboundFileMaxBytes: 20971520
    sttEnabled: false
    textImageThreshold: 300
    fontFiles:
      - "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
      - "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
    fontFamilies:
      - "WenQuanYi Zen Hei"
      - "Noto Sans CJK SC"

    # --- 上下文与日志 ---
    chatIdleEvictDays: 5
    recordInbound: false
    traceEnabled: false
    traceLevel: warn
    actionAuditEnabled: true
    logLevel: info
    unknownCommand: intercept
    injectEnabled: true
    injectDryRun: true
    injectIntervalMs: 2000
    adminUsers:
      - "__OWNER_QQ__"

# ============ 经验/梦境（按需，自动部分限额度）============
- id: dream
  name: '@stardustlc/dsh-dream'
  config:
    maxSessions: 6
    maxCharsPerSession: 3000
    autoCollect: true
    autoRetrieve: true
    autoMaxCallsPerDay: 2
    autoCooldownMs: 1800000

# ==== TOOLMODE-SECTION ====
# 当前模式：chat（由 /opt/dsh/toolmode.py 生成，别手改这段）
- id: preset-standard
  name: '@deepseek-ai/dsh-agent-preset'
  config:
    id: standard
    order: 1
    plugins:
      - id: persona
        name: '@deepseek-ai/dsh-persona'
        config:
          prefix: You are a coding agent powered by the {{model}} model.
      - id: agent-instructions
        name: '@deepseek-ai/dsh-agent-instructions'
        config:
          maxBytes: 65536
      - id: time-context
        name: '@deepseek-ai/dsh-time-context'
      - id: tool-bash
        name: '@deepseek-ai/dsh-tool-bash'
        disabled: true
      - id: tool-pwsh
        name: '@deepseek-ai/dsh-tool-pwsh'
        disabled: true
      - id: tool-fs
        name: '@deepseek-ai/dsh-tool-fs'
        disabled: true
      - id: tool-fs-search
        name: '@deepseek-ai/dsh-tool-fs-search'
        disabled: true
        config:
          sampleOverCapGlobResults: false
      - id: tool-jobs
        name: '@deepseek-ai/dsh-tool-jobs'
        disabled: true
      - id: tool-schedule
        name: '@deepseek-ai/dsh-tool-schedule'
        disabled: true
      - id: skill-filesystem
        name: '@deepseek-ai/dsh-skill-filesystem'
        disabled: true
      - id: tool-skill
        name: '@deepseek-ai/dsh-tool-skill'
        disabled: true
      - id: command-goal
        name: '@deepseek-ai/dsh-command-goal'
        disabled: true
      - id: tool-goal
        name: '@deepseek-ai/dsh-tool-goal'
        disabled: true
      - id: planning
        name: cordis:group
        disabled: true
        group: true
        isolate:
          planMode: true
        config:
          - id: plan-mode
            name: '@deepseek-ai/dsh-plan-mode'
            disabled: true
            config:
              section: >
                You are in plan mode. Stay in plan mode until the user approves
                your plan through exit_plan_mode or switches the session mode.
                Imperative language to implement changes means plan the
                implementation, not execute it. A user's conversational
                agreement — including an answer confirming something you asked —
                approves nothing and does not end plan mode; fold the confirmed
                decision into the plan and submit it through exit_plan_mode.


                Explore first. Use non-mutating reads, searches, static
                analysis, and checks to ground the plan in the actual
                repository. Do not edit or write files, change configuration,
                run formatters or code generation that rewrites tracked files,
                commit, or otherwise carry out the plan. Prefer existing
                functions and patterns over new machinery.


                The tool catalog stays the same across modes for request-cache
                stability. These plan-mode rules override any later tool
                description or guidance that suggests using mutation tools;
                those tools remain listed to keep the tool catalog unchanged. Do
                not use todo_write to track this planning phase: it tracks
                implementation after an approved plan, while the plan itself
                belongs in exit_plan_mode.


                Resolve discoverable facts by inspection. Use ask_user_question
                only for user-owned choices or material ambiguity that
                inspection cannot answer. Do not ask the user where code lives
                or how current behavior works when you can find out.


                Make the plan decision-complete: state the goal and success
                criteria; group implementation changes by subsystem; identify
                public API, schema, and data-flow changes; cover edge cases,
                failure modes, tests, acceptance criteria, and explicit
                assumptions. Keep it concise enough to review but detailed
                enough that another engineer can implement it without making
                design decisions.


                When ready, call exit_plan_mode with the complete plan markdown,
                starting with a # title. Make exit_plan_mode the only and final
                tool call in that assistant response: it presents the plan for
                approval, and implementation begins only in a later step after
                approval. Do not paste the final plan as a plain reply or ask
                "should I proceed?" through prose or ask_user_question. If
                review rejects it, incorporate the feedback and present again.
                If the review channel is unavailable or aborted, stay in plan
                mode and ask the user to switch modes manually; do not proceed
                with implementation.
      - id: compaction
        name: cordis:group
        group: true
        isolate:
          compaction: true
          toolResultPruner: true
        config:
          - id: compaction-basic
            name: '@deepseek-ai/dsh-compaction-basic'
          - id: command-compact
            name: '@deepseek-ai/dsh-command-compact'
          - id: tool-result-pruner
            name: '@deepseek-ai/dsh-compaction-tool-result-pruner'
            config:
              thresholdChars: 8192
              headChars: 4096
              tailChars: 1024
      - id: delegation
        name: cordis:group
        disabled: true
        group: true
        isolate:
          workflowEngine: true
        config:
          - id: tool-subagent-control
            name: '@deepseek-ai/dsh-tool-subagent-control'
            disabled: true
          - id: tool-subagent-list-agents
            name: '@deepseek-ai/dsh-tool-subagent-control/list-agents'
            disabled: true
          - id: tool-subagent
            name: '@deepseek-ai/dsh-tool-subagent'
            disabled: true
            config:
              provider: spawn
              toolName: subagent
              modelSelectionSettings: true
          - id: tool-subagent-fork
            name: '@deepseek-ai/dsh-tool-subagent'
            disabled: true
            config:
              provider: fork
              toolName: subagent_fork
          - id: workflow-ptc
            name: '@deepseek-ai/dsh-workflow-ptc'
            disabled: true
            config:
              provider: spawn
          - id: tool-workflow
            name: '@deepseek-ai/dsh-tool-workflow'
            disabled: true
      - id: tool-ask-user
        name: '@deepseek-ai/dsh-tool-ask-user'
        disabled: true
      - id: tool-todo
        name: '@deepseek-ai/dsh-tool-todo'
        disabled: true
        config:
          allowParallelInProgress: true
      - id: tool-web
        name: '@deepseek-ai/dsh-tool-web'
        disabled: true
        config:
          fetch: true
          searchTimeoutMs: 60000
      - id: present
        name: '@deepseek-ai/dsh-tool-present'
        disabled: true
      - id: tool-plugin-manager
        name: '@deepseek-ai/dsh-plugin-manager/tools'
        disabled: true
      - id: tool-plugin-market
        name: '@deepseek-ai/dsh-plugin-market/tools'
        disabled: true
- id: dream
  disabled: true
