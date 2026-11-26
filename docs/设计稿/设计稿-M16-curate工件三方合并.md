# 设计稿：M16 curate 工件三方合并

> 状态：**待批准**。对应构想v11-M16（硬核，"需设计稿：祖先快照存哪/冲突呈现形态"两个点名问题本稿正面回答）。
> 调研时点 2026-11-19；轮子调研结论沿用构想v11（git merge 三方语义权威 base/ours/theirs；OT 冲突分类参照：非冲突=自动合，冲突=标记留人工）。
> 场景真实（构想原文）："多会话并行梳理（本项目自身）"——两代理各自梳理同一仓，slices/structure 现状是 ingest 静默覆盖（curate.py:_ingest_slices 对同 slice_id DELETE+INSERT），代理间语义互相顶掉无痕，构成立项理由。

## 1. 现状与增量（诚实盘点）

已有底座：

- **ingest 面**：`_ingest_slices`（slice_id 主键 DELETE+INSERT，status='active'）/`_ingest_structure`（单行 DELETE+INSERT）——覆盖式，无合并语义；
- **校验面**：`_validate_slice_array`/`_validate_structure`（fid/边/层引用存在性校验）+ F2 `_revalidate_ingested`（etl 时复验标 stale）——合并产物的质量闸现成；
- **留痕底座**：`decisions(id, kind, target_id, decision, note, created_at)` 表 + `reject()` 先例（否决留痕形态）——冲突裁决落点现成；
- **身份键对齐先例**：#182 dossier-diff 列表身份键（id/fid/name/symbol/…偏好序）——structure 字段级对齐可依。

缺口：覆盖式 ingest 无共同祖先概念（M16 点名问题一）、冲突静默丢（M16 点名问题二）、两份产物对同一 slice_id 写出不同叙事时无裁决面。

**M16 增量**=`vrp curate-merge` 三方合并命令：ours=当前库 active 面，theirs=外来产物（.vrp/ai/ 目录或文件，复用 curate 输入面），base=自动维护的覆盖前快照表——非冲突自动合，冲突留痕保 ours 等人工。

## 2. 祖先快照：base 存哪（点名问题一）

**新表 `curate_base`（OWNED——跨 etl 存续，与 slices/structure 同回灌语义）**：

```sql
CREATE TABLE IF NOT EXISTS curate_base (
    key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
);
```

- **写入时机**：ingest 覆盖**前**——`_ingest_slices` 的 DELETE 前，把将被覆盖的旧行 payload 搬入 base（key=`slice:<slice_id>`）；`_ingest_structure` 的 DELETE 前同（key=`structure`）。**每次覆盖刷新**（D2）；
- **base[k] 语义 =「该 key 最近一次被 ingest 覆盖前的样子」**=与最近一次入库梳理的对比基线。非严格 git LCA（诚实记明）：theirs 出发点比 base 更旧时会把"theirs 未动"误判为"theirs 动了"——**误报只偏保守方向**（多变冲突变人工裁决，永不静默丢变更），v1 接受，记入局限；
- 无 base 行 ⇒ base=∅（该 key 从未被覆盖：库里现值即入库首值或从无值）；
- OWNED 追加一行（co_changes 先例）——etl 重建回灌存续；
- 拒绝文件快照备选（.vrp/curate-base.json）：与库事务脱同步、跨 etl 不随库走——表方案胜在事务一致与同生命周期（D1）。

## 3. 合并算法

### 3.1 slices（按 slice_id 三方）

| 分支 | 条件 | 动作 |
|---|---|---|
| theirs-only | base 无 ours 无，theirs 有 | **采纳**：INSERT active（复用校验面） |
| ours-only | theirs 未触碰 | **不动** |
| 双有·ours 未动 | ours == base | **采 theirs**（覆盖） |
| 双有·theirs 未动 | theirs == base | **不动** |
| 双有·双方同值 | ours == theirs | **不动** |
| 双有·双方都动且异 | 其余 | **冲突**：decisions 留痕，库保 ours 不写 |

- **相等判定=规范化 JSON 比较**（`json.loads` 后 dict 递归相等，D5）——键序/空白差异不构成冲突；
- 冲突留痕行：`kind='curate_merge'`，`decision='conflict'`，`target_id=slice_id`，`note=双方 name/summary/step 数摘要`——decisions 表零 schema 改动（reject 先例形态）。

### 3.2 structure（单行按字段三方，构想原意）

payload 顶层字段分组处理：

- **modules 列表**：按 `module_id` 身份键对齐（#182 先例）——单侧新增采纳（并集自动合）；同 module_id 双方都动且异 → 冲突；单侧未动采对侧；
- **layers 列表**：按 `name` 身份键同上；
- **其余顶层字段**：整字段三方（ours==base 采 theirs / theirs==base 保 ours / 都动且异冲突 / 同值不动）。

**校验兜底**：字段级拼合结果整体过 `_validate_structure`——不过（如 module 引用不存在 layer、fid 失锚）则**整体降级冲突**回人工，不落半成品。

### 3.3 原子性与复验

- **单事务**：全部采纳+留痕一起 commit，任一环节失败整体回滚（合并前先全量校验 theirs 载荷，坏载荷零写入即拒）；
- 合并后 F2 `_revalidate_ingested` 照跑——fid 漂移标 stale 语义（F2）在合并路径下逐字保持；
- freshness 闸门照旧（走 `_open_facts_rw` 写路径）。

## 4. 命令面

- `vrp curate-merge <repo> --theirs <dir-or-file> [--json]`；
- theirs 输入复用 curate 产物面（.vrp/ai/ 目录或单文件，slices/structure 各自识别）；
- 输出四类清单+计数：`adopted`（theirs-only/采 theirs）/ `kept`（ours-only/不动）/ `conflict`（含 decisions 行引用与双方摘要）/ `unchanged`（同值）；structure 面单独一段同四类；
- 冲突的 key 库内保持 ours——人工裁决路径：改 theirs 产物重跑合并 / `vrp curate reject` 否决 ours 后重跑 / 放弃 theirs（decisions 已留痕可追溯）；
- MCP 工具族不动（TOOL_NAMES 30 件不变；MCP curate 面另批评估）。

## 5. v1 范围收窄

- **v1 只做**：curate_base 表+ingest 快照挂钩+`vrp curate-merge` 命令（slices+structure 全分支）；
- **不在本件**：decisions 表自身不合并（留痕表本库各自记）；dossier_views/baseline/annotations 等其它 OWNED 工件不入合并面（dossier 是派生快照可重算）；交互式合并 UI 不做（终端工具形态）；.vrp/ai/ 产物文件不自动改写；J8 增量 etl 与本件正交（base 表 OWNED 存续对两条路径一致）。

## 6. 红线核对

| 红线 | 核对 |
|---|---|
| 静态产物 | ✓ 读产物文件+facts.db，写 facts.db 表；无网络无服务端 |
| 零第三方运行时依赖 | ✓ stdlib（json/sqlite3/difflib 可选用于摘要） |
| 同 facts.db→字节级同产物 | ✓ 不触产物链；curate_base 是库内状态表（fid_ledger 时间戳先例——created_at 类列不进派生产物） |
| 确定性 | ✓ 同输入双跑合并结果相等（排序键明确：slice_id/module_id/name 字典序） |

## 7. 成本与测试面

- **实施**：facts_schema.sql 追加 curate_base（+OWNED_TABLES 一行）+ `vrp/curate_merge.py`（~280 行：load_theirs/three_way_slices/three_way_structure/persist/render）+ curate.py ingest 挂快照（两处 DELETE 前）+ CLI 接线；
- **快速层测试 ~20 件**（合成 fixture：tmp 库+伪造 payload）：
  - slices 六分支逐支锚定（表 §3.1 全行覆盖）；
  - 规范化相等：键序/空白差异 → 不动非冲突；
  - structure 字段级：modules 并集自动合 / layers by name / 同 module_id 双动冲突 / 校验失败整体降级冲突；
  - base 生命周期：ingest 覆盖前快照落表（slice+structure 两面）/ 未覆盖 key 无行=∅ / etl OWNED 存续（重跑 etl 后 base 行不变）；
  - 原子性：theirs 坏载荷零写入；中途异常全回滚；
  - 冲突留痕 decisions 行形状（kind/decision/target_id/note）+重复合并幂等（同冲突重跑不重复留痕）；
  - F2 合并后照跑（stale 语义）；freshness 拒写；确定性双跑相等；CLI --json 形状/parser 旗标。
- **深测**：lusiem 真仓双产物合并冒烟（人工制造分叉：改一份 .vrp/ai/ 产物后合并，记档不钉值）。

## 8. 决策点（需批准）

| # | 决策 | 推荐 | 备选与理由 |
|---|---|---|---|
| D1 | base 载体=curate_base 表（OWNED） | **表** | 文件快照与库事务脱同步、不随 etl 回灌走；表与库同生命周期事务一致 |
| D2 | base 语义=最近一次覆盖前快照（每次覆盖刷新） | **最近覆盖前** | 首入库面过旧误报面更宽；非严格 LCA 的误报只偏保守（记入 §2 局限） |
| D3 | 冲突呈现=decisions 留痕+CLI 清单，冲突 key 保 ours 不写 | **采纳** | 交互式 UI 不做（终端形态）；保 ours 保住代理间语义零静默丢 |
| D4 | structure 字段级三方+合并后全量校验兜底 | **采纳** | 构想原意（fid 层面 rebase 依赖 remap 环不在 v1）；校验兜底防拼合违 curated 语义 |
| D5 | 相等判定=规范化 JSON 比较 | **规范化** | 字符串精确比较会把键序当冲突（假阳性面大） |
| D6 | v1 合并面只 slices+structure | **采纳** | decisions/dossier 各自性质（留痕/派生）不属合并面；防单批膨胀 |

## 9. 实施批次（批准后）

- #18x：schema（curate_base+OWNED 一行）+curate.py ingest 挂快照+`vrp/curate_merge.py`+CLI+测试 ~20 件+README/交接（单 commit+push，预期快测 1814→~1834）；
- 与 J8 实施顺序建议：**M16 先行**（改动面小、无等价判据争议）；J8 批内 base 表 OWNED 存续对两路径一致性有测试互锁；
- 冒烟：verify 仓只读构造分叉合并（临时副本，不污染 dogfood 基线）。
