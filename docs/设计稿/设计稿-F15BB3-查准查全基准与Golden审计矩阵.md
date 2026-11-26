# 设计稿：F15 查准查全基准 + BB3 Golden 审计矩阵（合批）

> 批准记录：用户睡前全权授权（2026-11-21，"继续全部推进……全部由你决策开发"）——批⑤同授权模式延续；本设计稿自批自决，决策点 D1-D7 随批定稿；实施与设计同批（单 commit #204）。

## 1. 现状与增量（诚实盘点）

- **查准查全现状**：唯一间接指标=xcheck agreement（CBM vs code-atlas 双引擎对账，vrp/xcheck.py）——两引擎互证非对真值。21 个 s7 执法引擎各自的违例检出准确性只有散落在 20+ 测试文件里的单点断言（violations==N / findings_written==N），无一统一"对独立真值"的查准查全面。
- **CGC 循环完美门教训**（构想v13 BB3 立项锚）：CGC 自审"导出与自己比"给出 21/21 PASS 假象，与真实 CALLS 准确率 84.6% 冲突——**审计必须对独立真值，golden 与 indexer 双向计数（missing/spurious），手工改过的 golden 不落盘=自欺**。
- **零新增标注起步资本**：20 引擎的合成 fixture+预期违例已在 quick 测试中存在且经真工具探针验证（如 php 探针 2026-11-07、go 1.26 探针、rustc/javac/dotnet 探针）——把这些已知事实提取为 committed 真值数据 = "21 probe 仓 ground truth 零新增标注起步"。
- **已确认不做的**：不建 facts.db 新表（真值=committed JSON 文件面——独立于被审计库才能防循环门）；不做标注工具（Label Studio 轮子调研记档——标注期工具不入产品依赖，手写 JSON 起步）；不做 21 语言全量初始矩阵（D4 起步集+扩语言=纯数据零代码）；不做 CI（README 发布节红线）。

## 2. 参照源码重读记实（file:line / 文档锚点）

- **CGC 记分卡形态**（verify/CodeGraphContext/CGC_CALL_GRAPH_AUDIT_REPORT.md，参照区只读）：21 语言 per-language 记分卡+missing/spurious 双向计数——本设计稿造 VRP 原生 receipt 同构面；审计工具流失教训→审计 harness 进 `vrp/golden.py` 受测试保护。
- **tree-sitter corpus "标注即测试"形态**（构想v10 §F15 轮子记档）：标注→断言逐例——真值 JSON 与测试同 commit，真值即测试数据。
- **findings 表锚**（facts_schema.sql:106-118）：pathology/kind/source/anchor_fid/file_path/start_line/payload_json——violations 面匹配键取 (kind, file_path, start_line)+payload importer/imported（s7 写入面实证：tests/test_s7_enforce_go.py:96-107 行断言形态）。
- **symbols 表锚**（facts_schema.sql:8-20）：fid/qualified_name/label/file_path/start_line/end_line——symbols 面匹配键 (qn, label, file, start, end) 严格精确。
- **真跑性核验**：18 语言 s7 引擎 happy-path E2E 在 quick 层零工具链真跑（各 test_s7_enforce_*.py 仅失败注入用 monkeypatch——grep 57 处实证）；go/ts 外链工具（go list/depcruise）→ deep skip-if-missing（stryker/work 沙箱先例）；ETL（vrp/etl.py）消费 CBM SQLite → BB3 深度面真跑 s1_cbm+etl。
- **R-AD/R-X 守卫约束**（tests/test_payload_discipline.py）：findings payload_json 解析逐层 isinstance 守卫——真值与实际两侧 loads 全守卫。

## 3. 真值格式与 fixture 设计（标注仓结构）

**格式 `vrp-golden-1`**（单 JSON 文件，committed；两种用法同一格式）：

```json
{
  "format": "vrp-golden-1",
  "name": "php-s7",
  "language": "php",
  "provenance": "非空必填——真值来源记档（提取自哪个探针/测试，何时，谁标注）",
  "fixture": {
    "files": {"src/Store.php": "<?php\n..."},
    "meta": {"git_head": ""},
    "symbols": [["f1", "p", "Store", "Class", "Store", "src/Store.php", 3, 10, "h"]],
    "structure": {"modules": [...], "layers": [...]}
  },
  "truth": {
    "violations": [{"kind": "layer-violation", "file": "src/Store.php", "line": 3,
                     "importer": "Demo\\Store", "imported": "Demo\\Api", "pathology": "P9"}],
    "symbols": [{"qn": "Store", "label": "Class", "file": "src/Store.php", "start": 3, "end": 10}]
  }
}
```

- **fixture 节可选**（CLI 对真仓审计时缺省——`vrp golden <repo> --file truth.json` 只带 truth）；**truth 两面均可选、空数组=有效「期望零」审计面**（反 spurious 面：声明"该 source 在该仓必须零违例"）。
- **形状守卫**（整文件拒绝，traces/testcov 同哲学）：format 非字面 "vrp-golden-1"/顶层非对象/provenance 空/violation 缺 kind/file/line/symbol 缺 qn/label/file/start/end/行号非正 int（bool 排除）→ SystemExit 零审计。
- **初始真值集**（D4）：`tests/golden/s7/{java,php,ruby}.json`（quick 真跑——JVM 自由格式/岛模型词法/脚本三解析器家族）+ `tests/golden/s7/{go,ts}.json`（deep）+ `tests/golden/etl/{py,ts}.json`（deep BB3——fixture+symbols 真值，**人工从源码独立完整枚举**标注）。七件起步，扩语言=纯数据追加。

## 4. 审计面设计（双向计数+匹配键）

- **violations 面**（F15）：findings 按 truth 声明的 source（默认 "s7-enforce"）过滤 → 匹配键 (kind, file_path, start_line, importer?, imported?)（importer/imported 从 payload_json 提取，R-AD 守卫，不可解析时键位 None；truth 条目可省略两可字段）→ **多集匹配**（同键多条按 min 计 matched，truth 侧多余=missing，实际侧多余=spurious——重复违例诚实计数）。
- **symbols 面**（BB3）：**域=truth.symbols 出现的 file 集合**（有界语料完整枚举——域外符号不触碰，真仓子系统工程化）→ 域内实际 symbols vs truth **严格精确匹配** (qn, label, file, start, end)——近失（同 qn 错行号）两侧成对计数（missing+spurious），解析不精度诚实现形。
- **指标**：matched/missing/spurious 三计数 + precision=matched/(matched+spurious) + recall=matched/(matched+missing)；**分母零→null 诚实**（不造 1.0——"期望零且得零"的 receipt 是计数面非百分比面）。
- **样本**：missing/spurious 样本 cap 5/侧全排序（确定性；消费方免 join）。

## 5. receipt 形态

```json
{
  "ok": true, "file": "...", "name": "php-s7", "language": "php",
  "faces": {
    "violations": {"source": "s7-enforce", "truth": 2, "actual": 2, "matched": 2,
                    "missing": 0, "spurious": 0, "precision": 1.0, "recall": 1.0,
                    "missing_samples": [], "spurious_samples": []},
    "symbols": {"truth": 4, "actual": 4, "matched": 4, "missing": 0, "spurious": 0,
                 "precision": 1.0, "recall": 1.0, "missing_samples": [], "spurious_samples": []}
  },
  "note": "..."
}
```

- rc 恒 0（D5——审计=度量非门禁，testcov D5/pitindex 同口径；红面归测试 pin/jq）。
- 无 truth 面的 golden → ok=true faces={} 诚实空（不报错——未来面留白）。

## 6. 红线核对

- 零第三方运行时依赖 ✅（纯 stdlib json/sqlite3/pathlib；标注工具不入依赖）
- 静态产物/无服务端/无前端构建链 ✅（CLI 只读审计面+committed JSON）
- **循环完美门防线** ✅（真值=独立 committed JSON 非 facts.db 派生；symbols 面域内完整枚举防标注不足假 spurious；provenance 必填守卫）
- 复现性 ✅（匹配键/样本/计数全排序确定；无时间戳写入——纯读面 receipt 内存构造）
- R-S/R-T/R-W/R-X/R-AD/R-Z census ✅（SQL 参数绑定/loads 逐层 isinstance 守卫/零写文件/无 any-all 字面量/无 subprocess）
- 模块级状态 ✅（仅常量；test_module_scope 零风险）
- **facts.db 零动** ✅（无新表无 OWNED 变更无写面——ro_connect 只读）

## 7. 成本与测试面

- 新模块 `vrp/golden.py`（~280 行：load_golden/build_fixture/audit_findings/audit_symbols/run_audit）；cli +cmd+parser（~35 行）；零 schema 变更。
- 新增 `tests/test_golden.py` ~16 件（格式守卫 5/双向计数与指标数学 4/多集匹配/域语义/近失成对/determinism 双跑/build_fixture/CLI 接线/CLI 缺库/provenance 守卫）+ `tests/test_golden_matrix.py` 3 件（java/php/ruby quick 真跑 pin）+ `tests/deep/test_deep_golden.py` ~4 件（go/ts F15+py/ts BB3 deep 真跑 pin，skip-if-missing）。
- **pin=观测诚实值非期望值**（D7——若 ETL 对 py fixture 漏抽嵌套函数，矩阵显 missing，pin 钉观测值——审计在工作非审计在装好）。
- 计数预估：quick 2013+19=2032 / deep 1525+4=1529 / total 3561（collect 后真值同步 README 四面+交接 Face E）。

## 8. CLI/文档接线面

- README CLI 表加 `golden` 行（drift 同族——对既有 facts.db 的只读审计查询面）。
- 构想v10 §F15 状态注记（已完成）；构想v13 §BB3 状态注记（已完成）；优先级总表批⑥→已完成；交接 #204。
- glossary 不加（无新表——glossary 是 facts.db 数据面登记处）。

## 9. 决策点（用户授权自批，随批定稿）

- **D1 双面分类**：violations（source 过滤多集匹配）+symbols（文件域完整枚举）——面可选、空数组=「期望零」有效面（反 spurious 声明能力）。
- **D2 匹配键**：violations (kind, file, line, importer?, imported?) payload 提取 R-AD 守卫；symbols (qn, label, file, start, end) 严格精确——近失两侧成对计数（解析精度信号不掩埋）。
- **D3 precision/recall 分母零=null**（不造 1.0——期望零面靠计数面呈现）。
- **D4 初始真值集七件起步**（quick 3：java/php/ruby 三解析器家族；deep F15 2：go/ts；deep BB3 2：py/ts）+扩语言=纯数据零代码；provenance 非空守卫测试钉死（「不落盘=自欺」防线工程化）。
- **D5 rc 恒 0**（度量非门禁；红面归 pin/jq）。
- **D6 无新表**（真值=committed JSON 独立文件面——与被审计库物理分离防循环门）。
- **D7 pin=观测诚实值**（CGC 84.6% 教训——矩阵可能暴露 <100%，那正是审计的价值；改进后 pin 随批更新）。
