# L2 深度数据 Pi Package：研究与方案（pi-orderbook）

> 研究日期：2026-09-27。宿主版本：Pi 0.87.1；IBKR Python 客户端：官方 TWS API 10.50.2（与 pi-technical 固定的同一官方归档）。
> 配套文档：[Pi Package 基础知识](./pi-package-fundamentals.md)。
> 标注约定：**[官方]** 表示 IBKR 或 Pi 官方文档、源码能证实；**[M0]** 表示必须实测确认，确认前不作为产品承诺；**[设计]** 表示本方案的取舍；带“初值”的阈值要在 M2 用录制数据标定。

## 0. 结论摘要

1. **形态**：新建独立 package `pi-orderbook`（名称待定），结构与 pi-technical 相同：1 个 extension、1 个 skill、若干 prompt 模板和 1 个 Python worker。它使用独立 clientId、独立进程，**不修改 pi-technical**。
2. **价值定位**：L1 包回答“已经发生了什么”（完成柱、指标、结构事件）；L2 包回答“此刻挂着什么，秒级内怎么变化”。L2 的主要价值在于**动态**：挂单墙是被成交吃掉还是被撤走，价位是否在吸收，是否出现扫单，流动性是否变薄，失衡是否持续。区分“被成交消耗”和“被撤单”必须对齐逐笔成交，所以包内同时订阅 tick-by-tick `Last` 作为伴随数据流。[官方] 这项数据属于 L1 订阅。
3. **本账户约束**：美股深度只来自 NASDAQ（TotalView，API 条件已满足）和 IEX（[M0]）两个场所，NYSE、ARCA、Cboe BZX、NASDAQ BX 的订单簿都看不到。因此每条输出都带 `coverage`，并用 Network A/B/C 的 NBBO 对照，量化可见订单簿的代表性。
4. **硬约束**：
   - 默认最多 3 个深度请求（按 100 条行情线计算）。
   - tick-by-tick 最多 5 路。
   - 深度回调不带时间戳。
   - TWS API 没有历史深度接口。
   - 深度数据不含 odd lot。

   对应设计：订阅名额租约、本地接收时间、预热状态、滚动缓冲和可选的本地录制。
5. **工具面**：沿用 L1 已验证的“两个工具 + 固定 view”模式。`orderbook_context` 返回摘要和 view_id；`orderbook_details` 读取 ladder、features、history、trades、events、levels、execution、quality 分区。配套命令 `/orderbook`，事件编号 D0–D7。自动推送默认关闭，开启后限频。
6. **与 L1 协作**：先由模型编排，即把 `technical_context` 中的价位作为 `prices` 参数传给 L2 工具，不做代码耦合。
7. **实施顺序**：先做 M0 实测（权限、语义、单位、名额），没有实测过的内容不写进产品承诺。

## 1. 依据、范围与术语

- **Pi**：见配套文档。关键约束包括：运行时绑定会话；各包的模块根相互隔离；工具输出不超过 50KB；自定义消息的 content 会进入模型上下文。
- **IBKR**：官方 TWS API 文档（ibkrcampus.com/docs）、市场数据订阅文档和 Market Data Pricing 页面。ibapi 源码取自 pi-technical 固定的官方归档 `twsapi_macunix.1050.02.zip`。本次下载的归档，其摘要与 pi-technical 固定的摘要一致。
- **附录材料**：pi-technical 的代码和文档只用来理解现状与约定，不在其上二次开发。
- **范围**：
  - v1 聚焦美股，即 NASDAQ 和 IEX 深度。
  - ICE 期货和债券的深度在 M0 实测后再决定是否纳入。
  - 包只读，不下单。
- **术语**：
  - **可见订单簿**：本账户有深度权限的场所上的挂单之和，不等于全市场。
  - **行（row）**：IBKR 深度回调中按 position 编号的条目，可能带交易所或 MPID。
  - **档（level）**：把行按价格聚合后的条目。
  - **NBBO**：来自 Network A/B/C L1 的全国最优买卖价。

## 2. L2 数据：IBKR 提供什么，本账户能拿到什么

### 2.1 三类行情对比

| | L1 顶层行情 | L2 深度 | 逐笔成交（Time & Sales） |
|---|---|---|---|
| 内容 | 最优买卖价和量、最新成交 | 每侧多行：价格、数量、做市商 MPID 或交易所 | 每笔成交的价格、数量、交易所、特殊条件 |
| TWS API | `reqMktData` | `reqMktDepth` | `reqTickByTickData`（`Last`/`AllLast`/`BidAsk`/`MidPoint`） |
| 采样 | [官方] 文档写“不同于 L1，深度数据不采样、不过滤”，由此可知 L1 是采样数据 | [官方] 不采样、不过滤，但不保证显示每个报价；不含 odd lot | 逐笔 |
| 权限 | L1 订阅 | L2 订阅；NASDAQ 通过 API 取数还需要 EDS | [官方] 需要 L1 订阅 |
| 时间戳 | — | [官方] 回调参数里没有时间戳 | [官方] `time`，秒级 Unix 时间（官方示例按秒格式化） |
| 历史数据 | `reqHistoricalData` | [官方] TWS API 文档只有实时深度请求和交易所清单，没有历史深度接口 | `reqHistoricalTicks`；或设置 `numberOfTicks`，先回放最多 1000 笔 |
| 并发上限（100 条行情线） | 100 条，TWS 与所有 API 连接共用 | 3 个 | 5 路（行情线数的 5%） |

### 2.2 IBKR 深度接口要点

**请求** [官方]：`reqMktDepth(reqId, contract, numRows, isSmartDepth, mktDepthOptions)`。
- `numRows` 是每侧行数，订单簿不足时只返回现有条目。文档没有写上限 [M0]。
- `isSmartDepth=True` 返回与 TWS BookTrader 相同的跨交易所聚合数据；`False` 返回单一交易所的直连数据，类似 TWS 的 Market Depth 窗口。
- `mktDepthOptions` 仅供 IBKR 内部使用，传空即可。

**回调** [官方]：
- `updateMktDepth(reqId, position, operation, side, price, size)`：数据不带做市商或交易所标识时使用。
- `updateMktDepthL2(reqId, position, marketMaker, operation, side, price, size, isSmartDepth)`：数据带标识时使用。`isSmartDepth=True` 时 `marketMaker` 是交易所，否则是做市商 MPID（官方举例：NASDAQ 会返回 MPID）。
- 取值：`operation` 为 0 插入、1 更新、2 删除；`side` 为 0 卖方、1 买方；`size` 的类型是 `Decimal`。

**场所清单**：`reqMktDepthExchanges()` 的结果通过 `mktDepthExchanges(list[DepthMktDataDescription])` 返回。
- [官方] 用户有对应行情订阅时，它返回可以提供深度数据的交易所。
- 10.50.2 的字段是 `exchange`、`secType`、`listingExch`、`serviceDataType`、`aggGroup`。
- 官方页面提到的 isL2 标记要在 M0 用 `serviceDataType` 的实际取值确认，原样保存，不做猜测映射。

**限制与合约代码** [官方]：
- 日历价差和组合（Combos）不支持深度。
- NASDAQ 在 IB Gateway 上仍要用 `ISLAND` 代码，TWS 10.16+ 开启兼容设置后可用 `NASDAQ`。本包以 `reqMktDepthExchanges` 实际返回的代码为准，不写死。

**关键消息码** [官方]：

| 码 | 含义 | 本包处理 |
|---|---|---|
| 309 | 已达到最多 3 个深度请求的上限；API 客户端可以对同一证券发起多个深度请求 | 名额已满：不重试，报告当前占用者 |
| 310 | 取消时找不到对应的深度订阅 | 记录，按幂等处理 |
| 316 | 深度数据被 HALTED，需要重新订阅 | 标记为暂停，退避后重新订阅 |
| 317 | 深度数据被 RESET，应用新条目前要清空深簿 | 清空后重建，记为 D7 |
| 354 / 10089 / 10186 | 未订阅 / 该订阅不支持 API / 未启用延迟数据 | 报 `not_entitled`，**不降级到延迟数据** |
| 2152 | “Market depth smart depth exchanges.”（SMART 深度涉及的交易所说明） | 原文存入 coverage；解析失败不影响运行 |
| 101 | TWS 与 API 的行情线合计已满 | 报告；停止新增 L1 参照订阅 |
| 326 | clientId 已被占用 | 报告，提示检查是否还有另一个 Pi 会话 |
| 1100 / 1101 / 1102 | 连接断开 / 已恢复但数据丢失，需重新提交请求 / 已恢复且数据保留 | 断开时标记 stale；1101 时全量重新订阅并记为 D7；1102 时继续 |
| 10197 | 竞争会话（模拟账户与实盘同时取实时数据） | 报告 |

**名额与连接** [官方]：
- 100–400 条行情线对应 3 个深度请求，以及 5–20 路 tick-by-tick。
- Quote Booster 每包 USD 30/月，增加 100 条 L1 行情线和 1 个深度名额。
- 行情线由 TWS 观察列表和所有 API 连接共用。深度名额是否也与 TWS 的深度窗口共用，官方写得不明确 [M0]。
- 每个 TWS 会话最多接受 32 个 API 客户端，用 clientId 区分。
- 同一合约 15 秒内最多发起 1 次 tick-by-tick 请求。

**合规前置** [官方]：必须先在 Client Portal 签署 Market Data API Acknowledgement，否则 API 取数会返回“未订阅”。

**Python 线程模型** [官方]：ibapi 的读线程把消息放进队列，应用必须运行 `EClient.run()` 来分发回调。在回调里做重计算会拖慢所有消息的处理。

### 2.3 本账户订阅对应的可用 L2

以下对应关系沿用上一轮按官方文档做的核对。

| 订阅 | API 深度可用性 | v1 处理 |
|---|---|---|
| NASDAQ TotalView-OpenView (NP,L2) + EDS (NP,L2) + Network A/B/C | [官方] 满足 IBKR 列出的 API 三项条件，可用，带 MPID | 核心数据源 |
| IEX Depth of Book (NP,L2) | 已订阅，但 IBKR 公开页面没有写 API 条款 [M0] | M0 确认后并入 SMART 覆盖范围 |
| ICE Futures US - Digital Asset Futures (L2)、Gold and Silver (L2) | [官方] 定价页标为 “(L1, L2) Fee Waived”；美国期货行情需要期货交易权限；具体覆盖哪些合约未公开 | v1 不纳入，M0 探测 |
| US and EU Bond Quotes (L2) | 覆盖范围未公开 | v1 不纳入，M0 探测 |
| NYSE OpenBook、NYSE ArcaBook、Cboe BZX、NASDAQ BX TotalView | 未订阅 | 这些场所的深度看不到，在 coverage 中写明 |
| Network A/B/C (L1) | 可用 | 用于 NBBO 对照和逐笔 `Last` |

### 2.4 约束对设计的影响

| 约束 | 影响 | 对策 |
|---|---|---|
| 最多 3 个深度请求 | 同一时间最多深看 3 个标的 | 名额租约：固定 watch 加模型临时租约；状态栏显示 n/3 |
| 场所覆盖不全 | 可见订单簿不等于全市场 | 每条输出带 `coverage`；与 NBBO 对照；统计最优价落在可见订单簿上的时间占比 |
| 深度回调没有时间戳 | 只有本地接收时间，而且受处理积压影响 | 输出 `received_at` 并注明是本地时钟；监测积压；要求主机做 NTP 对时 |
| 逐笔时间只到秒 | 成交与订单簿变化只能近似对齐 | 归因使用容差窗口，结果标注 `estimated`，同时给出匹配比例 |
| 没有历史深度 | 订阅前的订单簿无从得知，动态指标需要预热 | 逐窗口披露预热状态；保留 30 分钟滚动缓冲；可选本地录制 |
| 不含 odd lot；隐藏单和暗池不可见 | 可见量低于真实流动性 | 在输出中声明；执行估计只按可见量计算 |
| 316、317 和断线 | 订单簿会失效 | 清空后重建；用 D7 更正并撤回受影响的结论 |
| tick-by-tick 最多 5 路，同一合约有 15 秒限制 | 3 个深度标的加上余量，每个标的只配 1 路 `Last` | 管理冷却期 |
| 深度 size 的单位没有官方说明（官方只说明 lots 选项影响历史成交量） | 不能默认按股数处理 | M0 核验前 `size_unit = null`；skill 禁止拿它与 L1 的股数直接比较 |

## 3. L2 对交易员 Agent 的价值与应用场景

### 3.1 人类交易员怎样使用 L2

- **看挂单墙**：大额挂单集中的价位常被当作短线支撑或阻力的证据。但墙可以随时撤掉，关键在于价格靠近时墙的结局：被吃掉、被撤走，还是挪到别的价位。
- **看吸收**：大量主动卖单打在某个买价上，价格没有跌破，买量还在不断补回，说明该价位有人承接，可能是冰山单。
- **看扫单和真空**：主动单连续吃掉多档、价格跳过薄弱区，通常说明突破质量较高；订单簿变薄、价差拉大，则意味着风险上升、滑点变大。
- **看失衡**：近端两侧挂量持续失衡，或者最优价位的订单流入流出（OFI）持续偏向一方，通常意味着短时价格压力。Cont、Kukanov 和 Stoikov（2014）发现，在短时间尺度上，价格变化主要由最优价位的订单流失衡驱动。
- **执行决策**：挂限价单还是直接吃单，挂在哪一档，要不要拆单，止损放在墙前还是墙后，这个数量会滑多少点。
- **NASDAQ MPID**：观察哪些做市商在最优价、谁在加挂或撤单。只描述看得到的行为，不推断身份和意图。

### 3.2 工作流 × Agent 角色

| 工作流阶段 | 技术分析型 | 交易执行型 | 兼顾型 | 包内能力 |
|---|---|---|---|---|
| 准备（开盘前或新标的） | 判断能否交易：价差和可见深度是否足够 | 流动性状态、预期冲击成本 | 两者都要 | context、D3、execution |
| 监控关键价位 | 价位处的可见挂单和挂单墙；与 L1 的价位（pivot、ORB、VWAP）对照 | 限价单排队量 | 两者都要 | levels、D1 |
| 确认信号 | 突破：卖墙被吃并伴随扫单，还是墙在价格到达前被撤走；回踩：是否吸收 | 入场时机：失衡和 OFI 的方向 | 两者都要 | D2、D4、D5、D6 |
| 执行 | 不提供（不给交易指令） | 在可见订单簿上逐档成交的估计、分档情况、排队量、滑点 | 只提供执行事实 | execution |
| 持仓管理 | 反向失衡、支撑墙撤走 | 流动性变差（价差扩大、深度变薄）影响止损执行 | 两者都要 | D3、D4、D2 |
| 复盘 | 按时间复述事件序列 | 执行质量对照（需要开启录制） | 两者都要 | events、history、录制回放 |

### 3.3 L2 的证据边界

**能回答**：
- 此刻有权限场所上可见的挂单结构。
- 订阅之后观察到的变化。
- 某个价位的减少量有多少与成交匹配（估计为成交），有多少没有匹配（估计为撤单）。
- 价差和深度相对于本次观察基线的状态。
- 在可见订单簿上成交某个数量的估计。
- NASDAQ 上各 MPID 的可见报价。

**不能回答**：
- 隐藏单、冰山单的保留部分和暗池流动性。
- 未订阅场所（NYSE、ARCA、BZX 等）的订单簿。
- 参与者的身份和意图。MPID 不等于最终客户；“撤单”也不能直接定性为幌骗（spoofing）。
- 订阅开始之前的订单簿。
- odd lot。
- 深度更新的交易所时间戳。
- 未来的价格方向。L2 只提供证据，不提供概率。

### 3.4 与 L1 包的分工

| | pi-technical（L1 / 历史柱） | pi-orderbook（L2） |
|---|---|---|
| 时间尺度 | 5m、1d 完成柱 | 秒级，此刻 |
| 可回放性 | 可用官方历史数据回放 | 只能依靠本地录制 |
| 输出 | 指标、结构、E0–E6 | 可见订单簿、微观结构特征、D0–D7 |
| 数据状态 | 完成柱是确定的，可修订（E6） | 瞬时，很快失效，用 D7 更正 |
| 主要风险 | 数据缺口、单位 | 覆盖不全、时间对齐、名额 |

组合方式：L1 给出“在哪里”，即价位和趋势背景；L2 给出“此刻那个价位上发生了什么”，即挂单和成交的证据。L1 的 E1 突破事件可以用 L2 的 D2（墙被吃掉）和 D6（扫单）来验证。

## 4. 为什么单独成包，为什么这样分层

**单独成包**：
- 数据生命周期不同：L2 是流式数据，L1 包处理的是完成柱。
- 深度名额稀缺，需要独立管理。
- 负载和故障模式不同。
- 没有 L2 权限的用户可以不装。
- skill 路由更清晰。
- Pi 的 package 模型本身支持独立安装，以及用 `pi config` 单独开关。

**Python worker** [设计]：
- IBKR 官方客户端没有 JS 版本。
- 可以复用 pi-technical 已核验的官方归档和引导链。
- 高频处理放在 Pi 进程之外，不影响 Pi 进程里的 TUI 渲染和 agent 循环。

**每个 Pi 会话一个 worker** [设计]：
- 这符合 Pi 运行时绑定会话的约束。
- 代价是多个 Pi 窗口会争用 clientId 和深度名额。
- v1 明确规定一个 clientId 同一时间只服务一个 Pi 会话；第二个会话连接时会收到 326。
- 跨会话共享的守护进程列为待决问题。

**原始更新不跨进程** [设计]：订单簿和特征都在 Python 端维护，TS 端只接收低频消息，包括请求结果、事件、状态，以及不超过 4Hz 的 UI 帧。

## 5. 方案设计

### 5.1 目标与非目标

**目标**：
- **G1**：提供可追溯到数据源的 L2 事实和确定性计算，模型只负责解释（沿用 L1 原则）。
- **G2**：在名额和覆盖范围受限时仍可用，并如实披露限制。
- **G3**：服务三种角色：分析型需要证据，执行型需要可见流动性估计，兼顾型两者都要。
- **G4**：节省上下文：高频数据留在 worker 内，模型只看到有界摘要和事件。
- **G5**：可复现：通过录制和回放支持测试与验收。
- **G6**：所有功能在 TUI、RPC、JSON、print 四种模式下都能用，UI 只是锦上添花。

**非目标**：
- 不下单、不改单。
- 不替代 pi-technical。
- 不给交易指令或概率。
- 不推断参与者身份。
- 不提供超出订阅范围的全市场深度。

### 5.2 总体架构

```text
┌──────────────────────────── Pi 进程 ────────────────────────────┐
│ pi-orderbook extension (TypeScript)                              │
│  tools : orderbook_context / orderbook_details                   │
│  cmd   : /orderbook      prompts: /l2-level /l2-pretrade /l2-review│
│  skill : order-book-analysis                                     │
│  Controller ── Views(冻结快照) ── Presentation(12KB / 48KB)       │
│  AutoPush(限频·合并·空闲门控)   UI(setStatus / setWidget, 仅 TUI)   │
│        │ NDJSON，只传低频消息：请求 / 快照 / 事件 / 状态 / UI 帧≤4Hz  │
└────────┼─────────────────────────────────────────────────────────┘
         ▼
┌─────────────── Python worker（每个 Pi 会话 1 个）─────────────────┐
│ Connection(clientId = PI_ORDERBOOK_IB_CLIENT_ID)                  │
│ EntitlementProbe      SubscriptionManager(深度≤N · 逐笔≤M · 冷却)   │
│ BookBuilder(按 position 增删改 · 316/317 处理 · 按价聚合)           │
│ Tape(Last) + QuoteRef(reqMktData 的 NBBO)                          │
│ FeatureEngine(1s 定时, numpy)     EventEngine(D0–D7)               │
│ RingBuffers(30 min)               Recorder(可选, 仅本地)            │
└────────┼──────────────────────────────────────────────────────────┘
         ▼
  IB Gateway / TWS（与 pi-technical 共用，使用不同 clientId）
```

### 5.3 包结构与 manifest

```text
pi-orderbook/
├── package.json
├── extension.ts                 # 只做注册：工具、命令、事件
├── src/
│   ├── controller.ts            # worker 生命周期、名额视图、view、自动推送
│   ├── worker.ts                # 子进程与 NDJSON 帧（沿用 L1 的做法）
│   ├── protocol.ts              # TypeBox 严格 schema
│   ├── views.ts                 # 冻结 view、pin
│   ├── presentation.ts          # 有界文本（12KB / 48KB）
│   └── ui.ts                    # 状态栏和盘口小组件（仅 TUI）
├── skills/order-book-analysis/
│   ├── SKILL.md
│   └── references/{features.md, events.md}
├── prompts/{l2-level.md, l2-pretrade.md, l2-review.md}
├── python/
│   ├── bootstrap_ibapi.py  pyproject.toml  uv.lock  .python-version
│   └── orderbook/{main, connection, probe, subscriptions, book, tape,
│                  quote_ref, features, events, recorder, replay}.py
├── tests/{node, python, fixtures}   # fixtures 为 M0 录制样本，不进入发行包
├── README.md
└── tsconfig.json
```

```json
{
  "name": "pi-orderbook",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "pi": {
    "extensions": ["./extension.ts"],
    "skills": ["./skills"],
    "prompts": ["./prompts/*.md"]
  },
  "files": ["extension.ts", "src/", "skills/", "prompts/",
            "python/orderbook/**/*.py", "python/pyproject.toml", "python/uv.lock",
            "python/bootstrap_ibapi.py", "python/.python-version", "README.md"],
  "scripts": {
    "typecheck": "tsc --noEmit",
    "test": "node --experimental-strip-types --test tests/node/*.test.ts"
  },
  "peerDependencies": { "@earendil-works/pi-coding-agent": "*", "typebox": "*" },
  "devDependencies": { "@earendil-works/pi-coding-agent": "0.87.1", "@types/node": "22.19.19",
                       "typebox": "1.3.27", "typescript": "5.9.3" }
}
```

- 盘口小组件如果只用 `setWidget(key, string[])`，就不需要 `@earendil-works/pi-tui`；需要自定义组件时，再把它作为 `"*"` peer 依赖加进来。
- Python 依赖：`ibapi==10.50.2`（与 L1 同一官方归档和摘要）加 `numpy`，不需要 TA-Lib。

### 5.4 Python worker

#### 5.4.1 连接与探测

- 连接参数只从 `PI_ORDERBOOK_IB_HOST`、`PI_ORDERBOOK_IB_PORT`、`PI_ORDERBOOK_IB_CLIENT_ID` 读取，校验规则同 L1，不猜测。clientId 必须与 pi-technical 不同。
- 以 `nextValidId` 作为连接就绪信号 [官方]。
- 只请求实时行情（`reqMarketDataType(1)`）；没有权限就报错，不降级到延迟数据。
- 探测内容：`reqMktDepthExchanges` 的原始结果；用 `reqContractDetails` 解析合约，得到 conId、primaryExchange、minTick；2152 消息的原文。探测结果写入 `quality.entitlements`。

#### 5.4.2 订阅与名额管理

- **资源**：
  - 深度名额：`PI_ORDERBOOK_DEPTH_SLOTS`，默认 3；收到 309 后自动下调。
  - tick-by-tick 路数：默认最多用 3 路，给其他程序留余量。
  - L1 参照行：每个标的 1 行。
- **订阅键**：`(conId, book=smart|direct:<exchange>, rows)`。同一标的同时开 SMART 和直连 NASDAQ 是否只占 1 个名额，按 309 的官方说明“可以对同一证券发起多个请求”来理解，需 [M0] 确认。
- **租约**：
  - `/orderbook watch` 建立固定租约，永远不会自动回收。
  - `orderbook_context` 隐式建立模型租约。模型租约空闲超过 TTL（初值 10 分钟）才能被新的模型租约回收。
  - 名额满且没有可回收的租约时，报 `depth_capacity_exhausted`，并列出当前占用者。
- **冷却与退避**：同一合约的 tick-by-tick 请求至少间隔 15 秒 [官方]；316 或 1101 之后按指数退避重新订阅。
- **状态机**：`requested → warming → live → (stale | halted | reset) → live | closed`。

#### 5.4.3 BookBuilder

- 每侧维护一个行数组 `(position, price, size, attribution)`，按 insert/update/delete 更新。行在插入和删除后怎样移位、超出 `numRows` 时怎样处理，都要用 M0 录制的数据写成测试 [M0]。
- 按价格聚合成档：总量、行数、交易所或 MPID 构成。`attribution_kind` 取 `exchange`、`mpid` 或 `none`。
- 317：清空订单簿，`reset_epoch++`，记录缺口，产生 D7。316：标记暂停并重新订阅。
- 跨场所的可见订单簿可能出现交叉或锁定报价：记为质量标记，**不当作断言失败**。position 越界等异常同样记为质量标记。
- 时间：`received_at` 用本地 UTC 时间（纳秒精度）；持续时长用单调时钟计算。同时记录回调处理积压，即队列等待时间的估计。

#### 5.4.4 逐笔与 NBBO 参照

- `reqTickByTickData(reqId, contract, "Last", numberOfTicks=N≤1000, ignoreSize=False)`：订阅后立即回放最近的成交，缩短逐笔的预热时间 [官方]。`ignoreSize` 只对 BidAsk 生效。
- `reqMktData` 提供 NBBO（L1 是采样数据）。它只作参照，不当作真值。
- 主动方估计：成交价与接收时的 NBBO 比较，高于或等于卖价记为主动买，低于或等于买价记为主动卖，其余记为 mid 或 unknown。结果一律标 `estimated`。
- 成交时间精确到秒，订单簿时间是本地接收时间。归因容差初值为 ±1.5 秒，并按实测积压加宽；输出同时给出匹配比例和容差。

#### 5.4.5 缓冲与录制

- **滚动缓冲**：
  - 1 秒特征：保留 30 分钟。
  - 事件：最近 200 条。
  - 原始成交：最近 5 分钟，另存聚合数据。
  - 订单簿增量：最近 120 秒，用于归因。
- **录制**：可选，默认关闭。
  - 内容：原始回调加本地时间戳，按标的和日期写成压缩 NDJSON。
  - 位置：`PI_ORDERBOOK_RECORD_DIR`，不在包目录内。需要设定保留期。
  - 用途：回放测试和参数标定。
  - 约束：只在本地使用，不分发。
- **回放**：把录制数据喂给同一套 BookBuilder、FeatureEngine、EventEngine，得到确定性的输出，用于黄金测试。回放入口只放在测试里，不进入产品（与 L1 相同）。

### 5.5 特征（确定性计算）

- 静态特征按 1 秒频率计算；动态特征使用 10s、60s、300s 三个窗口。
- 每个特征都带 `unit`、`availability`、`window` 和 `cutoff`，规则与 L1 的逐字段可用性相同。
- 带“初值”的参数要在 M2 用录制数据标定。

| ID | 定义 | 单位 | 可用条件 |
|---|---|---|---|
| `spread` | `ask1 − bid1`，同时给出 tick 数（÷ min_tick）和 bps（÷ mid × 10⁴） | 价格 / ticks / bps | 两侧都有挂单 |
| `mid` | `(bid1 + ask1) / 2` | 价格 | 同上 |
| `weighted_mid` | `(bid1·Q_ask1 + ask1·Q_bid1) / (Q_bid1 + Q_ask1)` | 价格 | 同上；**它不是** Stoikov（2018）的 micro-price |
| `imbalance_l1` | `(Q_bid1 − Q_ask1) / (Q_bid1 + Q_ask1)` | [−1, 1] | 同上 |
| `depth_band` | mid ± N ticks 内每侧可见量之和（N 初值 10） | size_unit | 行数不能覆盖 N ticks 时标记 `partial` |
| `imbalance_band` | `(B − A) / (B + A)`，B、A 取自 `depth_band` | [−1, 1] | 同上 |
| `level_stats` | band 内的档数、档量中位数和最大值 | 个数 / size_unit | — |
| `walls` | band 内满足 `Q ≥ α × 档量中位数`（α 初值 4）且 `Q ≥ Q_min` 的档，附首次出现时间、存续秒数、交易所或 MPID 构成 | — | 存续时长需要预热 |
| `holes` | band 内连续没有可见量的 tick 区间 | ticks | 只在可见行范围内判断 |
| `inside_on_book` | 每侧可见订单簿的最优价是否等于 NBBO，以及 300 秒内相等的时间占比 | 布尔值 / 比例 | 需要 NBBO 参照 |
| `ofi_l1` | 最优价位订单流失衡：每次更新的 `e_n` 之和，除以窗口内最优档的平均深度（见下方公式） | size_unit / 比例 | 窗口已完整观察 |
| `flow_decomp` | 每侧 band 内的新增量、与成交匹配的减少量（估计成交）、未匹配的减少量（估计撤单） | size_unit | 需要逐笔；标注 `estimated` 和容差 |
| `trade_flow` | 成交量、笔数、主动买量、主动卖量和未知量的估计、delta、成交 VWAP | 成交 size 单位 [M0] | 需要逐笔 |
| `regime` | 当前 spread、depth_band 分别除以本次订阅内的滚动中位数 | 比例 | 至少观察 300 秒 |
| `update_rate` | 每秒订单簿更新次数 | 次/秒 | — |
| `refill` | 近端价位在与成交匹配的减少之后，数量又恢复的次数 | 次数 | 需要逐笔 |
| `sweep` | Δt 内成交跨越的价位数，以及被移除的档数 | 档数 | 需要逐笔 |

`ofi_l1` 中每次更新的 `e_n` 按 Cont、Kukanov、Stoikov（2014）的定义计算：

```text
e_n = 1{Pᵇₙ ≥ Pᵇₙ₋₁}·qᵇₙ − 1{Pᵇₙ ≤ Pᵇₙ₋₁}·qᵇₙ₋₁ − 1{Pᵃₙ ≤ Pᵃₙ₋₁}·qᵃₙ + 1{Pᵃₙ ≥ Pᵃₙ₋₁}·qᵃₙ₋₁
```

其中 P 是最优买价（b）或卖价（a），q 是对应的挂单量。

### 5.6 事件 D0–D7

- 编号用 D 前缀，避免与 L1 的 E0–E6 混淆。
- 每个事件都带：`id`、`kind`、`cutoff`（对应订单簿状态的本地接收时间）、`known_at`、`facts`（数值）、`params`（阈值）、`coverage` 快照和质量标记。
- 去重规则：同一 `(标的, id, kind, 价格)` 在冷却期内只产生一次。

| ID | 名称 | 触发条件（初值，M2 标定） | kind |
|---|---|---|---|
| D0 | baseline | 订阅就绪，并完成 60 秒预热 | 基线，描述当前状态，**不代表发生了变化** |
| D1 | wall_formed | band 内出现挂单墙，并存续 ≥ 10 秒 | `bid` / `ask` |
| D2 | wall_resolved | 已存续的墙在 5 秒内减少 ≥ 70% | `consumed`（匹配成交占减少量 ≥ 60%）/ `pulled`（≤ 20%，且 ±3 ticks 内 10 秒没有重现）/ `moved`（±3 ticks 内出现相近数量）/ `mixed`；facts 给出匹配比例和容差 |
| D3 | liquidity_regime | depth_band 比值 < 0.5，或 spread 比值 > 2，持续 ≥ 20 秒 | `thinning` / `widening` / `recovered` |
| D4 | imbalance_shift | imbalance_band 越过 ±0.4 且持续 ≥ 15 秒，或 60 秒 OFI 的符号翻转并持续 | `bid_heavy` / `ask_heavy` / `neutralized` |
| D5 | absorption | 同一价位 60 秒内估计的主动成交量 ≥ 该价位平均可见量的 3 倍，价格没有穿过该价位，且补单 ≥ 2 次 | `bid` / `ask` |
| D6 | sweep | 2 秒内成交跨越 ≥ 3 个价位，且对应档位被移除 | `up` / `down` |
| D7 | quality | 317 重置、316 暂停、1100/1101 断线、覆盖范围变化、积压超过阈值、交易时段内长时间没有更新 | 受影响时间窗内的 D1–D6 标记为 `withdrawn` 或 `superseded`（语义同 L1 的 E6） |

### 5.7 View 与数据模型

沿用 L1 的冻结 view：16 位十六进制 view_id，容量 8，分 manual 和 automatic 两种 pin，单个 view 不超过 4MB。下面是 snapshot 的字段示意，**数值只是占位，不是真实数据**：

```jsonc
{
  "identity": { "con_id": 0, "symbol": "XYZ", "sec_type": "STK", "currency": "USD",
                "primary_exchange": "NASDAQ", "min_tick": 0.01 },
  "book": { "mode": "smart", "venue": null, "rows": 10, "attribution": "exchange" },
  "coverage": {
    "depth_exchanges": ["<来自 reqMktDepthExchanges 的原值>"],
    "smart_depth_notice_raw": "<2152 原文，可能为 null>",
    "nbbo_reference": "reqMktData",
    "inside_on_book": { "bid": true, "ask": false },
    "inside_on_book_ratio_300s": { "bid": null, "ask": null },
    "not_covered_note": "未订阅场所的挂单不可见"
  },
  "cutoff": "<最后一次订单簿更新的本地接收时间, UTC>",
  "captured_at": "<UTC>",
  "clock": { "book_time": "local_receive", "trade_time": "ib_seconds", "backlog_ms_p95": null },
  "size_unit": null,
  "warmup": { "observed_s": 0, "needed_s": { "10s": 10, "60s": 60, "300s": 300 } },
  "ladder": { "bids": [], "asks": [] },
  "features": {},
  "events": [],
  "quality": { "state": "warming", "reset_epoch": 0, "halted": false, "update_rate_hz": null,
               "errors_seen": [], "gaps": [], "entitlements": {} },
  "budget": { "depth": { "used": 1, "max": 3 }, "tick_by_tick": { "used": 1, "max": 3 } }
}
```

### 5.8 工具、命令与 prompt 模板

两个工具都要提供 `promptSnippet`；工具描述里写入 skill 的绝对路径，并要求模型先读（沿用 L1 做法）。

```text
orderbook_context
  参数：
    target?  "AAPL" | "conid:265598"（只有一个订阅时可省略）
    book?    "smart" | "direct"，默认 smart
    venue?   book=direct 时必填，取值必须来自探测结果（如 ISLAND）
  executionMode：sequential（会改变订阅状态）
  行为：确保该标的有订阅（可能建立模型租约），然后生成一个冻结 view
  返回（≤ 12KB）：identity、book、coverage 摘要、captured_at/cutoff、warmup、
                  两侧前 5 档（按价聚合）、核心静态特征、已预热的动态特征、
                  活跃挂单墙（≤ 5）、最近事件（≤ 5）、质量摘要、名额状态、view_id

orderbook_details
  只读固定 view，不刷新数据；返回 ≤ 48KB
  参数：
    view_id
    section     ladder | features | history | trades | events | levels | execution | quality
    levels?     1–50                          ladder 用
    rows?       boolean                       ladder 用：是否展开到行（交易所 / MPID）
    feature_ids?  string[]                    features、history 用；规则同 L1 的 indicator_ids
    window?     "10s" | "60s" | "300s"        features 用
    prices?     number[]，1–8 个              levels 用
    band_ticks? 0–20，默认 2                   levels 用
    side?       "buy" | "sell"                execution 用
    quantity?   number                        execution 用；size_unit 为 null 时拒绝
    limit?      1–200                         history、trades、events 分页，最新在前
    before?     string                        同上
```

- **`levels` 分区**：对每个价位返回 ±band_ticks 内的可见量、挂单墙状态、最近 60 秒和 300 秒的增减归因、与 mid 的距离。价位超出可见行范围时返回 `beyond_visible_depth`，**不能**解读为“没有挂单”。
- **`execution` 分区**：返回按可见量能成交的数量、均价、最差价、吃掉的档数、相对 mid 和 NBBO 的滑点（bps）、占 band 深度的比例、`insufficient_visible_depth`。如果传了限价，还返回同价位的可见排队量。固定附带说明：只计可见量；不含隐藏单和其他场所；不考虑时间优先级；只在 captured_at 时刻有效；不构成建议。
- **错误码**：`missing_connection_input:<NAME>`、`invalid_target`、`not_entitled:<detail>`、`depth_capacity_exhausted`、`tbt_capacity_exhausted`、`client_id_in_use`、`view_expired`、`not_running`、`worker_fault:<code>`、`size_unit_unverified`、`invalid_prices`、`invalid_quantity`、`details_too_large`。预热中不算错误，用 `warmup` 字段表达。

命令（给人类交易员用）：

```text
/orderbook probe                                    探测深度场所和权限，不占深度名额
/orderbook watch <target> [smart|direct:<VENUE>] [rows=N]   固定订阅，不会被自动回收
/orderbook unwatch <target>
/orderbook auto on|off [target] [analysis|execution|both]   自动研究推送，默认 off
/orderbook show <target> | hide                     TUI 盘口小组件
/orderbook record on|off                            本地录制，默认 off
/orderbook status
/orderbook stop                                     关闭 worker 和全部订阅
```

Prompt 模板（固化交易员的高频提问）：
- `/l2-level <SYMBOL> <PRICE...>`：查看关键价位的可见流动性和最近变化。
- `/l2-pretrade <SYMBOL> <buy|sell> <QTY>`：交易前的可见流动性和成本估计。
- `/l2-review <SYMBOL>`：复述本次订阅窗口内的事件序列。

### 5.9 自动研究推送

- **默认关闭**。开启时可以选择 profile：
  - `analysis`：推送 D1、D2、D4、D5、D6、D7。
  - `execution`：推送 D3、D6、D7，以及靠近 NBBO 的 D2。
  - `both`：全部推送。
- **限频（初值）**：先在 30 秒窗口内合并；每个标的冷却 180 秒；每个会话每小时最多 12 次。D7 作为更正消息不受冷却限制。
- **投递条件（沿用 L1）**：auto 已开启；`ctx.isIdle()` 为真；`!ctx.hasPendingMessages()`；上一条推送已经被消费。
- **新鲜度**：投递前重新取快照生成 view；超过 120 秒的事件直接丢弃（D7 除外）。
- **消息格式**：`customType: "pi-orderbook/research"`，`display: true`，`details: { run_id, view_id, state_seq }`，`triggerTurn: true`，`deliverAs: "followUp"`。
- **消费跟踪**：`message_start` 中 details 匹配即确认消费；`agent_before_settle` 记录本轮结果；`agent_settled` 时解除 view 的 pin；结果不是 completed 时关闭 auto 并记录 fault（沿用 L1）。
- **可选模式（默认关闭，M3 再评估）**：
  - `nextTurn` 摘要：不单独触发一轮，而是把摘要附在用户下一条消息上 [官方语义见配套文档 §3.5]。这样更省成本，但内容可能过期，所以必须带 captured_at。
  - `context` 事件：在单次请求的上下文里，把超过 X 分钟的旧推送替换成占位文本，防止模型基于过期盘口推理。代价是 prompt 缓存前缀会失效。

### 5.10 生命周期

| 事件 | 行为 |
|---|---|
| 工厂函数 | 只注册工具、命令、渲染器和事件；不联网、不启动进程 [Pi 规范] |
| `session_start` | 保存 ctx；reason 为 reload、new、resume、fork 时都不继承旧状态 |
| 首次命令或工具调用 | 懒启动 worker 并建立连接 |
| `message_start` / `agent_before_settle` / `agent_settled` | 跟踪自动推送的消费和结果；settled 之后释放 manual pin |
| `session_tree` | 丢弃待推送的事件和 manual pin，**保留订阅和滚动缓冲**。理由：行情状态不属于某个对话分支，而重新预热代价很高。这是对 pi-technical 做法的有意调整 |
| `session_shutdown` | 取消全部订阅（`cancelMktDepth(reqId, isSmartDepth)`、`cancelTickByTickData`、`cancelMktData`），断开连接，结束 worker；必须幂等 |
| reload | 视为 shutdown 之后启动新运行时；预热状态会丢失 |

### 5.11 Skill 设计

```markdown
---
name: order-book-analysis
description: Interpret the pi-orderbook Level 2 view (visible depth, walls, order flow, execution estimates) within its venue coverage and timing limits. Use when a question involves the order book, market depth, Level 2, liquidity at a price, or pre-trade cost.
---
```

解读规则（正文要点）：

1. 先写清标的、book 模式、覆盖场所、captured_at 和 cutoff。L2 事实只在该时刻成立；问的是“现在”时，要重新调用 context。
2. 不要把可见订单簿称为“市场”，要与 NBBO 对照。最优价不在可见订单簿上时，要明确说出来。
3. 区分静态和动态。窗口预热未完成时，对应的动态特征不可用，不能当成 0。
4. consumed 和 pulled 都是估计的归因（成交时间精确到秒，订单簿时间是本地接收时间），必须同时报告匹配比例和容差。
5. 挂单墙只是“可见的挂单量”，不等于支撑或阻力，它可能被撤走；隐藏流动性无从知道。
6. 不推断意图或身份，比如幌骗、机构。只描述看得到的行为，例如“价格靠近时大额挂单在没有成交的情况下撤出”。
7. 执行估计只计可见量，不含隐藏单和其他场所，只在当时有效，不构成交易建议。执行决策属于用户或交易类 package。
8. 问题涉及技术价位时，先取得价位（安装了 pi-technical 就调用 `technical_context`），再调用 `orderbook_details(section: "levels", prices: [...])`。
9. 原样保留 view_id、时间（UTC）和编号。`size_unit` 为 null 时，不要与 L1 的股数比较。
10. D7 会使更早的结论失效或被取代，要按 cutoff 和 known_at 说明。
11. 输出格式：标识、覆盖范围和时间 → 事实 → 解释（附反证）→ 限制与缺口。默认不超过 5 条要点。

`references/features.md` 和 `references/events.md` 列出特征 ID 和事件定义，写法参照 L1 skill 的 Indicator IDs 表。

### 5.12 UI（只在 TUI 下）

- `ctx.ui.setStatus("orderbook", "L2 2/3 · TBT 2/3 · AAPL● NVDA◐")`：● 表示已预热，◐ 表示预热中，○ 表示 stale。
- `ctx.ui.setWidget("orderbook-ladder", lines)`：显示某个标的的简版盘口，刷新频率不超过 4Hz，由 `/orderbook show` 和 `hide` 控制。
- UI 内容不进入模型上下文，但能让人类交易员看到与 Agent 相同的数据，便于核对。
- RPC、JSON、print 模式下不显示小组件；有 UI 的模式（`ctx.hasUI`）只用 `notify`。

### 5.13 与 pi-technical 协作

- **v1**：两个包各自独立，由模型编排组合（见 skill 第 8 条）。哪个包没装，另一个都照常可用。
- **共用 Gateway**：
  - 两个包使用不同 clientId；每个 TWS 会话最多 32 个客户端 [官方]。
  - 行情线在 TWS 和所有 API 连接之间共用 [官方]。
  - 深度名额是否与 TWS 窗口共用 [M0]。
- **可选的后续方案**：通过 `pi.events` 约定带版本号的频道（例如 `pi-technical/levels.v1`），payload 用 TypeBox 校验，不形成硬依赖。这需要改动 pi-technical，本轮不做。
- **单位对齐**：L1 按部署约定（Gateway 的 lots 选项关闭）以股为单位；L2 的 `size_unit` 经 M0 核验后，两者才能直接比较。

### 5.14 安全、合规与数据使用

- **只读**：包不调用任何下单或撤单接口，并用静态测试保证 Python 源码里没有这类调用。
- **凭据**：代码里没有凭据；连接参数走环境变量；认证由 Gateway 负责。
- **IBKR 前置条件**：Market Data API Acknowledgement 已签署；NASDAQ 的 API 取数需要 EDS（已有）；非专业用户身份（NP）。
- **API User Activity Certification**：[官方] 它针对通过 API 交易美国期货的用户（CME Rule 576）。本包只读，不会触发；以后交易类 Agent 下期货单时才相关。
- **数据使用**：录制文件只留在本地，不分发。会话 JSONL 会保存工具输出，所以工具不输出原始盘口的全量数据。
- **Pi 安全模型**：包拥有用户的全部权限，只从可信来源安装；项目级安装受项目信任控制。

### 5.15 性能与资源

- ibapi 的回调只做入队和订单簿更新；特征计算放在单独的定时线程里，先对订单簿做快照拷贝。内存全部有界（滚动缓冲）。
- 监测 `update_rate` 和积压。负载过高时依次：减少行数、降低 UI 帧率、暂停优先级最低的订阅，并把降级事件记为 D7。
- TS 端只处理低频消息，不影响 Pi 的 agent 循环和 TUI 渲染。

## 6. 验证与验收

### 6.1 M0 实测清单（开发前）

| # | 项目 | 方法 | 产出 |
|---|---|---|---|
| 1 | 深度场所清单 | 调用 `reqMktDepthExchanges`，原样记录 | 确认 ISLAND、IEX、期货、债券场所是否出现，以及 `serviceDataType` 的取值 |
| 2 | SMART 深度 | NASDAQ 上市和 NYSE 上市的股票各选 1 只 | 2152 原文、`marketMaker` 取值分布、行数、与 NBBO 的差异 |
| 3 | 直连 NASDAQ（Gateway 上用 ISLAND） | 请求直连深度 | 是否出现 MPID，走哪个回调 |
| 4 | IEX 直连 | 请求直连深度 | 是否可用，走哪个回调 |
| 5 | 深度名额 | 依次请求 4 个不同标的；同一标的同时开 SMART 和直连；在 TWS 打开深度窗口 | 第 4 个是否返回 309；同一标的是否只算 1 个；TWS 窗口是否占用名额 |
| 6 | numRows | 分别请求 10、20、50、100 行 | 实际返回的最大行数 |
| 7 | size 单位 | 保持 lots 选项关闭，同一时刻与 TWS Market Depth 窗口（设为显示股数）对照，并检查数值分布；逐笔 size 同样核验 | 确定 `size_unit` |
| 8 | insert/delete 语义 | 录制原始回调 | position 移位规则，以及超出行数上限时的行为 |
| 9 | 时段 | 在盘前、盘后、隔夜分别请求 | 各时段是否有深度；316、317 的出现频率 |
| 10 | 负载 | 选活跃标的连续运行 | 更新频率、Python 端积压、CPU 占用 |
| 11 | 期货与债券 | 找到对应合约后请求深度（需要期货交易权限） | 能否取到数据，覆盖哪些合约 |
| 12 | 合规 | 检查 Client Portal 设置 | Acknowledgement 已签署；实盘和模拟账户不同时取数（10197） |

通过标准：每一项都有录制证据，结论写进决策表；仍未查明的保持未知，不猜。

### 6.2 自动化测试

- **Python**：
  - BookBuilder 的操作语义（用 M0 录制样本）以及 316、317 的处理。
  - 按价聚合。
  - 特征公式（用手算样例核对）。
  - 归因容差。
  - 事件去重与更正。
  - 回放确定性（黄金输出）。
  - 静态检查：源码里不能出现下单类 API。
- **Node**：
  - 协议 schema：严格模式、有限数字、帧上限。
  - view 的冻结和容量。
  - 输出上限：12KB / 48KB。
  - 名额租约与回收。
  - 生命周期：shutdown 幂等、`session_tree` 的行为。
  - 自动推送的限频和消费跟踪。
- **集成**：用回放模式驱动 worker，端到端跑 context 和 details。测试入口不进入产品。

### 6.3 模型验收

用固定的录制场景，让真实的 Pi 模型回答，按评分表评分（与 L1 的验收方式相同）。场景：
- (a) 卖墙被吃后突破：consumed 加 sweep。
- (b) 卖墙在价格到达前撤走：pulled。
- (c) 买价吸收。
- (d) 流动性变薄：D3。
- (e) 317 重置后发出更正：D7。
- (f) 最优价不在可见订单簿上。

评分项：
- 是否写明标识、覆盖范围和时间。
- 事实和解释是否分开。
- 是否把可见订单簿说成全市场。
- 是否把估计的归因说成确定结论。
- 是否给出交易指令。
- view_id 是否用对。
- 预热不足时是否如实说明。

## 7. 里程碑

| 阶段 | 内容 | 退出标准 |
|---|---|---|
| M0 实测 | 探测脚本、录制、决策表 | §6.1 的 12 项都有证据 |
| M1 静态快照 MVP | worker、BookBuilder、NBBO 参照、静态特征；context，以及 details 的 ladder、features、levels、execution、quality；probe、watch、unwatch、status、stop；skill v1；测试 | 回放测试和实时冒烟测试都通过 |
| M2 动态与事件 | 逐笔、归因、滚动特征；history、trades、events；D0–D7（先不自动推送）；录制与回放；阈值标定 | 场景回放产出预期事件；模型验收达到基线 |
| M3 推送与 UI | 限频推送、消费跟踪、TUI 状态栏和小组件、profile；评估 nextTurn 和 context 修剪 | 推送频率在上限内且不循环；RPC 和 print 模式下功能完好 |
| M4 扩展 | 期货和债券深度（取决于 M0 结果）、可选的 `pi.events` 协作、跨会话守护进程评估 | 按需 |

## 8. 风险与待决问题

**风险**：

| 风险 | 缓解 |
|---|---|
| 覆盖偏差导致误读（最大风险） | coverage 字段、NBBO 对照、skill 第 2 条、验收场景 (f) |
| 时间对齐误差导致归因错误 | 容差、`estimated` 标签、匹配比例、积压监测 |
| 名额争用（TWS 窗口、多个 Pi 会话） | M0 第 5 项；明确的错误码 |
| 负载过高导致积压 | 按 §5.15 降级，并记为 D7 |
| 模型过度解读（幌骗、机构） | skill 第 6 条；验收评分项 |
| Pi API 快速变化 | 锁定宿主版本；升级时跑回归 |
| 自动推送的 token 成本 | 默认关闭；限频；profile |

**需要你决定**：

1. 包名和工具名（建议 `pi-orderbook` 和 `orderbook_*`）。
2. 逐笔成交是否放在 L2 包内（建议放入：没有它就无法区分成交和撤单）。
3. 默认使用 SMART 还是直连 NASDAQ（建议 SMART；需要看 MPID 时再开直连，名额占用以 M0 结果为准）。
4. 自动推送是否默认关闭，以及限频初值。
5. 是否保留 `execution` 分区。它只提供可见流动性和成本估计，不含指令；需要确认这与 L1 的“不给交易指令”原则是否一致。
6. 是否启用本地录制，以及存放位置和保留期。
7. `session_tree` 的行为（建议保留订阅）。
8. v1 是否纳入 ICE 期货和债券深度（建议 M0 之后再定）。
9. 是否加订 NYSE OpenBook、ArcaBook、Cboe BZX 来扩大覆盖。这属于订阅决策，影响 coverage。
10. 是否需要多个 Pi 会话同时使用 L2。需要的话，要评估 Pi 之外的共享守护进程。

## 9. 参考资料

**Pi（0.87.1，npm 包内 `docs/`）**：
- packages.md、extensions.md、skills.md、prompt-templates.md、settings.md、security.md、tui.md、message-types.md、rpc-commands.md
- `dist/core/extensions/types.d.ts`；示例 `examples/extensions/event-bus.ts`、`truncated-tool.ts`
- 在线：<https://github.com/earendil-works/pi/tree/main/packages/coding-agent/docs>

**IBKR 官方**：
- Market Depth (L2)：<https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/market-depth-l-2/introduction>（Request / Receive / Cancel 子页）
- Market Depth Exchanges：<https://ibkrcampus.com/docs/tws-api/doc/market-data-live/market-depth-exchanges/introduction.md>
- Tick-by-tick：<https://ibkrcampus.com/docs/tws-api/doc/market-data-live/tick-by-tick-data/introduction.md>
- Error Codes：<https://ibkrcampus.com/docs/tws-api/doc/error-handling/error-codes.md>；System Message Codes：<https://ibkrcampus.com/docs/tws-api/doc/error-handling/system-message-codes.md>
- Specialized Market Data Lines：<https://www.interactivebrokers.com/docs/general/market-data-subscriptions/market-data-lines/specialized-market-data-lines>
- Market Data Lines：<https://ibkrcampus.com/docs/general/market-data-subscriptions/market-data-lines/introduction.md>
- NASDAQ Specialty Subscriptions：<https://www.interactivebrokers.com/docs/general/market-data-subscriptions/nasdaq-specialty-subscriptions>
- ISLAND to NASDAQ：<https://www.interactivebrokers.com/docs/general/contracts/contract-management/island-to-nasdaq-api-compatibility>
- Market Data API Acknowledgement：<https://ibkrcampus.com/docs/general/market-data-subscriptions/compliance-requirements-for-api-market-data/market-data-api-acknowledgement.md>
- Historical Volume Scaling：<https://ibkrcampus.com/docs/tws-api/doc/market-data-historical/historical-data-limitations/historical-volume-scaling.md>
- Connectivity（32 个客户端）：<https://ibkrcampus.com/docs/tws-api/doc/connectivity/introduction.md>
- Market Data Pricing：<https://www.interactivebrokers.com/en/pricing/market-data-pricing.php>

**学术**：
- Cont, R., Kukanov, A., Stoikov, S. (2014). The price impact of order book events. *Journal of Financial Econometrics*, 12(1), 47–88.
- Stoikov, S. (2018). The micro-price: a high-frequency estimator of future prices. *Quantitative Finance*, 18(12), 1959–1966.
