# 构想v7——AI 重构主流程与梳理层定稿

> 状态：定稿 r2（2026-11-04）。r1 以渲染层四投影为中心，经产品定位纠偏后修订：
> **主流程 = 工具分析 → AI 梳理分析 → AI 重构 → 工具分析（回归验证）**；
> 可视化降格为各环节中间产物的呈现（横切服务），引入用户决策环节。
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

### 1.3 vrp 侧闭环缺口（三件）

1. **事务契约文档化**：schema 现只在 s6 docstring——出契约文档（schema+样例+G 码对照表：生产常见错误→验收失败码映射）+ AI 取材指南（T1-T4 各档 dossier 七节取材路径）
2. **verdict 回馈面**：dossier evidence 节内嵌上轮未成功 verdicts + `vrp verdicts` 命令（§0.1-5）
3. **curate 定位修正**：从"渲染供数"改为"AI 重构理解的沉淀与消费面"（§2）

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

四投影技术选型保留 r1 定稿：archify（图）/ SARIF 2.1.0（行级诊断）/ Mermaid+repomap（地图）/ 数据层不硬引入（facts.db 即产物，Datasette 列推荐）。

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

## 6. 实施批次（r2 重排，主线优先）

| 批次 | 内容 | 验收 |
|---|---|---|
| **R1'** | 事务契约文档（schema+样例+G 码对照表+T1-T4 取材指南）+ verdict 回馈双入口（dossier evidence 节内嵌上轮未成功 verdicts + `vrp verdicts` 命令） | 契约文档对照 s6 代码逐字段核；dossier 内嵌快测；命令单测；门禁绿 |
| **R2'** | curate 梳理层（slices/structure 表+验证器+curate 命令）+ 用户决策点（`--reject` + decisions 留痕表） | 验证器全拒绝路径参数化；手工 AI 工件样例→curate 入库 E2E；幂等/stale/留痕测试 |
| **R3'** | 中间产物可视化（按 §5.2 对象表：SARIF、archify 沙箱五视图、Mermaid、T4 清单、降级链） | 深测 E2E 两仓出 report.html（`archify validate --json` rc=0）；降级测试；dogfood 自报告 |
| **R4'** | S7 架构规则固化 | 远期，随 R2' 经验定稿设计 |

每批次：小步提交、门禁快测全绿、深测按批跑、文档三件套同步（交接/踩坑/构想v7 增补节）。

## 7. 与既有文档的关系

- 《渲染层方案.md》：讨论稿，**被本文档取代**（保留作决策过程记录；其 R1/R2 渲染优先批次作废，以本文 §6 为准）
- 《构想v4》：S6-S8 设计的原始出处（编号映射见 §0.1-1）
- 《产品目标.md》：R1' 实施时修订——主流程四环明示、"不做渲染引擎与服务"红线保留、阶段表补 S7/S8 行
- 《交接文档》：每批次收口时同步（下一步节 + 代码地图）
