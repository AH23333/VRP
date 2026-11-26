# 设计稿：R2 co-change 隐式耦合边

> 状态：**待批准**。对应构想v12-R2（亮点/硬核，"需设计稿→批准→实施"约定本件即设计稿）。
> 调研时点 2026-11-17；立项前复轮子调研结论沿用构想v12（code-maat 共现矩阵/Tornhill temporal coupling 语义；CodeScene 商业参照——无引入，纯 stdlib 自实现）。
> 与 v10-A1 区分（沿用构想v12 原文）：A1 是时间线度量曲线，本件是隐式耦合**入一等公民表**。

## 1. 现状与增量（诚实盘点）

已有底座比构想v12 写作时所以为的更厚——设计稿必须直面：

- **CBM `FILE_CHANGES_WITH` → `history_coupling` 链路已存在**：etl 从 CBM 图读该边型，路由进 `history_coupling(src_fid, dst_fid, properties_json)`（etl.py:383/429/444；evidence.py:45 同路由）。实测 lusiem 27 行/tsa 33 行。
- 但该底座有四件局限，构成本件立项理由：
  1. **语义黑盒**：CBM 的 coupling_score 算法无文档承诺、不可解释——与 S6"评分公式开放可解释"哲学正面冲突；
  2. **写而不读**：全仓唯一产品读点是 `cmd_status` 的 COUNT(*)（cli.py:578）——数据落库后无任何消费面（dossier/slices/验证器均未接）；
  3. **覆盖稀疏且不可调**：tsa 万级提交历史只产出 33 对——CBM 内部阈值未知、无参数面；
  4. **fid 锚定跨 etl 不稳**：行锚 CBM 的 file fid，fid 含 content_hash 语义随 etl 轮换；表本身每 etl 重建（非 OWNED），历史对连续性无保证。
- **R2 增量**：自研 git log 共现挖掘（**公式开放、阈值可调、文件路径键跨 etl 稳定**）+ 一等消费面（CLI 呈现先行，dossier/slices 后续立项接入）。

## 2. 数据与算法（学 code-maat/Tornhill，公式全开放）

- **输入**：`git log --no-merges --name-only --pretty=format:%x1f%H%x1f%aI`（US 分隔避免路径/哈希歧义；`--no-merges` 排合并提交——merge 的文件集是冲突消解产物非真实共变信号；只 HEAD 分支历史，不用 `--all`——复现性红线：同 HEAD 同产物）。经 `procs.run_tree`（超时参数化，默认 60s——大仓 git log 全量可能超 etl 先例的 30s 面）。
- **规范化**：路径正斜杠、去首尾空白（etl `_norm` 先例）；同 commit 去重。
- **度量**（temporal coupling 标准语义）：
  - `support(a,b)` = a、b 同 commit 共现次数；
  - `changes(a)` = a 出现过的 commit 数；
  - `confidence(a→b) = support(a,b) / changes(a)`——"改 a 时 b 也改了"的条件概率。
- **诚实局限（记明不假装）**：
  - 不做 rename 追踪（历史中改名的文件以旧/新名各自计——`--follow` 是单文件 API 不适用全量对；code-maat 同样默认不做）；
  - 纯共现非因果（共变可能同因于第三个文件/同一个 feature 分支切割）——诊断信号非裁决依据（v_hotspots fan_in_same_file_pct 同定位）；
  - 大单核提交（bulk commit）会抬高全仓 support——`--min-support`/`--min-confidence` 阈值面留给用户调。

## 3. Schema 面

**独立新表 `co_changes`**（与 history_coupling 不混写——语义不同源：CBM 黑盒 vs 自研透明；CBM 链路零动）：

```sql
CREATE TABLE IF NOT EXISTS co_changes (
    file_a TEXT NOT NULL,
    file_b TEXT NOT NULL,
    support INTEGER NOT NULL,
    changes_a INTEGER NOT NULL,
    changes_b INTEGER NOT NULL,
    confidence_ab REAL NOT NULL,
    confidence_ba REAL NOT NULL,
    first_co_change TEXT NOT NULL,
    last_co_change TEXT NOT NULL,
    PRIMARY KEY (file_a, file_b)
);
```

- **行规范**：`file_a < file_b` 字典序（无序对的规范表示——杜绝同一对两行）；
- **文件路径键而非 fid**：路径跨 etl 稳定（fid 轮换问题根治；qn/路径作稳定键 = fid_ledger 同哲学）；
- **无 created_at 列**：数据本身含 commit 时间（first/last_co_change）；无时间戳进产物——复现性红线（同 HEAD 挖掘两次字节级同产物）；
- **`OWNED_TABLES` 追加 `co_changes`**：跨 etl 快照回灌存续（runtime_calls 先例）——git 历史跨 etl 不变，数据持续有效，重挖是浪费；freshness（M4）保证挖掘时点的 HEAD 一致性（写路径走 `_open_facts_rw` 硬拒 stale，stale 意味着 etl 都没重跑，更不该挖新历史）；
- `CREATE TABLE IF NOT EXISTS` 自愈迁移（ensure_schema executescript 既有模式——旧 facts.db 升级零手工步骤）。

## 4. 命令面

- `vrp cochange <repo> [--json] [--min-support N] [--min-confidence X]`；
- **挖掘=写路径**：走 `_open_facts_rw`（freshness M4 硬拒照旧）→ `DELETE FROM co_changes` → 全量 INSERT → commit（**幂等=DELETE+INSERT 全量替换**，#62 xcheck ledger 教训：当前状态语义必须幂等，重跑零膨胀）；
- **呈现=读路径**：排序 `(support DESC, confidence_max DESC, file_a, file_b)`——整数优先排序、浮点仅作展示（#159 同哲学）；默认阈值 `--min-support 3`/`--min-confidence 0.3`（Tornhill 惯例参考值，可调）；默认呈现 top 50 对+汇总（commits 扫描数/文件数/对总数/阈值后幸存数——诚实计数）；
- 边界：非 git 仓/空历史→诚实空结果（`changes=0` 仍写空表清残）；MCP 工具族不动（TOOL_NAMES 30 件不变）。

## 5. 消费面（v1 范围收窄，后续各自立项）

- **v1 只做**：`co_changes` 表底座 + CLI 呈现；
- **后续候选（不在本件范围）**：
  - dossier 影响面："改 A 必改 B"提示（hotspots 详情页附 co-change 对）；
  - slices/structure 验证器：co-change 对**跨 slice/模块边界** = 边界切错的可疑信号（"AI 梳理 vs 历史耦合"交叉验证——R1"AI 梳理 vs 图论社区"的姊妹叙事）；
  - S6 评分加权：隐式耦合度进影响面维度。
- 叙事定位：CALLS/CALL_REFERENCE 是**显式依赖**，co_changes 是**隐式耦合**——两图分歧即发现（"无调用边却总是同改"= 隐藏耦合；"有调用边从不共变"= 稳定接口）。这一定位在 README/交接记明，消费面落地时兑现。

## 6. 红线核对

| 红线 | 核对 |
|---|---|
| 静态产物 | ✓ git log 只读；产物=facts.db 表 + CLI 呈现 |
| 零第三方运行时 | ✓ subprocess git + stdlib 组合数学（itertools.combinations） |
| 同 facts.db→字节级同产物 | ✓ git log 拓扑序确定；无时间戳列；`--all` 不用；排序整数键优先 |
| 复现性风险点 | git 对象不可变 → 同 HEAD 历史恒同；浮点列（confidence）只展示不进排序键 |

## 7. 成本与测试面

- **实施**：facts_schema.sql 追加表（+OWNED_TABLES 一行）+ `vrp/cochange.py`（~200 行：parse_log/mine/persist/run/render）+ CLI 接线 + README/交接同步；
- **快速层测试 ~12 件**（合成 git 仓 fixture：tmp `git init`+脚本化多 commit）：
  - 度量正确性：support/confidence 手算锚定（3 commit 小仓逐对手算）；
  - 行规范：file_a<file_b 字典序、同 commit 去重；
  - 幂等：重跑 DELETE+INSERT 零膨胀（#62 同型断言）；
  - 阈值过滤：min_support/min-confidence 边界（等于/越过）；
  - 合并提交排除：造 merge commit 断言其文件集不计入；
  - 非 git 仓：诚实空结果；
  - 确定性：同仓双跑产物相等；
  - OWNED 存续：etl 重跑后 co_changes 行集不变；
  - freshness：stale facts.db 挖掘被硬拒；
  - render_text/JSON 形状、cli parser 旗标面。
- **深测**：lusiem/tsa 真仓冒烟（数字记档交接不钉值——CBM history_coupling 行数漂移先例教训：跨纪元数字不作断言）。

## 8. 决策点（需批准）

| # | 决策 | 推荐 | 备选与理由 |
|---|---|---|---|
| D1 | 独立 `co_changes` 表 vs 扩展 history_coupling | **独立** | 不同源不混写；CBM 链路零动；history_coupling 保留作 CBM 原始路由不动其语义 |
| D2 | OWNED 跨 etl 存续 vs 每 etl 重挖 | **OWNED** | git 历史跨 etl 不变；重挖纯浪费；freshness 已保证挖掘时点一致性 |
| D3 | 默认阈值 min-support=3 / min-confidence=0.3 | 推荐采纳 | Tornhill 惯例参考值；CLI 可调，非硬编码语义 |
| D4 | v1 消费面只 CLI+表 | 推荐采纳 | dossier/slices 接入各自立项（防单批膨胀——构想v12 收敛提醒） |

## 9. 实施批次（批准后）

- #161：schema+`vrp/cochange.py`+CLI+测试+README/交接（单 commit+push，预期快测 1541→~1553）；
- 冒烟：verify 双仓（只读呈现+挖掘各一轮，挖掘会写 verify 仓 facts.db——**预先备份**或挖掘后恢复，避免污染 dogfood 基线；或冒烟只在临时副本）。
