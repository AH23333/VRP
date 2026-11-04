# 构想v7——AI 重构主流程与梳理层定稿

> 状态：定稿 r4（2026-11-04）。r1 以渲染层四投影为中心，经产品定位纠偏后修订：
> **主流程 = 工具分析 → AI 梳理分析 → AI 重构 → 工具分析（回归验证）**；
> 可视化降格为各环节中间产物的呈现（横切），引入用户决策环节。
> r3 增补：AI 接口层（MCP+skill，§1.4）与驱动循环定稿（§1.5）。
> r4 定稿：市场定位翻转（vrp 本质 = MCP 插件，§0.4）、导航接口族（§1.4.2）、
> 现成工具最佳组合 + 工具组全局原则（§1.6）、信任分级交付（§3.3）、批次含 R3.5'（§6）。
> 《渲染层方案.md》为讨论稿，本文档为其定稿取代（保留作决策过程记录）。

## 0. 决策记录

### 0.1 五项裁决（r1 定，r2 保留）

| # | 决策点 | 裁决 | 理由 |
|---|---|---|---|
| 1 | 编号收编 | **S6↔S7 换位**：transact 保持 S6（=v4-S7），架构规则固化候补 **S7**（=v4-S6），可视化层定名 **S8**（=v4-S8） | transact 的 S6 已进发布文档/代码/README，改名成本高无收益；v4-S6 未建，顺延 S7 无历史包袱 |
| 2 | AI 梳理层触发 | **`vrp curate <repo>` 手动验收命令**——AI 工件的生产在管道外（LLM 外部步骤），管道只做验收入库（与 ai-gate 证据闸门同构） | 管道保持确定性可复现；LLM 调用属交互域，不进 `vrp run` |
| 3 | Datasette | **不硬引入**——S8 报告"数据层"节只写 facts.db 路径与推荐浏览方式（任何 SQLite 工具可开），Datasette 列为推荐之一 | 守住 vrp 纯标准库零运行时依赖红线；facts.db 本身即产物，浏览工具不缺位 |
| 4 | archify 钉法 | **work/ 沙箱 git clone 钉 commit 25cf3708**（本地克隆 `D:\Visual Studio Code\Something\archify` = v3.2.0+9 作 mirror 源）；bootstrap `--with-archify` 可选开关，默认检测提示 | validate 收据可对源码排查；与 cargo-mutants 可选模式先例一致；archify 缺失降级不阻塞管道 |
| 5 | verdict 回馈面 | **dossier 内嵌 + 命令双入口**：dossier evidence 节自动带上轮未成功 verdicts（AI 消费面单入口闭环）；`vrp verdicts <repo>` 命令作人查辅助 | 内嵌保证 AI 下一轮必然看到上轮失败；命令满足即时排查 |

### 0.2 定位纠偏（r2，用户裁决）

- **主流程四环不变**：工具分析 → AI 梳理分析 → AI 重构 → 工具分析（回归验证）。这是产品核心，一切围绕它。
- **可视化降格**：用户可视化需求只是**各环节中间产物的呈现**，横切服务主流程，不构成独立产品形态，不改变主流程结构。
- **引入用户决策环节**：闭环中显式定义人工介入点（§3 决策点矩阵）。
- **S8 从"交付层"降格为"中间产物可视化层"**：服务两个消费者——用户（看懂才会决策）与 AI/人审核（产物核对）。
- **批次重排**：主线（闭环契约、curate、用户决策）优先；可视化和 S7 后移（§6）。

### 0.3 ②环生产侧与驱动循环裁决（r3，用户裁决+代理定稿）

- **②环生产侧必须处理**（用户裁决）：vrp 提供 MCP server（`vrp mcp`，纯 stdlib JSON-RCP 零依赖）+ skill（vrp-loop/vrp-curation 操作指南）两层接口；生产仍在管道外，确定性红线不动（§1.4）。
- **决策体验配套**（用户裁决"允许"）：`transact --preview` diff 预览跟随 T4 confirm 走，进 R1'，confirm 不盲签。
- **驱动循环**（用户裁决"帮定"）：外部 agent 经 MCP 驱动为主、人肉为底、vrp 内置 LLM 循环明确排除；T4 人工闸 MCP 侧不可绕过；循环协议 8 步进 skill（§1.5）。
- **分析层定位**（用户裁决）："已完成的 S1-S5 只是最初步的"——分析层后续深化经 dossier/facts.db 统一消费面自动流到 AI，接口层不变更；深化方向另轮讨论。

### 0.4 r4 定稿（用户验收通过）

**市场定位翻转（用户构想，核心）**：项目虽叫"管道"，市场定位是**重构项目的 AI 分析工具，本质是一个 MCP 插件**，需 AI 深度参与——梳理业务、发现问题、给出建议、配合可视化材料让用户拍板、AI 重构、再分析进入循环。**核心价值主张：不让 AI 全读裸代码——AI 对代码的认知来自 vrp 的分层接口**。视角翻转：CLI（run/transact/curate）= 引擎面，MCP = 用户面；r3 的"接口层"升格为"产品本体"。

**四子点定稿**：
1. **导航接口族**（§1.4.2）：不同深度/颗粒度的分层查询接口，全链可查（L0 架构→模块→符号→行/边，每层返回下钻句柄），防 AI 只分析浅层——双机制：句柄体系与 curate 验证器同一套 fid/edge 锚（过验收必须下钻到底）+ 导航引导。
2. **初末态对比（诚实版）**：session_begin/end 快照（sqlite 单文件复制）+ session_report = 初末计数 diff + 双 L0 架构图并排 + verdict 时间线；**不做图 diff**（fid 内容敏感重构后必变，符号匹配研究级，远期再议）。
3. **MCP 工具面信息架构**：五族前缀命名约 22 件（nav_*/tx_*/curate_*/render_*/session_*），描述含用途/何时用/返回什么/下钻约定。
4. **地基质量透传**：nav 返回值统一带 confidence + repo xcheck 摘要徽标；低置信边需下钻核实；每批次 dogfood 反哺分析深化。

**工具组全局原则**（§1.6）：每环节 = 契约 + 工具组（多项目可插拔）+ 策略（按语言选择/全跑交叉/降级链）——把 S2 病理源组、G4 双工具交叉、archify 降级链三个既有隐式模式升格为全局架构原则；**每个环节不局限于单工具，用多个项目组成工具组**（用户裁决）。

**r3 推翻项**：`vrp mcp` 弃自研 stdio JSON-RPC 子集，改官方 MCP Python SDK（`mcp` 包，24.8k★ MIT，Py3.11+ 匹配），optional extra `pip install vrp[mcp]`——核心管道零依赖红线不变，AI 接口层/可视化层作为生态接入件允许 extra 依赖（与 cargo-mutants/archify 可选先例一致）。

## 1. AI 重构主流程（核心闭环）

### 1.1 闭环结构

```
┌─> ① 工具分析        vrp run（S0-S5）：CBM→ETL→dossier+facts.db（病理/锚定/verdict 回馈）
│       ↓
│   ② AI 梳理分析      管道外 LLM：读 dossier → 产 annotations(点)/slices(线)/structure(面)
│       ↓              → vrp curate 验收入库（确定性验证，拒收出收据）
│   [用户决策]         slices/structure 入库批准；否决留痕
│       ↓
│   ③ AI 重构          管道外 LLM：读 dossier（含上轮 verdicts）+ 梳理层 → 产事务 tx JSON
│       ↓
│   [用户决策]         T4 improvement 需 --confirm 放行（已建）
│       ↓
│   ④ 工具分析(回归)   vrp transact：G0-G5 五闸门 → 全过 git commit + facts.db promote
│       ↓              失败 git 原子回滚 + verdict（稳定 G 码）
│   ⑤ verdict 回馈     verdicts/<tid>.json → dossier evidence 节（下一轮 AI 必见）
└── 修正循环（④失败→③重提；全过→①新目标）
```

可视化（S8）横切呈现 ①-⑤ 各环节中间产物（§5），不参与流程本身。

### 1.2 已建事实（执行环全通，v4 loop 骨架在）

- **事务格式**（JSON 保零依赖，s6_transact.py docstring）：
  `{"id","type" T1-T4,"summary","declared_moves":[{"from","to"}],"edits":[{"file","op" create/modify/delete,"content"}]}`
- **执行链**：clean-tree precheck → register → apply edits → G1 重扫（CBM+旁路 facts.db）→ G2 lint → G3 test → G4 等价（declared_moves/边等价/dangling/verify_changes/difftastic 结构信号，两者不可用时非阻塞降级）→ G5 死码单调（T2/T4）→ 全过 git commit + promote facts.db；**T4 需 `--confirm`**；transact_batch 目录按序消费遇非 commit 停
- **verdict**：每次运行必写 `.vrp/verdicts/<tid>.json`，RULE_CODES 十码稳定——**repair receipt 已存在**
- **dossier**：七节（architecture/repo_map_brief/repomap/sql_views/evidence/summary/raw_pack）按 T1-T4 档位预算分配 = AI 消费面
- **annotations**：ai-gate 四态已入库

### 1.3 vrp 侧闭环缺口（r4 增补为六件）

1. **事务契约文档化**：schema 现只在 s6 docstring——出契约文档（schema+样例+G 码对照表：生产常见错误→验收失败码映射）+ AI 取材指南（T1-T4 各档 dossier 七节取材路径）
2. **verdict 回馈面**：dossier evidence 节内嵌上轮未成功 verdicts + `vrp verdicts` 命令（§0.1-5）
3. **curate 定位修正**：从"渲染供数"改为"AI 重构理解的沉淀与消费面"（§2）
4. **AI 接口层**：②环生产侧落地载体——MCP server + skill，vrp 对 AI 从"文件格式契约"升级为"可编程接口"（§1.4）
5. **决策体验配套**：`vrp transact --preview`（difflib unified diff 兜底 + difftastic 结构化优先，工具组）——T4 confirm 前人工/AI 自查 diff，不盲签；契约文档配套
6. **导航接口族**（r4 新增）：AI 梳理所需的**不同深度/颗粒度查询接口**，全链可查防浅层（§1.4.2）——现有 get_dossier（预算化组装产物，被动）与 query_facts（裸 SQL，无颗粒度引导）均不满足，此为产品核心缺口

### 1.4 AI 接口层：MCP server + 导航族 + skill（产品本体）

**问题**：主流程四环中①④是工具（已建）、③有事务契约，但②环 vrp 只定义了验收端（curate）——梳理的**生产**（谁调 LLM、读什么、什么策略）完全在管道外。核心价值主张：**不让 AI 全读裸代码**——AI 的代码认知来自 vrp 的分层接口（facts.db 6691 边级结构化数据、dossier 分节预算、repomap），而非裸代码塞 context。

**定稿**：vrp 提供**可编程接口 + 操作指南**两层，生产仍留管道外（确定性红线不动）：

#### 1.4.1 `vrp mcp`——MCP server（官方 SDK，optional extra）

- 框架：官方 MCP Python SDK（`mcp` 包，modelcontextprotocol/python-sdk，24.8k★ MIT，Python 3.11+ 匹配 vrp requires-python），装饰器风格；**弃 r3 自研 stdio JSON-RPC 子集**（§0.4）
- 依赖形态：`pip install vrp[mcp]`；核心管道零第三方运行时依赖红线不变——AI 接口层/可视化层是生态接入件，允许 extra 依赖（与 cargo-mutants/difftastic/archify 可选外部工具先例一致）
- 双 server 组合：`vrp mcp`（vrp 特有工具）+ **bytebase/dbhub**（3.8k★，SQLite 只读通用数据库 MCP server，进阶自由 SQL 浏览 facts.db，推荐非必须）
- **安全闸**：`tx_run` 对 T4 一律返回 needs_confirmation，不收 confirm 参数——**T4 放行只能 CLI 人工 `--confirm`，agent 不可绕过**（§3 决策点矩阵的执行保障）
- 长任务（gate 跑测试）同步等待返回，文档注明调用方设长超时；v1 不做异步句柄（简单优先）

#### 1.4.2 导航接口族（nav_*，产品核心）

分层下钻，每层返回下钻句柄；句柄体系与 curate 验证器同一套 fid/edge 锚——**AI 想过 curate 验收就必须下钻到底，浅层结论进不了库**（防浅层双机制：导航引导 + 验证锚定）：

```
nav_search(query)            → 符号/模块名搜索（AI 入口）            [R1.5']
nav_architecture(repo)       → L0：structure 优先，缺失包级聚合兜底（repomap 算法复用）；
                               返回 module_id 句柄                    [R1.5']
nav_module(module_id)        → L1：符号清单+病理摘要；返回 fid 句柄    [R1.5']
nav_symbol(fid)              → L2：签名/行号/调用关系/所在 slices；返回 edge 句柄 [R1.5']
nav_callers / nav_callees    → 调用链横跳                            [R2']
nav_slice(slice_id)          → 业务线视角（steps+acts+符号句柄）      [R2']
nav_path(from, to)           → 耦合路径查找（sqlite WITH RECURSIVE）  [R4']
```

- **地基质量透传**：返回值统一带 `confidence`（edges 现成列）+ repo xcheck 摘要徽标；工具描述声明"低置信边需下钻核实或读源码确认"——接口的"全链可查"承诺受限于分析质量（xcheck 84-95%），导航族只开放数据已验证路径，别让接口层跑在地基前面
- **导航族无现成轮子**（已核实：roam-code 等同类皆为独立产品非可组合库）——差异化所在，实现为 facts.db 上的 SQL 封装（sqlite3 标准库），成本低
- query_facts 保留为轻量直查（mode=ro + SELECT 校验，带 vrp schema 引导——通用工具不教 AI 哪个视图干嘛用）

#### 1.4.3 skill（repo 内分发，随批次出）

- `skills/vrp-loop/SKILL.md`（R1.5'）：重构循环协议（§1.5）+ 事务契约摘要 + G 码对照 + 重试纪律
- `skills/vrp-curation/SKILL.md`（R2'）：梳理生产指南——dossier/导航族取材路径、slices/structure 样例与验证规则（§2）、收据拒绝码处理
- skill 是文档（MCP 工具的使用说明书），不是运行时——vrp 安装不含 skill，AI 客户端按需引用；内容自研不可避免（教 vrp 独有流程），格式参照现有 skill 生态

#### 1.4.4 分析层深化的接口保证

"S1-S5 只是最初步的"——分析层（病理源、evidence 厚度）后续深化通过 **dossier/facts.db 统一消费面**自动流到 AI，MCP/skill 接口层无需变更。深化方向（新病理工具、更厚 evidence、测试盲区分析等）另轮讨论定稿。

### 1.5 驱动循环定稿（③环接口期望）

**方案裁决**：**外部 agent 经 MCP 驱动为主，人肉为底，vrp 内置 LLM 循环明确排除**（管道确定性红线：vrp 进程内不出现 LLM 调用）。

**循环协议**（写进 `skills/vrp-loop/`，任何 MCP 客户端——Claude Code/lingxi/Cursor 等——即是驱动器）：

```
1. get_dossier(task)          → 定位目标病理（findings/evidence）
2. nav_* 下钻                 → 全链核实：architecture→module→symbol→callers/callees
                                （低置信边下钻核实或读源码确认）
3. 产事务 JSON                 → 按契约：最小事务、declared_moves 齐全
4. tx_preview                 → 自查 diff（difftastic 优先/difflib 兜底）
5. tx_submit                  → 落 .vrp/transactions/
6. tx_run                     → T1-T3 直接出 verdict；
                                T4 返回 needs_confirmation → 人工 CLI --confirm
7. verdict 失败               → 读收据 G 码 → 修正重提（重试纪律：建议 ≤3 次/事务，
                                应用层约定，verdict 历史可审计防失控）
8. verdict committed          → 下一目标（回到 1）；多轮会话信任分级见 §3.3
```

**安全边界**：
- T4 人工闸不可自动化（§1.4.1）；transact_batch 按序停错（已建）；verdict 全历史落盘可审计（已建）
- 循环纪律（重试上限、目标数）属应用层，vrp 只提供 verdict 历史与 dossier 回馈面作事实依据

### 1.6 工具组全局原则（r4 定稿）

> **每环节 = 契约（输入/输出 schema，vrp 拥有）+ 工具组（多项目可插拔，实现契约即接入）+ 策略（按语言自动选择 / 全跑交叉验证 / 降级链）**
>
> 每个环节不局限于单工具，用多个项目组成工具组；新工具接入 = 写一个契约 adapter，编排层零改动；工具组启用/优先级走 repo 级配置。

| 环节 | 工具组 | 策略 |
|---|---|---|
| 结构提取 | CBM + code-atlas | 主备 + 交叉（xcheck） |
| 变异测试 | stryker(JS/TS) + cargo-mutants(Rust) + **mutmut(Python，待评估补位)** | 按语言选择 |
| dup/静态检查 | jscpd + ast-grep | 全跑 |
| 结构 diff | difftastic + difflib（兜底） | 降级链 |
| G2/G3 闸门执行器 | 项目自身工具链（ruff/mypy/eslint/tsc/cargo check/pytest/jest…） | 按语言探测 |
| G4 交叉 | difftastic + code-atlas verify_changes | 双跑交叉，缺失降级 |
| AI 接口 | mcp 官方 SDK + bytebase/dbhub | 双 server 组合 |
| 可视化 | archify + Mermaid + SARIF + repomap（+ Datasette 推荐） | 多出口同 IR，archify 缺失降级 |
| S7 执法（远期） | import-linter(Python) + eslint-plugin-boundaries / dependency-cruiser(JS/TS) | 按语言选择 |
| 会话交付 | archify 双图 + 计数 diff + verdict 时间线 | 组合输出 |

本表是既有模式的升格：S2 病理源组、G4 双工具交叉、archify 降级链三个隐式先例 → 全局原则。**自研保留边界**（产品本体，无轮子可替代）：facts.db/ETL 分析引擎、G0-G5 闸门、curate 验证器、导航族 SQL 封装、session 语义、skill 内容。

## 2. AI 梳理层（主流程②环）

定位：AI 重构理解的**结构化沉淀**——点是符号语义、线是业务脉络、面是整体结构；既服务 AI 重构决策（③环取材），也服务用户决策呈现（§3）。

### 2.1 三层数据模型

| 层 | 工件 | 状态 | 入库 |
|---|---|---|---|
| 点 | annotations（符号语义标注） | **已建**（ai-gate 四态） | annotations 表 |
| 线 | slices（业务线） | 新 | slices 表（§2.2） |
| 面 | structure（整体结构） | 新 | structure 表（§2.3） |

AI 工件以文件形态落地 `.vrp/ai/`（slices.json、structure.json），由 `vrp curate` 验收后入库——**验收通过才入库，不通过出收据拒绝**（收据含稳定错误码与逐条理由，模式照抄 ai-gate/receipt 哲学）。

### 2.2 线：slices schema

```json
{
  "slice_id": "checkout-flow",
  "name": "下单结账流程",
  "summary": "从提交购物车到订单落库的主干链路",
  "steps": [
    {"fid": "<sha256>", "role": "entry", "note": "POST /checkout 路由入口"}
  ],
  "acts": [{"name": "入口段", "from_step": 0, "to_step": 5}]
}
```

- `role ∈ {entry, process, branch, persist, respond}`；`acts` 可选（无 acts = 单幕）
- **验证规则（确定性，全过才入库）**：
  1. 每 `step.fid` ∈ facts.db `symbols`（存在性）
  2. 相邻 steps 之间在 facts.db 有真实边（`CALLS`/`CALL_REFERENCE`/`HTTP_CALLS` 任一方向，带行号）——**严格优先，跨跳即拒**（诚实失败优于宽松猜测）
  3. `name`/`summary` 非空
  4. `acts` 边界单调递增、覆盖 steps 区间
- 入库表：

```sql
slices(
  id INTEGER PRIMARY KEY,
  slice_id TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL, summary TEXT NOT NULL,
  payload_json TEXT NOT NULL,          -- 完整 steps/acts
  status TEXT NOT NULL DEFAULT 'active',  -- active/stale/rejected（cosmetic 不适用）
  created_at TEXT NOT NULL
);
```

- 防漂移：fid 即稳定锚（sha256）；重跑 curate 时复验全部 fid/边仍存在，失效者标 `stale`——与 annotations 四态同精神
- **同一 slice_id 重提交 = 幂等覆盖**（DELETE 后 INSERT，谁写入谁清理，#62 纪律）

### 2.3 面：structure schema

```json
{
  "modules": [
    {
      "module_id": "core-graph",
      "name": "图构建模块",
      "responsibility": "从结构提取结果构建符号图",
      "symbol_fids": ["<sha256>"],
      "layer": "domain"
    }
  ],
  "layers": [
    {"name": "domain", "may_depend_on": []},
    {"name": "service", "may_depend_on": ["domain"]}
  ],
  "rules": [
    {"rule": "domain 不许 import ui", "enforcement_hint": "import-linter"}
  ]
}
```

- **验证规则（确定性，全过才入库）**：
  1. `symbol_fids` 全部 ∈ facts.db `symbols`
  2. 模块间 fid **不重叠**（一个符号一个模块；未分配符号属隐式 unassigned，不阻断）
  3. `layers` 的 `may_depend_on` 图**无环**（层序 DAG）
  4. module.layer ∈ layers；rule 文本引用的层名存在
  5. `responsibility`/`name` 非空
- 入库表：`structure(payload_json, status, created_at)` 单行幂等覆盖（state 语义，#66 纪律）
- `rules` 是 S7 的直接原料（§4）

### 2.4 命令面

- `vrp curate <repo>`：扫描 `.vrp/ai/` → 逐文件 schema+语义验证 → 全过入库 / 任一不过整文件拒绝（收据逐条，含行级定位）→ stdout 汇总（accepted/rejected/stale 计数）
- `vrp curate <repo> --reject <slice_id|structure>`：用户否决已入库工件，status→rejected 并留痕（§3.2）
- `vrp run`/`vrp report` **不自动触发** curate（§0.1-2）
- AI 工件样例进 `docs/`（格式契约文档化，AI 消费方可照抄）

## 3. 用户决策环节（r2 新增）

### 3.1 决策点矩阵

| 主流程环 | 决策内容 | 机制 | 状态 |
|---|---|---|---|
| ① 分析 | 病理采纳为重构目标 | 现状 dossier findings 由 AI 直消；远期加采纳/忽略留痕 | 远期 |
| ② 梳理 | slices/structure 入库批准 | curate 机器验收（确定性规则）；`--reject` 用户否决留痕 | R2' |
| ③ 重构 | 事务放行 | **T4 improvement 需 `--confirm`（已建）**；T1-T3 直执行 | 已建 |
| ⑤ 回馈 | 失败事务放弃/重试 | verdict 在，决策由 AI/人下轮读 dossier 自行处置 | 已建 |

原则：机器能验证的交给 curate（确定性）；只有**价值判断**留给用户（reject/confirm）。决策不阻塞主流程自动化（reject 前数据可用，reject 后下轮失效）。

### 3.2 决策留痕

```sql
decisions(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,        -- slice/structure/transaction/finding
  target_id TEXT NOT NULL,
  decision TEXT NOT NULL,    -- approve/reject
  note TEXT,
  created_at TEXT NOT NULL
);
```

细节随 R2' 实施定稿；原则：决策是数据（可追溯），不是口头约定。

### 3.3 信任分级交付（r4 新增，session_* 族）

多轮重构会话的监督强度分两级（用户构想）：

| 级别 | 模式 | 交付物 |
|---|---|---|
| 全监督 | 每轮决策点拍板（curate reject / T4 confirm / render 材料） | 逐轮 verdict + 材料 |
| 全信任 | AI 多轮重构期间零打扰 | **只在最初产出初态、最后产出末态的可视化对比作交付** |

实现（诚实版，不做图 diff）：
- `session_begin`：facts.db 文件快照（sqlite 单文件复制）+ 会话元数据
- `session_end`：末态快照
- `session_report`：初末计数 diff（节点/边/病理/xcheck 四组数字）+ 双 L0 架构图（archify）并排 + verdict 时间线
- **不做图 diff**：fid 是内容敏感 sha256，重构后必变；符号匹配+结构对齐是研究级问题，远期再议——诚实版并排+计数足够拍板交付
- 批次 R3.5'；MCP session_* 三件 + CLI 双入口

## 4. S7 架构规则固化（候补 = v4-S6）

- 定位不变：**AI 理解，工具执法**——structure.rules → 各语言依赖规则工具配置 → 工具执行 → 违例清单 = 重构目标
- 前置：structure"面"层落地（R2'）；无 structure 则 S7 无原料，诚实跳过
- 接口边界（本版只定，不展开设计）：
  - 输入：structure.rules（enforcement_hint 指向语言工具：Python→import-linter、TS→eslint-plugin-boundaries 等）
  - 输出：违例 findings 入 facts.db（新 pathology 类别，规则码实施时定稿）
  - 与 S6 关系：违例清单经 dossier 进入 AI 重构目标（v4 原链路）
- 排期：R4' 远期，设计随 R2' 落地经验再定稿

## 5. S8 中间产物可视化层（降格，= v4-S8）

### 5.1 定位

**各环节中间产物的静态呈现**，服务两个消费者：用户（决策辅助——看懂才会决策）与 AI/人审核（产物核对）。不构成独立产品形态；报告生成器三特征不变：**静态产物、无服务端、无前端构建链**；数据真值 100% 来自 facts.db；渲染执行全部委托外部工具。

> 注：archify 是 v4-S8 设想的回归而非新引入（v4 line 52 即写 "S8 交付 + Archify 图"）。

### 5.2 可视化对象（按主流程环节）

| 主流程环 | 中间产物 | 呈现 | 技术 |
|---|---|---|---|
| ① 分析 | facts.db/dossier | 架构图、代码地图、诊断 | archify architecture / repomap+Mermaid / SARIF |
| ② 梳理 | slices/structure | 业务线时序图（分幕）、结构视图 | archify sequence / architecture |
| ③ 重构 | tx JSON | 事务 diff 预览 | difftastic（S6 已有基础） |
| ④⑤ 回归/回馈 | verdicts | verdict 时间线、未决清单 | archify Timeline / index 列表 |

四投影技术选型保留 r1 定稿：archify（图）/ SARIF 2.1.0（行级诊断）/ Mermaid+repomap（地图）/ 数据层不硬引入（facts.db 即产物，Datasette 列推荐）。触发方式双入口（r4）：`render_*` MCP 工具（AI 在决策点调可视化给用户出拍板材料）+ `vrp report` CLI（人查）。

### 5.3 渲染分层规范

| 层 | 规则 | 投影 |
|---|---|---|
| L0 全局架构 | **structure 存在且 active：组件=modules、分层=layers、依赖=层规则+实测模块间边；structure 缺失：包级聚合+PageRank 确定性兜底** | archify architecture |
| L1 模块级 | 按包/模块展开，确定性规则 | archify architecture / Mermaid |
| L2 符号级 | **slices 驱动，全集渲染不许筛选**（steps 有几个画几个，长线按 acts 分幕） | archify sequence |
| L3 明细层 | 零筛选全量 | SARIF + facts.db 直读 |

### 5.4 架构与复现性

- 产物落 `<repo>/.vrp/report/`（进 info/exclude 防 CBM 吞工件，#76 纪律）；index.html 极简目录页（数据层说明节在此）
- 报告不进 freeze 字节级基线（派生物）；复现性由两层保证：facts.db 可复现（既有）+ IR 生成确定性（同 facts.db → 字节级同 IR）
- report.html 头部带 `facts.git_head` + IR 内容 hash（可溯源），**不带时间戳**（保字节级复现）
- archify 钉法与降级链（§0.1-4）：沙箱缺失/validate 失败 → 降级 Mermaid-only，rc=0 不阻塞

## 6. 实施批次（r4 重排，含文件级要点）

| 批次 | 内容 | 里程碑 / 验收 |
|---|---|---|
| **R1'** | 事务契约文档 `docs/契约/事务契约.md`（schema+样例+G 码对照+取材指南，对照 s6 代码逐字段核）+ verdict 双入口（dossier evidence 节内嵌上轮未成功 verdicts + `vrp verdicts` 命令）+ `transact --preview`（工具组：difflib 兜底+difftastic 优先） | 契约可依；快测三件；门禁绿 |
| **R1.5'** | `vrp mcp`（官方 SDK，`pip install vrp[mcp]` extra，pyproject 加 optional-dependencies）+ nav 基础四件（search/architecture/module/symbol，`vrp/nav.py` SQL 封装）+ tx 四件（preview/submit/run/verdicts）+ query_facts + `skills/vrp-loop/` v1 | **AI 可开始重构循环**；MCP 集成测试（官方 SDK client in-process 对打）；nav SQL 单测；T4 闸测试（tx_run 必返 needs_confirmation） |
| **R2'** | `vrp/curate.py`（slices/structure schema+验证器+表迁移）+ `vrp curate` CLI + `--reject` + decisions 留痕表 + curate MCP 族五件 + nav_slice/callers/callees + `skills/vrp-curation/` v1 | **AI 梳理闭环**；验证器全拒绝路径参数化；手工 AI 工件→curate 入库 E2E；幂等/stale/留痕测试 |
| **R3'** | `vrp/render.py`（render_* MCP 工具 + `vrp report` CLI 双入口）+ S8 可视化（archify 沙箱 bootstrap `--with-archify` 钉 25cf3708、SARIF——sarif-om 与手写 JSON 二选一实施时定、Mermaid、T4 清单、降级链）——**已完成（2026-11-04）**：SARIF 手写 JSON 定案；踩坑 #83（archify validate 四坑） | **AI 可出拍板材料**；深测 E2E 两仓出 report.html（`archify validate --json` rc=0）；降级测试；dogfood 自报告——**已验收**（vrp 仓 8 件 + unwind 仓 5 件，archify=true degraded=[]；降级 rc=0） |
| **R3.5'** | `vrp/session.py`（session_begin/end/report，sqlite 文件快照）+ session MCP 族三件 + trust 模式进 skill（§3.3）——**已完成（2026-11-04）**：快照走 sqlite backup API；CLI `vrp session <repo> begin/end/report` 双入口 | **全信任模式**；初末快照/计数 diff/双图并排 E2E——**已验收**（vrp 仓+unwind 仓，archify=true degraded=[]；diff 全零（facts 未动）；快测 +16，352 绿） |
| **R4'** | S7 架构规则固化（执法器工具组按语言）+ nav_path（WITH RECURSIVE）+ 变异测试组 mutmut(Python) 补位评估 + 分析深化持续批次化（dogfood 反哺）——**进行中（2026-11-05 暂停收尾，详见 §8）**：nav_path 已落（c4b086d）；S7 Python 垂直切片已落（2b353a2，import-linter，TS 执法器后续批次）；mutmut 补位评估定案（§8.3：保持 2.4.5）；剩低负载全量复跑收口 | 规则固化；远期设计随 R2' 经验再定稿 |

每批次：小步提交、门禁快测全绿、深测按批跑、文档三件套同步（交接/踩坑/构想v7 增补节）、dogfood 反哺分析深化（§0.4-4）。

## 7. 与既有文档的关系

- 《渲染层方案.md》：讨论稿，**被本文档取代**（保留作决策过程记录；其 R1/R2 渲染优先批次作废，以本文 §6 为准）
- 《构想v4》：S6-S8 设计的原始出处（编号映射见 §0.1-1）
- 《产品目标.md》：R1' 实施时修订——主流程四环明示、"不做渲染引擎与服务"红线保留、阶段表补 S7/S8 行
- 《交接文档》：每批次收口时同步（下一步节 + 代码地图）

## 8. R4' 批次增补（2026-11-05，进行中——暂停收尾）

### 8.1 nav_path 耦合路径查找（R4' 第一件，c4b086d）

rp/nav.py nav_path(from_fid, to_fid, max_hops=5)：WITH RECURSIVE BFS 最短路径。**实现关键**：sqlite 递归 CTE 的 UNION 按整行 (fid,depth) 去重——同 fid 多深度合法共存，归一须 `SELECT fid, MIN(depth) FROM walk GROUP BY fid`（直接 INSERT 撞 PRIMARY KEY，实测 IntegrityError）；同长路径 fid 字典序确定性选一；hops_detail 透传 type/evidence/strategy/confidence/line；不可达 found=false 诚实返回；max_hops 上限 10。MCP nav_path 第二十四件；快测 +5。

### 8.2 S7 架构规则固化（R4' 第二件，2b353a2）——"AI 理解，工具执法"垂直切片

rp/s7_enforce.py（纯标准库 + import-linter 2.15 子进程，VRP_IMPORT_LINTER_BIN 覆盖）：

- **契约生成**：层 DAG 每对不允许 (A,B) 生成一条 forbidden 契约（覆盖线性栈与任意 DAG；may_depend_on 只豁免直连——传递违例同抓，线性栈 top 直 import bottom 违规实测在案）；rules[] "X 不许 import Y"（from_layer/to_layer 字段优先，"不许/不能/禁止/must not import" 文本模式兜底）映射为同型契约，映射不出进 unenforced_rules 诚实清单
- **容器推导**：module.symbol_fids → symbols.file_path → 点分模块名（`__init__.py`→父包；非 .py 仓 language-unsupported——TS 执法器 dependency-cruiser 等为后续批次，Python 引擎垂直切片先行）
- **违例 = P9 findings**（P 码序列顺延；kind='layer-violation'，source='s7-enforce'，幂等 replace-all；锚定覆盖符号优先/文件首符号兜底；payload 带 contract/层/双方/行/structure_status）
- **配置生命周期**：`.vrp/importlinter.ini` 生成→执行（--config= --no-cache --no-logo，cwd=repo）→finally 清理（同 stryker.conf.json 模式）
- **状态诚实六态**：ok / skipped（无 structure、非 active）/ language-unsupported / enforcer-not-found / enforcer-failed（rc 非 0/1 或 rc=1 无 "Broken contracts" 块=配置/环境错误，不误删既有 findings）/ not-enforceable
- **接线**：CLI `vrp enforce <repo> [--timeout]` + MCP enforce 第二十五件
- **测试**：quick 21 例（单元+mocked E2E 六态全覆盖+CLI+MCP over-the-wire）、deep 4 例（真 lint-imports E2E：违例落库含函数内 import 锚定/幂等/修复清库/合规仓）
- **dogfood**：vrp 仓自身 active structure（pipeline/transact/ai-layer 三模块、core/interface 两层）执法——1 契约（core 不许 import interface）kept、0 违例、0 findings；事实刷新走 `vrp run`（OWNED_TABLES 保 structure 跨 etl 存续）
- **取证探针**（work/，保留参考）：probe_r4_importlinter.py / probe_r4_forbidden.py（输出格式+rc 语义+中文契约名+文件级容器实测）、probe_r4_structure.py（structure 实际形态）

### 8.3 变异测试组 mutmut(Python) 补位评估（R4' 第三件，评估定案零代码变更）

§1.6 工具组表 mutmut(Python，待评估补位)——三引擎（mutmut/Stryker/cargo-mutants，构想v6 §13.9.21/§13.9.22/§13.9.26）已全接线，本件收口版本线评估。实测取证（work/probe_r4_mutmut_eval.py，2026-11-05，隔离 venv 双版本）：

- **mutmut 3.8.1（最新）**：原生 Windows 硬拒实证——"To run mutmut on Windows, please use the WSL. Native windows support is tracked in issue boxed/mutmut#397"；3.x 架构重写（无 .mutmut-cache、libcst 生成 mutants/ 目录、[tool.mutmut] 配置、rc 语义变更）——迁移 = WSL 依赖层（新外部依赖，破坏子进程零摩擦模式）+ 契约三件（CLI 参数组/rc 位图/pony cache schema）全换，**否决**
- **mutmut 2.5.1（2.x 线末位）**：三契约全兼容实测——2.4.5 集成用的 CLI 参数组（--paths-to-mutate/--runner/--tests-dir/--test-time-multiplier/--no-progress）原样接受、rc=2 位图（survived）、.mutmut-cache pony 库同构（Mutant/Line/SourceFile JOIN 6 行 bad_survived/ok_killed）——**记为验证过的 2.x 线内升级上限**（未来 2.4.5 出问题可无代码改动直升）
- **裁决：保持 mutmut==2.4.5**：无已知影响管道的缺陷；升 2.5.1 收益（小修）< 全量深测重跑成本
- **替代引擎（cosmic-ray 等）不评估**：三语言三引擎闭环无缺口驱动；cosmic-ray 重型（work queue/DB）不合子进程轻量适配器模式
- **上游观察点**：boxed/mutmut#397（Windows 原生支持）——落地则重开 3.x 迁移评估
- Python 侧 P8 无引擎缺口；剩余缺口为语言侧（JS/TS TESTS=0 预筛，构想v6 §12.4 已规划 ETL 自建），非 mutmut 补位范畴

### 8.4 R4' 剩余（恢复后收口清单）

1. 低负载窗口全量复跑（口径 1784：快速 378 + 深度 1406；上轮 R3.5' 诚实记录 1752+1F+1E 定性环境性 #84 内存悬崖——收口前须确认全绿）
2. （可选）unwind 仓 curate 入库 structure 后 `vrp enforce` 补第二仓 dogfood 数据点
3. 队列清空后停下汇报；push 等用户指令（提交链 …→c4b086d（nav_path）→2b353a2（S7）本地，最后推送 d46757c）
