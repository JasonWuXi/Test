# Pi Package 基础知识（基于 Pi 0.87.1 官方文档）

> 研究日期：2026-09-27。
> 依据：`@earendil-works/pi-coding-agent@0.87.1` npm 发行包内的 `docs/`（packages、extensions、skills、prompt-templates、settings、security、how-pi-works、message-types、tui、compaction、rpc-commands）、`dist/core/extensions/types.d.ts` 与 `examples/extensions/`。0.87.1 是 npm `latest`（2026-09-22 发布），也是 pi-technical 当前的宿主版本。
> 在线版本：<https://github.com/earendil-works/pi/tree/main/packages/coding-agent/docs>；包画廊：<https://pi.dev/packages>。
> 本文只记录文档或源码能证实的内容；推断会明确标注“推断”。

## 1. Package 是什么

Pi package 把 **extension、skill、prompt template、theme** 四类资源作为一个单元来安装和分发。它就是一个普通目录或 npm 包：可以沿用约定目录，也可以在 `package.json` 的 `pi` 字段里显式声明资源路径，还能携带自己的运行时依赖。官方建议在两种情况下使用 package：定制需要通过 npm/git 共享；或者几个资源本来就属于同一项功能。

| 资源 | 本质 | 如何进入模型上下文 | 适合承载 |
|---|---|---|---|
| Extension | TypeScript 模块，在 Pi 进程内执行 | 工具定义、自定义消息、system prompt 片段 | 可执行能力：工具、命令、事件钩子、UI、子进程管理 |
| Skill | 含 `SKILL.md` 的目录（Agent Skills 规范） | 启动时只注入 name、description 和路径，需要时模型再读取全文 | 领域解读规则、操作流程，可附带脚本和参考资料 |
| Prompt template | Markdown 文件，变成 `/命令` | 展开后作为用户消息 | 固化高频提问 |
| Theme | JSON 调色板 | 不进入上下文 | 终端外观 |

### 1.1 两种声明方式

约定目录（没有 `pi` manifest 时自动发现）：

```text
my-pi-package/
├── package.json
├── extensions/   # .ts / .js，或含 index.ts 的子目录
├── skills/       # 含 SKILL.md 的目录
├── prompts/      # .md
└── themes/       # .json
```

显式 manifest（资源放在别处或需要筛选时使用）：

```json
{
  "name": "my-pi-package",
  "keywords": ["pi-package"],
  "pi": {
    "extensions": ["./src/extension.ts"],
    "skills": ["./resources/skills"],
    "prompts": ["./resources/prompts/*.md"],
    "themes": ["./resources/themes/*.json"]
  }
}
```

- 路径相对于包根目录；数组支持 glob 和排除写法。点号开头或经符号链接的资源根目录要直接列出，因为 glob 遍历找不到它们。
- `pi-package` 关键字让 npm 包出现在 Pi 包画廊中；可选的 `pi.image` / `pi.video` 字段提供画廊预览。

## 2. 安装、加载与作用域

| 操作 | 命令 |
|---|---|
| 安装 | `pi install npm:@scope/pkg@1.0.0`、`pi install git:github.com/u/repo@v1`、`pi install ./local-package` |
| 写入项目设置 | 加 `--local` / `-l`，写入 `.pi/settings.json`；不加则写入 `~/.pi/agent/settings.json` |
| 仅本次试用 | `pi -e <source>`，不写设置 |
| 列出、移除 | `pi list`、`pi remove <source>` |
| 更新 | `pi update --extensions`（全部包）、`pi update <source>`（单个）、`pi update --all`（Pi 与全部包） |
| 开关资源 | `pi config`（Tab 切换作用域；`pi config --local` 从项目层开始） |
| 会话内生效 | 修改后执行 `/reload` |

来源语义：

| 来源 | 行为 |
|---|---|
| npm | 装到 Pi 的 npm 目录；指定版本即锁定 |
| git | clone 后对齐到指定 ref；tag/commit 被锁定，`update` 不会移动已配置的 ref |
| URL | 按 git 处理 |
| 本地路径 | 不复制，直接从解析后的路径加载；相对路径以所在 settings 文件为基准；指向文件时加载单个 extension，指向目录时按包规则发现 |

- **依赖安装**：官方说明“Pi 在安装 npm 或 git 来源时安装包依赖”。文档没有说本地路径会自动安装依赖。推断：本地安装需要自己准备依赖。pi-technical 没有运行时 npm 依赖，只有 peer 和 dev 依赖，正好避开这个问题。
- **项目信任**：项目级 package 要等项目信任确定后才会安装和加载。package 可以执行代码，skill 也可以让模型运行程序，所以要先审阅来源。
- **资源过滤**（settings 中的对象形式）：省略某类属性表示全部加载，`[]` 表示不加载；`!pattern` 按 glob 排除，`+path` 精确包含，`-path` 精确排除。过滤只能收窄 manifest 已声明的资源，不能暴露额外资源。
- **身份与去重**：npm 包按包名识别，git 包按去掉 ref 的仓库 URL 识别，本地包按解析后的绝对路径识别，同一个包不会通过等价声明加载两次。同一个包同时出现在个人和项目设置里时，项目条目通常替换个人条目；如果项目条目设了 `autoload: false`，它就作为个人条目之上的过滤增量。

## 3. Extension 能力

### 3.1 形态与加载

- 默认导出一个工厂函数，接收 `ExtensionAPI`，可以同步也可以异步（Pi 会等待异步工厂完成再继续启动）。
- Pi 通过 `jiti` 加载，TypeScript 不需要单独编译。
- Extension 在 Pi 进程内运行，拥有相同的操作系统权限，能看到 prompt、工具调用、文件、凭据和会话历史。

### 3.2 集成点

| 能力 | 主要 API |
|---|---|
| 观察或修改生命周期 | `pi.on(event, handler)`，返回取消订阅函数 |
| 模型可调用的操作 | `pi.registerTool()` |
| `/` 命令、快捷键、CLI 参数 | `pi.registerCommand()`、`pi.registerShortcut()`、`pi.registerFlag()` |
| 注入消息 | `pi.sendMessage()`（自定义消息）、`pi.sendUserMessage()`（用户消息，总是触发一轮） |
| 持久化不进上下文的数据 | `pi.appendEntry()` |
| 切换工具、模型、思考级别 | `pi.setActiveTools()`、`pi.setModel()`、`pi.setThinkingLevel()` |
| 模型提供方 | `pi.registerProvider()` |
| 渲染 | `pi.registerMessageRenderer()`、`pi.registerEntryRenderer()`、`ctx.ui.*` |
| 扩展间通信 | `pi.events`（`emit(channel, data: unknown)` / `on(channel, handler)`） |
| 执行外部命令 | `pi.exec(command, args, options)` |

### 3.3 事件（0.87.1 `types.d.ts`）

| 类别 | 事件 |
|---|---|
| 资源与信任 | `project_trust`（只有个人级和命令行 extension 能参与）、`resources_discover` |
| 会话 | `session_start`（reason：startup/reload/new/resume/fork）、`session_before_switch`/`_fork`/`_compact`/`_tree`、`session_compact`、`session_compact_failed`、`session_tree`、`session_info_changed`、`session_shutdown` |
| 上下文与请求 | `before_agent_start`、`context`、`context_with_system`、`before_provider_request`、`before_provider_headers`、`after_provider_response`、`cache_warming_decision` |
| Agent 生命周期 | `agent_start`、`agent_end`、`agent_before_settle`（最后一个可操作边界，可追加条目并请求一次续跑）、`agent_settled`（只通知：Pi 不会再自动继续） |
| 轮次与消息 | `turn_start`、`turn_end`（可操作边界）、`message_start`、`message_update`、`message_end`（可替换已定稿消息） |
| 工具 | `tool_call`（可改输入或拦截）、`tool_result`（可组合改写）、`tool_execution_start`/`_update`/`_end` |
| 输入与模型 | `input`、`user_bash`、`model_select`、`thinking_level_select`、`ui_prompt_start`/`_end` |

要点：handler 按加载和注册顺序执行；有的事件只通知，有的可以变换、替换或取消，要看各事件声明的结果类型。`turn_end` 和 `agent_before_settle` 可以返回 `continue: true` 请求一次续跑，续跑条件必须加保护，否则会循环。

### 3.4 工具

`ToolDefinition` 的字段：`name`、`label`、`description`、`promptSnippet`（写入 system prompt 的 Available tools 段；不提供时，自定义工具不出现在该段）、`promptGuidelines`、`parameters`（TypeBox schema）、`prepareArguments`、`executionMode`（`"sequential"` / `"parallel"`）、`execute(toolCallId, params, signal, onUpdate, ctx)`、`renderCall`、`renderResult`、`renderShell`、`constrainedSampling`。

- 返回值必须有给模型看的 `content`，以及用于渲染或状态重建的 `details`（没有时写 `details: undefined`）。
- 要让模型看到失败结果就在 `execute()` 里 throw；正常返回的对象不会被标记为错误。`terminate: true` 表示跳过自动续跑，并且要求同一批次的所有工具都同意。
- 同一条 assistant 消息里的多个工具调用**可能并行执行**，共享可变内存状态的工具要用 sequential。
- **输出必须截断**：内置上限是 50KB 或 2000 行，先到者为准（`DEFAULT_MAX_BYTES = 50 * 1024`，`DEFAULT_MAX_LINES = 2000`）。包导出了 `truncateHead`、`truncateTail`、`formatSize`；截断时要告诉模型完整输出在哪里。
- 动态工具：先全部注册，再用 `setActiveTools()` 激活。变化会追加进 transcript，不支持增量表达的 provider 会拿到完整检查点，缓存前缀可能失效。
- 嵌套调用模型时，要在结果里带上 `usage`，保证会话用量统计准确。

### 3.5 消息注入语义

`pi.sendMessage({ customType, content, display, details }, { triggerTurn, deliverAs })`：

- `content` 在请求模型前被转成 user 消息；`details` **不发给模型**；`display` 只控制终端是否渲染。
- `deliverAs: "steer"`：当前 assistant 轮的工具执行完后、下一次调用 LLM 前送达（见 rpc-commands.md）。
- `deliverAs: "followUp"`：等 agent 完成全部工作后才送达。
- `deliverAs: "nextTurn"`：先挂起，随用户下一条消息一起注入，不单独触发一轮（源码 `core/agent-session.js` 中的 `_pendingNextTurnMessages`）。
- 源码 `sendCustomMessage` 的完整分支：
  - agent 运行中，且没有设 `triggerTurn: false`：`followUp` 进入 follow-up 队列，其余情况（包括不指定 `deliverAs`）按 steer 处理。
  - agent 空闲且 `triggerTurn: true`：立即开始新一轮。
  - agent 空闲且不触发：只追加到会话。
  - agent 运行中且 `triggerTurn: false`：等当前轮结束后再追加。
- `pi.sendUserMessage()` 总会触发一轮。

### 3.6 状态存储选择（官方表）

| 状态 | 存储 |
|---|---|
| 跟随当前分支的工具状态 | 工具结果的 `details` |
| 持久但不进模型上下文的数据 | `pi.appendEntry()` |
| 需要存储并发送给模型的内容 | `pi.sendMessage()` |
| 跨会话的数据 | 外部存储 |

会话是 JSONL 文件里的一棵树。与分支有关的状态要在 `session_start` 时从 `ctx.sessionManager.getBranch()` 重建，不要用整个文件的全部条目重建，因为被放弃的分支代表另一段历史。

### 3.7 Context 与命令上下文

- `ExtensionContext` 提供：`cwd`、`mode`、`hasUI`、`ui`、`sessionManager`、`modelRegistry`、`model`、`signal`、`isIdle()`、`hasPendingMessages()`、`abort()`、`getContextUsage()`、`compact()`、`shutdown()`、`getSystemPrompt()` 等。
- 命令 handler 拿到的 `ExtensionCommandContext` 还多了 `waitForIdle()`、`reload()`、`newSession()`、`fork()`、`navigateTree()`。这些操作只能在命令里调用，在生命周期 handler 里调用可能死锁。
- 会话替换会让旧 ctx 失效：切换前只保留纯数据，之后用 `withSession` 提供的新 ctx 继续。

### 3.8 UI 与运行模式

- `ctx.ui` 提供 `notify`、`setStatus`、`setWidget`（字符串数组或组件）、`setFooter`/`setHeader`、`custom`（自定义组件或 overlay）、`select`/`confirm`/`input`/`editor` 等。
- Extension 会在 interactive、RPC、JSON、print 四种模式下加载：interactive 有完整终端 UI；RPC 能转发官方支持的对话框和通知，但**不支持自定义终端组件**；JSON 和 print 没有 UI。终端专属行为用 `ctx.mode === "tui"` 守卫，交互类行为用 `ctx.hasUI` 守卫；工具和事件逻辑不能依赖渲染。
- TUI 组件规范：每行不超过给定宽度，用 `visibleWidth()` / `truncateToWidth()` 测量；状态变化后先 invalidate，再调用 `tui.requestRender()`，渲染请求会被合并；昂贵的布局按宽度缓存；不要在 extension 里自建第二个渲染器。

### 3.9 扩展间通信

`pi.events` 是同一个 Pi 进程内所有 extension 共享的事件总线。`emit(channel, data: unknown)` 的 payload 不带类型，接收方要自己校验。见 `examples/extensions/event-bus.ts`。

### 3.10 错误与清理

- Pi 会报告 handler 错误并尽量继续。`tool_call` handler 失败时会拦截该工具（fail-safe）；工具执行失败则变成给模型的错误结果。
- 资源在 `session_shutdown` 中释放，而且清理必须幂等：取消、reload、会话替换、进程退出可能走到同一条路径。

## 4. Skill 规范

- Pi 实现了 [Agent Skills 规范](https://agentskills.io/specification)。Skill 是含 `SKILL.md` 的目录，可附带 `scripts/`、`references/`、`assets/`。
- 渐进加载：启动时只把 name、description 和路径写入 system prompt，任务匹配时模型才去读 `SKILL.md`。**模型可能漏读**，需要时可用 `/skill:name` 强制加载，命令后面的参数会作为用户请求追加。
- Frontmatter 字段：`name`、`description`（必填，决定路由）、`license`、`compatibility`、`metadata`、`allowed-tools`（实验性）、`disable-model-invocation`（只允许显式命令调用）。
- 命名规则：只用小写字母、数字和连字符，不能以连字符开头或结尾，不能有连续连字符，最长 64 字符；description 最长 1024 字符。Pi 不强制 name 与目录名一致，但为了可移植最好一致。
- 格式错误的 `SKILL.md` 或缺少 description 的 skill 不会加载；重名时保留第一个发现的，并给出警告。
- 写法：description 要同时写“做什么”和“何时用”；引用随附文件用相对 skill 目录的路径；环境准备写在 skill 里，运行时依赖在 package 中声明。

## 5. Prompt template 规范

- Markdown 文件名即命令名（`review.md` → `/review`）。Frontmatter 可以写 `description` 和 `argument-hint`（`<必填>`、`[可选]`）。
- 参数替换：`$1`、`$@` / `$ARGUMENTS`、`${1:-默认}`、`${@:N}`、`${@:N:L}`；参数按 shell 规则处理引号。
- 模板在进入 agent 前展开；extension 先通过 `input` 事件拿到原始输入。约定目录只加载直接子级的 `.md`，package manifest 可以用 glob 选择嵌套文件。

## 6. 能力边界（与 L2 设计直接相关）

| # | 边界 | 对 L2 package 的含义 |
|---|---|---|
| 1 | **没有沙箱**：extension、安装脚本和子进程都拥有 Pi 进程的权限；安全边界来自操作系统、容器或虚拟机；项目信任不等于安全 | 只从可信来源安装；代码中不放凭据；包保持只读，不提供下单能力 |
| 2 | **运行时绑定会话**：reload、会话替换、退出都会重建或销毁 extension 运行时；工厂函数里不能启动进程、socket、计时器 | 行情 worker 只能在命令或工具首次使用时启动，在 `session_shutdown` 中幂等关闭；如需跨会话常驻，只能在 Pi 之外单独做守护进程 |
| 3 | **包之间隔离**：各包有独立模块根，不能共享同一个依赖实例，也不能解析别的包没声明的依赖 | L2 包拿不到 pi-technical 的连接对象，只能自建连接（独立 clientId）；跨包协作只能走 `pi.events`（进程内、无类型）或外部进程 |
| 4 | **依赖管理只覆盖 npm** | Python 和官方 ibapi 要自己引导，沿用 pi-technical 的 `bootstrap_ibapi.py`、SHA-256 校验和 `uv.lock` |
| 5 | **上下文是稀缺资源**：工具输出要自己限量；每次推送都占用或触发一次模型轮次 | 高频 L2 数据不能“流进”模型：worker 内部计算，模型只看有界摘要和事件；自动推送必须限频 |
| 6 | **Skill 只是建议**：模型可能不读 | 硬约束（配额、覆盖、可用性、单位）要在代码里校验并标注，skill 只负责解读规则；工具描述里要求先读 skill |
| 7 | **UI 能力随模式变化** | 盘口小组件只在 TUI 下提供，所有功能不依赖 UI |
| 8 | **工具调用可能并行** | 会改动订阅状态的工具设为 sequential，或由控制器自己串行化 |
| 9 | **API 变化快**：0.x 版本，0.83.0（07-29）到 0.87.1（09-22）共发布 12 个版本，整个 CHANGELOG 有 59 行提到 “breaking” | devDependency 锁定宿主版本；每次升级宿主都要跑回归 |
| 10 | **会话会持久化**：`sendMessage` 内容和工具结果都写入会话 JSONL | 不要把原始盘口写进会话；只写有界摘要和 view_id |

## 7. 开发规范清单

1. **package.json**：`type: "module"`；用 `pi` manifest 声明资源；用 `files` 白名单控制发行内容；Pi 提供的包（`@earendil-works/pi-coding-agent`、`@earendil-works/pi-ai`、`@earendil-works/pi-agent-core`、`@earendil-works/pi-tui`、`typebox`）放进 `peerDependencies` 并写 `"*"`，不打包；devDependencies 锁定宿主版本；不公开发布时设 `private: true`，公开发布时加 `pi-package` 关键字。
2. **生命周期**：工厂函数里只注册；`session_start` 时保存 ctx；首次使用时再启动资源；在 `session_shutdown` 中幂等清理；reload 之后不复用旧运行时的状态。
3. **工具**：description 讲清参数语义和出错后怎么恢复；schema 严格（`additionalProperties: false`）；输出不超过 50KB；失败时 throw；尊重 `signal`；提供 `promptSnippet`；会改共享状态的工具设为 sequential。
4. **状态**：按官方表选择存储方式；分支敏感的状态从 `getBranch()` 重建。
5. **消息推送**：`customType` 加命名空间（`<包名>/<类型>`）；`details` 放关联 ID；只在空闲且没有排队消息时投递；给续跑条件加保护。
6. **UI**：用模式守卫；组件遵守宽度、缓存和 invalidate 规范。
7. **Skill**：description 能正确路由；引用文件用相对路径；名称与目录一致。
8. **安全**：连接参数和凭据走环境变量；审阅第三方包；说明包需要的系统权限。
9. **测试**：`tsc --noEmit` 加 node test；覆盖协议校验、输出上限和并发边界；端到端可以通过 SDK 或 RPC 模式驱动。

## 8. pi-technical 如何落地这些规范（可复用模式）

| 规范点 | pi-technical 的做法 | L2 包 |
|---|---|---|
| Manifest | `pi.extensions: ["./extension.ts"]`、`pi.skills: ["./skills"]`，`files` 白名单，peer 依赖为 `pi-coding-agent` 和 `typebox` | 沿用，另外声明 `prompts` |
| 按需启动 | 加载包时不连 Gateway，指定目标后才启动 worker | 沿用 |
| 进程边界 | 单个 Python worker；NDJSON；TypeBox 严格 schema；拒绝非有限数字；run_id 校验；ready 时校验版本；4MB 帧上限；请求超时；最多 16 个并发请求 | 沿用；L2 还要保证高频原始更新**不跨进程** |
| 证据一致性 | 冻结 view（16 位十六进制 view_id，容量 8，manual/automatic pin）；`context` 与 `details` 两个工具；details 不刷新数据 | 沿用 |
| 输出上限 | context 12KB，details 48KB（低于 Pi 的 50KB） | 沿用 |
| 自动研究 | `pi-technical/research` 自定义消息，`triggerTurn` + `followUp`；空闲且无排队消息才投递；用 `message_start` 确认消费，用 `agent_before_settle`/`agent_settled` 记录结果 | 沿用，并加限频、合并和过期丢弃 |
| Skill 触达 | 工具描述里写入 skill 的绝对路径，要求先读 | 沿用 |
| 连接 | 连接参数来自环境变量，不猜；client ID 不与其他连接冲突；不修改 Gateway 设置 | 沿用，改用新前缀和独立 clientId |
| Python 依赖 | 官方 TWS API 归档（10.50.2），核对 SHA-256，用 `uv.lock` 安装 | 沿用同一官方版本 |
| 事实严谨 | 单位写明（shares 或 null）；时间用 UTC；区分 cutoff 与 known_at；逐字段标注可用性；修订事件 E6 | 扩展到 L2：`size_unit`、`received_at`（本地时钟）、覆盖范围、D7 更正 |
| 会话事件 | `session_shutdown` 和 `session_tree` 都停止 worker | 调整：`session_tree` 只清理 view 和待推送内容，保留订阅（理由见方案文档 §5.10） |
