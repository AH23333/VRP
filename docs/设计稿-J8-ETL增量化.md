# 设计稿：J8 ETL 增量化

> 状态：**待批准**。对应构想v11-J8（硬核，最高痛点×最高成本，"需设计稿→批准→实施"约定本件即设计稿）。
> 调研时点 2026-11-19；轮子调研结论沿用构想v11（CBM 缓存层可平移 rebuild_reason 溯源思想；`git ls-files -m` 增量检测参照；无现成"SQLite 图增量更新"轮子——**造**）。
> 构想原文风险定级照抄："**风险最高**：fid 身份/ledger/remap 全依赖 ETL 语义，增量路径破坏身份连续性=灾难"——本稿以等价判据（§2）与不变式测试（§7）正面回应。

## 1. 现状与增量（诚实盘点）

`run_etl` 现行全量流程（etl.py:74）：OWNED 表全量快照 → tmp 库全新建 schema → OWNED 回灌 → **全 CBM nodes 重读+全符号重铸 fid+全边重路由**（含 usage_hints/history_coupling/test_linkage/findings similar_to 级联面）→ coverage/file_hashes 复制 → 噪音检测 → curate F2 revalidate → 原子 `os.replace`（H2 崩溃安全）。

已有底座比构想v11 写作时所以为的更厚：

- **`_ContentHasher` per-run 文件行缓存**（etl.py:31）：同文件多符号只读一次盘——每 etl 每文件恰一次 I/O 已是地板；
- **`file_hashes` 表每轮从 CBM 全量复制**（etl.py:474）：全文件 sha256 现成落库——**变更检测的原料已在**，缺的只是跨轮比对；
- **fid_ledger OWNED 存续**（F1 身份记忆）+ **meta 溯源面**（cbm_db_uid/cbm_mutation_gen/cbm_version，#144）——CBM 侧漂移信号已落档；
- **freshness 闸门**（check_facts_freshness）+ **M1 replaced 级联** + **2026-11-03 事务边界裁定**——级联与事务语义有先例可依。

但四件局限构成立项理由：

1. **无文件级跳过面**：未变文件也要重读 CBM nodes、重铸 fid、重插全部行——大仓（tree-sitter-analyzer 量级）每轮 dogfood 全量重提取，是"大仓慢的根因"（构想v11 原判定）；
2. **J10 基线未落地**（[记实]：无性能回归 harness）——本稿所有成本论断以代码结构推断，**精确 phase 数据缺位是诚实现状**，D1 将最小基线并入实施批先行；
3. **OWNED 快照/回灌每轮全表序列化**：增量路径下 OWNED 表本就原地不动，快照回灌纯浪费；
4. **tmp 库全新建+全量重插**：sqlite 页布局每轮重排，增量路径可保留既有页局部性（次要收益，不作承诺）。

**J8 增量**=文件粒度跳过：工作树内容未变的文件，其 symbols/edges/级联行**原样携带**（零 CBM 重读、零重铸、零重插）；已变/新增文件全量提取；删除文件级联清行。fid 身份连续性由检测面的不变式保证（§2）。

## 2. 增量窗口与变更检测（fid 稳定性正面回应）

### 2.1 检测面：自持久工作树快照

**新表 `etl_file_state`**（schema 追加，CREATE TABLE IF NOT EXISTS 自愈迁移）：

```sql
CREATE TABLE IF NOT EXISTS etl_file_state (
    file_path TEXT PRIMARY KEY,
    wt_hash TEXT NOT NULL
);
```

- **`wt_hash` = 工作树全文件 sha256 @ etl 时点**——不是 CBM `file_hashes` 的转存。理由：CBM 索引可能滞后工作树（index 后又改文件未 reindex），信任 CBM 哈希会开"增量≠全量"语义缺口——全量路径对这种文件会以**当前工作树内容**算 definition_hash（etl.py:54 语义），增量携带旧行则会错过。自持久快照按检测时点实读工作树，**按构造精确**；
- 每文件读一次+sha256（与现行全量 `_ContentHasher` 每文件一次 I/O 同地板，不新增量级成本）；
- 无时间戳列——复现性红线（fid_ledger first_seen_at 是状态表先例，本表连状态时间戳都不需要）。

### 2.2 分类与不变式

检测将文件分四类：**unchanged**（wt_hash 命中存档）/ **changed**（命中路径但哈希异）/ **new**（无档）/ **deleted**（有档但 CBM 当前文件集与工作树均已无）。

**核心不变式（fid 身份连续性的全部依据）**：

> 文件字节不变 ⇒ `definition_hash` 不变（同内容同行区间）⇒ `cast_fid` 不变（project|qn|content_hash 确定性拼接）⇒ 该文件全部 fid 不变 ⇒ 行携带与全量重铸**逐列相等**。

逆否：fid 轮换只发生在 changed/new 文件内——级联失效范围与 M1 replaced 语义同构（边+边族表，2026-11-03 裁定范围），不外溢。

### 2.3 等价判据（红线澄清，需用户确认——D6）

- **同输入下增量 etl vs 全量 etl ⇒ 逐表行集相等**（每表 SELECT 全行排序后对拍断言）；
- 红线"同 facts.db → 字节级同产物"按其本义**绑定下游产物**（dossier/report/SARIF 对给定 facts.db 字节级可复现），**不要求 facts.db 自身二进制复现**——sqlite 行序/页布局/rowid 分配天然漂移，全量路径自己也不满足字节自复现（etl_at 时间戳、行插入序）。此澄清不放松任何既有红线，只把它从"facts.db 字节复现"的误读里解出来。

### 2.4 CBM 侧漂移安全网

增量跳过 CBM nodes 读取的前提是"同内容同解析器 ⇒ 同节点面"。守卫：

- **cbm_version 漂移**（meta 存档 vs 当前 `s1_cbm.cbm_version()`）⇒ **强制全量**——解析器升级可能改变同内容文件的节点面，携带旧行会漏新节点；
- cbm_db_uid 漂移（换了 CBM 库）⇒ 强制全量；
- cbm_mutation_gen 仅记档不设闸——reindex no-op 也会 bump 代数，设闸会把增量打回全量（诚实权衡：mutation_gen 变化+wt_hash 全命中时节点面仍确定，不需闸）。

## 3. 级联面与交叉边

### 3.1 表级规则（ETL 写入面精确枚举）

| 表 | unchanged 文件 | changed/new 文件 | deleted 文件 |
|---|---|---|---|
| symbols | 行携带 | 重读 CBM nodes+重铸 fid+重插 | 删行 |
| edges / usage_hints / history_coupling / test_linkage / findings(similar_to) | 两端均 untouched ⇒ 行携带 | 任一端触碰 ⇒ 重算（M1 replaced 级联同构） | 删行 |
| coverage_gaps | — | 全量重算（聚合面，廉价） | — |
| file_hashes | — | 全量从 CBM 复制（单表读，廉价） | — |
| fid_ledger（OWNED） | 零动 | 新 fid `INSERT OR IGNORE`（F1 语义不变） | 零动 |
| etl_file_state | 零动 | upsert 新哈希 | 删行 |
| meta | — | 照写（stats 增 mode/文件四分类计数） | — |
| 其余 OWNED 表（slices/structure/annotations/…） | **原地零动**（无快照/回灌） | 同左 | 同左 |

### 3.2 交叉边解析（changed→unchanged 边）

CBM edge 端点落在 untouched 文件时无新鲜 node→fid 映射（增量路径没读那些 nodes）。解析链：CBM node → (project, qualified_name) → **旧 symbols 表查 fid**（(project, qn) UNIQUE 锚，M1 REPLACE 语义下幸存者即在库行）。该 fid 与全量路径重铸结果逐列相等（§2.2 不变式）。

**拒绝持久化 node_id→fid 映射表**：CBM reindex 可能重排内部 id，映射跨轮脆弱——旧表 qn 锚是既有语义，零新假设。

### 3.3 F2 revalidate 照跑

增量合并完成后对全库跑 `curate_mod._revalidate_ingested`（SQL 校验，廉价）——fid 轮换时刻（etl）的 curate 漂移标 stale 语义（F2）在增量路径下**逐字保持**，三条写路径（etl/run/transact G1 promote）收敛不变。

## 4. 路径选择与崩溃安全

- **默认自动**：etl 首跑无档 / cbm 漂移 / 增量内部异常 ⇒ 自动落全量（增量是优化，**永不改语义**）；`--full` 旗标强制全量逃生舱；
- **崩溃安全（D3）**：`shutil.copy2` facts.db → 同目录 tmp 副本 → 副本上事务内增量 patch → 校验 → `os.replace`——保 H2"中途失败旧库完好"语义（原地事务虽也原子，但换库语义与全量路径不一致，统一 tmp-swap 降低认知面）；副本成本 ~库文件一次复制，远小于全量 CBM 重提取；
- freshness 闸门照旧；`run`（etl+s2+verdict）走同一 run_etl 入口自动受益。

## 5. v1 范围收窄

- **v1 只做**：etl 侧文件粒度增量（上述全部）+ CLI `--full`；
- **不在本件**：S2 病理引擎并行=J9；性能回归完整 harness=J10（本件只落最小基线，D1）；CBM reindex 自身增量=CBM 缓存层职责（外部工具，不碰）；watch=I7；render/dossier 消费面零动（行集等价 ⇒ 下游产物不变）。

## 6. 红线核对

| 红线 | 核对 |
|---|---|
| 静态产物 | ✓ 读 CBM+工作树，写 facts.db；无网络无服务端 |
| 零第三方运行时依赖 | ✓ stdlib（hashlib/sqlite3/shutil/subprocess） |
| 同 facts.db→字节级同产物 | ✓ 未放松；本件新增义务=同输入下增量与全量**行集等价**（§2.3，对拍测试钉住） |
| 确定性 | ✓ sha256 检测确定性；etl_file_state 无时间戳；行携带保字节 |

## 7. 成本与测试面

- **实施**：facts_schema.sql 追加 etl_file_state + 新模块 `vrp/etl_incremental.py`（~300 行：classify/patch/carry/verify）+ etl.py 薄接线（路径选择+stats）+ CLI `--full`；
- **快速层测试 ~20 件**（合成仓 fixture：tmp 建仓+脚本化 CBM 假库或 monkeypatch src）：
  - **等价对拍（本件最重要）**：小仓改动一文件 → 全量 vs 增量 → 逐表排序行集相等断言；
  - 四分类各路径：unchanged 携带零重插 / changed 重铸 fid 轮换+ledger 增行 / new 提取 / deleted 级联清边清行；
  - fid 不变式：未变文件 fid 集零轮换；
  - 交叉边：changed→unchanged 边重解析锚定旧 fid，行值与全量相等；
  - cbm_version 漂移→强制全量；`--full` 逃生舱；首跑无档→全量建档；
  - 崩溃安全：patch 中途抛异常旧库完好（tmp 副本清理）；
  - OWNED 存续：增量路径 slices/structure/annotations 行不变；F2 revalidate 照跑（漂移工件标 stale）；
  - freshness 照旧；确定性：同输入双跑行集相等；
  - etl_file_state upsert/清理；stats 字段面；CLI parser 旗标。
- **深测**：lusiem/tree-sitter-analyzer 真仓冒烟——改动单文件 before/after 计时**记档交接不钉值**（环境方差，#153 同纪律）。

## 8. 决策点（需批准）

| # | 决策 | 推荐 | 备选与理由 |
|---|---|---|---|
| D1 | J10 最小基线并入实施批（phase 计时进 stats+大仓记档） | **并入** | 单开 J10 批拖延主线；完整 harness（JSONL 落档+批次对账）另立项 |
| D2 | 检测源=自持久 etl_file_state 工作树快照 | **自持久** | CBM file_hashes 信任有"索引滞后工作树"语义缺口（§2.1）；git ls-files -m 依赖 VCS 且漏内容回改（A→B→A） |
| D3 | 崩溃安全=库副本+patch+os.replace | **副本换库** | 原地事务也原子，但与全量路径换库语义分叉；统一 tmp-swap 认知面小 |
| D4 | cbm_version/db_uid 漂移强制全量 | **采纳** | 解析器升级安全网；保守取向（增量是优化，漏新节点是灾难面） |
| D5 | 增量默认自动+`--full` 逃生舱 | **默认自动** | 独立 --incremental 旗标增采用摩擦且无语义收益；异常自动落全量兜底 |
| D6 | 等价判据=行集对拍（红线澄清 §2.3） | **需确认** | 唯一需用户点头的语义澄清：红线绑定下游产物不绑定 facts.db 二进制 |

## 9. 实施批次（批准后）

- #18x：schema+`vrp/etl_incremental.py`+etl.py 接线+CLI `--full`+测试 ~20 件+README/交接（单 commit+push，预期快测 1814→~1834）；
- 批内先落 D1 基线（phase 计时）再实施增量，before/after 记档交接；
- 冒烟：verify 双仓（tree-sitter-analyzer 为大仓主证；只读校验+临时副本计时，不污染 dogfood 基线库）。
