# vrp — Vibe 项目重构分析管道

把 AI 快速堆出来的仓库，变成**有事实基础、可验证、可执行重构**的对象。

**为谁服务**

- **主用户：通用 AI agent**（不绑定特定框架）——消费 facts.db 与 MCP 十一件工具；产物是文件与数据库，不是服务
- **辅用户：vibe 开发者（人类）**——消费 report / dossier 等人读产物：理解项目、判断梳理意见、做决定

**三红线**

1. 静态产物、无服务端、无前端构建链；同一 facts.db → 字节级相同产物
2. 纯 Python 标准库（≥3.11），零第三方运行时依赖；外部工具全部子进程化、可审计可重放
3. 诚实优先：外部信号缺失/超时一律诚实降级（skipped / 诚实记录），不伪造结果

<!-- doclint {"id": "readme-quick-count", "claim": "快速层 (\\d+) 例", "source": {"kind": "pytest-collect", "component": "quick"}} -->
<!-- doclint {"id": "readme-deep-count", "claim": "深度层 (\\d+) 例", "source": {"kind": "pytest-collect", "component": "deep"}} -->
<!-- doclint {"id": "readme-total-count", "claim": "全量 (\\d+) 例", "source": {"kind": "pytest-collect", "component": "total"}} -->
<!-- doclint {"id": "readme-astgrep-rules", "claim": "ast-grep (\\d+) 规则", "source": {"kind": "glob", "pattern": "vrp/rules/s2/*.yml"}} -->

## 管道一览

```
S0 precheck ─→ S1 CBM 索引 ─→ S1.5 ETL(facts.db) ─→ xcheck 交叉验证
                    │
                    ├─→ S2 病理检测（ast-grep/死代码/变异三引擎/P4·P2 补位）
                    ├─→ S3 校验器组（锚定/覆盖率/证据闸门）
                    ├─→ S4 freeze（基线快照 + 变更预筛）
                    ├─→ S5 dossier（五视图装配 + repomap + 任务型预算）
                    ├─→ S6 重构事务（G1-G5 闸门 + git 原子提交/回滚）
                    ├─→ S7 enforce（架构执法：21 引擎违例 → P9 findings）
                    └─→ S8 report（archify 报告 + SARIF/Mermaid 降级链）
```

核心产物：`<repo>/.vrp/facts.db`（规范化事实层与注解宿主）与 `dossier.json`（信息装配）；重构事务在其上声明、验证、执行。

## 核心概念

| 概念 | 一句话 |
|---|---|
| facts.db | 规范化事实层——符号/边/病理/verdict 唯一事实源；CBM 索引经五级证据路由 ETL 而来（文件粒度增量） |
| fid | 符号身份（sha256）——文件字节不变⇒fid 不变；`fid_ledger` 记录身份史，drift 重定位的锚 |
| dossier | 五视图信息装配（T1-T4 任务型预算）——agent 开工前的领地地图 |
| curate 工件 | AI 对仓库的理解以可验证数据入库：slices（业务线）+ structure（模块+层 DAG+规则）；全验过才入库，否决留痕 |
| 重构事务 | 声明式 tx.json 过 G0-G6 闸门——git 原子提交或整体回滚；T4（内容微调）永不自动放闸 |
| verdict | 事务裁决（PASS/FAIL/needs_confirmation）——open 项自动回馈进 dossier evidence |
| drift | 事务落地后工件失锚——drift 诊断 / remap 半自动恢复 / curate-refresh 库内直刷 / refresh 一键环 |
| P 码 | 病理类型码（P4 死代码/P5 危险 API/P9 架构违例等）；S6 开放评分公式排序，分量明细人可手算复验 |

## 安装

```powershell
pip install -e .                       # 纯标准库，无第三方依赖
```

- **CBM 缓存零配置**：默认落 `<repo>/.vrp/cache`（per-repo 隔离）；`CBM_CACHE_DIR` 可选覆盖；`vrp gc` 审计/清理
- **零安装试用**：`python -m vrp.build_pyz` 构建单文件 `dist/vrp.pyz`，`python vrp.pyz --help` 即用
- **工具沙箱**：`python bootstrap.py` 一条命令重建 `work/`（外部工具全部钉版本收录，幂等）；前置：Python ≥3.11、Node.js + npm、git、[CBM](https://github.com/DeusData/codebase-memory-mcp/releases)

## 快速开始

repo 参数绝对/相对路径皆可（相对=当前终端目录），省略时默认当前目录。

```powershell
vrp run <repo>                          # S0→S1→ETL→xcheck 全管道（首次必跑）
vrp status <repo>                       # facts.db 新鲜度快照
vrp s2 <repo>                           # 病理检测（findings + 差异登记簿）
vrp findings <repo>                     # S6 开放评分公式排序的病理清单
vrp dossier <repo> --task T1            # 五视图装配（T1-T4 任务型预算）
vrp curate <repo>                       # AI 梳理工件验收入库（.vrp/ai/ slices/structure）
vrp transact <repo> tx.json --preview   # 重构事务 diff 预览（无闸门不改树）
vrp transact <repo> tx.json             # 重构事务（声明→闸门→原子提交或回滚）
vrp verdicts <repo>                     # verdict 历史（open 项自动回馈进 dossier evidence）
vrp report <repo>                       # 人类产物：.vrp/report/ 静态报告
vrp mcp <repo>                          # AI 接口：MCP server over stdio（十一件工具）
```

## CLI 命令（69 子命令，按场景分组）

`vrp --help` 全列；`vrp <cmd> --help` 参数详情；多数命令支持 `--json` 机器读。设计动机与实施记录见《交接文档.md》对应条目。

### 管道主线

| 命令 | 用途 |
|---|---|
| `run` | 全管道一发 S0→S1→ETL→xcheck；`--watch` 变更自动重跑（防抖+增量，Ctrl+C 干净退出）；`--reindex` 强制重索引 |
| `precheck` | git 快照 / 语言普查 / 命令发现 |
| `index` / `status` | CBM 索引（缓存默认 `<repo>/.vrp/cache`）/ 状态查询 |
| `etl` | CBM → facts.db 五级证据路由；文件粒度增量默认自动，`--full` 强制全量；schema 迁移链见《契约/schema版本承诺.md》 |
| `etl-health` | ETL 健康面：fid 唯一/六向零孤儿/去重等不变式检测+修复建议（只检测不自愈；rc 0 健康/1 违例） |
| `xcheck` | code-atlas 四元组交叉验证 → 差异登记簿 |
| `xcheck-explain` | xcheck 差异人话解释（现象→原因→修法） |
| `s2` | 病理检测：ast-grep 12 规则（9 基础+3 危险 API 族）+ 死代码（三级置信度）+ knip/vulture + jscpd |
| `freeze` | 基线命令执行 + graph 快照 + 变更预筛 |
| `dossier` | 五视图装配（架构/简报/repomap/证据/摘要；T1-T4 任务型预算） |
| `report` | 静态报告 `.vrp/report/`：archify 渲染 + chain 多页互链 + fusion 六风格总览 + tour 新人导览 + SARIF（缺工具降级 Mermaid） |
| `session` | 全信任模式：begin/end facts.db 快照 + 初末计数 diff + verdict 时间线 |

### 梳理与执法

| 命令 | 用途 |
|---|---|
| `curate` | AI 梳理工件验收入库（全验过才入；`--reject` 否决留痕；复验失效标 stale） |
| `curate-auto` | 内置 LLM 梳理循环（配 OpenAI-compatible key；自动六步；全留痕、key 不落盘） |
| `curate-merge` | 两代理梳理产物三方合并（base/ours/theirs；冲突留痕保 ours、零静默丢；rc=1 待裁决） |
| `curate-refresh` | fid 重映射库内直刷（drift 恢复写路径；全量重验才入库；同步治愈 `.vrp/ai` 声明文件） |
| `drift` | 梳理工件漂移查询（fid_ledger 反查重定位候选；`--history N` 漂移历史；有漂移 rc=1） |
| `remap` | 漂移恢复半自动：唯一候选一键写回 `.vrp/ai/*.json`；多/无候选诚实 unresolved |
| `refresh` | 漂移恢复一键环：S0→S1→ETL→xcheck→drift→remap→curate→report |
| `enforce` | S7 架构执法：structure 层 DAG+rules → 按语言路由 21 引擎（py/ts/go/rs/java/cs/kt/c/php/scala/rb/dart/swift/zig/lua/objc/pl/erl/r/clj/ml 等）→ P9 findings；混仓分治（逐语言只插不删+跨语言边上报） |
| `seams` | 模块间跨界边聚合（strangler 重建顺序原料） |
| `propose` | 重组切片提议：确定性分区草案 → `.vrp/proposals/` |
| `scaffold` | strangler scaffold：模块骨架 + re-export shim 模板 + 迁移序（先边缘后核心） |
| `migprog` | 迁移进度追踪（跨界边收缩%；`--snapshot` 落审计流） |
| `tour` | 新人模块导览（职责一句话+由浅入深阅读序+锚点；`--html` 静态页） |

### 重构事务

| 命令 | 用途 |
|---|---|
| `transact` / `transact-batch` | 事务执行（`--preview` diff 预览不改树）/ 目录批处理 |
| `verdicts` | verdict 历史查询（`--tid` 单条；open 项自动入 dossier evidence） |
| `fixpropose` | 单 finding → LLM 产事务草案（草案永不执行——执行只走 transact 闸门；不可修诚实转义） |
| `validate` / `gate` | 锚定+覆盖率校验 / AI claim 证据闸门 |

### 病理与分析

| 命令 | 用途 |
|---|---|
| `assess` | 迁移前评估（引擎能力矩阵+风险提示；纯只读） |
| `graph` | 调用图网络指标：PageRank + LPA 社区 + structure 模块对账（图论社区 vs AI 梳理交叉验证） |
| `findings` | 病理清单（S6 开放评分排序；`--dismiss` 反馈降权——只调序不删除） |
| `score` | finding 评分：分量明细人可手算复验；`--weights` 可覆盖 |
| `groups` | 同根因聚合（Sentry fingerprint 语义；归组不删行） |
| `complexity` | 圈复杂度+认知复杂度（py/ts 先行；MI/Halstead 诚实不做） |
| `analyze` | 终端原生 AI 分析：单轮 LLM 结构化报告（材料=dossier T1+findings top 20 确定性派生；facts 陈旧自动重建；落 `.vrp/analysis.md`，审计 `.vrp/analyze/`） |
| `chat` | 终端原生 AI 问答 REPL（dossier T1 简报注入；斜杠命令 /quit /dossier /findings /refresh /clear；`--log` 才留会话日志） |
| `cochange` | git 共现隐式耦合边（support/confidence 开放公式；CALLS 显式依赖的对照面） |
| `timeline` | 架构腐化时间线：git 采样回放+步间度量+转折点叙事（"这个仓库哪天开始烂的"） |
| `layermetrics` | 层度量：fan-in/out、instability、可选阈值违例（rc=1 CI 可用） |
| `profile` | 仓库数字画像：Gini/集中度/边密度/枢纽 top5 |
| `pitindex` | 坑→测试防复发索引（GAP 缺口 / DRIFT 失锚分开呈现） |
| `dossier-diff` | 两份 dossier 对账（新增/消除/变化清单；release notes 素材） |

### 证据与审计

| 命令 | 用途 |
|---|---|
| `traces` | 运行时调用证据导入（runtime_calls 独立表；agreed/observed_only/static_only 对账） |
| `coverage` | 索引完备性核验（引用图文件前先核验；六态+建议） |
| `impact` | 变更影响面 BFS+建议测试（suggested ≠ sufficient） |
| `testcov` | 静态关联 vs 实际行覆盖对账（「静态说有测试但从不执行」假关联现形） |
| `golden` | Golden 审计矩阵：committed 真值双面审计（violations 多集匹配+symbols 五键；禁止导出与自己比） |
| `mdcode` | Markdown 代码块 vs facts 符号比对（doc-only/kind-mismatch 漂移候选） |
| `argcheck` | 参数级对账：too_few/too_many/unknown_kwarg（签名字符串为唯一基准） |
| `sweep` | 阈值敏感度扫描（离线重切 payload；悬崖点选阈值） |
| `review` | SARIF → PR review comments 载荷（本地转换零服务端） |

### 治理与工程

| 命令 | 用途 |
|---|---|
| `doclint` | 活文档防漂移：markdown 内数字断言对真值源对账——文档说谎检测器（本 README 顶部四个标记即活例） |
| `doctor` | 工具链自诊断（core 缺失 rc=1；engine 缺失只报能力降级） |
| `explain` | 错误码人话速查（gate/curate/P 三族） |
| `perf` | 性能回归追踪（run 四阶段自动计时；相对+绝对双门旗标） |
| `relnotes` | release notes 半自动（commit × 交接交叉核对；实时核码不信快照） |
| `smoke` | 跨仓冒烟一键化（verify/ 逐仓全链；rc=1 on FAIL） |
| `gc` | CBM 缓存 GC（审计→dry-run→真删三段式） |
| `flakes` | flake 登记表（只记录不掩盖；validate 漂移守卫） |
| `langmatrix` | 语言能力矩阵（每格实时核码生成，零硬编码） |
| `secrets` | 密钥扫描（worktree+`--history` blob 遍历；命中 rc=1 CI 可用） |
| `mutate-plan` / `mutate-run` | 变异预算采样 / 变异执行（mutmut·Stryker·cargo-mutants） |
| `config` | LLM API 配置向导（OpenAI-compatible；交互三问写 `.vrp/config.json`，key 默认走环境变量不落盘；`--show` 掩码回显 / `--test` 连通性 ping / `--store-key` 明文存储） |

## AI 接口（MCP，十一件工具）

`vrp mcp <repo>`：MCP server over stdio（per-repo 绑定；`pip install vrp[mcp]`）。十一件：

| 工具 | 用途 |
|---|---|
| `vrp_query` | 单强查询工具，17 类型：search/architecture/module/symbol/facts/callers/callees/slice/path/seams/coverage/impact/drift/curate_slices/curate_structure/curate_receipt/tx_verdicts（options_json 载次级参数） |
| `dossier` | S5 开工简报（T1-T4 任务型预算） |
| `curate_submit` | 梳理工件提交验收入库（kind=slices/structure） |
| `curate_reject` | 用户否决留痕（agent 可转达） |
| `curate_refresh` | 确定性 fid 重映射（与 CLI 同权） |
| `tx_preview` | 事务 diff 预览 |
| `tx_submit` | 事务提交执行 |
| `tx_run` | 事务执行（T4 返回 needs_confirmation——永不自动放闸） |
| `render` | 渲染（kind=verdicts/slice/report） |
| `session` | 全信任模式（action=begin/end/report） |
| `enforce` | S7 架构执法（P9 findings） |

- **isError 面归零**：内部错误统一 `{"error"}` 成功形状，不裸崩
- **五层输出预算**：24K 硬顶+JSON 感知确定性收缩；静态限额表+`VRP_TOOL_RESULT_LIMITS` env 覆盖；SQL 超时 progress_handler；输入上界 1MiB/10K
- **`VRP_OUTPUT_FORMAT=gcf`** 可选紧凑编码（语义键声明一次；混合形状诚实回退 JSON）

## 场景 skill

`skills/` 目录四个场景操作协议（SKILL.md；agent 接 vrp MCP/CLI 后的消费指南；形状守卫 tests/test_skills.py）：

| Skill | 场景 | 一句话 |
|---|---|---|
| `vrp-loop` | 重构 | 八步事务循环（定位→下钻→产出→预览→执行→verdict→修复→回归）；T4 不放闸、每事务至多 3 重试 |
| `vrp-curation` | 梳理 | slices/structure 六步生产线（验证入库+drift remap）；拒收码速查 |
| `vrp-archaeology` | 考古 | 读懂陌生/遗留仓：事实层→dossier→timeline→cochange→病理→report（只读零写） |
| `vrp-review` | 审查 | 证据化审查：findings→xcheck→impact→testcov→review 评语；advisory 非 gate |

## 重构事务与闸门

事务失败即整体回滚（git 原子性），verdict 落 `.vrp/verdicts/`：

| 闸门 | 检查 | 稳定规则码 |
|---|---|---|
| G0 | apply 前置校验 | `gate/G0/apply-edits` |
| G1 | 提交后重索引（旁路 ETL） | `gate/G1/parse-errors` |
| G2 | lint/typecheck | `gate/G2/static-check` |
| G3 | 测试套件 | `gate/G3/test` |
| G4 | 声明等价：**五信号**——符号集/使用边/悬垂引用/verify_changes/**difftastic 结构 diff**（同符号体内偷改可抓） | `undeclared-change` `edge-mismatch` `dangling-reference` `verify-broken` `structural-change` |
| G5 | 死代码单调性 | `gate/G5/dead-code-increase` |
| G6 | git 提交失败→回滚树+诚实 aborted 裁决（提交先于记账：绝不留 DB=committed 却无 git 提交的假象） | `gate/G6/commit-failed` |

外部信号缺失/超时一律诚实降级（skipped / 诚实记录），不伪造结果。

## 测试

```powershell
python -m pytest -q                        # 快速层 2424 例 ~4-8min（默认排除 deep）
python -m pytest -m deep -q                # 深度层 1529 例 ~15-30min（真实工具全外链 E2E）
python -m pytest -m "deep or not deep" -q  # 全量 3953 例（阶段性完工验收）
```

深度层含：双仓基准、序贯事务 E2E、闸门假阳性性质、fuzz、字节级复现、三语言变异执行 E2E。

## 文档（docs/）

阅读路径与状态分类见《docs/README.md》；逐篇一句话定位如下（设计动机/实施记录见《交接文档.md》对应条目）。

**docs/ 根（接手即读）**

- 《产品目标.md》——定位与成功判据
- 《交接文档.md》——环境/基准数字/代码地图/批史（append-only）
- 《踩坑历史.md》——实测坑与纪律（pitindex 防复发索引数据源）
- 《glossary.md》——术语表（fid/remap/drift/verdict/OWNED_TABLES 等）
- 《理论出处.md》——概念↔经典书/论文映射

**契约/**——AI 生产方必读

- 《契约/事务契约.md》——事务 schema/G 码对照/生产指南
- 《契约/梳理工件契约.md》——slices/structure schema 唯一权威
- 《契约/schema版本承诺.md》——facts.db schema 版本政策与迁移链

**构想/**——定稿与候选池

- 《构想v6-核验后S1-S5定稿.md》——S1-S5 定稿（历史）
- 《构想v7-AI梳理层与S7-S8定稿.md》——主流程当前权威
- 《构想v9-融合报告形态.md》——展示层定稿候选（原型待拍板）
- 《构想v10-拓展性候选与轮子调研.md》——候选清单 I（18 项）
- 《构想v11-拓展性候选与轮子调研II.md》——候选清单 II（25 项）
- 《构想v12-拓展性候选与轮子调研III.md》——候选清单 III（28 项）
- 《构想v13-参照调研优化候选.md》——参照调研提炼（11 项，AA/BB/CC 系列）

**设计稿/**——立项→实施锚点

- 《设计稿-AA1AA2-MCP消费面改造.md》——已实施 #199
- 《设计稿-B4-测试覆盖对账.md》——已实施 #203
- 《设计稿-F15BB3-查准查全基准与Golden审计矩阵.md》——已实施 #204
- 《设计稿-BB1V17-调用边置信度分层与schema迁移矩阵.md》——已实施 #205
- 《设计稿-C9-修复提案闭环.md》——已实施 #206
- 《设计稿-A1-架构腐化时间线.md》——待批
- 《设计稿-C7-混仓分治执法.md》——待批
- 《设计稿-J8-ETL增量化.md》——待批
- 《设计稿-J9-S2病理引擎并行.md》——待批
- 《设计稿-M16-curate工件三方合并.md》——待批
- 《设计稿-R2-co-change隐式耦合边.md》——待批
- 《渲染层方案.md》——archify 渲染层（未实施）

**调研/**

- 《参照调研-codegraph双仓.md》——codegraph 双仓学习报告
- 《优先级总表-44项重排.md》——剩余 44 项排期定稿（排期≠批准）
- 《跨项目实测报告-2026-11-10.md》——15 个本机项目全链实测

**评估/**

- 《A-B评估协议-防侧信道.md》——A/B 评估方法论（防侧信道）
- 《SARIF-CI消费指南.md》——外部 CI 消费 vrp SARIF 指南

**治理/**

- 《Checklist-并发归并确定性.md》——新并行代码点必过审查项
- 《vendored-上游跟踪规范.md》——外部组件上游跟踪 SOP
- 《第三方许可登记.md》——license 全清单+兼容性结论
- 《致谢与轮子来源.md》——致谢+轮子学习方法论
- 《竞品对照矩阵.md》——与 SonarQube/CodeScene 等定位对照

## 外部工具（可选，缺失则降级）

CBM（索引）、code-atlas（交叉验证/MCP）、ast-grep、mutmut 2.4.5 / Stryker 10.1.1 / cargo-mutants（变异）、difftastic 0.72.0（结构 diff）、knip / vulture / jscpd、repomix（rawpack）、[archify](https://github.com/tt-a1i/archify) v3.2.0（S8 渲染）。二进制沙箱化于 `work/`，路径可用环境变量覆盖（`VRP_*_BIN` 等）。

- 重型子进程默认低 CPU 优先级（不抢交互前台；`VRP_CHILD_PRIORITY=normal` 恢复）
- Stryker worker 孤儿进程自动收割（Windows）

## 发布

开发与测试全在本地完成；GitHub（`AH23333/VRP`）仅作 push 与 release 目标，**不做 CI/Actions**。

- **本地门禁**：`.githooks/pre-push`（`git config core.hooksPath .githooks` 激活）——推送前快速层 2424 例全绿才放行（~4-8min，慢环境留足超时）；跳过用 `git push --no-verify`
- **发布**：GitHub release API 打 tag 发版；当前 **v0.1.3**（2026-11-25）

## 平台

主开发环境 Windows + PowerShell 5.1；Python ≥3.11；产物尽可能落 D 盘。
