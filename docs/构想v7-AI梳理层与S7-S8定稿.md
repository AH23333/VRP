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
| **R4'** | S7 架构规则固化（执法器工具组按语言）+ nav_path（WITH RECURSIVE）+ 变异测试组 mutmut(Python) 补位评估 + 分析深化持续批次化（dogfood 反哺）——**已完成（2026-11-05，详见 §8）**：nav_path 已落（c4b086d）；S7 Python 垂直切片已落（2b353a2，import-linter，TS 执法器后续批次）；mutmut 补位评估定案（§8.3：保持 2.4.5）；收口件 S7 parse 修复+tsa 第二仓 dogfood（§8.5：真违例抓出+parse bug 反哺修复）；全量 1784 绿（§8.4） | 规则固化；远期设计随 R2' 经验再定稿 |
| **R5'** | S7 TS/JS 执法引擎（dependency-cruiser 18 沙箱垂直切片）——**已完成（2026-11-05，详见 §9）**：`vrp/s7_enforce_ts.py` 双引擎语言路由（.py→import-linter / .ts/.tsx/.js/.jsx/.mjs/.cjs→depcruise / 混语言诚实拒绝）；typescript **5.9.3 钉版**（7.x 超界静默空跑陷阱）；dogfood unwind 仓 1 契约 KEPT（73 模块真巡航）；全量口径 1803 | 双语言执法闭环；CLI/MCP 零新增面（enforce 内部路由） |
| **R6'** | 渲染诚实性修复（病根二 M4 不对称：freshness 只保护 rw 消费方展示路径裸奔）+ 病根五（S1/xcheck reused 短路）+ **agent 真实长跑 E2E**（真实 MCP stdio 客户端全链驱动 vrp 自仓：session→nav→edge→slices→structure→enforce→T2/T4，抓双真 bug 反哺修复）——**已完成（2026-11-05，详见 §10）**：T2 真死码 committed、T4 人工放行 committed；快测 402 绿 | 展示路径同享 M4 保护；长跑=环境保真的最强验收 |

每批次：小步提交、门禁快测全绿、深测按批跑、文档三件套同步（交接/踩坑/构想v7 增补节）、dogfood 反哺分析深化（§0.4-4）、**自身 dogfood 产物刷新**（收口前 `vrp run`+`vrp report` 自家仓——产物是活文档不是测试输出；R6' 病根一制度化：R1'~R5' 只顾推批次，自家 report 陈旧 5 批次无人知）。

## 7. 与既有文档的关系

- 《渲染层方案.md》：讨论稿，**被本文档取代**（保留作决策过程记录；其 R1/R2 渲染优先批次作废，以本文 §6 为准）
- 《构想v4》：S6-S8 设计的原始出处（编号映射见 §0.1-1）
- 《产品目标.md》：R1' 实施时修订——主流程四环明示、"不做渲染引擎与服务"红线保留、阶段表补 S7/S8 行
- 《交接文档》：每批次收口时同步（下一步节 + 代码地图）

## 8. R4' 批次增补（2026-11-05，全部完成）

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

### 8.4 R4' 收口清单（2026-11-05 全部完成）

1. ~~低负载窗口全量复跑~~ **已完成**：**1784 全绿 1058.46s**（2026-11-05，6.0GB 空闲内存窗口；R3.5' 上轮 1752+1F+1E 定性 #84 内存悬崖理论再实证——同代码库健康内存全过）
2. ~~第二仓 dogfood~~ **已完成（目标改 tsa）**：unwind 是 TS 仓，enforce 只能产出 language-unsupported（单测已覆盖，无数据点价值）；改 tsa（Python 大仓 66132 symbols）出真实执法数据点——**1 契约 BROKEN、真违例、dogfood 即抓 parse bug 反哺修复**，详见 §8.5
3. 队列清空后停下汇报；push 等用户指令（提交链 …→c4b086d（nav_path）→2b353a2（S7）→7ef0472（mutmut 评估+文档）→收口提交 本地，最后推送 d46757c）

### 8.5 S7 parse 修复 + tsa 第二仓 dogfood（R4' 收口件）

**tsa dogfood 全链**（verify/tree-sitter-analyzer，第二仓数据点）：聚焦切片两层 structure——kernel（core/ 21 文件，层 core）+ interface（cli+mcp+lsp+api/ 323 文件，层 interface），一 fid/文件诚实锚定，curate 入库 → `vrp enforce`（真 lint-imports 分析 1012 文件/3112 依赖）→ 1 forbidden 契约 **BROKEN**：真违例 `tree_sitter_analyzer/core/query_service.py:320` `from ..api import analyze_code`（内核反向依赖接口层入口，try/except 包裹仍违例）→ P9 findings 锚定覆盖符号。**大仓 vibe 代码的真实架构债首次被 S7 执法抓出**。

**dogfood 即抓 bug（反哺闭环兑现）**：首跑 2 findings 落库、锚点相同而契约名错乱——取证（work/probe_r4_tsa_raw.py）发现 import-linter forbidden 契约把同一条链列两遍（(容器,被禁模块) 聚合组在前、(模块,被禁模块) 明细组在后），且描述行 ~70 字符**折行**——折行片段无冒号尾，`parse_broken` 误当契约名，第二份违例挂错契约。**修复**：`parse_broken` 按 (importer, imported, line) 去重保首（首现聚合组，契约名归属正确）+"is not allowed to import" 描述片段跳过；+1 罐头测试（真实 tsa 输出形态）；修复后 tsa 复跑 1 violation/1 finding/1 anchored；快测 379 绿 73s、受影响深测组 s7 4 绿、全量口径 1785。探针：work/probe_r4_tsa_dogfood.py（authoring+curate+enforce 全链）/probe_r4_tsa_raw.py（原始 stdout 取证）。

## 9. R5' 批次增补（2026-11-05，S7 TS/JS 执法引擎）

### 9.1 S7 TS/JS 垂直切片：dependency-cruiser 18 沙箱

**`vrp/s7_enforce_ts.py`**（dependency-cruiser 18.7.2 + typescript **5.9.3 钉版**，work/dep-cruise-probe 沙箱，VRP_DEPCRUISE_BIN 覆盖）：s7_enforce.run() 按语言路由——structure.symbol_fids 的 file_path 推导容器，.py 点分模块 → Python 引擎（import-linter，§8.2）；.ts/.tsx/.js/.jsx/.mjs/.cjs 目录前缀 → 本引擎；混语言（py+ts 容器并存）not-enforceable 诚实拒绝；CLI/MCP 零新增面（`vrp enforce`/MCP enforce 内部路由）。

**契约生成复用**：层 DAG（§8.2 的 build_contracts/_forbidden_pairs/_rule_pair）语言无关，TS 容器=目录前缀（`_prefix`：根级文件/node_modules/非 TS 诚实跳过）；配置 `.vrp/dependency-cruiser.cjs`（forbidden[] {name 中文可, severity=error, from/to.path 正则}，按 `^(?:p1|p2)(?:/|$)` 组装防前缀污染——`^src/core` 误配 `src/corefish.ts`）；运行后清理（同 importlinter.ini 模式）。

**探针契约四坑**（work/probe_r5_depcruise_contract.py，2026-11-05）：
- **typescript 7.x 静默空跑陷阱**：depcruise 支持 typescript ">=2 <7"，装到 7.0.2 → extensionsFound .ts available:false、totalCruised:0、**rc=0**——空跑不是错误是陷阱；钉 5.9.3（同 stryker 沙箱先例）
- **Windows shim**：npm .bin 无扩展名项是 sh 脚本（WinError 193），须 depcruise.cmd（cmd /c 包装）
- **rc 语义与 import-linter 相反**：`-T json` 输出下 rc 恒 0（含违例）；rc≠0=配置/环境错误（stderr 出错）——违例判定只能看 JSON（modules[].dependencies[].rules[] severity=error + summary.error），不能看 rc
- **无顶层 violations 键 + 无行号**：模块级图无行号——payload line=None 诚实记录，findings 行号取锚符号 start_line（文件首符号）

### 9.2 dogfood：unwind 仓（TS，1 契约 KEPT）

verify/unwind（pnpm workspace，packages/core 41 文件 + packages/dashboard 29 文件）：诚实分层——core（确定性扫描库）不许 import dashboard（UI）；一包一前缀容器（`packages/core/src`、`packages/dashboard/src`，`^(?:pkg/src)(?:/|$)` 覆盖全部嵌套）；curate 入库 → `vrp enforce` → **status ok / engine dependency-cruiser / 1 契约 / total_cruised 73 / 0 violations——KEPT**（core→dashboard 反向依赖不存在，干净架构数据点；73 模块真巡航非空跑）。**两引擎双数据点齐**：tsa（Python/import-linter）真违例抓出 vs unwind（TS/depcruise）干净契约保持。

### 9.3 测试与验收

- quick +14（test_s7_enforce_ts.py：_prefix/derive_ts_containers/build_config 形状含中文契约名与 regex 转义/parse_violations error-only 去重；mocked E2E：violation 落库锚定+payload 逐字段/幂等/合规清旧/enforcer-failed 保旧（rc=1 stderr_tail）/timeout/not-found/全缺目录 not-enforceable；路由：run() 命中 TS 引擎/混语言拒绝/CLI 接线）；test_s7_enforce.py language-unsupported 改 .rs（.ts 已路由 TS 引擎）
- deep +4（tests/deep/test_deep_s7_enforce_ts.py：真 depcruise E2E——violation→P9 锚定/幂等/修复清库/合规仓干净；模块级 skipif 沙箱缺失）
- 快测 393 绿 64s；全量口径 1803（393+1410）；探针 work/probe_r5_*.py（契约取证/unwind dogfood/新鲜度检查）

## 10. R6' 批次增补（2026-11-05，渲染诚实性 + agent 真实长跑）

### 10.1 渲染诚实性修复（1/2，37d1c6f）

病根二（M4 不对称）：freshness 硬拒只保护 `_open_facts_rw` 消费方，展示路径（render/report）裸奔——自家 report 陈旧 5 批次被平静打开无人知。修复三件（`vrp/render.py`）：**`render._staleness`**——facts.git_head vs 当前 HEAD（behind 计数），不可判定（无 git/无 head）诚实返回 None；**`_stale_banner_html`** 横幅插 meta 行后——横幅随渲染时 HEAD 变化是复现契约的有意例外（同 HEAD 仍字节级全等）；**`_clean_render_out`** 渲染前清自有产物（ir/ html/ *.mmd findings.sarif report.html，外部文件不动）——timeline.html 孤儿（两轮渲染混目录）教训制度化。test_render +5（staleness 三态/横幅有无/清孤儿保外部）；archify 沙箱改 bootstrap 同级复制优先（ROOT.parent/archify，钉值一致即用，勿重复拉取）。

### 10.2 病根五：S1/xcheck reused 短路取消（1b，bb7109d）

`s1_cbm.index_repository`/`xcheck.index_with_code_atlas` 的 reused 快路（缓存命中直接 return）跳过底层 CLI 调用——底层自身增量语义被 wrapper 吞掉，且任何依赖"底层跑过"的链路（如 CBM 自身增量刷新）被短路。修复：恒调底层 CLI（自身增量，reused 判定只用于溯源/提示），reindex 参数保 API 兼容等价；`run_cli` 恒调使 xcheck agreement 的 ledger 新鲜度与 facts.git_head 推进同链（M4 新鲜度链补洞）。test_s1_cbm_provenance 改写为"同仓二次调用恒增量刷新"语义。

### 10.3 agent 真实长跑（2/2）：MCP stdio 真客户端全链 E2E

**work/agent_longrun.py**（本地留存，不入库）：真实 MCP stdio 客户端（mcp ClientSession + StdioServerParameters，spawn `python -m vrp.mcp_server <REPO>`，env 合并 get_default_environment()+CBM_CACHE_DIR），九阶段全链驱动 vrp 自仓：session_begin（幂等续用，撞 "session already active" 解析续跑）→ nav 发现（nav_search 定位 6 符号 cmd_run/index_repository/run_cli/cmd_transact/transact/apply_edits）→ 边验证（nav_edges 四对相邻步 CALLS 实证）→ curate slices（幂等重提交，slice_id 覆盖）→ structure 重建（陈旧 fid→文件域→当前全量 fid 重解析，见 10.4-F1）→ enforce（1 契约 5 containers kept）→ T2（tx_preview→tx_run）→ T4（needs_confirmation 停人工闸）→ render_verdicts。

**长跑即抓双真 bug（反哺闭环再兑现，坑 #85/#86）**：

- **enforce GBK 解码**（f83a4b3 修）：lint-imports 子进程以 locale（GBK）读 UTF-8 importlinter.ini（中文契约名）→ UnicodeDecodeError rc=1 无 broken 块 → enforcer-failed。**为什么此前 dogfood 全绿**：开发 shell 有 PYTHONUTF8=1 代代继承；MCP stdio spawn 链（get_default_environment 最小集）第一次剥掉它就炸——**长跑是环境保真的最强验收**。修复：_execute env 钉 PYTHONUTF8=1。
- **difftastic T2 删除误判**（f83a4b3 修）：纯删除形态 = lhs 有 token + rhs 存在但 token 空（非 rhs 缺失）；旧字典真值判定漏形态 → T2 删真死码被 G4 误拒 structural-change。修复：token 有无分类（双侧皆空=噪声跳过）。

**T2/T4 双 verdict 落地**：T2 `tx-r6-dead-archify-available`（G1-G5 全过 confidence=high，**committed 4b79292**）删真死码 `render.archify_available`（v_dead_code 命中、全仓零调用方、无测试引用）；T4 `tx-r6-status-freshness`（cmd_status 输出加 freshness 节——facts.git_head vs HEAD 的 M4 新鲜度信号，仅扩函数体无新符号）**needs_confirmation → 用户拍板人工 CLI --confirm 放行 committed 96fca8f**——T4 人工闸在真实 agent 场景首次走完全程（agent 停闸→交人→放行→committed）。

**session 收口**（20261105-071111，50min）：末态 git_head=96fca8f（先 `vrp run` 刷新 facts 再 session_end，末态快照含 T2/T4 实况）；diff：symbols +1（2155→2156）、edges +9（7373→7382）、findings ±0、ledger +4；session report 双 L0 图并排 + verdict 时间线 2 条全 committed；tsa 跨仓复验（enforce 受 #85 修复影响）：已知阳性对照复现一致（1 BROKEN、query_service.py:320 core→interface、1 P9 锚定）——修复无回归。

### 10.4 诚实发现（进下批队列，非阻塞）

- **F1 structure 载荷 fid 随内容轮换**：fid=sha256(project|qualified_name|content_hash)，符号内容变→fid 变——旧 structure 载荷里的 fid 在 facts 刷新后批量陈旧（本轮 8 个）。agent 侧靠"可溯 fid→文件集→当前 fid 重解析"重建；**待固化**：curate.structure 读出侧提供陈旧 fid 的文件域恢复辅助（nav/curate 面暴露），消除每个 agent 各自重实现的漂移风险。
- **F2 工件漂移复验只在 CLI curate 路径触发**：`vrp curate` CLI 重跑时复验已入库工件（漂移标 stale），但 MCP `curate_submit` 与 `vrp run` 不复验——旧工件（slices/structure）可在 facts 刷新后保持假 active。**待固化**：把 stale 复验钩进 facts 刷新路径（etl 末尾或 MCP curate 读路径）。
- **F3 facts 刷新后 session/slices/structure 的 M4 语义**：facts.git_head 推进后，slices 里 fid 引用仍有效（符号内容未变则 fid 不变），但"哪些 fid 陈旧"消费方无统一查询面——与 F1 同根。

### 10.5 测试与验收

- 快测 +9（R6'1/2：test_render +5 staleness/横幅/清孤儿；R6'1c：+2 真形态回归（PYTHONUTF8 env 钉值/difftastic rhs-changes-空 删除记账）+ test_s1_cbm_provenance 改写 +2）→ **402 绿 96s**；全量口径 1812（402+1410，复跑待下批或用户指令）；**受影响深测组复验 12 绿 48s**（dif003 T2 删死码 committed 正中 #86 改动高危区 + s7 双引擎 E2E 8 例）
- 探针 work/probe_r6_*.py（facts 复验/长跑侦察/陈旧 fid 溯源）+ work/agent_longrun.py（长跑主脚本）+ work/agent_longrun_result.json（运行结果）
- 提交链（本地未 push）：37d1c6f（1/2）→bb7109d（1b）→f83a4b3（1c）→4b79292（T2）→96fca8f（T4）

## 11. R7' 批次增补（2026-11-06 收口：多级颗粒度 + 交互图链 + 英文化）

### 11.1 R7'(1) 多级颗粒度 L1/L2（7f5e743）

- **病根**：nav 有 L1/L2 分级数据但渲染 IR 只有三种顶层摘要，分层数据与可视化断链（用户反馈；question 拍板：静态多页 + 主链优先）。
- **落地**：L1 模块页 `modules/m{i}-{arch_id}.html`（模块内符号图=内嵌 Mermaid 零子进程——N 页×子进程性能不可行，archify 保持 L0 专用 + 符号表度 top + findings 表）；L2 符号页 `symbols/{fid}.html`（源码片段 ±3 行 cap 80 行号对齐 + 邻域图 + 出入邻居 + 所属业务线 + 病理徽章 P1-P9 classDef）；report→modules→symbols 全相对路径互链 + 面包屑回链。
- **同源与诚实**：模块来源与 L0 IR 同源（structure active 优先层序排序，目录聚合兜底）；符号页选择=每模块度 top 20 ∪ 锚定 findings fids，全局 cap 300 截断诚实标注；路径守卫（战果 #59/#60 同族：绝对/含 `:`/`..`/尖括号 synthetic 全拒）；modules-index IR 进 ir_sha256（复现契约）但不进 archify 循环；无时间戳 + 全链确定性排序，两渲字节级全等。
- **验证**：快测 +8（主链贯通/互链完整性/目录兜底/cap 诚实/代码片段变体/病理徽章/模块图/复现性）；dogfood vrp 自仓 5 模块页+118 符号页 5.2s、tsa 2+40 页 2.0s（findings 1154 截断标注诚实）。

### 11.2 R7'(2) 模块交互图链 chain/（fa6213f）

- **vrp/render_chain.py**（探针 15 生产化移植）：三段式原子提交——Pass1 逐模块规划+暂存渲染（任一图失败整模块弃链）、Pass2 文件页归属（首个成功模块）+预读源码+全局 SymbolIndex（fid_ok 过滤：只收录「文件页已落盘且起始行≤截断帽」的符号，chips/clicks 天然不指向缺失页/锚点）、Pass3 index 总入口；ok=False 整链清理无半成品。
- **入口分流（通用规则零特调）**：2-8 文件真实符号→模块 dataflow（列=文件）；多目录→目录总览树；单文件→符号角色网格（角色全由图形状推导：入口/分发/家族/枢纽/其他）；目录页/整文件页兜底（修复探针单目录单文件无真实符号入口断链）。交互铁律：单击选中+底部浮板、再击/按钮/回车进入、`?from=` 代码页「← Back」、sessionStorage 一次性提示。
- **render.py 集成**：find_archify_mjs 加 vendored 路径（archify vendored 入库零上游改动）；render_report 分流——链成功则 L1/L2 不出、chain-index 进 ir_sha256、findings 链接指向文件页；链意外崩溃 broad except 降级 L1/L2 不阻塞报告。
- **验证**：快测 +6（happy path/链接锚点 href+M 双源完整性/无 archify/假 mjs 降级/路径守卫/复现性）；dogfood vrp 27 页、tsa 364 页 BFS 全可达断链 0，全产物两渲字节级全等，tsa 全报告 20s。

### 11.3 渲染英文化整改（暂缓队列①，本批收口）

- **原则（定案）**：工具生成的 chrome/标题/注记全英文；数据本身（模块名/符号名/AI 梳理职责）保持原样；代码 docstring/注释保持中文。
- **范围**：render.py（L1/L2 页/report.html/staleness 横幅/降级注记/MCP note）+ render_chain.py（chain/ 全部页面 chrome/交互 JS/面包屑/index 卡片/IR meta.title 图内标题）+ session.py（session report HTML chrome）；`<html lang>` zh→en；tests 断言同步（陈旧横幅/目录兜底/截断标注）。
- **判定边界**：chain 卡片 kind 标签、目录聚合兜底 responsibility 等工具生成串→英文；「幕一」等测试 fixture acts 名是数据→不动；快测 430 绿复验。

### 11.4 踩坑对账

R7' 渲染坑由 **#83**（archify validate「schema 不报但渲染炸/报」双层约束四坑，R3' 期入册）覆盖——R7' 复用 `_arch_id`/`_fit_label`/`_layout_components` 清洗层与 meta.output/evidence 契约；R7'(1)/(2) 批次无新增独立坑入册，开发期坑（单目录单文件入口断链、sublabel 抬共享行高 41→27 巨列表放大 27%、pos/size 渲染期必需）均当批修复并记于提交信息与代码注释。

## 12. F 批次增补（2026-11-06，curate 漂移治理）

### 12.1 三病同根（R6' §10.4 诚实发现 F1/F2/F3 的固化）

F1（载荷 fid 随内容轮换，消费方各自重实现恢复）/ F2（漂移复验只在 CLI curate 路径触发，etl/MCP 不复验→假 active 窗口）/ F3（无统一漂移查询面）同根：fid=sha256(project|qn|content_hash) 内容敏感，漂移是常态而治理面缺失。一体交付（2281c60）：

- **F2 复验补洞**：`run_etl` 在 `_etl_coverage` 后、`_write_meta` 前挂 `curate_mod._revalidate_ingested(dst)`——**一处覆盖三路**（`vrp run`/`vrp etl`/事务 promote：G1 的 s6_transact 两处调用都走 run_etl）；MCP `curate_submit` 在 `curate_payload` 入库成功后同复验，收据带 `stale` 节（被拒路径不触发）。stats 新键 `curate_stale_slices`/`curate_stale_structure` → meta count_ 键。重建即 fid 轮换时刻，假 active 窗口就此关闭。
- **F1 fid_ledger 身份史**：新自有表 `fid_ledger(fid PK, project, qualified_name, file_path, first_seen_at)` + qn/file 两索引；`_etl_symbols` 末尾 INSERT OR IGNORE（首见时刻稳定、内容不变零增长、幂等）；经 OWNED_TABLES 快照回灌跨 etl 存续。**不改 fid 契约**——ledger 是读出辅助：旧 fid 反查 qn → 正查现行 symbols（UNIQUE(project,qn) 天然无歧义）得唯一新 fid 即 remap；qn 消亡→ledger 文件域现行符号补位（cap 10，人工判读）；ledger 无记录诚实零候选。
- **F3 统一查询面**：`curate.drift_report(con)` 只读——slices[]/structure 逐条 `fresh`/`missing_fids`（含候选）/`missing_edges`，`fids_recovered`（内容回退 fid 复活，原载荷可原样重提），`summary.active_but_drifted` 非空=复验链有洞（对账信号）；CLI `vrp drift <repo>`（rc=1 仅当 stored active 实际漂移；stale 已是诚实状态不影响 rc）+ MCP `drift_report`（二十六件）。
- **remap 是提议不是旁路**：改写后仍走正常 curate_submit 全量重验（fid 存在性/相邻边/acts）；标记永远发生在写路径，drift_report 零副作用。

### 12.2 dogfood 实录（真实轮换首燃）

- **vrp 自仓**：`vrp run`（2281c60 代码态）→ 钩子 `curate_stale_structure: 1`——**F2 首燃即抓出历史遗留假 active**：missing 三 fid = `vrp.cli`/`build_parser`/`cmd_status`，其中 cmd_status 是 R6' T4（96fca8f）改的，F2 之前跨 R7' 整批保持假 active，本轮一次抓齐；三 fid 均 `ledger:false`（表与漂移同轮诞生，pre-F fid 天然无记录——诚实局限，非缺陷）；复梳理=ai-layer 文件域（cli.py）当前全量 29 fid 重解析（R6' 长跑先例）→ CLI curate accepted（收据 stale 节空）→ drift 全 fresh rc=0 → report 刷新 20.3s（chain/ 链复现）。**下批任何 cli.py 内容变更即首次真实 remap 触发**（本轮 ledger 已灌，fid 全覆盖）。
- **tsa 跨仓**：`vrp run` 24.5s 无回归；fid_ledger 首灌 **66,132 行**（=符号数），fid→qn 反查 0.12ms（索引）；R4' structure（kernel/interface）active 存续 fresh；drift rc=0。

### 12.3 测试与文档

- 快测 +14（test_drift.py 12：drift 单元×9 + MCP 补验 + etl 钩子全周期 E2E + 无漂移零误伤；MCP/CLI 接线 2）→ **430 绿 121s**；test_etl_preserve OWNED_TABLES 契约+ledger 回灌改写；test_mcp_server TOOL_NAMES 26 件+drift over MCP+CLI 接线
- 契约文档《梳理工件契约》§0/§3 重写 + 新 §4（漂移查询面与重定位）；README 快速开始/CLI 表/MCP 件数三处
- 提交链（本地未 push）：2281c60（功能本体：代码+测试+契约+README）→ 本节（docs 收口）

## 13. G 批次（2026-11-06，内置 LLM 梳理循环——已批并实施）

> 状态：设计已批（用户 2026-11-06，同批拍板：全自动+落盘留痕、**TUI 砍**）；G1-G3 已实施（§13.9）。

### 13.0 裁决修订（用户 2026-11-06）

- **§0.3「vrp 内置 LLM 循环明确排除」修订为「内置薄驱动器，仅 curate 数据面」**：r3 排除的是内置 LLM 驱动**重构**循环；本批用户拍板的是「用户配置 API 后直接使用」的零 agent 路径——内置循环只做**梳理**（curate 工件生产+验收入库），重构事务面（tx/T4/工作树写）永不出现在内置循环。§0.1 裁决 2「LLM 调用不进 `vrp run`」**不变**：`curate-auto` 是独立交互域命令，管道确定性可复现红线不动。
- **自动化程度（用户拍板）**：全自动+落盘留痕——无人工确认环节，prompt/响应/工件/回执全落 `.vrp/auto/` 可复查；curate 本就不碰代码树，风险面小。
- **TUI 砍（用户拍板）**：report.html 已覆盖人类消费面，CLI 已覆盖驱动面。

### 13.1 定位

MCP+skills（§1.4）服务**有 agent 环境的用户**；内置循环服务**只有 API key 的用户**。两者共用同一套底层模块（s5_dossier/curate/s7_enforce）与同一验证器——内置循环不是 agent 框架，是把「外部 agent」换成约两百行薄驱动器。

### 13.2 决策记录

| # | 决策点 | 裁决 | 理由 |
|---|---|---|---|
| D1 | 工具调用协议 | **无 function calling——校验器即工具**：LLM 每步直接产出工件 JSON → `curate_submit` 确定性校验 → 拒收回执带稳定码 → 回执驱动重试（≤3/工件，skills 红线同款） | curate 契约本来就是确定性 JSON 校验，天然防幻觉；不需要 agent 框架/工具调用协议 |
| D2 | LLM 协议 | **OpenAI-compatible 单协议**（`POST {base_url}/chat/completions`） | 一个协议覆盖 OpenAI/DeepSeek/GLM/Qwen/本地 vLLM/Ollama-compat；不做 provider 插件系统 |
| D3 | 依赖 | **stdlib `urllib` 实现，不建 optional extra，直接进核心** | urllib 是标准库，零依赖红线不破（对比：mcp extra 因 mcp 包是第三方） |
| D4 | 信任边界 | **循环只走 curate 数据面**：模块不 import s6/tx 面，物理不可达重构事务；curated 内容默认英文红线写进 prompt（全局默认英文指令延续） | LLM 输出只能变成 facts.db 里的梳理数据，永远碰不到代码 |
| D5 | 留痕 | **全自动+全留痕**：`.vrp/auto/run-<UTC>/` 保存脱敏配置回显/每步材料/响应原文/工件/回执/summary；key 任何形态不落盘（测试扫描锁） | 用户拍板；可复查性替代人工确认环节 |
| D6 | 对话模型 | **无状态单发**：每次调用携带完整上下文（材料+现有工件+历史回执），不做多轮会话状态 | 状态less 可复现、可重放；多轮状态增加复杂度无增益 |
| D7 | prompt 来源 | **协议 prompt 内嵌为 vrp 模块常量**（精简版：输出契约+默认英文红线+回执语义），skills 保持完整协议文档，互引注释防漂移；测试断言 prompt 含红线关键句 | skills/ 目录不随 pip 包分发；全文复制 skill 会造成双份协议漂移 |
| D8 | 解析容错 | **响应剥离**：markdown 围栏剥离+首个 `{`/`[` 到末 `}`/`]` 提取；解析失败计一次尝试（等同拒收，消耗重试额度） | 各家 json_mode 支持不一，不依赖 `response_format`，防御性提取最大化兼容 |

### 13.3 配置层

- 文件：`<repo>/.vrp/config.json`（仅 repo 级；密钥走环境变量引用）：
```json
{
  "llm": {
    "base_url": "https://api.openai.com/v1",
    "api_key_env": "VRP_LLM_API_KEY",
    "model": "gpt-4.1",
    "timeout": 180,
    "max_output_tokens": 16384
  }
}
```
- `api_key_env` 优先（引用环境变量）；`api_key` 明文键允许（私有仓便利）但留痕一律脱敏、README 不推荐
- 必填三键：`base_url`/`model`/（`api_key_env` 或 `api_key` 之一）；缺键诚实报错并给出最小配置示例；无 config 文件→诚实拒绝（不猜默认 provider）

### 13.4 循环协议（六步，对齐 vrp-curation skill）

1. **取材**：`build_dossier(task, skip_external=True)`——材料进 prompt（快速+确定性，与 MCP 面默认一致）
2. **slices**：单发产出 slices JSON → `curate_submit(kind=slices)` → 拒收则回执进上下文重试 ≤3
3. **structure**：单发（材料含已 accepted slices）→ `curate_submit(kind=structure)` → 重试 ≤3
4. **enforce**：`s7_enforce.run`（P9 findings 计数进 summary；库无 active structure 诚实 skip）
5. **drift_report**：读出存档进 summary
6. **汇总**：`summary.json` + rc

- **中止语义**：slices 3 次拒收→中止（structure 材料质量依赖 slices，继续会产出无上下文的劣质工件），rc=1；structure 3 次拒收→enforce 照跑（库态决定），drift 落盘，rc=1
- **重试双层**：验证拒收 ≤3/工件（D1）；HTTP 传输层 429/5xx 退避重试 ≤2 另计（不消耗验证重试额度）
- **M4 新鲜度**：与 CLI curate 同闸（`_open_facts_rw`）

### 13.5 接口与留痕

- CLI：`vrp curate-auto <repo> [--task T4] [--budget 32000] [--model <override>]`；rc=0=全部 accepted，rc=1=有工件 3 次拒收或配置缺失
- 留痕目录 `.vrp/auto/run-<UTC时间戳>/`：`config.echo.json`（脱敏）、`steps/`（每步 request/response.raw/artifact/回执）、`summary.json`；跨 run 追加索引 `.vrp/auto/log.jsonl`（每 run 一行）
- **MCP 不加工具**：内置循环是 CLI 用户面；agent 用户有自己的循环，防止面重复

### 13.6 测试策略

- 单元：config 解析/校验/脱敏；prompt 装配；响应剥离（围栏/前后杂文本）；重试计数边界
- E2E mock：注入 fake 传输函数——happy path 全绿 rc=0；拒收→回执驱动→第 2 次 accepted；3 次拒收→中止 rc=1；429→退避重试成功
- 红线断言：prompt 含默认英文红线关键句；循环模块无 s6/tx import；扫描 run 留痕目录断言 key 零出现
- deep（可选）：真实 LLM 冒烟（skipif 无 `VRP_LLM_API_KEY`）

### 13.7 明确不做

通用 agent/工具调用框架；多轮对话状态；流式输出；LLM 驱动事务（tx/T4 面永不出现在内置循环）；provider 插件系统；TUI（已砍）；进 `vrp run`（§0.1 裁决 2 不变）。

### 13.8 实施切分

- **G1**：config 层 + LLM client（urllib POST、超时/传输重试/错误诚实）+ 单测
- **G2**：循环本体+留痕+CLI 接线+mock E2E，快测全绿
- **G3**：《梳理工件契约》增§6（内置循环）+ README + skills 交叉引用 + dogfood 实录

### 13.9 实施记实（2026-11-06）

- **G1**（`vrp/llm_client.py`）：`LlmConfig`（内存 key + echo 掩码）/`load_config`（缺键诚实报错+最小示例；**utf-8-sig 容错读取**——dogfood 抓到 PS 5.1/记事本写 BOM 配置即崩，真 Windows 用户必踩，改用 utf-8-sig）/`chat`（urllib POST，429/5xx 退避 ≤2 不消耗验证额度，4xx/网络错诚实抛）/`extract_json`（D8 围栏剥离+首尾括号提取，失败计一次尝试）
- **G2**（`vrp/auto_curate.py` + cli.py 接线）：`run_auto_curate` 六步（dossier skip_external=True → slices → structure（材料含本轮已入库 slices）→ enforce → drift → summary）；`_produce_artifact` 验证重试 ≤3（拒收回执/解析失败均计），request_error 即工件失败；slices 失败→structure skipped、enforce/drift 照跑（库态报告）、rc=1；留痕 `.vrp/auto/run-<UTC>/`（config.echo 脱敏/steps 全量/summary）+ `log.jsonl` 追加；prompt 内嵌协议常量（D1/D7：curated 默认英文红线句+只用材料中 fid+拒收码速查进上下文）
- **G3**：契约《梳理工件契约》新 §6（配置/六步与重试/留痕与 rc/红线）；README 快速开始+CLI 表 `curate-auto` 行；skills/vrp-curation 头部零 agent 路径注记；本节收口
- **测试**：tests/test_auto_curate.py **31 例**——config 单元 12（缺文件/inline+脱敏/env 引用/参数化 7 拒/坏 JSON/BOM 容错）、chat 传输 5（429 退避成功/401 不重试/3 次耗尽/网络错/坏响应形）、extract_json 6（围栏/语言标注/前后散文/数组/非法）、E2E 7（happy path 含留痕全清单+key 零落盘扫描+prompt 红线句断言/拒收→回执驱动→第 2 次 accepted/3 拒中止（03- 文件不存在+enforce/drift 仍落盘）/structure 失败 rc=1/解析失败计尝试/缺 config/缺 facts.db）、红线 1（双模块源码无 s6_transact/tx_run 引用）
- **dogfood（vrp 自仓）**：无 config → 诚实拒+最小示例 rc=1 ✓；M4 闸正确触发（facts.db 落后 HEAD 硬拒，重跑 etl 过）✓；伪 config（127.0.0.1:9）→ 连接诚实拒，留痕树完整（request+error 落盘，key 零出现），enforce 诚实 skip（本轮 etl 复验把旧 ai-layer structure 标 stale——F2 钩子顺带佐证）✓
- **真实 LLM 冒烟**：待用户配置真实 key 后跑（`vrp curate-auto <repo>`）；deep 冒烟（skipif 无 `VRP_LLM_API_KEY`）留作下批可选
