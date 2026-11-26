# 设计稿：AA1+AA2 MCP 消费面改造

状态：**已批准并实施完成**（D1-D8 全批，2026-11-20；实施交接 #199——工具面 30→10+isError 归零+五层预算（`vrp/mcp_budget.py`）+MCP server 0.2.0 破坏性收敛；起草时参照源码已逐行重读核验——codegraph `src/mcp/tools.ts`/`AGENTS.md`、CGC `tool_definitions.py`/`utils/tool_limits.py`，防转述失真纪律兑现）

## 1. 现状与增量（诚实盘点）

- **30 工具**（mcp_server.py，README 记"三十件"）：读/图查询 16（nav_search/architecture/module/symbol/callers/callees/slice/path、query_facts、seams、coverage、impact、drift_report、curate_slices/structure/receipt——末三只读）+ dossier + curate 写族 2（submit/reject）+ tx 族 4（preview/submit/run/verdicts）+ render 族 3（verdicts/slice/report）+ session 族 3（begin/end/report）+ enforce。
- **isError 逃逸缺口（最高危）**：`_guard` 只捕 `(SystemExit, ValueError, RuntimeError, OSError, sqlite3.Error)`——KeyError/TypeError/IndexError/AssertionError 逃逸到 SDK → `isError: true`。codegraph 维护者实测：**一次 isError 早出现就足以让 agent 整个抛弃工具集**（"Errors teach abandonment"，AGENTS.md 反复观察）。
- **无输出预算层**：dossier 默认 `budget=32000` 超 codegraph 实测的 ~24K 内联硬顶（宿主 ~25K 以上把结果外置成文件再让 agent Read 回来——重引入一次读+缓存写成本，n=4 A/B 实录）；其余工具无统一字符顶。
- **无 per-statement 超时**：query_facts 收任意 SQL（含 WITH RECURSIVE BFS）、nav_path 递归 CTE，均无时间顶。
- **无输入上界**：sql/payload_json/tx_json 无长度限（codegraph 以 MAX_INPUT_LENGTH 10K 防巨型输入打爆服务器）。
- **已合面**：agent 引导单源（`_INSTRUCTIONS` 唯一权威，#529 同律）；限额默认值散在函数签名（nav_search 20/callers 50/query_facts max_rows 100……）可用但无中央表无回归钉死。
- **测试面**：tests/test_mcp_server.py 19 件——真实 lowlevel session `call_tool` 端到端（非 mock 协议层），翻新面集中。

## 2. 参照源码重读记实（file:line 锚点）

- codegraph `src/mcp/tools.ts:100-111`：NotIndexedError→catch 转**成功形状指引文本**（NO isError）；isError 仅留安全拒绝（PathRefusalError）与真故障（附 retry-once 注）。
- codegraph `tools.ts:265-271`：`getExploreBudget(fileCount)` 调用数档（<500→1 … ≥25K→5）；`tools.ts:317-369+`：`getExploreOutputBudget` 每档 maxOutputChars/defaultMaxFiles/maxCharsPerFile——**不变量：更大档不得更小每文件额度**；注释实录 ~24K 内联硬顶机理；ITER1/2/3 反复横跳教训=预算魔数必须测试钉死。
- codegraph AGENTS.md"Adapt the tool"：新工具低选取率（trace 被砍）、措辞 3 变体全回归——**改造已在调的工具有效，造新工具/改措辞无效**。
- CGC `tool_definitions.py:85-131`：`analyze_code_relationships` 单工具 15 query_type（find_callers…find_functions_by_decorator），参数 `query_type+target+context+depth` 四参盖全；target 多义（call_chain 用 `'start->end'`）。
- CGC `utils/tool_limits.py` 全文：TOOL_RESULT_LIMITS/TOOL_QUERY_TIMEOUTS JSON 中央表+env 覆盖；默认超时 120s=**168,745 函数图实测最慢读 12s 的 10×**（校准法：10× 实测最慢，写明出处）。

## 3. AA1 工具面收敛（30→10）

### 3.1 分族方案

| 新工具 | 聚合 | 旧工具 |
|---|---|---|
| `vrp_query(query_type, target, options_json)` | 17 类型 | nav_search/architecture/module/symbol/callers/callees/slice/path、query_facts、seams、coverage、impact、drift_report、curate_slices、curate_structure、curate_receipt、tx_verdicts |
| `render(kind)` | 3→1 | render_verdicts/slice/report |
| `session(action)` | 3→1 | session_begin/end/report |
| `dossier` | 独立 | briefing 动词+自身预算参数 |
| `curate_submit` / `curate_reject` | 独立 | 写路径动词显著性=安全闸（reject 是人否决，名字必须显眼） |
| `tx_preview` / `tx_submit` / `tx_run` | 独立 | 动词显著=安全：preview 先行靠名字 salience；run 是唯一执行动词不与读混 |
| `enforce` | 独立 | S7 写路径（P9 findings 幂等替换） |

- **vrp_query 参数形态=CGC 四参律收敛为三参**：`query_type`（17 枚举）+`target`（主目标串：query/fid/module_id/slice_id/sql/tid；nav_path 用 `"from_fid->to_fid"`，CGC call_chain 同式先例）+`options_json`（次参数 JSON 串：`{"limit":50}`/`{"max_rows":100,"params":[...]}`/`{"base":...,"depth":2}`）。
- **收敛判据**（正面回应"单强工具"）：只聚合**同构只读查询**（CGC 15 类型先例）；写路径/执行动词保持独立名字（codegraph"动词显著"面）；首次呈现面收敛≠改造已在调的工具（30 件对新会话 agent 是选择过载，AGENTS.md 低选取律不适用于首次呈现）。
- **破坏性一步到位**：MCP `_VERSION` 0.1.1→0.2.0；不保留旧 30 名（并存=40 件更重）；单用户面（自有 agent 会话），无外部消费者承诺。
- **isError 面归零**：`_guard` 捕面拓至 `except Exception`（KeyboardInterrupt 属 BaseException 不受影响照传）——KeyError/TypeError 族入网转 `{"error": ...}` 成功形状。**与 codegraph 分歧记档**：codegraph 保留真故障 isError+retry-once 注；VRP 单仓 stdio server，agent 重试无法自愈真故障，稳定 `{"error"}` 消息面更可调试且彻底消除 isError 弃用 hazard；SDK 传输层/server 崩溃自然 isError——那是"stop trying"的真面目。
- **_INSTRUCTIONS 重写**：重组循环为 `dossier → vrp_query(探查) → curate_submit → tx_preview/tx_run → render/session`；引导单源律不变。

## 4. AA2 输出预算层（五层）

- **L1 输出硬顶（新增）**：MCP 层统一 `MAX_TOOL_OUTPUT_CHARS = 24000`（~24K 内联上限）。实现=序列化后测量，超顶做 **JSON 感知确定性收缩**而非砍字节（砍 JSON 中段=agent 收到语法破碎垃圾）：递归找最长列表对半截（保头部）+`"_truncated"` 记录键路径与原长，循环至达标；无列表可收缩（单个巨型标量）→信封 `{"truncated": true, "original_chars": N, "value_head": "...", "hint": "narrow the query or lower limit"}`。截断**永远可见**（codegraph"截断可见告警"律）。
- **L2 行数限额中央表（收编）**：`_TOOL_RESULT_LIMITS` 模块级表收编现散在签名的默认值（search 20/callers 50/max_rows 100/…）+回归测试钉死（ITER1/2/3 教训）+`VRP_TOOL_RESULT_LIMITS` env JSON 覆盖（CGC 律，缺省回落内置表）。
- **L3 自适应档位（否决，诚实记档）**：行数随仓规模自适应**否决**——CGC 生产形态本就是**静态** per-tool 限额；VRP 行均 ~50 字符 token 便宜（codegraph 自适应是因为 explore 返回**源码文本**，字符即成本）；limit 参数已在 agent 手。YAGNI 记档。dossier 的 task 档预算（T1-T4）已存在不动。
- **L4 per-statement 超时（新增）**：`con.set_progress_handler` 检查 deadline→`sqlite3.OperationalError`→`_guard`→成功形状 `{"error": "query_timeout ..."}`。默认顶=**10× 实测最慢律**：实施时在 tree-sitter-analyzer facts.db（本地最大图）实测 query_facts BFS/nav_path 最慢值，默认=10× 出处记档；`VRP_TOOL_QUERY_TIMEOUT` env 覆盖。
- **L5 输入上界（新增）**：sql/payload_json/tx_json/options_json ≤ 1 MiB（curate slices 合法大 payload 余量）；query/target ≤ 10K（codegraph MAX_INPUT_LENGTH 律）；超界→成功形状 `{"error": "input too large ..."}`。

## 5. 红线核对

- 静态产物字节级一致：MCP 层是**消费面**非产物面——facts.db 写路径（curate/tx/enforce）、render_report 产物、report/SARIF 面零动；MCP 返回 JSON 形态变是消费面变更，不触产物红线。
- 零第三方运行时依赖：预算/限额/超时/上界层纯 stdlib（json/os/sqlite3 progress handler）；mcp SDK 仍是 optional extra 不动。
- 确定性：收缩/截断确定性（同输入同输出）；限额表与硬顶魔数回归测试钉死。
- 无服务端/无前端构建链：stdio transport 不变，不触。

## 6. 成本与测试面

- **实施面**：mcp_server.py 重构——聚合转发层（17 类型→既有内层函数，**内层 nav/curate/tx/render/session 函数签名零改动**，纯转发）+`_guard` 拓宽一行+L1 收缩层 ~50 行+L2 表 ~30 行+L4 超时 ~20 行+L5 上界 ~15 行；README mcp 行+契约文档指引+_INSTRUCTIONS 重写。
- **测试翻新**：test_mcp_server.py 19 件按新工具名/参数形态翻新（端到端 lowlevel session 框架不动）；新增 ~18-22 件：L1 收缩确定性（长列表对半/信封/可见性）/硬顶与限额表钉死/env 覆盖三态/L4 超时（mock 慢查询）/L5 上界/isError 面归零（KeyError 注入端到端断言成功形状）/vrp_query 17 类型逐一冒烟/nav_path `from->to` 解析/0.2.0 版本串。
- **dogfood**：tsa facts.db 上 17 类型逐一实跑+输出字符实测记档（记档不钉值）。
- 计数预估：+18-22 件→~1945。

## 7. 决策点（需批准）

- **D1 收敛形态 30→10**：vrp_query(17)+render(3)+session(3)+dossier+curate_submit/reject+tx_preview/submit/run+enforce；tx_verdicts 与 curate 只读三件并入 vrp_query（§3.1）。
- **D2 vrp_query 三参**：query_type+target+options_json（CGC 四参律收敛；nav_path 用 `"from->to"` target，CGC call_chain 同式）。
- **D3 破坏性一步到位**（0.2.0，不保留旧名不并存）。
- **D4 isError 面归零**：`_guard` 拓至 `except Exception`；与 codegraph 真故障-isError 分歧及理由记档（§3）。
- **D5 输出硬顶 24K+JSON 感知确定性收缩+可见信封**；dossier 32000 与硬顶的冲突由收缩层统一解决（不 special-case dossier）。
- **D6 行数限额静态中央表+env 覆盖；行数自适应档位否决**（CGC 静态实证+行 token 便宜+limit 已在 agent 手，YAGNI 记档）。
- **D7 超时=progress_handler+10×实测律+env 覆盖；输入上界 1 MiB/10K**。
- **D8 单批实施**（预算层先行+工具面重构+测试翻新一次过；拆两批会让 19 件测试翻新做两轮）。

## 8. 实施批次（批准后，单批）

1. L1-L5 预算/限额/超时/上界层+单测（先行，纯新增零重构风险）。
2. 工具面 30→10 聚合重构+`_guard` 拓宽+test_mcp_server 翻新+新增件。
3. _INSTRUCTIONS/README/契约文档重写+dogfood 实测记档+交接+快测全量+commit/push。
