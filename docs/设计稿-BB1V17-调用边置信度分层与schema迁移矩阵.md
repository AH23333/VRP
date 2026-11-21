# 设计稿：BB1 调用边置信度分层 + V17 schema 迁移矩阵（合批，V18 政策 rider）

> 批准记录：用户睡前全权授权（批⑤⑥同授权模式延续）——设计稿自批自决，决策点 D1-D8 随批定稿；实施与设计同批（单 commit #205）。

## 1. 现状与增量（诚实盘点）

- **edges 表现状**（facts_schema.sql:25-37）：confidence REAL/strategy/evidence 列已在（CBM 透传面）——BB1 增量=三值分层 label 列（EXTRACTED/INFERRED/AMBIGUOUS）+消费面接线，**非**从零建置信度。
- **三仓真值探针**（2026-11-21，probe_bb1_edges.py，本批起步资本）：
  - 自仓：CALLS heuristic 2 策略（unique_name 0.38-0.75 / suffix_match 0.28）+lsp 9 策略（0.85-0.97）+structural 3 策略（0.90-0.95）；CALL_REFERENCE n=33 无 conf。
  - lusiem（TS 仓）：lsp_ts_* 0.95 族+heuristic unique_name/suffix_match；CALL_REFERENCE n=1 无 conf。
  - tsa（Python 大仓）：**conf 连续带 0.01-0.55（suffix_match 连续评分非离散档）**+8 条 CALLS 无 strategy 无 conf+CALL_REFERENCE n=1268 全无 conf；32 个 distinct confidence 值。
- **关键判据事实**：evidence 维度不够分层（heuristic 下既有 0.95 的 cs_method_typed 也有 0.01 的 suffix_match）；strategy 值空间开放（lsp_ts_*/lsp_kt_*/cs_*/php_* 长尾持续增长）——**label 判据必须 confidence 阈值为主**，evidence/strategy 白名单路线否决。
- **断层干净**：三仓 conf 全集在 0.75（unique_name 顶）/0.85（cs_ctor_synthetic/import_map_suffix 底）之间有空洞；0.65（lsp_kt_any）/0.70（php_function_global_fallback）为低带边界。
- **ensure_schema 现状**（etl.py:106-108）：executescript 一行式；历史 schema 变更全是加新表（IF NOT EXISTS 自动补）——**从未加过列，BB1 是第一次列迁移**，ALTER TABLE ADD COLUMN 不幂等（重复报 duplicate column）→V17 机制顺势点火（构想v12 V17 触发条件"下次 schema 变更时"命中）。
- **已确认不做的**：不加 resolution_tier 列（CGC 9 级 tier 是其解析器内部枚举，VRP 消费 CBM 非 CGC，tier 无数据源——对构想v13 原文"resolution_tier+confidence+label"的诚实修正：confidence 已在，tier 无从透传）；不做全边类型标注（只 CALLS/CALL_REFERENCE 族——MENTIONS conf=NULL 全边标注会把它误标未知外语义，超出 BB1 原文范围）；不扩 golden edges 面（label 是 ETL 派生列，golden 扩面留未来批）。

## 2. 参照源码重读记实（file:line / 文档锚点）

- **CGC label 映射**（verify/CodeGraphContext/src/codegraphcontext/tools/indexing/resolution/calls.py）：9 级 tier conf 1.00/0.95/0.88/0.72/0.90/0.85/0.70/0.25/0.08；`_confidence_label` 三值映射：AMBIGUOUS=unresolved_external 或 tier≥8；EXTRACTED=tier 1/2/5/6；INFERRED=tier 3/4/7。**对齐物=三值词汇+单调方向**，非逐级复刻（CGC 的 label 是 tier 枚举函数非 conf 纯函数；VRP 无 tier，取 conf 单调阈值是自有语义）。
- **HEURISTIC_CALLS 独立表事故**（embedded_schema.py:84-90 注释）：表未声明致 Kùzu binder exception 全面炸 dead-code——"部分覆盖比没有更糟"教训→本批 label 全 CALLS 族覆盖不漏 NULL。
- **v_dead_code 视图**（facts_schema.sql:325-366）：单条 NOT EXISTS 覆盖 ('CALLS','CALL_REFERENCE','MENTIONS','SPAWNS') 四型（364 行）+usage_hints/test_linkage 两独立 NOT EXISTS——BB1 行为变更点=四型 NOT EXISTS 拆分。
- **ensure_schema 调用面**（cli.py:112/493/551、auto_curate.py:313/339、mcp_server.py:112）：六处入口共享一函数——迁移链收口单点。
- **构想v12 V17 原文**（114-118 行）：学 Django migrations/alembic 迁移链思想+PRAGMA user_version 原生机制；旧版库 fixture×迁移路径全覆盖测试。
- **构想v12 V18 原文**（120-124 行）：表结构对外版本承诺政策文档；v0.x 宜"破坏性变更随 minor 走"宽松政策；反向风险=公开承诺变更成本上升。
- **构想v13 BB1 原文**（32-36 行）：AMBIGUOUS 边不参与"必死"判定只参与提示；下游按置信度过滤。
- **evidence.py call_evidence 契约**：confidence 参数不参与 evidence 分级（v6 §12.3）——label 函数与其正交并列，互不侵入。

## 3. label 语义与判据设计（BB1 核心）

- **列名 `resolution_label TEXT`**（edges 表新列；不用裸 `label`——symbols.label 已占用词汇；不用 CGC 的 confidence_label——resolution 语义更准且对齐 BB1 原文词汇）。
- **三值+NULL 语义**：
  - `EXTRACTED`：confidence ≥ 0.85——编译/结构级高置信（lsp 全族、structural 全族、typed heuristic 高分带）。
  - `INFERRED`：0.70 ≤ confidence < 0.85——中等推断（php_function_global_fallback 0.70、lsp_kt_any 0.65 归此线下方 AMBIGUOUS？否——0.65 < 0.70 归 AMBIGUOUS，记档）。
  - `AMBIGUOUS`：confidence < 0.70——低置信（unique_name 低段、suffix_match 连续低带）。
  - `NULL`：confidence IS NULL——**未知≠低置信**（CALL_REFERENCE 全族无 conf 是 CBM 未标注非低分；tsa 8 条无 conf CALLS 同理）。未知不惩罚（D4）。
- **计算函数**：`vrp/evidence.py` 新增 `resolution_label(confidence)`（纯函数，None 透传 None；边界 ≥ 含等号——0.85 EXTRACTED、0.70 INFERRED）。
- **标注范围**：ETL `_etl_edges` 写 CALLS/CALL_REFERENCE 时计算写入；其余边类型一律 NULL（不适用）。CALL_REFERENCE conf=None→label NULL（诚实）。
- **旧数据不回填**：迁移只加列；label 到 ETL 重跑（replaced 级联删+重插）自然补——旧行 NULL=未知，消费面 NULL 保留原行为，零回归。

## 4. v_dead_code 行为变更（唯一动行为点）

- **拆分**（facts_schema.sql:364 单 NOT EXISTS → 两条）：
  - `NOT EXISTS (CALLS/CALL_REFERENCE 且 (resolution_label IS NULL OR resolution_label != 'AMBIGUOUS'))`——SQL 三值逻辑坑显式防：`label != 'AMBIGUOUS'` 对 NULL 行返回 UNKNOWN 静默排除，必须写 `IS NULL OR`。
  - `NOT EXISTS (MENTIONS/SPAWNS)`——维持无过滤（非 BB1 范围）。
- **行为语义**：唯一调用方是 AMBIGUOUS CALLS 的函数入死代码候选（unique_name 0.38 假救活防线——BB1 原文意图）；NULL（CALL_REFERENCE/tsa 无 conf CALLS/旧库未重跑）维持救活——**tsa 1268 条 CALL_REFERENCE 行为零变化**。
- **提示面**（"AMBIGUOUS 只参与提示"落实）：s2_deadcode.classify 死因列表增"仅有低置信调用方"计数信号（ambiguous_callers）——低置信边不再静默改变判定，而是显式提示。

## 5. V17 迁移矩阵机制

- **PRAGMA user_version**：SCHEMA_VERSION=2（v1 为理论历史基线槽——机制与首次列变更同批引入，从未有库真实处于 1；0=pre-tracking 任意历史形态）。
- **facts_schema.sql**：edges 表定义加 resolution_label TEXT 列；文件尾 `PRAGMA user_version = 2;`（新库 executescript 直接到位）。
- **迁移链**（etl.py，学 alembic 线性链极简版）：
  ```python
  MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
      (2, _migrate_v2_add_resolution_label),
  ]
  def ensure_schema(con):
      current = con.execute("PRAGMA user_version").fetchone()[0]
      for target, fn in MIGRATIONS:
          if target > current:
              fn(con)   # 幂等：PRAGMA table_info 探测列缺否，缺才 ALTER
      con.executescript(SCHEMA_PATH.read_text(...))  # 视图重建需列已在
      con.commit()
  ```
  顺序硬约束：**ALTER 先于 executescript**——v_dead_code 新视图引用 resolution_label，旧库列缺时 executescript 中 CREATE VIEW 报错。
- **矩阵测试面**（V17 核心=旧版库 fixture×迁移路径断言全覆盖）：fixture 生成器按版本建库灌种子数据→跑 ensure_schema→断言列存在/数据保留/version=2/视图可查。路径：新库直建=v2；v0 库（现 schema 无 label 无 version）→探测 ALTER→v2；v2 库重跑幂等（无 duplicate column）；v0 库迁移后 ETL 增量续跑不炸。

## 6. 红线核对

- 零第三方运行时依赖 ✅（纯 stdlib sqlite3/pathlib；迁移链自建无 alembic 依赖——只学思想）。
- 静态产物/无服务端/无前端构建链 ✅（schema/ETL/视图内聚变更）。
- 同 facts.db 字节级同产物 ✅（label 纯 conf 确定函数；迁移链幂等确定；PRAGMA user_version 写入确定性）。
- census 纪律 ✅（ro 探针一律 dbpath.ro_connect；迁移 rw 面参数绑定）。
- "部分覆盖比没有更糟"防线 ✅（label 全 CALLS 族覆盖含 NULL 诚实路径；未知不惩罚显式语义）。
- 旧库零回归 ✅（未重跑库 label NULL→消费面原行为；M4 freshness 硬拒拦截跨 HEAD 半迁移态）。
- 模块级状态 ✅（MIGRATIONS 常量表；无 runtime 可变态）。

## 7. 成本与测试面

- 改动面：facts_schema.sql（edges 列+视图拆分+user_version）+etl.py（ensure_schema 迁移链+MIGRATIONS+_etl_edges 标注）+evidence.py（resolution_label 函数）+s2_deadcode.py（ambiguous_callers 信号）。
- 新增测试预估：
  - `tests/test_migrations.py` ~8 件（V17 矩阵：新库 v2/v0 探测迁移/幂等双跑/数据保留/视图可查/ETL 续跑/version 探针/调用面收口）。
  - `tests/test_resolution_label.py` ~8 件（阈值边界 0.85/0.84/0.70/0.69/None 透传/ETL CALLS 高低分标注/CALL_REFERENCE NULL/其他类型 NULL）。
  - `tests/test_dead_code_label.py` ~6 件（AMBIGUOUS 不救活/NULL 维持/EXTRACTED/INFERRED 救活/MENTIONS_SPAWNS 不过滤/classify ambiguous_callers 信号）。
- **既有测试风险点**：v_dead_code 相关既有断言（G5/s2/deep）若 synthetic fixture 的 CALLS 边 conf<0.70 会判 AMBIGUOUS→死代码断言变化——实施期逐查，fixture conf 钉 ≥0.85 则零影响（多数测试灌边不带 conf=0 默认→NULL 路径不受影响是预期主力）。
- 计数预估：quick 2036+22=2058 / deep 1529 / total 3587（collect 后真值同步 README 四面+交接 Face E）。

## 8. 文档接线面

- `docs/schema版本承诺.md` 新建（V18 政策）：user_version 语义/承诺面（N-1 库可迁移）/v0.x 破坏性变更随 minor 宽松政策/迁移链工程规则（每批单迁移函数+矩阵测试）/反向风险记档（承诺变更成本上升）——README 文档节登记（Face C 守卫）。
- README：计数四面同步+migrations 无 CLI 面（ensure_schema 内聚自动触发，CLI 表不加行）。
- 构想v12 V17/V18 状态注记（已完成）；构想v13 BB1 状态注记（已完成+tier 修正记档）；优先级总表批⑦→已完成；glossary edges 条目若有列清单同步（实施期查）；交接 #205。
- golden 面不扩（记档）。

## 9. 决策点（用户授权自批，随批定稿）

- **D1 label 判据=confidence 双阈值 0.85/0.70**（纯 conf 单调，None→NULL 未知；evidence/strategy 白名单否决——真值探针：heuristic 下 0.01-0.95 全谱，白名单不可枚举）。CGC 对齐物=三值词汇+单调方向，非逐级 tier 复刻（记档）。
- **D2 列名 resolution_label**（裸 label 与 symbols.label 词汇冲突；CGC confidence_label 弃——resolution 语义对齐 BB1 原文）。
- **D3 标注范围只 CALLS/CALL_REFERENCE 族**（BB1 原文范围；MENTIONS/SPAWNS/DEFINES 等不标——全边标注会把 conf=NULL 的 MENTIONS 误入分层语义，超出立项意图）。
- **D4 NULL=未知不惩罚**（CALL_REFERENCE/tsa 8 条无 conf CALLS/旧库未重跑——维持原行为；与 AMBIGUOUS（显式低置信，不救活）严格区分）。
- **D5 v_dead_code 拆分+IS NULL OR 显式防**（唯一动行为点；tsa 1268 条 CALL_REFERENCE 行为零变化实证路径）。
- **D6 迁移链先 ALTER 后 executescript**（视图引用新列的顺序硬约束；MIGRATIONS 线性表学 alembic 思想零依赖自建）。
- **D7 SCHEMA_VERSION=2 起步**（v1 理论基线槽记档于 V18 政策文档；0=pre-tracking 探测路径）。
- **D8 V18 政策=v0.x 宽松**（破坏性变更随 minor 走；承诺面限"N-1 库可迁移"；反向风险=公开承诺变更成本上升，记档接受）。
