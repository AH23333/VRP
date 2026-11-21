# Glossary（Q24 术语表）

> Diátaxis 定位：**reference**（查阅面，非教程）——新读者接手成本直接降低（构想v11-Q24）。
> 纪律：每条目只描述已落地事实（码/表/文档锚点真实可核，#153 宁缺勿错同源）；
> 指针失效=漂移，`tests/test_glossary.py` 锚点锁守卫（路径/表名两 face）；
> 理论维度见姊妹篇《理论出处.md》（W21）。

## 一、核心数据面（facts.db）

| 术语 | 定义 | 详处 |
|---|---|---|
| facts.db | SQLite 事实库，管道全量事实唯一落点（22 表）；三红线首条要求同输入字节级同产物 | `vrp/schema/facts_schema.sql`（表清单单源） |
| fid | 符号唯一标识（CBM 符号 id），ETL 路由入 `symbols.fid`，一切跨表引用的锚点 | `vrp/etl.py`；契约《事务契约》 |
| symbol / edge | 符号与符号关系（调用等），ETL 从 CBM 单向路由进 facts.db | `symbols`/`edges` 表 |
| finding | S2 病理登记条目（ast-grep 规则/死代码/克隆/变异逃逸/P9 执法违例） | `findings` 表；`vrp/s2_*.py` |
| anchor（S3） | AI 声明的锚定符号对，S3 校验器核对符号存在与归属 | `vrp/s3.py` |
| coverage（S3） | 声明覆盖面核对（哪些符号被 AI 声明触达），缺口入 coverage_gaps | `vrp/s3.py`；`coverage_gaps` 表 |
| verdict（S6） | 重构事务收据（repair receipt）：每次事务必写 `.vrp/verdicts/<tid>.json`，失败带稳定 G 码；open verdict 回灌 dossier evidence | `vrp/s6_transact.py`；《契约/事务契约》 |
| G 码 | 闸门失败码（G0-G6，G4 含五信号），`s6_transact.RULE_CODES` 单源，README 闸门表双向锁 | `vrp/s6_transact.py`；test_docs_drift Face B |
| P 码 | 病理码（render 层人话图例），`render.PATHOLOGY_LEGEND` 单源 | `vrp/render.py`；`vrp explain` 三族码之一 |
| evidence chain | 证据链：dossier 内嵌 evidence（runtime_evidence + last_verdicts + discrepancy_ledger）——AI 生产方每轮必读的上轮失败上下文 | `vrp/s5_dossier.py`；《契约/事务契约》T3 行 |
| OWNED_TABLES | ETL 自有表清单——重灌 facts.db 时被快照/回灌存续的表集合（用户裁决/AI 工件不随重灌丢失） | `vrp/etl.py`（`OWNED_TABLES` 常量）；test_etl_preserve |
| drift_history | drift 复验历史事件流（A3 前置）——每次 `_revalidate_ingested` 落一行（复验统计快照+git_head 批次锚，etl 全量/增量/curate/curate_merge/MCP 五类时刻）；跨 etl 经 OWNED_TABLES 存续，外推本体留触发池 | `vrp/curate.py`（`drift_history` 读出/`_record_drift_history`）；`vrp drift --history N`；test_drift_history |
| test_coverage | 测试覆盖对账导入面（B4）：外部 coverage.py JSON 报告 → 独立表（file_path 键 OWNED 存续，per-label 整批替换）+ test_linkage 静态关联 vs 实际行覆盖五桶对账收据（body 区间 def 行排除——import 执行非测试执行） | `test_coverage` 表；`vrp/testcov.py`；`vrp testcov` |
| fid_ledger | 符号账本：fid 的存续/重定位记录，漂移恢复的记账面 | `fid_ledger` 表；`vrp/curate.py` |
| discrepancy_ledger | 差异登记簿：xcheck/S2 与 CBM 对账差异留痕 | `discrepancy_ledger` 表 |
| co_changes | co-change 隐式耦合边（git 历史挖掘），R2 设计稿落地表 | `co_changes` 表；《设计稿-R2-co-change隐式耦合边.md》 |
| baseline（S4） | 基线快照+变更预筛：freeze 语义 | `baseline` 表；`vrp/s4_freeze.py` |

## 二、管道阶段（S0→S8）

`vrp run` 主链（README 管道节权威）：**S0 precheck → S1 CBM 索引 → S1.5 ETL(facts.db) → xcheck 交叉验证**，下游八出口：

| 阶段 | 定义 | 详处 |
|---|---|---|
| S0 precheck | 预检（文件面/忽略目录剪枝），assess 评估同族只读面 | `vrp/s0_precheck.py`；`vrp/assess.py` |
| S1 | CBM（codebase-memory）索引，符号/边来源；缓存库 store_meta 章（db_uid）溯源 | `vrp/s1_cbm.py`；`vrp/gc.py`（缓存治理） |
| S1.5 ETL | CBM→facts.db 五级证据路由 | `vrp/etl.py` |
| xcheck | code-atlas 四元组交叉验证（CBM 对账），agreement_pct 汇总 | `vrp/xcheck.py` |
| S2 病理 | ast-grep 12 规则（9 基础+3 危险 API 族 P5）+死代码（P4）+克隆（jscpd）+变异三引擎 | `vrp/s2_*.py`；`vrp/rules/s2/` |
| S3 校验 | 锚定/覆盖率/证据闸门（AI claim 核对面） | `vrp/s3.py` |
| S4 freeze | 基线快照+变更预筛 | `vrp/s4_freeze.py` |
| S5 dossier | 五视图装配+repomap+任务型预算（AI 消费面） | `vrp/s5_dossier.py` |
| S6 transact | 重构事务（G0-G6 闸门+git 原子提交/回滚），T1-T4 档位 | `vrp/s6_transact.py` |
| S7 enforce | 架构执法：curate 入库 structure 按语言路由（21 引擎），违例→P9 findings | `vrp/s7_enforce*.py`；`vrp/assess.py`（引擎后缀单源） |
| S8 report | archify 静态报告+SARIF/Mermaid 降级链 | `vrp/render.py`；`vrp/render_chain.py` |

## 三、梳理与漂移（drift/remap/curate）

| 术语 | 定义 | 详处 |
|---|---|---|
| curate | AI 梳理工件（slices/structure）入库：全量重验，提议不旁路 | `vrp/curate.py`；《契约/梳理工件契约.md》 |
| slices / structure | 业务切片与结构（层 DAG/模块归属）工件，AI 生产方产出 | `slices`/`structure` 表；契约文档 |
| drift | 漂移：代码变更后梳理工件与 facts 的失配（fresh/missing_fids/missing_edges 逐条） | `vrp/curate.py::drift_report`（统一漂移查询面 F3） |
| remap | 漂移重定位：唯一 qualified_name 候选一键写回 `.vrp/ai/*.json`（F1 产品化）；多候选/无候选诚实留 unresolved | `vrp/curate.py::apply_remap_to_ai`；CLI `vrp remap` |
| refresh | 漂移恢复一键环：S0→S1→ETL→xcheck→drift→remap→curate→report | CLI `vrp refresh` |
| F1/F2/F3 | 漂移恢复三面：F1 remap 写回 / F2 写路径钩子 / F3 统一漂移查询 | `vrp/curate.py` docstring |

## 四、工程与纪律面

| 术语 | 定义 | 详处 |
|---|---|---|
| 三红线 | 静态产物/无服务端/无前端构建链——报告纯本地静态文件 | 《产品目标.md》；README |
| 复现性红线 | 同 facts.db → 字节级同产物（确定性渲染） | test 套件确定性双跑族 |
| deep / 快测 | 测试双层：`-m deep`（真实工具外链 E2E）vs 默认快速层 | README 测试节；`.githooks/pre-push` |
| SECRETS.local.md | 掩蔽清单（渲染层密钥命中落盘，gitignored 本地文件） | `vrp/render.py::_write_secrets_manifest` |
| 密钥检测器单源 | mask_source_lines（七型 token/URL 凭据/私钥块）——渲染掩蔽与 `vrp secrets` CLI 共用零复制 | `vrp/render_chain.py`；`vrp/secrets.py` |
| 量词上界纪律 | 正则量词必须有界（scheme `{0,15}`/jwt `{…,4096}`）——无界量词×长行=O(n²) 假挂 | `vrp/render_chain.py`（#176 注记）；test_secrets_mask 长行回归锁 |
| smoke | 跨仓冒烟：verify/ 全仓 S0→S1→S1.5→xcheck 一键验证 | `vrp/smoke.py` |
| flake 登记表 | 复发追踪：首观测记 prose 不进表，复发进表 count+1，count>=2 抖王名单 | `vrp/flakes.py` |
| 活文档防漂移 | #153 纪律：文档计数/清单实时核码生成，不信摘要快照 | `vrp/doclint.py`；test_docs_drift 五 Face |
| CBM 缓存 GC | 三段式缓存治理（审计/dry-run/--yes），repo-missing 唯一自动候选 | `vrp/gc.py` |

## 五、反链

- 概念的理论出处映射：**《理论出处.md》**（W21 姊妹篇）
- 轮子/工具致谢全清单：《致谢与轮子来源.md》（Z28）
- 事务/梳理工件生产指南：《契约/事务契约.md》《契约/梳理工件契约.md》（AI 生产方必读）
- 接手即读：《交接文档.md》（环境/E2E 基准数字/代码地图）
