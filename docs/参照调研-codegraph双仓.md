# 参照调研：codegraph 双仓学习报告

> 状态：调研完工（2026-11-19）。对象：`verify/codegraph`（colbymchenry/codegraph v1.6.3，78k★，TypeScript）与 `verify/CodeGraphContext`（CGC v0.6.13，4.4k★，Python/KùzuDB→LadybugDB）。
> 动机：用户发现重合项目，学习正规项目用于优化本项目。两仓已浅克隆入 `verify/` 参照区（只读）。
> 结论先行：**五项铁证验证 VRP 路线正确**（SQLite 图库/零依赖/字节级可复现/批处理静态产物/PR 级门禁）；**九项优化候选提炼入《构想v13》**（MCP 消费面工程/置信度分层/Golden 审计/ETL 健康面等）。
> 方法论：只读源码研究（explore 代理双线深读+交叉综合），关键数字实测（文件/行数统计），结论附源文件路径锚点。

## §1 colbymchenry/codegraph（78k★ 参照）

规模快照：`src/` 341 个 TS 文件、200,115 行；`codegraph-kernel/` Rust 原生内核 27 文件、29,412 行；`__tests__/` 963 测试文件、246,506 行；`docs/` 80+ 篇设计/基准文档；CHANGELOG 35 版（0.7.6→1.6.3，2026-05→2026-11）。

### 1.1 数据模型与存储

- **6 张核心表**（`src/db/schema.sql`，233 行）：`nodes`（20 列，id=文件路径+限定名哈希）/`edges`（含 **provenance** 列：`tree-sitter|scip|heuristic`）/`files`（content_hash）/`unresolved_refs`（完整生命周期 pending→删除或 failed 留存重试，`name_tail` 支持点分名重试，#1240/#1269）/`name_segment_vocab`（camelCase 分词倒排）/FTS5 虚表+3 触发器同步。
- **NodeKind 23 种/EdgeKind 13 种**（`src/types.ts:22-129`）；数组顺序是原生内核 wire contract 的一部分，只许追加不许重排。
- **索引工艺**：表达式索引 `lower(name)`；边唯一性表达式索引 `idx_edges_identity ON edges(source,target,kind,IFNULL(line,-1),IFNULL(col,-1))`（#1034 重复边事故）；约 8 个部分索引（partial index）针对 failed refs 重试；迁移系统 v2–v16（`src/db/migrations.ts`）。
- **存储**：SQLite via Node `node:sqlite`（DatabaseSync）+WAL+FTS5，零原生编译；每项目 `.codegraph/` 目录；WAL 治理阀（`src/db/wal-valve.ts`——曾观察到 64GB WAL，索引期软阈值 max(256MB, 索引/4, ≤2GB)）。
- **"Pre-indexed" 确切含义**：不是分发预建索引，而是"索引成本前置"（`codegraph init` 在 agent 会话前一次建图，查询路径永不触发索引，重引擎 `src/lazy-engine.ts` 延迟加载）。"图是预先建好的，代理直接问"（`README.md:200-202`）。

### 1.2 索引管线

- **tree-sitter 双引擎**：Rust 原生内核（20 语言编译进原生码，每文件一次边界穿越返回 5 个扁平 buffer，栈深保护 #1581+解析预算 #3274，换语言须过逐字节等价门）+wasm 后备（29 语法，语法错误文件逐文件回退，图相同）。Spike 数据：dubbo 4048 Java 文件 wasm 7-worker 4.7s vs Rust 单线程 1.07s。
- **42 语言标识符/34 语言提取器/41 框架 resolver**（Express/Laravel/Rails/FastAPI/Django/Spring/NestJS…产出 route 节点，`src/resolution/frameworks/`）。
- **动态分派合成**（核心卖点）：callback-synthesizer（285KB）/tier-synthesizer/React setState→render/JSX child/RN 桥/C++ 模板实例化——所有合成边标 `provenance:'heuristic'`+`metadata.synthesizedBy`+`registeredAt`。
- **auto-sync 三层**：原生 fs.watch+防抖 2000ms（clamp 100ms–60s，小 pending 快速通道 100ms）；防抖窗口内引用 pending 文件的 MCP 响应前置 ⚠️ 横幅；连接时补账（(size,mtime)+content-hash 对账吸收无 server 期间外部编辑）。变更检测 content_hash 为主，IndexHealth 补刀"哈希是最新的但符号缺失"的漂移（#2336，`src/types.ts:295-306`）。
- **速度**：Linux 内核 70k 文件/200 万符号/640 万关系 2 核 VPS <12min；单文件保存同步 ~0.3s。

### 1.3 查询与消费面（精华所在）

- **MCP 8 工具定义但默认只暴露 `codegraph_explore` 一个**（`src/mcp/tools.ts:1717-1894`；`CODEGRAPH_MCP_TOOLS` 环境变量重开其余）——实测"一个强工具比一排窄工具更能引导代理"；`codegraph_context`（模糊输入）与 `codegraph_trace`（代理不选它）都被删掉（`AGENTS.md` "Adapt the tool"）。
- **双层自适应预算**（`tools.ts:265-409`）：调用预算 <500 文件→1 次…≥25000→5 次（上限 5）；输出预算 <150 文件 13K/4 文件/3800 字符…≥15000 文件 24K/8 文件/7000。**硬约束：总输出压在 ~24K 字符以内**（宿主 ~25K 内联上限，超过被外置成文件再让代理 Read 回来）。"更大档不得更小每文件额度"单调性不变量。
- **"适配代理而非改造代理"设计律**（`AGENTS.md` Retrieval performance）："让代理已经在调的工具用它已经给的输入做更多事"；3 个措辞变体的 server-instructions 引导实验全部回归，新工具代理不选。
- **错误即放弃**：`isError:true` 一两次就让代理整个抛弃 codegraph——可恢复状态一律返回**成功形状的指引文本**（`tools.ts:100-111` NotIndexedError）；`isError` 只留给安全拒绝和真故障。
- **explore-flow**：命名符号袋→调用路径（≤1 个无名桥接防走神、重载感知、PascalCase token 偏置），Flow 置顶+按文件分组带行号源码+blast-radius。
- **代理引导单源**：指引只在 MCP initialize 响应下发（issue #529 后唯一权威）；安装块只写标记包裹短块。
- **诚实披露**：`docs/benchmarks/residual-context-occupancy.md` 承认 explore 会话驻留上下文比 grep 代理多 ~80%（67k vs 18k tokens）。

### 1.4 工程实践

- **测试**：963 文件/246k 行；不 mock 数据库（临时目录+真 SQLite）；抛弃式 HOME 沙箱（`__tests__/setup-home-sandbox.ts`，#2275）；参数化安装契约测试（幂等/可逆/字节等价 ~47 件）；memo 陈旧化防护测试；平台门控 `it.runIf(process.platform==='win32')`；三平台实测纪律（Linux Docker `--init` PID1 僵尸收割/Windows Parallels VM+SSH）。
- **发布供应链**：Rust 内核 4 runner×7 target 预编译→npm OIDC trusted publishing（无长期 token）+provenance+SLSA Build L2；**Windows 事务式换文件安装器**（运行中文件先 rename `.old-<token>` 再替换，失败全量回滚，`install.ps1:19-89`）。
- **Telemetry**：Cloudflare Worker 公开可审计 ingest 端点（不读 IP/90 天删除/三层关闭 DO_NOT_TRACK）。
- **无 PR CI**：只有 release+deploy-site 两个 workflow——4600+ 测试靠贡献者本机跑，与"发布版 vs main 正确性漂移"直接相关。

### 1.5 短板与教训

1. **巨石文件失控**：`name-matcher.ts` 1.96MB（全库最大）、`tools.ts` 484KB、5 巨石占 src 相当比例；回归审计发现 5 处"发布版相对 main 丢失边"（缓存路径与未缓存路径语言族不一致，`docs/benchmarks/regression-audit-2026-09.md`）——启发式解决层最容易悄悄回归。
2. **探索预算反复横跳**：ITER1/ITER2/ITER3 注释实录（"iter2 激进收缩把代理逼回 Read，iter3 回退"）——**输出预算类魔数必须有单调性不变量+回归测试钉死**。
3. **基准方法论是被逼出来的**：早期 A/B 没封 CLI——WITHOUT 臂 26/28 次通过 Bash 摸到 codegraph，"控制臂不是控制臂"；低显著度渠道（instructions/description 措辞）改变不了代理行为。
4. **平台长尾税**：Windows V8 concurrent-marking 杀 worker（0xC0000005 无 dump）、NTFS last-access 噪声、WSL2 /mnt 的 fs.watch/SQLite 锁全废、Linux inotify 预算——**凡"常驻 watcher+共享 SQLite"的设计决定，五个平台各付一次税**。
5. **合成边时间性妥协**：`CODEGRAPH_SYNTH_MIN_INTERVAL_MS` 允许合成边滞后代码最长 24h 且工具输出不声明。

## §2 CodeGraphContext（CGC，4.4k★ 参照）

规模快照：Python，tree-sitter 0.24-0.25+language-pack（27 注册 grammar）；29 MCP 工具；281 测试文件（unit 240/integration 17/e2e 3/perf 1）；6 图数据库后端；15 CI workflow。核心四份自审报告（`CGC_*.md`）是本次调研最有价值的材料。

### 2.1 图 Schema 与存储

- **33 节点表+34 边表**（`src/codegraphcontext/core/embedded_schema.py`）：Kùzu 主键只能单列→复合自然键压进合成 uid；两个写进注释的踩坑：Kùzu 保留字 `Macro`/`Property`/`Union` 在 REL TABLE 里必须反引号转义否则**静默失败**；`HEURISTIC_CALLS` 曾未声明导致 Kùzu 上 `analyze dead-code` 全挂。
- **CALLS 边携带**：`line_number, args[], full_call_name, confidence DOUBLE, resolution_tier INT64, confidence_label, source, resolution_method`——**每条调用边带置信度分层**，最有价值的建模决策。
- **增删策略**：写入=MERGE upsert（幂等）；例外 CALLS 边在 Neo4j/Nornic 用 CREATE 非幂等——**watcher 增量在 Neo4j 上每次保存产生重复边**（`watcher.py:315-320` 注释自认）。文件级增量=改动文件∪反向 CALLS caller 文件∪INHERITS 邻居→删边重解析重连。全量重建无事务（"partially written graph with no rollback"，`pipeline.py:228-232` 自认），补救=MERGE 幂等性+写失败重试一次+事后不变量自愈（`repair_missing_contains_links`）。
- **存储引擎踩雷**：**KùzuDB 上游 2025-10-10 归档**，0.11.3 最终版，默认后端已切 LadybugDB（Kùzu 维护 fork，issue #852）。**LadybugDB 官方自述"SQLite/relational queries underneath to simulate property graph operations"**（`docs/docs/concepts/backends.md:46-51`）——CGC 生态自己收敛到"SQLite 上模拟图查询"。同库索引耗时：FalkorDB 1.5s vs Kùzu 22s vs Ladybug 18s（`CGC_E2E_BUG_REPORT.md:32-41`）。

### 2.2 索引管线

- 9 步流程（`pipeline.py:131-537`）：.cgcignore 文件发现（默认忽略表带事故注释：`obj/` #1585 .NET 生成源幻影模块、`.terraform/` 23000 vendored vs 1900 真文件）→pre_scan imports_map→**并发解析**（asyncio.Semaphore 默认 10，上限 32）→**有序写入**（按 path 排序"共享节点获得确定性 canonical metadata"）→继承/结构边→**调用解析 9 级分层**→out-of-line 后处理→领域专项（Spring/Maven/ORM/MyBatis）→可选 embedding/继承感知再解析。
- **9 级解析分层**（`resolution/calls.py:65-86`）：tier1 explicit this/self/super（1.00）→tier2 同文件局部（0.95）→tier3 推断 receiver+FQN（0.88）→tier5 唯一短名（0.90）→tier7 FQN 路径子串（0.70）→tier8 多候选取字母序第一（0.25，写 HEURISTIC_CALLS）→tier9 同文件兜底（0.08）；标签 EXTRACTED/INFERRED/AMBIGUOUS。
- **确定性是补出来的**：asyncio.as_completed 顺序随调度变化→"any last-writer-wins or first-match-wins choice becomes nondeterministic. The graph write loop already sorted for exactly this reason; resolution was left unsorted"（`pipeline.py:308-318`）——解析后、解析/后处理前两次按 path 排序。
- **致命依赖坑**：language-pack 1.21.0 重塑 Haskell grammar 导致 Class 节点全丢，pin `<1.21.0`（`pyproject.toml:32-49`）——**tree-sitter grammar 上游漂移直接改变图内容**。

### 2.3 消费面工程

- **29 MCP 工具**（`tool_definitions.py`）：索引（异步 job_id+check_job_status 轮询）/搜索（find_code 四路合并打分：函数名 0.9/类名 0.8/变量名 0.7/内容 0.6，依赖代码降权 0.7/0.6/0.5/0.4，模糊搜索 Lucene `token~编辑距离` vs 便携自实现 Levenshtein 双路径）/关系分析（15 query_type 聚合一工具）/质量/管理/Bundle/Watch/领域/架构模拟（`analyze_architectural_evolution`：git churn+复杂度→技术债热点，对标我们的 timeline）。
- **find_dead_code 精细化**（`code_finder.py:995-1150`）：排除规则从子串改**整词匹配**（旧版 `name CONTAINS 'main'` 误杀 `domain_check`/`remainder`/`maintain_index`）；入口名三层排除（全语言/JVM-Android 框架回调 24 个/Python 测试钩子）；**每结果 low/medium/high 置信度+可读 reason**（dunder/测试钩子/入口名/override→low，带装饰器→medium，无排除信号→high，#1332）；limit 在 Python 侧分页使 total_count 为真实总数；防注入三层（depth clamp 1-20/decorator 单引号转义/ignore 目录同时套 func 与 caller）。
- **消费面硬工程**：`TOOL_RESULT_LIMITS`（find_code 20/cypher 100/dead_code 50）+`TOOL_QUERY_TIMEOUTS`（默认 120s=最慢读 10 倍，"168,745 函数图上 find_dead_code 实测 12s" 校准出处写进注释）+响应截断带可见告警；**GCF 压缩编码降 token ~62%**（键声明一次+值管道分隔，`CGC_OUTPUT_FORMAT=gcf`）；只读守卫先剥字符串字面量再看首关键字（修"字符串里含 CREATE INDEX 被误吞"）。

### 2.4 自审报告四件套（最有价值）

- **CGC_CALL_GRAPH_AUDIT_REPORT.md**：21 语言 CALLS 平均 98.3% 记分卡（dart 80%/C 90%/PHP 96.8%/Python 98.4%），FAILURE 列=当前导出 vs 期望 golden 的 missing/spurious 双向计数。
- **CGC_E2E_BUG_REPORT.md**（12 bug：0 Critical/4 High/5 Medium/3 Low）：BUG-001 配置渗漏（repo 本地 .env 覆盖隔离 HOME——**环境配置分层必须显式 scope**）；BUG-002/003 模块级嵌套调用在已发布 wheel 上解析错误导致**官方 quickstart 主示例直接失败**（教训：文档示例必须是 CI 里的 e2e 用例）；BUG-005 删除留 9 个无 path 孤儿；BUG-012 **发布 wheel vs golden 11/20 语言节点超标**（dart +79.6%/misc +170%——golden 必须钉住生成它的确切发布工件）。
- **CGC_GRAPH_INCONSISTENCIES.md**（100 项 backlog）：ID 78-83 meta 条目最深刻——**"循环完美门"自省**：`GOLDEN_PERFECTION_REPORT.md` 是导出与自己比的循环门，给出误导性 21/21 PASS，与真实 CALLS 准确率 84.6% 冲突；结论句式 "Retire circular perfection gate; trust call_graph_audit.py only"。ID 84-100：动态/反射调用是静态调用图的永久盲区（`globals()[name]()`/`getattr`/闭包/链式调用）。
- **CGC_REPORT.md**：generate_report 输出模板（God Nodes 高 fan-in/最复杂函数/跨模块连接/AMBIGUOUS 置信度边排查模板）。

### 2.5 短板与教训

1. **依赖面失控**：核心依赖 20+ 项含 4 个图库驱动互相锁死（redis-py 6 破坏 Unix socket、falkordb 三方 pin 死）；perf 测试是 Mock 假测试（`tests/perf/test_large_indexing.py` 断言 Python 循环 <1.0s——形同虚设）；16 个语言 toolkit 全部 raise NotImplementedError；6 后端里 2 个没法测；默认后端 FalkorDB Lite 还是 Unix-only+Python 3.12+（**Windows 上不可用**）。
2. **可复现性系统性缺失**：绝对路径做 PK（检出位置不同图不同）、indexed_at 时间戳入库、Neo4j CREATE 非幂等。
3. **评估指标自我矛盾**：perfection gate 假象 vs 真实 84.6%；手工改过的 golden 不落盘（563/796 vs 磁盘 482/621）；审计工具 `call_graph_audit.py` 本身流失（scripts/ 目录已不存在）。

## §3 交叉综合

### 3.1 验证 VRP 路线正确的五项铁证

1. **SQLite 收敛**：CGC 生态终点 LadybugDB=官方自述"SQLite 模拟图查询"；Kùzu 归档+写慢 15 倍+无事务。VRP 纯 SQLite+递归 CTE 殊途同归且无 6 后端方言负担。
2. **依赖地狱实录**：两仓 pin 注释全是踩雷记录（grammar 漂移毁 Class 节点/图库驱动锁死/redis-py 破坏性升级）。VRP 零第三方运行时依赖红线被证明正确。
3. **可复现性缺失的代价**：绝对路径 PK/时间戳入库/非幂等 CREATE/发布物漂移——VRP"同库同参数字节级可复现"是两项目都做不到的硬差异。
4. **常驻进程工程税**：daemon registry/PPID watchdog/writer lock/WAL 64GB/Windows V8 崩溃/inotify 耗尽/fs.watch 平台长尾——占 codegraph CHANGELOG 大篇幅；VRP 批处理+静态产物天然免疫。
5. **无 PR CI 的漂移**：codegraph 回归审计丢 5 处边、CGC 发布 wheel 11/20 语言超标——VRP 本地 pre-push 门禁+冻结件哈希路线正确。

### 3.2 VRP 已有而两项目缺失的整条线

时间维度（git history 分析/timeline 腐化曲线/cochange 热点）、符号级稳定事实 ID（fid 内容哈希轮换）、ETL 行集等价校验（J8）、评估产物流水线（dossier/评分/冻结件）、字节级冻结复现。

### 3.3 优化候选 → 《构想v13-参照调研优化候选.md》

九项提炼（AA 组=MCP 消费面，BB 组=事实质量面），详见构想v13：AA1 单强工具与成功形状错误契约/AA2 自适应输出预算与 per-tool 限额/AA3 GCF 压缩编码/BB1 调用边置信度分层/BB2 死代码置信度三级+reason/BB3 Golden 准确率审计矩阵/BB4 ETL 健康面（IndexHealth+不变量自愈）/BB5 并发产物确定性排序 checklist/BB6 A/B 评估防侧信道。

## §4 参照区登记

- `verify/codegraph`：colbymchenry/codegraph v1.6.3 浅克隆（只读参照，2026-11-19 加入）
- `verify/CodeGraphContext`：CGC v0.6.13 浅克隆（只读参照，2026-11-19 加入）
- 与既有 `verify/codebase-memory-mcp`（CBM 索引真身）、`verify/code-atlas-lusiem`（TS 小仓）、`verify/tree-sitter-analyzer`（Python 大仓）并列
