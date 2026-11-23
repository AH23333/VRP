# vrp — Vibe 项目重构分析管道

面向通用 AI 的 vibe 项目重构分析管道：把 AI 堆出来的仓库变成**有事实基础、可验证、可执行重构**的对象。

- **主用户：通用 AI agent**（不绑定特定 Agent/MCP 框架）——产物是文件与数据库，不是服务
- **辅用户：vibe 开发者（人类）**——两条消费线分开：AI 消费深层数据（facts.db/MCP），vibe 开发者消费 report/chain 用户产物（AI 梳理职责/层规则/病理体检报告），目标是「理解项目、判断梳理意见、做决定」
- 纯 Python 标准库（≥3.11，零第三方运行时依赖），全管道 CLI 子进程化、可审计可重放

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

核心产物：`<repo>/.vrp/facts.db`（规范化事实层 + 注解宿主）与 `dossier.json`（信息架构装配）；重构事务（T1 重命名/T2 删死码/T3 建调用方/T4 内容微调）在其上声明、验证、执行。

## 安装

```powershell
pip install -e .                       # 纯标准库，无第三方依赖
$env:CBM_CACHE_DIR = "D:\cbm-cache"    # CBM 缓存目录（必须显式设置）
```

零安装试用（U13）：`python -m vrp.build_pyz` 确定性构建单文件 `dist/vrp.pyz`（同源字节级同 pyz；自解压+re-exec——sha256 键控解压缓存 `~/.vrp/pyz/`（`VRP_PYZ_CACHE` 可覆写），仓内 13 处 `__file__` 资源点零改动、单代码路径），`python vrp.pyz --help` 即用（Python ≥3.11，无需 pip install；配合 Release 分发可做）。

### 环境自举（work/ 工具沙箱重建）

外部工具全部钉版本收录在 `bootstrap.py`（纯标准库）：六个 npm 沙箱
（ast-grep/knip/jscpd/Stryker/repomix/code-atlas）+ difftastic 0.72.0 下载解压 +
pytest/mutmut/vulture 及深度测试依赖。`work/` 不入 git（~600MB），删掉后一条命令重建：

```powershell
python bootstrap.py                    # 幂等：已装则跳过
python bootstrap.py --skip-npm --skip-pip --skip-difft   # 只看就绪状态报告
```

前置：Python ≥ 3.11、Node.js + npm、git。系统级手动安装项：
[CBM](https://github.com/DeusData/codebase-memory-mcp/releases)（S1 索引核心，0.12.1）、
Node.js。`cargo-mutants` 可选（`--with-cargo-mutants` 开关，需 Rust 工具链）。

## 快速开始

```powershell
vrp run <repo>                          # S0→S1→ETL→xcheck 全管道
vrp run <repo> --watch                  # I7 变更自动重跑（轮询 mtime+防抖；前台循环 Ctrl+C 退出；J8 增量链让重跑廉价）
vrp s2 <repo>                           # 病理检测（findings + 差异登记簿）
vrp mutate-run <repo> --budget N        # 变异执行（Python/TS/Rust 三引擎）
vrp dossier <repo> --task T1            # 五视图装配（T1-T4 预算）
vrp transact <repo> tx.json             # 重构事务（声明→闸门→原子提交或回滚）
vrp transact <repo> tx.json --preview   # diff 预览（difflib+difftastic 工具组，无闸门不改树）
vrp verdicts <repo>                     # verdict 历史（open 项自动回馈进 dossier evidence）
vrp curate <repo>                       # AI 梳理工件验收入库（.vrp/ai/ slices/structure；--reject 用户否决留痕）
vrp curate-auto <repo>                  # 内置 LLM 梳理循环（零 agent 路径）：配好 .vrp/config.json 的 API key 即自动跑 dossier→slices→structure→enforce→drift（契约 §6；全自动+全留痕 .vrp/auto/）
vrp fixpropose <repo> <finding_id>      # C9 修复提案闭环（零 agent 路径）：单 finding 单文件 → LLM 产 transact 草案 .vrp/proposals/fix-<n>.json（确定性校验器拒收回执重试≤3；单文件不可修→unfixable 诚实转义；草案永不执行——执行只走 vrp transact 闸门；--force 覆盖已审草案）
vrp drift <repo>                       # 梳理工件漂移查询面（只读）：stale 原因 + fid_ledger 反查重定位候选 remap；stored active 实际漂移 → rc=1；--history N 追加最近 N 条复验历史事件（drift_history 表，A3 数据积累面）
vrp remap <repo>                       # 漂移恢复半自动环：drift 的唯一候选 remap 一键写回 .vrp/ai/*.json（unresolved 诚实上报 → rc=1；再跑 vrp curate 复活）
vrp curate-refresh <repo>              # #225 确定性 fid 重映射（remap 的 in-DB 一步兄弟）：structure/slices 死 fid 经 fid_ledger qn 稳定键重映射直刷入库（content churn 同项目 qn 正查 + 项目名派生变更 qn 剥旧前缀换当前前缀桥——remap/drift 面未覆盖的根因）；语义原样仅 fid 指针刷新；全量重验零旁路；不可重映射诚实剔除计数、空模块剔除、全灭拒做；收据 .vrp/curate-refresh-receipt.json
vrp refresh <repo>                     # 漂移恢复一键环：S0→S1→ETL→xcheck→drift→remap→curate→report（代码变更后工件全链路自动追平）
vrp seams <repo>                       # 缝合线查询面（#143，只读，学 unwind seams）：active structure 模块间跨界边聚合（总数/by_type/top 边样本/未分配与内部计数）——模块依赖图原料（strangler 重建顺序）
vrp propose <repo>                     # 重组切片提议（#143，学 unwind slices propose）：确定性分区草案（顶层目录播种+边权贪心凝聚+平衡帽）→ .vrp/proposals/structure-proposal.json（curate structure 输入格式；不在 .vrp/ai/ 不旁路 curate——采纳=改写后放 .vrp/ai/ 全量重验）
vrp traces <repo> --file f.json [--label L]  # 运行时调用证据导入（#144，学 CBM ingest_traces）：观测调用路径/裸对（qn 键，reindex-proof）→ facts.db runtime_calls 独立表（不并入静态 facts，CALLS 边/视图零动）；per label 整批替换（CBM "whole runs age out together"）；收据带对账三分类（agreed/observed_only=静态漏报信号/static_only=死代码佐证）；写入经 _open_facts_rw（M4 硬闸）
vrp coverage <repo> [--path P ...] [--kind K ...]  # 索引完备性核验面（#145，只读，学 CBM check_index_coverage）：引用 graph 文件前先核验——--path 逐路径 status 六态（unusable/partial/unresolved_calls/skipped/excluded/no_recorded_issue）+recommended_action（exact+祖先目录匹配，cap 128）；无 --path=coverage_gaps 盘点（kind 分布+样本）；no_recorded_issue 非完备证明（clean means no recorded gap）
vrp impact <repo> [--base B] [--path P ...] [--depth N] [--direction D] [--no-tests]  # 变更影响+建议测试查询面（#146，只读，学 CBM detect_changes/test_impact）：默认 worktree 相对 HEAD diff hunk 行区间重叠播种（--base=merge-base..HEAD∪worktree；--path=显式播种跳过 git；零重叠/hunk cap 超限回退整文件播种——绝不静默欠播种）→ 多源 BFS 影响面（CALLS/CALL_REFERENCE，depth clamp 15，行 cap 5000 触顶 saturated 诚实）→ 建议测试（test_linkage 消费按测试文件聚合）；suggested ≠ sufficient——建议面非 gate 面，全量测试由消费方决定
vrp testcov <repo> --file coverage.json [--label L]  # 测试覆盖对账（B4，#203，traces #144 独立表+对账模式第三次复用；学 coverage.py JSON 出口+diff-cover 行区间映射）：导入外部 coverage.py JSON 报告（pytest --cov 生产者外部——VRP 只记录外部观测零依赖红线）→ test_coverage 独立表（file_path 键跨 etl 稳定，per label 整批替换，OWNED 存续）+ test_linkage 静态关联 vs 实际行覆盖对账收据——符号执行判定=body 区间 (start_line, end_line] 命中 executed/missing（def 声明行不计——coverage.py 对未调用函数恒报 def 行 executed，import 执行非测试执行）；五桶：linked_and_covered/linked_not_covered_measured（**「静态说有测试但从不执行」假关联现形**，B4 核心动机）/linked_not_covered_unmeasured（范围信号弱桶不混）/linked_undetermined（单行符号诚实未定）/covered_not_linked（静态漏关联假阴性面）+linked_unresolved 悬挂锚计数；unresolved_files=覆盖文件不在 facts 诚实上报；class 符号 import 体执行记档（Function 语义最准）；写入经 _open_facts_rw（M4 硬闸）
vrp golden <repo> --file truth.json      # Golden 审计矩阵（F15+BB3，#204，CGC 循环完美门反面教训——审计必须对独立 committed 真值，禁止导出与自己比）：golden JSON（format/provenance 非空/fixture 可选/truth 双面/samples cap 5 严格形状守卫）双面审计 facts.db——①violations 面：truth 期望违例多集匹配 s7 findings（kind/file/line 三键，R-AD 违例 payload 守卫提取 importer/imported 条件键，source 过滤多集），missing（漏报）+spurious（误报）双向计数；②symbols 面：truth 期望符号五键（qn/kind/file/start/end）严格精确匹配，近失两侧成对报（scope=truth 文件域有界枚举）；precision/recall 分母零→null 诚实（空数组=期望零有效面）；fixture 节（repo_name+files+symbols 9 元组 seed）一键重建可跑 fixture 仓（写盘 newline='' + seed 库，repo 已存在拒覆盖）；rc 恒 0（审计面不是门面——假信号是发现不是命令失败）；只读 ro URI
vrp enforce <repo>                      # S7 架构执法：curate 入库的 structure 按语言路由执行（.py→import-linter；.ts/.js→dependency-cruiser；.go→go list 原生导入图；.rs→内置 use 解析器；.java→内置 package/import 解析器；.cs→内置 namespace/using 解析器；.kt→内置 package/import 解析器；.c/.cc/.cpp→内置 #include 解析器；.php→内置 namespace/use 解析器；.scala→内置 package/import 解析器；.rb→内置 module/require 解析器；.dart→内置 import 解析器；.swift→内置 Package.swift target 解析器；.zig→内置 @import 解析器；.lua→内置 require 解析器；.m/.mm→内置 #import 解析器），违例落 P9 findings
vrp report <repo>                       # S8 静态报告（.vrp/report/：IR+SARIF+Mermaid+archify html+fusion.html 融合报告（构想v9 V-2：六风格单页总览——仪表盘/叙事/结构图/下钻/双引擎对账/数据切片，vendored ECharts 零外链，chain 同批联动行级跳转 #L{n}，主题令牌与 chain 同源三预设）；R7' 多级颗粒度——archify 图链成功出 chain/（替代 L1/L2），否则 L1 模块页+L2 符号页；archify 缺失降级不阻塞；用户产物信息架构——Bottom line（结论先行一屏健康摘要：findings 计数/top 病理/行动项，archify v3.3.0 explainer 概念吸收，节标题稳定锚点 slug `#pathology-checkup` 等节内导航）/How to read/Project overview（AI 梳理职责+层规则）/Pathology checkup（P 码人话体检）/Deep data 折叠（AI 材料归拢）；报告产物密钥卫生——chain 源码页命中高置信密钥探测器（云 token/JWT/私钥块/URL 凭据）渲染层掩蔽 [VRP-MASKED:type:preview…]（行数不变 #L 锚点保持），命中清单落 .vrp/SECRETS.local.md 本地勿提交；finding 影响面——切片行锚定符号调用图 BFS 直达/传递可达（dc/dx/uc/xc+top 调用方/被调名单），BFS 帽截断下界诚实注记，无锚行注记不可算））
vrp session <repo> begin|end|report     # 全信任模式（§3.3）：初末 facts.db 快照 → 计数 diff + 双 L0 图并排 + verdict 时间线
```

## CLI 命令

| 命令 | 用途 |
|---|---|
| `precheck` | git 快照/语言普查/命令发现 |
| `assess` | T10 迁移前评估（只读，构想v12）：git 快照+census+引擎能力矩阵（扩展名→s7 引擎映射，后缀集合从各引擎模块常量实时取用——单一事实源）+mutate 引擎面（s2_mutate_run 常量同源）+内容仓探针+风险提示（混仓 not-enforceable/非 git/脏树/.h 双引擎归属/大仓/无引擎覆盖）；verdict `run`/`skip`；纯只读不落 `.vrp/`；`--json` 机器读 |
| `graph` | R1 网络科学指标（只读，构想v12）：CALLS/CALL_REFERENCE 加权调用图（桩滤除：空 file_path 与 `<...>`/`>...` 合成桩家族，v_hotspots 同口径）→ 确定性 PageRank（dangling mass 均匀回注）+LPA 社区检测（无向邻接+fid 排序异步更新+字典序平局——无随机数，收敛即稳定）+active structure 模块 Jaccard 对账（"AI 梳理 vs 图论社区"交叉验证，分歧即发现）；诚实局限：LPA 无模块度保护（输出 lpa_rounds/社区数供人判读）；ro_connect 不写库；`--json` 机器读 |
| `findings` | S7 反馈环降权（构想v12 亮点）：finding 排序视图（**基础序=S6 评分降序**（#179 接入），零分量 fixture 自然退化 id 序）+`--dismiss <id>` 反馈手势（decisions 表留痕 kind='finding'/decision='dismiss'，findings 行零改动）——按 kind 类聚合历史 dismiss，weight=0.5^类次数 调序；排序 (类 dismiss 次数升序, score 降序, id 升序)；**红线：降权不删除**（finding 永远在列表，只调序+留痕原因）；`--limit` 截断展示（summary.total 恒真）；只读排序 ro_connect/写手势走 freshness 硬拒；`--json` 机器读 |
| `score` | S6 finding 优先级评分（构想v12 甜点偏后，学 SonarQube 评分维度、造开放公式不学黑盒）：score = w·impact + w·evidence − w·span——impact=log1p(锚入度)（edges dst 计数，v_hotspots 入向信号同源）、evidence=log1p(同锚独立 kind 数−1)（**第二类信号起才是交叉佐证**，DISTINCT kind 防同类重复抬高）、span=log1p(修复行面) 成本在分母取负号；每条 finding 输出全部分量明细（components 人可手算复验——公式开放可解释红线）；默认权重 {impact 1.0, evidence 0.5, span 0.25} 可 `--weights` JSON 覆盖（未知键诚实拒绝）；聚合单遍预取零 N+1（25 万边大仓一次 GROUP BY）；排序 (−score, id) 平局 id 纯确定；只读 ro_connect；`--limit` 截断 summary.total 恒真；`--json` 机器读 |
| `complexity` | R3 复杂度度量族 v1 py/ts（构想v12 甜点偏后，py/ts 先行验证形态）：圈复杂度 CC 学 radon（1+决策点，嵌套函数独立成条复杂度归自己）+认知复杂度学 SonarQube 白皮书简化口径（嵌套惩罚 (1+depth)/elif-else 平级 +1/逻辑运算符每个 +1/直接递归 +1——docstring 即声明）；MI/Halstead **诚实不做**（SEI 公式需 Halstead 全统计，先攒案例宁缺勿错）；py=stdlib ast、ts=沙箱 typescript 真 AST（`_TS_SCAN_SRC` JS 扫描器内嵌模块常量（build_pyz 启动器先例）+临时 .mjs+node，沙箱缺 typescript 诚实降级 unavailable，py 面照常）；CC>10 告警线（radon/lizard 惯例）exceeds 列表；argv 150/批分批（WinError 206 实录）；只读纯文件面**不碰 facts.db**（pathology 面/ETL 接入待形态验证后立项）；`--cc-threshold`/`--limit` 展示面（summary 恒真）；`--json` 机器读 |
| `pitindex` | F16 坑→测试防复发索引（构想v10 甜点，#148 自检方法论延伸）：解析 docs/踩坑历史.md 编号条目提取 `test_*` 锁定引用，对 pytest 收集面自动生成"坑→锁定测试"反查表——**GAP**（坑无测试锁=防复发缺口，待办信息）/ **DRIFT**（引用的测试已不存在=失锚红面）分开呈现；锁定判定双面：测试名（参数化 `[case]` 前缀归并）∪ 测试文件 stem（模块级引用）；路径提及（`/` 前缀、`.py` 后缀）排除——首跑 dogfood 8 条外仓路径提及假阳性辨伪实录；收集面 `-m ''` 覆盖 `-m 'not deep'`（坑 token 含深层测试）；报告面 rc 恒 0（缺口是信息不是错误）；确定性不写文件；`--json` 机器读 |
| `dossier-diff` | A2 dossier diff 报告（构想v10 甜点，release notes 自动素材——v0.1.2 手工做的活）：两份 dossier.json 递归 dict/list 对账 → 新增/消除/变化清单（ADDED/REMOVED/CHANGED 分组呈现）；**列表身份键对齐**（dossier 列表多为带身份键 dict 数组——hotspots symbol/dup_clusters fid 等，按身份键偏好序 id/fid/name/symbol/path/... 对齐，头插病理只报 added 不产生整列假 changed，键冲突取首不猜）；无公共身份键回退逐位对齐；`generated_at` 时间戳排除（纯噪音）；deepdiff **不引入**（dict 递归对账纯 stdlib，jd/json-diff 输出形态参照）；坏 JSON/非对象顶层诚实退出；报告面 rc 恒 0；纯函数零 IO 副作用不写文件；`--json` 机器读 |
| `scaffold` | D10 strangler scaffold 生成器（构想v10 甜点，propose 的自然延伸——从"提议"到"可执行的迁移计划"；Martin Fowler strangler-fig 模式权威，hygen/plop 模板引擎形态参照，**造**：string.Template 纯 stdlib 渲染，Python 先行 21 引擎后批）：读 `.vrp/proposals/structure-proposal.json`（fid join facts.db）产出三样到 `.vrp/scaffold/`——①模块骨架 `<safe_id>/__init__.py`（docstring 直引 [PROPOSED] name/responsibility+迁移 TODO 符号清单 name/label/来源文件）②re-export shim 模板 `shims/<old>__to__<safe_id>.py`（Function/Class 模块级可导出面，Method 随 Class 走诚实排除；同旧文件多模块认领=每对一 shim）③`MIGRATION.md` 迁移序=Fowler **先边缘后核心**（跨模块边数（SLICE_EDGE_TYPES 单一权威）升序，平局 module_id 字典序）+逐模块检查单；**不碰用户源文件**（shim 是模板，采纳=人移文件）；module_id 含 `/`/括号→包名安全形；`--proposal`/`--out`/`--json`；确定性无时间戳；`--json` 机器读 |
| `tour` | T11 代码导览报告（构想v12 甜点，curate 消费面新增导览模板——curated 数据已有叙事模板不同，与 v10-E14 skill 区分：输出产物非操作协议；VS Code Code Tour 步骤化+Diátaxis tutorial 教学序形态参照）：面向新人的模块导览——每模块一句话职责（curated `structure` active 载荷 name/responsibility 人写语义单源直引，active 优先于最新非 active）+建议阅读顺序（**教学序=由浅入深**：层拓扑序（`layers[].may_depend_on` 方向——被依赖地基先读，Kahn+字典序 tie-break，环残余字典序尾附防御呈现）→层内符号数升序（小模块先读）平局 module_id 字典序）+具体锚点（符号数/主要文件 top3 按符号数降序/代表符号 top5）；structure 缺失诚实退出（先跑 curate）；**诚实披露（#215 dogfood 修复）**：非 active structure 回退（status/created_at 上浮）与全空 fid 解析（skeleton-only）两情形 render 输出响亮 WARNING——谎言变披露的 caveat（rc 仍 0 报告面语义）；Step N 步骤化正文人读呈现；`--html`（#223 archify v3.3.0 story #1036 概念吸收）opt-in 写静态 HTML walkthrough 页 .vrp/report/tour.html（顶部步骤导航页内锚点 #step-N+断锚强校验同守；与 render_text 同数据同诚实面——双 WARNING 原样进 HTML；opt-in 唯一写面，默认路径仍只读零写入不变量）；确定性无时间戳（同 facts.db 同字节）；`--json` 机器读 |
| `curate-merge` | M16 curate 工件三方合并（学 git merge base/ours/theirs 语义权威；OT 冲突分类参照：非冲突自动合、冲突留人工）：两代理各自梳理同一仓的冲突检测+合并——theirs=外来产物（.vrp/ai 形态目录或单文件，复用 curate 输入面）、ours=库 active 面、base=新表 `curate_base` 覆盖前快照（ingest 自动维护，OWNED 存续；「最近一次被覆盖前的样子」非严格 LCA，误报只偏保守）；slices 按 slice_id 六分支三方、structure 字段级三方（modules by module_id/layers by name 并集+合并后全量校验兜底不过整体降级冲突）；冲突=decisions 留痕（kind=curate_merge，同冲突重跑幂等）+库保 ours 不写（**零静默丢**核心保证）；坏载荷先全量校验零写入即拒（R-B 同语义）；合并后 F2 复验照跑；rc=1=有冲突待裁决；`--theirs` 必填、`--json` 机器读 |
| `curate-refresh` | #225 确定性 fid 重映射写路径（`vrp remap` 的 in-DB 一步兄弟——remap 走 .vrp/ai 文件环+`vrp curate` 两步，本命令库内直刷一步；F1/F3 drift remap 提议的执行面）：fid 轮换两大根因均经 `fid_ledger` qn 稳定键桥接（qn 跨 etl 稳定）——content_hash 变（同项目 qn 正查）与**项目名派生变更**（qn 剥 ledger 旧项目前缀换当前前缀再正查，remap/drift 面未覆盖的根因，自仓 2026-11-16 起 0/225 全灭实锤）；语义原样（模块名/职责/层/slice 步骤角色/文案）仅 fid 指针刷新；**验证零旁路**：重映射载荷走 `_validate_structure`/`_validate_one_slice` 全量重验才入库；诚实面——不可唯一重映射 fid 剔除计数（dropped）、空模块剔除（modules_dropped）、全灭拒做不落库（failed）、跨时代双 fid 撞键去重（deduped）、slice 缺边跳过原样保留（skipped+reason，收据可查）；structure/slices 独立处理（slices-only 仓 structure missing 不拦）；只动 active/stale（rejected 已裁决出列）；fresh 幂等不重入库；收据落 `.vrp/curate-refresh-receipt.json`+drift_history 记 `curate-refresh` 事件；MCP `curate_refresh` 工具同权；rc=0 有成果（refreshed/fresh 任一）非 failed |
| `cochange` | R2 co-change 隐式耦合边（构想v12 亮点，挖掘+呈现一体）：`git log --no-merges --name-only` 共现挖掘（code-maat/Tornhill temporal coupling，公式开放：support/confidence(a→b)=support/changes(a)）→ 独立 `co_changes` 表（file_a<file_b 字典序 PK，路径键跨 etl 稳定，无时间戳列——同 HEAD 字节级同产物）；幂等 DELETE+INSERT 全量替换；OWNED 跨 etl 存续（git 历史不变不重挖）；`--min-support`/`--min-confidence` 只过滤展示面（表存全量对，默认 3/0.3）；`--code-only` 呈现过滤（剔除任一侧为非代码制品的对——#215 dogfood 文档对噪音（README↔文档共现主导）+#216 本机样例战役制品对噪音（archify gallery.html↔manifest.json 155/excalidraw package-lock.json↔package.json 344 锁文件/数据/打包产物机械共变）；黑名单后缀集（文档 .md/.rst/.adoc+数据/配置 .json/.xml/.yml/.toml/.lock+资产 .svg/.png+打包 .zip 等）——白名单误伤陌生代码面更痛；表存全量不变）；阈值后幸存数诚实计数；叙事定位：CALLS 显式依赖 vs co_changes 隐式耦合——两图分歧即发现；CBM history_coupling 链路零动；`--json` 机器读 |
| `timeline` | A1 架构腐化时间线（构想v10 中期硬核，"这个仓库哪天开始烂的"——学 Tornhill hotspot=chg×complexity 度量定义+造回放调度）：`git log --no-merges` stride 采样（默认 `--samples 20`，恒含首尾 commit，溢出丢尾前点保端点）→ 单 detached worktree 串行推进（系统 temp，finally remove+prune——用户工作树零接触红线）→ 每样本 CBM 索引（专用名 `vrp-timeline-<name>` 不碰主缓存库，运行毕删除）→ J8 增量 ETL 链（链式 facts.db 相邻样本 content-hash 命中跳过重解析，#190 是成本前提）→ 步间度量十字段（files/loc/symbols/edges/edge_density/dead_code_candidates/births/deaths（fid 集差分，样本1 births=全部）/scc_count（edges file→file 投影迭代 Tarjan size>1，自环不算）/hotspot_top10（chg×cc，Python-only 缺席记档））→ `.vrp/timeline.json` 确定性静态件（git 历史+参数的确定函数，固定键序无 wall-clock 字段——同仓同参数字节级复现）+ 转折点文本叙事（死代码/环数/耦合最大正向跳变附 commit 对与日期）；错误契约：非 git 仓/无 commit 响亮 RuntimeError（静默空曲线是说谎）、单样本失败中止全回放不跳样本（跳样本=曲线缺口说谎，增量链断不可续）、<2 样本单点输出+明示；loc 缓存键 (path, wt_hash) 相邻样本零重读；`--cbm-name` 覆盖 |
| `review` | T9 PR review 格式输出（构想v12 甜点，学 reviewdog 行锚定+GitHub Review Comments API 载荷）：SARIF→review comments 本地转换器——一条 result 一条 comment（path/line/side=RIGHT/body，1:1 不聚合——聚合是 S5 立项面），落 `.vrp/report/review-comments.json` 供人贴（**零服务端**：贴不贴贴几条由人定，`gh api .../comments` 载荷形状）；缺 location 诚实跳过计数；`--sarif` 换源/`--limit` 只截断写出面（summary 恒真）；确定性——重跑字节级同产物（无时间戳）；`--json` 机器读 |
| `groups` | S5 finding 同根因聚合（构想v12 甜点，Sentry fingerprint 语义）：指纹=根因符号集——锚定对 (anchor,related) **无序归一**（互环 a→b/b→a 与克隆族同键），无 fid 回退 (file_path,start_line) 键；group_id=sha256[:12] 跨 etl 稳定；**归组不删行**（只读查询面，红线同 S7 降权不删除）；排序 (-members, group_id) 纯确定；member_ids cap 10（summary 记 cap 恒真）；`--limit` 截断展示 summary 恒真；确定性无时间戳；S6 评分/dossier 聚合呈现接入各自立项；`--json` 机器读 |
| `profile` | R4 仓库数字画像（构想v12 甜点，census 统计深化，纯 stdlib）：三面分布 Gini（入度/出度/文件符号数）+集中度 top1%/top10% 累计占比（**幂律拟合的诚实替代**——不做 CSN 拟合，note 字段明示）+边密度（n<=1 诚实 0.0）+扇入扇出枢纽 top5（(count DESC, fid) 纯确定）；只读查询面（ro_connect 行零改动）；确定性无时间戳；`--json` 机器读 |
| `doclint` | P23 活文档防漂移（构想v11 亮点首推，自家 test_docs_drift 守卫产品化，原创无轮子）：markdown 内 `<!-- doclint {...} -->` 声明式标记（HTML 注释内嵌 JSON，渲染不可见）声明可证伪数字断言，真值源（glob/recount/pytest-collect 三种只读源）重算对账——**文档说谎检测器**；rc=1 on drift；零写入只读面 |
| `doctor` | I4 工具链环境自诊断（构想v11 甜点，npm doctor/rustup doctor 形态）：聚合各适配器已有 find_* 发现函数（单一事实源，不重写探测逻辑）分两层报告——core（python/git/node/CBM，缺失 rc=1 阻断管道）+ engine（21 引擎外链+scc，缺失只报能力降级不判红）；git/node 廉价 --version 探针（run_tree 15s），python 报 sys 实况，其余只报定位路径（版本钉位归 bootstrap.py，doctor 不做第二真值源）；无 repo 参数，只读 |
| `explain` | I5 错误码人话速查（构想v11 甜点，cargo explain 形态）：三族码全文检索——gate 闸门码（单源直引 s6_transact.RULE_CODES，G0-G6 含 G4 五信号）/curate 收据拒码 SLICE_*/STRUCT_*/FILE_*（AST 抽取 curate/auto_curate 源码全串常量，新码落源即入表不建第二登记处，守卫双向锁人话覆盖）/P 病理码（直引 render.PATHOLOGY_LEGEND 零复制）；无参三族总览，前缀匹配大小写不敏感，无命中 rc=1；无 repo 参数，只读 |
| `perf` | J10 管道性能回归追踪（构想v11 甜点，rustc perf tracker 思想）：`vrp run` 四阶段（precheck/cbm_index/etl/xcheck）monotonic 计时自动落档 `.vrp/perf.jsonl`（零额外命令记录）；`vrp perf` 报 last vs previous 逐阶段 delta，回归旗标双门=相对门 +25% 且绝对门 +1.0s（小基数噪声免疫）；单样本对比 noise note 明示（hyperfine 会取 N 轮中位）；rc 恒 0（报告面不是门面）；只读查询面 |
| `relnotes` | K11 release notes 半自动生成（构想v11 甜点，changesets 累积模型）：commit 域 `#NNN:` subject × 交接文档条目交叉核对（commit 有码交接无条目=漂移警告）；**实时核码 #153 教训落实**——测试计数调 doclint pytest-collect 活数、执法语言 glob 活数、commit 数 git 活数，不信摘要快照；产出 markdown 草稿 stdout 人审后贴 release（半自动边界明示）；date 注入同输入字节级同草稿 |
| `smoke` | K12 跨仓冒烟一键化（构想v11 甜点，发布纪律脚本化——v0.1.2 是手工跑的）：verify/ 含 .git 子目录自动发现（无注册表，新验证仓落目录即入面）逐仓全链 S0→S1→S1.5→xcheck+symbols/edges/findings 计数汇总；content-repo skip=pass（P-2 语义）；阶段异常/零符号/facts 缺失诚实 FAIL 带详情；`--skip-run` 快面=freshness（M4 陈旧=FAIL）+计数；`--only` 锁名（发布面：双主力仓）；rc=1 on any FAIL |
| `gc` | L14 CBM 缓存 GC（构想v11 甜点，npm cache clean 治理形态，三段式）：默认审计=cache_dir()/*.db 逐库溯源（store_meta/vrp_repo_path 章 H3 单源复用）+仓存活核对+体量/节点数；`repo-missing`（章指向已不存在目录）=唯一自动候选；unknown-source（无章旧库）/unreadable（无 db_uid 非法库）报告不自动收（#77 盲删教训）；`--dry-run` 列 would_delete（含 -wal/-shm 边车）+would_free_bytes；`--yes` 真删（失败逐件记录 rc=1）；dogfood 真仓 62 库审出 4 候选 11MB 已清 |
| `flakes` | L15 flake 登记表（构想v11 甜点，GitHub flaky bot 形态借鉴；pytest-rerunfailures **不引入**——重试即掩盖，只记录不掩盖）：首观测记交接 prose 不进表，复发进表 count+1；count>=2=抖王名单→隔离候选；validate() 漂移守卫（active 条目测试文件+函数必须在树，改名未同步=红 rc=1）；判例入册：fd0d7db rawpack 过断言"复发优先找根因，定案缺陷修不进表"+testsig rust 环境瞬态"单跑绿不修只记档"+#170 push"门禁红先查环境负载勿急修代码" |
| `langmatrix` | N18 语言能力矩阵（构想v11 甜点，tree-sitter README 语言支持表形态，#153 纪律全量落实——**每格实时核码生成零硬编码语言清单**）：行=21 s7 引擎语言（assess 单源）∪ indexed-only 诚实行（GDScript——认知无引擎 #124 先例）；列=五逐语言面各从属主模块派生：astgrep 规则 yml language 字段→EXT_LANGUAGES 反查→引擎后缀交集归行（js 规则归 ts 行 9 规则不失踪）/mutate 三引擎（mutmut/stryker/cargo-mutants）/deadcode-p4（knip/vulture 门控镜像+锚点锁）/testsig 自建面；js→ts、C++→c 归并注记；语言无关面（jscpd/S3/S5/dossier）注记不入逐语言列；rc 恒 0（缺口是待办非错误）；缺口清单即补齐 backlog（astgrep 19 语言/mutate 18/deadcode 19） |
| `mdcode` | N20 Markdown 代码块提取（构想v11 甜点，doctest 哲学——文档示例必须可信；litprog 学思想造实现）：fenced block 语言标注→提取器路由（py=stdlib ast 真解析（ast.walk BFS 按 (lineno,name) 排序恢复文档序）；ts/js=正则轻量五形状（function/class/interface/type/const-arrow）——树库不引边界记档）→符号（name+类别）与 facts.db symbols（name+label）比对→漂移候选两类：doc-only（db 无同名——改名/删除/虚构 API 嫌疑）+kind-mismatch（仅 py 清晰面 def↔Class（或反向）；Method≈Function 同族不误报；ts label 词表未钉 name-only 诚实边界）；报告非门（langmatrix 先例 rc 恒 0——漂移是发现不是失败）；name 级比对（参数级签名不在 facts.db——content_hash 是摘要非签名）+同名碰撞可能记档；无语言标注/未认知语言块不路由不猜（plain 计数）；排除目录复用 watch.EXCLUDE_DIRS 单源 |
| `argcheck` | N19 参数级对账（构想v11 探索→#217 schema v3 前置解锁→#219 本体→#220 跨语言 HTTP 面，OpenAPI 契约对账思想——调用点实参对被调方签名的漂移检测）：caller 面=CALLS 边 properties_json args[{i,e,k,v}]（CBM 原样）×callee 面=symbols.signature（**签名字符串是唯一对账基准**——CBM param_count=类型标注参数计数，dogfood 实测 ~21% 与签名字符串不一致+builtins 占位 0，皆参考值非基准）；裁决三类：too_few（实参<必选数）/too_many（实参>声明总数）/unknown_kwarg（kwargs 键名∉签名参数名）；区间语义 required≤n_args≤total（默认值/ts-?参数可选省；*args 消除上界+**kwargs 消除键名查——变长容忍）；Method 首 self/cls 剥离；builtins.*/无签名 callee 跳过计数不猜；显式空 args[] 参与对账（零参 vs 必选>0=真 too_few）、args 键缺席跳过；报告非门（mdcode/langmatrix 先例 rc 恒 0）；dogfood 实测 9794 checked/93.6% ok/6.4% 候选（`_report_html` 8/9 参、`gcf.dumps` 4/1 参真漂移候选；echarts.min 压缩资产面=上游 CBM 索引噪声记档非修复面——#216 ANY 路由噪声家风）；**HTTP_CALLS 跨语言面（#220）**：轴 1 同构 args×签名（真链实测弱面——dst=fetch/axios 等客户端库函数多无签名，82/82 skip 诚实记档）+轴 2 endpoint inventory（url 归一剥 query/hash/尾斜杠、method upper 归并；同 url 多 method=REST 常态盘点非漂移——restruct-music-player 真链 5 multi-method urls；method 缺席=fetch(url) 无 init 真链 45/82 诚实 skip；全 URL 匹配不同服务不合并）；输出双节 calls+http（json 形状 {calls:{...},http:{...}}） |
| `etl-health` | BB4 ETL 健康面（构想v13 甜点，codegraph IndexHealth #2336+CGC repair BUG-005 判定学——健康字段与不变量清单照学，自愈不照抄：owned 表是重建派生面（同 facts.db 字节级同产物红线），修复=重跑 vrp run，本命令只检测+建议）：查询式不变式清单（镜像 deep ref_integrity 断言集作运行时纵深防御层——fid 唯一/六向零孤儿（edges src+dst/usage_hints/test_linkage）/零自环/8 元组全行去重/**symbols_stale_files**（CGC BUG-005 VRP 对偶——符号声称的文件不在 J8 etl_file_state 快照：文件已删或 CBM 索引滞后，facts 陈旧信号；空 file_path 豁免——内建/合成符号合法无文件））；schema 陈旧（表缺失）不炸诚实报告（dogfood 实录：J8 前 facts.db 无 etl_file_state 表→报"重跑 vrp run"而非 traceback）；rc 0 健康/1 违例（健康检查语义 CI 可用） |
| `xcheck-explain` | Q25 xcheck 差异叙事化（构想v11 甜点，rustc --explain 叙事结构：现象→原因→修法；v11 自注"先收集真实困惑案例再模板化"前置已满足——xcheck.py:103 实录 27.7% agreement 真实案例）：只读 facts.db discrepancy_ledger 四 category 计数+top-3 示例（rowid 序确定性）+meta xcheck_* 汇总→逐类人话模板（ca_only=目标未被 CBM 索引诸因/ca_only_builtin_target=内建 CBM 刻意不索引/cbm_only=usage 弱证据或覆盖差异/cbm_only_test_source=测试源面排除）+总述行（agreement<60% 标 LOW）；xcheck 未跑过（ledger 空+meta 无 xcheck_agreement_pct 键）→诚实提示先跑 vrp xcheck；rc 恒 0（解释面非门） |
| `layermetrics` | C8 层度量与阈值（构想v10 甜点，JDepend D 度量公式+deptrac 阈值语义判定学——规则仍走 structure JSON 声明不引 DSL）：per-module/per-layer **fan_in（Ca）/fan_out（Ce）= 模块间去重边对**（DISTINCT (src,dst)——类依赖语义非调用次数；多属 fid 模块对全组合 s7 先例；structure 符号宇宙外排除——内建噪声地板）+instability I=Ce/(Ca+Ce)（孤立 scope I=None 诚实未定义）；**abstractness 不可推导诚实记档**（facts.db 无修饰符数据——BB2 同源边界；D=\|A+I-1\| 半面不造 A）；thresholds=structure payload 可选顶层键（curate 不拒未知键纯数据面）`{"modules"/"layers": {name: {max_fan_in/max_fan_out/max_instability}}}`；违例 rc=1（CI 可用）/无声明纯度量 rc=0/structure 未 curate 或 stale（s7 同语义只消费 active）诚实提示 rc=0；命名约定规则面缓议（v10 自注"表达力按需扩避免过度设计"） |
| `sweep` | S8 阈值敏感度扫描（构想v12 甜点，scikit-learn validation curve 只学形态不引库）：**离线**重切已落盘 findings payload（零引擎重跑——读侧直方图）→ minTokens 20-200/minLines 5-50 网格×计数（payload 值≥阈值）单调不增曲线+**悬崖点**（相邻网格最大绝对落差——选阈值参考：悬崖右侧=噪音快速衰减区）；jscpd 默认 50 在 tokens 曲线标 current；可扫面诚实边界——仅 jscpd tokens/lines 数值 payload 可扫，规则面布尔命中/deadcode/vulture 无数值阈值字段诚实记档不硬造（NON_SWEEPABLE_NOTES）；坏 JSON/非 int 宽容跳过；报告非门 rc 恒 0 |
| `migprog` | D11 迁移进度追踪（构想v10 甜点，strangler 完成度）：seams 跨界边快照（**单源 nav_seams #143 零重复实现**，只消费 active structure）→ `--snapshot` 追加 `.vrp/migprog-history.jsonl` 审计流（不清理——runtime_evidence/perf.jsonl 追加先例；ts 含在审计流不违反产物确定性红线——facts.db 派生产物不含 ts）；progress=（基线-当前）/基线×100 跨界边收缩% + vs_last 环比 delta_pct（正号=收缩）；基线=history 首条（迁移起点）；零基线 None 诚实未定义/无快照诚实提示/耦合增长负值诚实（迁移倒退可见）；tracking 面 rc 恒 0 非门 |
| `secrets` | H1 密钥扫描独立 CLI（构想v11 甜点，gitleaks 形态——规则组织+全 blob 历史遍历；trufflehog 活性验证**不做**——离线红线，只检测不验证）：检测器**单源** render_chain.mask_source_lines（七型 token/URL 凭据/私钥块零复制+`_ANCHOR_SCAN` 保守超集预筛——无锚行 1 次 search 跳过 10 正则，历史面 459s→12s #178 实录，全谱样张锁兜底漏检）；worktree 面 os.walk+IGNORE_DIRS 剪枝（.vrp 内 SECRETS.local.md 是掩蔽产物不扫）+binary 嗅探/4MB 单文件封顶跳过计数；`--history` 历史面（显式 opt-in）=rev-list --objects --all→cat-file --batch-check 过滤（2MB blob 封顶跳过+行数对账截断诚实红）→cat-file --batch 流式扫描（procs.stream_lines——写线程并行喂 stdin 防真仓双管道死锁 #178+耗尽 rc 诚实红），**已删文件的密钥在历史 blob 仍可发现**，按 blob 批量喂（私钥块状态机 blob 内正确运转）；blob 归位=期望头精确匹配锚定，desync 诚实红；text 报告面连续 private-key-body 折叠区间×N（BEGIN 无 END 偏保守状态机的信噪比治理），JSON 事实面全量不折叠；rc 语义 gitleaks 同型：命中 1 零命中 0（CI 可用）；#176 实录修共享检测器量词上界纪律（scheme 无界 `*` 在 2MB 长单行 O(n²) 假挂→有界 {0,15}/{…,4096} 单遍线性，现实输入语义不变） |
| `index` / `status` | CBM 索引（缓存）/ 状态查询 |
| `etl` | CBM → facts.db 五级证据路由（J8 文件粒度增量默认自动，`--full` 强制全量）；symbols 落 signature/return_type/param_count 三列（schema v3，#217——CBM nodes.properties 提取为可查询列，N19 参数级对账 callee 面；caller 面=CALLS 边 properties_json args 原样转存；迁移链 [2,3] 见 docs/schema版本承诺.md） |
| `xcheck` | code-atlas 四元组交叉验证 → 差异登记簿 |
| `run` | S0→S1→ETL→xcheck 一发；`--watch` I7 变更自动重跑（轮询 mtime（os.walk 快照 mtime_ns+size，产物/噪音目录剪枝——.vrp 不剪即自我触发死循环）+防抖（变更持续窗口滑动，静默 ≥--debounce（默认 1.0s）才触发——编辑器保存风暴不连发；--interval 默认 2.0s）；初始全跑建基线后前台循环，Ctrl+C 干净退出 rc=0；rerun 失败/异常不杀 watch（实时反馈面生存性优先）；J8 增量链（#190）让重跑廉价=成本前提；watchdog/entr/ptw 判定绕——零依赖纯 stdlib；`--reindex` 作用于每次重跑） |
| `s2` | 病理检测：ast-grep 12 规则（9 基础+3 危险 API 族 P5（#209，H3：py-eval-exec 动态执行/py-unsafe-deserialize 反序列化（yaml Loader 关键字下钻排除）/py-sql-concat SQL 拼接（execute/executemany 首参二元 +，双 arity）；规则是数据不是代码，m.eval() 方法调用/Loader=SafeLoader/干净 execute 零误报实核）） + 死代码（BB2 置信度三级 low/medium/high+reason——五类误报源整词匹配分层非静默丢弃，CGC CONTAINS 事故锁）+ knip/vulture + jscpd |
| `mutate-plan` / `mutate-run` | 变异预算采样 / 变异执行（mutmut·Stryker·cargo-mutants） |
| `validate` / `gate` | 锚定+覆盖率校验 / AI claim 证据闸门 |
| `freeze` | 基线命令执行 + graph 快照 + 变更预筛 |
| `dossier` | 七节装配（架构/简报/repomap/五视图/证据/摘要/rawpack 指针） |
| `transact` / `transact-batch` | 重构事务单发（`--preview` diff 预览）/ 目录批处理 |
| `verdicts` | verdict 历史查询（`--tid` 单条详情；dossier evidence 自动内嵌 open 项） |
| `curate` | AI 梳理工件验收（slices/structure 确定性验证全过才入库；`--reject` 否决留痕 decisions 表；复验失效标 stale） |
| `curate-auto` | 内置 LLM 梳理循环（零 agent 路径，构想v7 §13）：`.vrp/config.json` 配 OpenAI-compatible API key → 自动六步（dossier 取材→slices→structure→enforce→drift→summary）；无 function calling（确定性校验器即工具，拒收回执重试≤3）；循环只走 curate 数据面不触重构事务；全自动+全留痕 `.vrp/auto/`（key 不落盘） |
| `fixpropose` | C9 修复提案闭环（零 agent 路径，构想v10 C9）：单 finding（须有 file_path）+现文件全文行号檐口材料 → LLM 产 transact 草案 `.vrp/proposals/fix-<n>.json`（事务契约 v1 载体零新 schema；校验器=load_transaction 复用+三加验 WRONG_ID/WRONG_FILE/WRONG_OP 稳定回执码，拒收回执重试≤3；单文件不可修→unfixable 诚实转义 rc=0）；草案永不执行（C9 红线——LLM 输出物理上只能变成草案文件，s6_transact 引用仅 load_transaction 形状校验，源码守卫测试钉死；执行只走 `vrp transact` G0-G6 闸门，preview→transact 两步）；防覆写（`--force` 显式覆盖）；全自动+全留痕 `.vrp/fixpropose/`（key 掩码） |
| `drift` | 梳理工件漂移统一查询面（只读）：每次 etl/事务 promote/MCP 提交自动复验（F2 假 active 窗口已关）；drift 逐条给 fresh/missing_fids/missing_edges + fid_ledger（fid 身份史，跨 etl 存续）反查重定位候选——qn 未变给 remap 映射，qn 消亡给同文件域候选（cap 10）；remap 是提议不旁路，重提仍全量重验；stored active 实际漂移 → 退出码 1；每次复验落 `drift_history` 历史事件（A3 前置：etl 全量/增量/curate/curate_merge/MCP 五类时刻，git_head 批次锚+统计快照，跨 etl 经 OWNED_TABLES 存续；`--history N` 读出最近 N 条） |
| `remap` | 漂移恢复半自动环：drift 的唯一候选 remap 一键写回 `.vrp/ai/*.json` 工作工件（只改 fid 引用不触 facts.db）；多候选/无候选/文件缺失 → `unresolved` 诚实上报 rc=1；写回后 `vrp curate` 全量重验复活 active |
| `refresh` | 漂移恢复一键环：S0→S1→ETL→xcheck→drift→remap→curate→report（代码变更后 curate 工件全链路自动追平；remap unresolved 或 curate 拒收 → rc=1） |
| `enforce` | S7 架构执法（构想v7 §4/§8.2/§9）：structure(active) → 层 DAG+rules 生成 forbidden 契约 → 按语言路由执行——.py→import-linter（VRP_IMPORT_LINTER_BIN 覆盖）；.ts/.tsx/.js/.jsx/.mjs/.cjs→dependency-cruiser（typescript 5.9.3 钉版沙箱，VRP_DEPCRUISE_BIN 覆盖）；.go→go list 原生导入图（`go list -e -json`，传递闭包 BFS 最短链，go.mod module 指令四形态 mini-parse（裸/带引号/行首缩进/括号块——go 1.26 探针实证，未闭合块拒绝、`//` 注释剥离，BOM go.mod 工具链拒绝真值对齐），根包锚定文件路径无 "./" 前缀，嵌套 internal/vendor 是真实包不计 vendor（仅模块根 vendor/ 跳过=go list 排除对齐），VRP_GO_BIN 覆盖）；.rs→内置源级 use 解析器（纯 stdlib 零工具链依赖，mod 树+传递闭包 BFS 最短链，生产行内 mod use 边计入（use 视图行内体保留+ctx 走查器下钻，self/super 相对路径解析 super=enclosing，边归属性=所在文件模块——rustc 探针实证），macro_rules 定义体跳过（模板非真实边——定义体内 use 不误计、模板 mod 声明不入树），普通串跨行合法（kt/cs/java 未闭合串行截断家族修法不适用——rustc 探针实证），cfg(test) 排除，glob 导入不展开（边指 glob 根——诚实局限），orphan .rs 诚实计数）；.java→内置源级 package/import 解析器（纯 stdlib 零工具链依赖，容器=Java 包，Java 自由格式——package/import 段间点号两侧空白/换行宽容+捕获组去空白（javac 21 探针实证），文本块转义感知扫描（转义引号技巧产出裸三引号不提前闭合——javac/jshell 21 探针实证），未闭合普通串换行截断（JLS 3.10.5 串内禁换行），4 连引号开符缺行终止符走串分支优雅降级，`/test/java/` 测试源根排除，传递闭包 BFS 最短链，orphan 无包文件诚实计数）；.cs→内置源级 namespace/using 解析器（纯 stdlib 零工具链依赖，容器=C# 命名空间，自由格式段间空白/注释宽容+global:: 限定符支持（dotnet 9 探针实证），未闭合普通串换行截断（C# 普通串不得含字面换行——CS1010），空串/转义首字符 off-by-one 防吞码，tests 测试目录排除，传递闭包 BFS 最短链，orphan 无命名空间文件诚实计数）；.kt→内置源级 package/import 解析器（纯 stdlib 零工具链依赖，容器=Kotlin 包，`/test/kotlin/` 测试源根排除，嵌套块注释/raw string/分号可选/`as` 别名词法，声明终止 `[;\r\n]|$`（文件末行无换行不丢），关键字与目标同行（Kotlin 语法该处无 NL 记号），未闭合普通串换行截断（跨行须 raw string），文件首 BOM 免疫，传递闭包 BFS 最短链，orphan 无包文件诚实计数）；.c/.cc/.cpp/.cxx/.c++/.h/.hh/.hpp/.hxx/.h++→内置源级 #include 解析器（纯 stdlib 零工具链依赖，容器=目录（C 无语言级模块系统 include 才是依赖边界），引号目标近似 -I 解析+尖括号系统头不入图，指令记号间隙注释（C 预处理注释=空白语义——间隙注释空白化后不丢 include 边），续行合并，不可嵌套注释/raw string 词法，文件首 BOM（\ufeff）空白化（BOM+行 1 include 假阴防御），test/tests 目录或文件名约定排除，传递闭包 BFS 最短链，锚定链尾 include 真行号）；.php→内置源级 namespace/use 解析器（纯 stdlib 零工具链依赖，容器=PHP 命名空间，use 边 only（deptrac 同口径，require/include/inline FQN 不入图），岛模型词法（HTML 岛外空白化/attribute 非注释/heredoc-nowdoc），引号串（单/双/反引号）合法跨行（换行是字面内容——扫至闭合引号，未闭合病态 EOF 保守空白化），文件首 BOM 免疫（岛模型——BOM 在岛外 HTML 区空白化），namespace 声明名段间空白/注释间隙（合法但病态）不识别，tests 目录段或 `*Test.php` 约定排除（命名空间仍入表），传递闭包 BFS 最短链，锚定链尾 use 真行号，orphan 无命名空间文件诚实计数）；.scala→内置源级 package/import 解析器（纯 stdlib 零工具链依赖，容器=Scala 包，package 三形态（平铺/packaging 块/嵌套链拼接首链，package object 子句排除——`package objectx` 合法包名不误伤），Scala 2 `._`/Scala 3 `.*`/选择器 `{C, D}`/重命名 `{C => D}` import 全形态（import/package 与目标同行——Scala 换行即语句终止、残缺跨行代码不产伪边/伪包；文件首 BOM 免疫），嵌套块注释/三引号串词法，`/test/scala/` 测试源根排除，传递闭包 BFS 最短链，锚定链尾 import 真行号，orphan 无包文件诚实计数；Scala 3 `export` 声明重导出不入图（诚实局限））；.rb→内置源级 module/require 解析器（纯 stdlib 零工具链依赖，容器=module/class 嵌套全路径（compact `A::B`/嵌套 `module A; module B` 同构，`class Foo < Bar` 超类忽略），require/require_relative 声明边（require_relative 相对文件目录/require $LOAD_PATH 近似根，括号实参可跨行、成员访问 `obj.require` 拒、动态 require（插值/变量实参）不入图），heredoc 四形态/% 字面量嵌套定界/正则位置启发式（`=~` 与 if/unless 等关键字尾）/`__END__` DATA 数据节截断/Ruby 3 endless def 不开块词法，test/tests/spec 目录或 `test_*.rb`/`*_test.rb`/`*_spec.rb` 约定排除（minitest+rspec，容器仍入表），传递闭包 BFS 最短链，锚定链尾 require 真行号，orphan 无声明文件诚实计数）；.dart→内置源级 import 解析器（纯 stdlib 零工具链依赖，容器=Dart 文件（每文件默认一 library；part 文件归并 owner 文件），依赖边=import/export URI（package: 就近 pubspec 判包（name 行尾注释容忍——YAML `#` 前空白语义）、dart: SDK 跳过、相对 URI 归一；part 指令非依赖边），raw 串/三引号串（raw 三引号转义不生效——尾 `\'''` 闭引号不被转义吞，整文件假阴防御）/指令关键字-目标间隙注释（行/块注释空白化后扫描继续，URI 不丢）/嵌套块注释/插值词法，test/tests/integration_test 目录或 `*_test.dart` 约定排除，传递闭包 BFS 最短链，锚定链尾 import 真行号）；.swift→内置源级 Package.swift target 解析器（纯 stdlib 零工具链依赖，容器=SwiftPM target 名（Package.swift targets 权威清单——path 实参或默认 Sources/<名>/Tests/<名>；testTarget 测试排除；未闭合 targets 数组截断解析不崩——畸形清单部分降级），依赖边=`import <TargetName>`（@ 属性前缀全形态——@testable/@_exported/@_implementationOnly/@_spi(...) 任意 @ 前缀放行；限定形态 import class/struct/... 取模块段；未声明系统模块跳过），raw 串（# 定界计数）/多行三引号串/嵌套块注释词法，传递闭包 BFS 最短链，锚定链尾 import 真行号，orphan 未归属 .swift 诚实计数）；.zig→内置源级 @import 解析器（纯 stdlib 零工具链依赖，容器=Zig 文件（repo 相对 posix 路径），依赖边=`@import("*.zig")` 相对路径（无 .zig 后缀=包名/命名模块→外部跳过；build.zig 程序化声明不解析；动态 import（comptime `++` 拼接实参）不入图），多行字符串（行首 `\\` 前缀）/行注释（Zig 无块注释）/单行串/字符字面量词法，test 块内 @import 排除（Zig 测试主形态——文件内嵌），传递闭包 BFS 最短链，锚定链尾 @import 真行号）；.lua→内置源级 require 解析器（纯 stdlib 零工具链依赖，容器=Lua 文件（repo 相对 posix 路径），依赖边=require 实参字符串（括号/无括号/level-0 长括号全形态，点转斜杠+repo 根/init.lua 候选近似 package.path，动态 require（变量/拼接实参）不入图），长括号 level 配对/`\z` 续串/贪心 `[[` 长字符串词法，spec/test/tests 目录或 `*_spec.lua`/`*_test.lua`/`test_*.lua` 约定排除（busted/luaunit，容器仍入表），传递闭包 BFS 最短链，锚定链尾 require 真行号）；.m/.mm→内置源级 #import 解析器（纯 stdlib 零工具链依赖，容器=目录（同 C——ObjC 无语言级模块系统 #import 才是依赖边界，@class 前向声明非依赖边），依赖边=引号 #import/#include（尖括号框架/系统头与 @import clang 模块不入图；源文件目录→仓根→header_dirs 解析=等价 Xcode header map 近似），词法底座同 C（文件首 BOM（\ufeff）空白化——Windows 编辑器 BOM 头不污染行首指令词法，BOM+行 1 #import 假阴防御；指令记号间隙注释（C/ObjC 预处理注释=空白语义——间隙注释空白化后不丢 import/include 边，与 C 引擎同型）），test/tests 目录（大小写不敏感）或驼峰 *Test.m/*Tests.m 约定排除，传递闭包 BFS 最短链，锚定链尾 #import 真行号）；.pl/.pm/.t→内置源级 package/use 解析器（纯 stdlib 零工具链依赖，容器=Perl 包（首 package 声明，use/require 目标即包名；引号 .pm 目标 lib/→仓根→源目录三候选），use/require 行首或语句边界后（`;`/`{`/`(`/`=`——条件加载 `if { require X; }`/`$m = require X;` 命中，`->`/标识符后不触发），`#` 注释（$# sigil 守卫）/POD/q·qq·qw·qr·m·s·tr·y 算符（**闭合括号 `)]}>` 不作定界符**——`$h{s}`/`foo(s)` 裸字形态防误触，误触会空白化真实代码）/heredoc 全形态/`__END__`/`__DATA__` 词法，t/xt/test/tests 目录或 `.t` 后缀约定排除（TAP，包仍入表），传递闭包 BFS 最短链，锚定链尾 use/require 真行号）；.erl→内置源级 module/远程调用解析器（纯 stdlib 零工具链依赖，容器=Erlang 模块（OTP 扁平命名空间，首 -module 声明，远程调用目标即模块名免 code path 猜测），依赖边=`mod:fun` 远程调用（fun 侧后随 `(`/`/arity` 才认定——map `:=`/typespec 不误触；多行形态 `mod\n:f(`/`mod:\nf(`/`mod:f\n(` 的 `:`/fun/`(` 间空白含换行——长实参列表换行合法 Erlang）+`-behaviour(mod)`+`-import(mod,...)`（实参可跨行；外部模块 gen_server/lists 等跳过），`%` 注释/`$c` 字符字面量/双引号串/带引号原子原文保留词法，test/tests 目录或 `_test/_tests/_suite.erl` 约定排除（EUnit/Common Test，模块仍入表），传递闭包 BFS 最短链，锚定链尾调用真行号（多行调用=模块原子行），orphan 无 -module 文件不入图、其远程调用行建图时丢弃）；.r/.R→内置源级 source/包引用解析器（纯 stdlib 零工具链依赖，容器=R 包目录（DESCRIPTION `Package:` 声明最近祖先目录，根包收编全部散文件；无包归属散 .R=文件自身），依赖边=DESCRIPTION `Imports:`/`Depends:` 字段声明即依赖（DCF 续行合并/版本规格剥离，`Suggests:` 软依赖不产边，锚 DESCRIPTION 字段行）+source() 字符串实参（文件目录/仓根双候选解析，实参可跨行）+library/require/loadNamespace/requireNamespace（裸词/串/`package=` 命名，实参可跨行——关键字与 `(` 须同行）+`pkg::`/`:::` 命名空间引用（本地包索引命中→包目录，CRAN 外部跳过），`#` 注释含 roxygen/单双引号串/反引号标识符词法，test/tests/testthat 目录或 `test-*.R`/`test_*.R` 约定排除（testthat，容器仍入表），传递闭包 BFS 最短链，锚定链尾直接边（library/source 调用行或 DESCRIPTION 声明行））；.clj/.cljc→内置源级 ns/require 解析器（纯 stdlib 零工具链依赖，容器=Clojure 命名空间（首 `(ns name)` 声明（reader 元数据 `^`/`#^` 前缀跳过）——require 目标即命名空间；无 ns 文件=文件自身可出边不可入边叶可见），依赖边=ns 子句 :require/:use/:require-macros+require/use 函数形态（嵌套 require 递归提取——条件 require 支持）+reader 条件 `#?`/`#?@` 形树级展开（.cljc 分支不区分全分支按序拼接）+load/load-file 串实参（classpath 候选解析），`;` 注释/`#!` shebang/`#"..."` 正则字面量/`\c` 字符字面量（`\"` 防误开串）/`#_` 词法，test/tests 目录或 `*_test.clj`/`*_test.cljc` 约定排除（ns 仍入表），传递闭包 BFS 最短链，锚定链尾 require 真行号，orphan 无 ns 散文件诚实计数）；.ml/.mli→内置源级 module/open 解析器（纯 stdlib 零工具链依赖，容器=OCaml 编译单元文件（repo 相对 posix 路径，模块名=文件名词干首字母大写，引用路径头→模块名索引→命中文件，.ml 优先 .mli），依赖边=open/let open/include+module 别名（具名/括号署名提取）+functor 应用实参（`F(X)` 别名/include RHS 后上下文限定防构造器 `Some (x)` 误触）+`Mod.` 大写路径访问（路径头解析；外部模块 Stdlib 等索引未命中跳过；同词干 .mli↔.ml 非依赖），嵌套块注释（注释内字符串字面量整体跳过）/可跨行串/`{id|...|id}` quoted string/字符字面量·类型变量歧义消解词法，test/tests 目录或 `*_test.ml`/`*_test.mli` 约定排除（alcotest，容器仍入表），传递闭包 BFS 最短链，锚定链尾 open 真行号）；混仓分治执法（构想v10 C7/设计稿 D1-D8，2026-11-20）：langs>1 → 单次清场+逐语言引擎只插不删（多语言 findings 并存）+跨语言边关联上报（edges⋈symbols 只读，.h 等多引擎后缀歧义跳过不猜，top 100 组 cap+诚实总组数）+status 聚合（全 ok→ok/全同态→该态/否则 partial；顶层和严格 all-int 守卫，缺字段诚实下钻 languages）；per-engine 超时全额不均摊；配置运行后清理；违例 = P9 findings（`source='s7-enforce'`，幂等 replace-all）；无 structure/非 active 诚实 skipped |
| `report` | S8 静态报告：三 IR（architecture/sequence/timeline）→ archify validate+render（缺失降级 Mermaid-only）→ `.vrp/report/`（report.html 无时间戳字节级复现+SARIF 2.1.0）；R7' 多级颗粒度（构想v7 §11）：archify 模块交互图链成功 → `chain/` 静态多页互链（dataflow/目录树/符号网格/整文件源码页=唯一代码视图，三段式原子提交失败降级）替代 L1/L2；失败/缺失 → L1 模块页（modules/）+ L2 符号页（symbols/）互链兜底；**V-2 融合报告（构想v9 §11）：fusion.html 六风格单页总览**（仪表盘/叙事/结构图/下钻/双引擎对账/数据切片；纯 stdlib FR 布局确定性；vendored ECharts 5.6.0 零外链；chain 同批联动行级跳转 `#L{n}`（render_chain 行级锚点+file_caps）；主题令牌与 chain 同源三预设（render_theme 单一权威））；渲染 chrome 全英文，数据（模块名/AI 梳理职责）原样；**#224 报告家族整合（archify story #1036/#281 概念吸收）：tour.html 新人 walkthrough 同批产出**（structure 缺失诚实跳过记 degraded 注记不阻塞；与 `vrp tour --html` 独立产出字节一致；report.html 模块节互链；`_clean_render_out` 清 tour.html 防 R6' 病根四孤儿残留） |
| `session` | 全信任模式（构想v7 §3.3）：begin/end 出初末 facts.db 快照（sqlite backup 安全复制），report 出初末计数 diff（四组）+ 双 L0 架构图并排 + 会话窗口 verdict 时间线；不做图 diff（诚实版） |
| `mcp` | AI 接口层：MCP server over stdio（per-repo；`pip install vrp[mcp]`；0.2.0 破坏性收敛三十→十（#225 后十一件：+curate_refresh 确定性 fid 重映射——structure/slices 死 fid 经 fid_ledger qn 稳定键重映射直刷入库收据）：vrp_query 17 类型单强查询工具（search/architecture/module/symbol/facts/callers/callees/slice/path/seams/coverage/impact/drift/curate_slices/curate_structure/curate_receipt/tx_verdicts）/dossier/curate_submit/curate_reject/curate_refresh/tx_preview/tx_submit/tx_run/render(3 kind)/session(3 action)/enforce；isError 面归零（_guard 捕 (SystemExit, Exception) 统一成功形状 {"error"}，KI 照传）+五层预算（24K 输出硬顶+JSON 感知确定性收缩（mcp_budget.emit）、静态限额中央表+VRP_TOOL_RESULT_LIMITS env 覆盖、SQL 超时 progress_handler（nav_path 300s/query_facts 10s+VRP_TOOL_QUERY_TIMEOUT）、输入上界 sql/tx_json 1MiB·query 10K；设计稿-AA1AA2 §4））、`VRP_OUTPUT_FORMAT=gcf` 可选 GCF 紧凑编码（AA3 构想v13——CGC 语义键声明一次+值管道分隔：dict 两行/全等键 list[dict] 键声明一次逐行记录（token 主收益面），混合形状诚实回退 JSON（强编码=断列歧义宁缺勿错），值内 \·换行·| 三转义防断列，无解码器前沿模型直读；超帽回退 JSON 收缩链——GCF 面不收缩（收缩算法锚定 JSON 结构）诚实边界；不设 env 即 JSON 默认面零动） |

## 场景 skill

`skills/` 目录四件场景操作协议（SKILL.md；agent 接 vrp MCP/CLI 后的消费指南；形状守卫 tests/test_skills.py——frontmatter/name==目录/description 单行 ASCII ≤1024/LF 纯/交叉链防烂链）：

| Skill | 场景 | 一句话 |
|---|---|---|
| `vrp-loop` | 重构 | 八步事务循环（定位→下钻→产出→预览→执行→verdict→修复→回归）；T4 不放闸、每事务至多 3 重试 |
| `vrp-curation` | 梳理 | slices/structure 六步生产线（验证入库+drift remap）；拒收码速查 |
| `vrp-archaeology` | 考古 | 读懂陌生/遗留仓：事实层→dossier 简报→timeline 腐化时间线（A1 转折点）→cochange 隐式耦合→病理清单→report 交付（E14 批⑨ #207；只读零写场景） |
| `vrp-review` | 审查 | 证据化审查：findings 开放公式排序→xcheck 引擎交叉→impact 爆炸半径→testcov 假测试关联现形→review 评语（SARIF→review-comments.json）；advisory 非 gate——人决策 |

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
| G6 | git 提交失败→回滚树+诚实 aborted 裁决（提交先于记账：如全 no-op edits 无可提交，绝不留 DB=committed 却无 git 提交的假象） | `gate/G6/commit-failed` |

外部信号缺失/超时一律诚实降级（skipped / 诚实记录），不伪造结果。

## 测试

```powershell
python -m pytest -q                        # 快速层 2281 例 ~4-8min（默认排除 deep）
python -m pytest -m deep -q                # 深度层 1529 例 ~15-30min（真实工具全外链 E2E）
python -m pytest -m "deep or not deep" -q  # 全量 3810 例（阶段性完工验收）
```

深度层含：双仓基准（TS 1887 符号 / Python 66132 符号·25 万边）、序贯事务 E2E、闸门假阳性性质（合法 rename 零误报×15、丢边加边必抓×25）、fuzz、五视图污染清扫、freeze 字节级复现、三语言变异执行 E2E、difftastic G4 E2E。

## 发布

开发与测试全在本地完成（工具链沙箱、verify 仓、CBM 缓存均为本地资源）；GitHub（`AH23333/VRP`）仅作 push 与 release 目标，**不做 CI/Actions**。

- **本地门禁**：`.githooks/pre-push`（`git config core.hooksPath .githooks` 激活）——推送前快速层 2281 例全绿才放行（~4-8min，git push/sh 环境实测可至 ~19min——工具超时须留足）；跳过用 `git push --no-verify`
- **发布**：GitHub release API 打 tag 发版（v0.1.0 起）；当前 **v0.1.2**（2026-11-16，Latest：S7 执法引擎 21 语言收齐+AI 梳理层全链，174 commits 自 v0.1.1）

## 外部工具（可选，缺失则降级）

CBM（索引）、code-atlas（交叉验证/MCP）、ast-grep、mutmut 2.4.5 / Stryker 10.1.1 / cargo-mutants（变异）、difftastic 0.72.0（结构 diff）、knip / vulture / jscpd、repomix（rawpack）、[archify](https://github.com/tt-a1i/archify) v3.2.0（S8 渲染，`bootstrap.py --with-archify` 装，`VRP_ARCHIFY_MJS` 覆盖）。二进制沙箱化于 `work/`，路径可用环境变量覆盖（`VRP_*_BIN` / `VRP_DIFFTASTIC_BIN` 等）。

- **子进程 CPU 优先级**（#147，学 TSA local_low_impact）：重型子进程（CBM 索引/变异引擎/freeze 基线/archify 等 procs.run_tree 全链+s4_freeze/s5_cabrief 流式通道）默认 BELOW_NORMAL（Windows creationflags 0x4000 纯 stdlib）/`nice -n 15`（POSIX，which 探测缺失回退）——不抢占交互前台，空闲机满速、争时机让位，只影响调度不影响结果（确定性零影响）。逃生门：`VRP_CHILD_PRIORITY=normal` 恢复普通优先级（专用 CI 机/基准场景）。**Stryker worker 泄漏孤儿收割**（#221，坑驱动——#220 深测三轮 3 errors 实录：stryker.cmd 正常返回后孙 node（vitest worker）存活持 work/stryker / work/_deep_stryker 沙箱目录句柄，rmtree 六轮退避全 PermissionError——AV 型退避救不了永久孤儿锁）：procs.is_stryker_orphan_cmd（路径归一纯判定，误杀红线=deveco/qoder/npm 等常驻 node 一律不命中）+procs.reap_stray_stryker_nodes（PowerShell CIM 枚举+taskkill /F 逐个收割，零依赖；POSIX 返回 []；枚举失败诚实 [] 降级不抛）；深测 stryker fixture setup/teardown 双收割（setup 时刻本 session 未 spawn，扫到即铁定上轮孤儿）。

## 文档（docs/）

- 《产品目标.md》——定位与成功判据（主流程四环）
- 《构想v6-核验后S1-S5定稿.md》——S1-S5 权威技术方案（§12-13 为实测/实施记录）
- 《构想v7-AI梳理层与S7-S8定稿.md》——**当前权威**：主流程四环/MCP 接口层/curate/S7-S8/批次 R1'-R4'
- 《构想v8-多层级下钻展示层.md》——展示层历史：多层级下钻（网格/代码页/rel 条/目录树），V-0→V-0.9 全程实录
- 《构想v9-融合报告形态.md》——**展示层当前权威（定稿候选）**：六风格融合报告形态（仪表盘+叙事+力导向图+下钻+对比+数据切片，单 HTML 静态），V-9 原型待拍板
- 《构想v10-拓展性候选与轮子调研.md》——拓展性发散清单（未立项）：18 项候选（时间维度/证据源/混仓分治执法/修复闭环/查准查全基准/SARIF 出圈等）逐项轮子调研+优先级总表（短期甜点四项/中期硬核六项/探索八项）
- 《构想v11-拓展性候选与轮子调研II.md》——拓展性发散清单第二批（未立项，与 v10 正交）：25 项候选（安全合规/CLI 工程/增量性能/发布工程/数据治理/多代理协作/语言深化/契约互操作/活文档防漂移/可解释性）逐项轮子调研+优先级总表（亮点 P23 活文档防漂移/甜点十一项/硬核四项）
- 《构想v12-拓展性候选与轮子调研III.md》——拓展性发散清单第三批（未立项，与前两批正交）：28 项候选（分析科学/管道智能化/消费者特化输出/部署分发/Schema 演进/定位对照/极端规模/交互探索/运营健康）逐项轮子调研+三批 71 项全景+跨批依赖链（亮点 R1 网络科学×propose 对账/S7 反馈环降权/R2 co-change 隐式耦合边）
- 《参照调研-codegraph双仓.md》——codegraph（78k★）/CodeGraphContext（4.4k★）双仓学习报告（2026-11-19）：五项铁证验证本仓路线（SQLite 收敛/依赖地狱反面教材/可复现性缺失代价/常驻进程工程税/无 PR CI 漂移）+九项优化候选提炼入构想v13+事故锚点全录（源文件路径+issue 号）
- 《构想v13-参照调研优化候选.md》——参照调研提炼批（未立项，AA/BB 双字母新系列，A-Z 已耗尽）：9 项候选（AA 组 MCP 消费面：单强工具+成功形状错误/自适应输出预算/GCF 压缩编码；BB 组事实质量面：调用边置信度分层/死代码置信度三级/Golden 审计矩阵/ETL 健康面/确定性排序 checklist/A/B 防侧信道）每项带参照仓事故锚点
- 《优先级总表-44项重排.md》——剩余 44 项（35 旧+9 v13）全盘排期定稿（2026-11-19）：重排判据五条/主推执行序九批（首两批 BB2 死代码置信度+AA1/AA2 MCP 消费面）/穿插甜点池九项/触发驱动池 21 项各带触发条件/依赖链更新（J8/A1/D10 已解锁项）——排期≠批准，主序批仍走设计稿→批准→实施
- 《设计稿-AA1AA2-MCP消费面改造.md》——批②设计稿（D1-D8 全批 2026-11-20，实施 #199）：MCP 工具面 30→10 分族收敛（vrp_query 17 类型单强查询工具/CGC 四参律/写路径动词独立）+isError 面归零（_guard 拓至 Exception，与 codegraph 真故障-isError 分歧记档）+五层输出预算（24K 硬顶+JSON 感知确定性收缩/静态限额中央表+env 覆盖/progress_handler 超时 10×实测律/输入上界 1MiB-10K）；参照源码 file:line 逐行重读记实
- 《设计稿-C7-混仓分治执法.md》——批③设计稿：混仓 not-enforceable→分治执法——写面互斥解法（wrapper 单次清场+引擎 write_findings/replace 机械参数，"分"已隐式存在于 derive 按语言过滤）/LANG_RUNNERS 表驱动路由（单语言路径字节级不变）/跨语言边 CBM edges 模块对聚合只上报不判（Bazel/Nx 双轨制参照，诚实差距记档）/状态聚合 partial+languages 明细/决策点 D1-D8（待批准实施）
- 《竞品对照矩阵.md》——W20：与 SonarQube/CodeScene/deptrac/import-linter 定位对照（只引对家公开声明，不贬损，欢迎 PR 修正）
- 《glossary.md》——Q24：术语表（Diátaxis reference 面；fid/remap/drift/verdict/evidence chain/OWNED_TABLES 等散落概念一站式，详处指针锚点锁 test_glossary 守卫）
- 《理论出处.md》——W21：概念↔经典书/论文映射（glossary 姊妹篇理论维度；Clean Architecture/POSA/Martin 度量论文/变异测试 1978/克隆检测 1995/ACID 1983 等，无出处原创面诚实标注）
- 《vendored-上游跟踪规范.md》——Z26：外部组件上游跟踪 SOP（季度检查+升级评估清单+钉位表，离线手动 dependabot）
- 《第三方许可登记.md》——Z27：in-repo vendored/work 沙箱/pip 钉版本三段 license 全清单+兼容性结论（本地实查+上游 LICENSE 抓取，时点 2026-11-17）
- 《致谢与轮子来源.md》——Z28：管道在用/测试基建/形态参照/理论经典全清单致谢+轮子学习方法论（学形态不引依赖/三判定/探针先行/红面自证/宁缺勿错）公开呈现
- 《设计稿-R2-co-change隐式耦合边.md》——R2 立项设计稿：co_changes 表/OWNED 存续/git log 挖掘/幂等重算/阈值与决策点 D1-D4（待批准实施）
- 《设计稿-J8-ETL增量化.md》——J8 立项设计稿：etl_file_state 工作树快照检测/fid 不变式（文件字节不变⇒fid 不变⇒行携带）/级联面精确枚举/交叉边 qn 锚/行集对拍等价判据/cbm 版本漂移强制全量/决策点 D1-D6（待批准实施）
- 《设计稿-M16-curate工件三方合并.md》——M16 立项设计稿：curate_base 覆盖前快照表（OWNED）/slices 按 slice_id 六分支三方/structure 字段级（modules by module_id+layers by name+校验兜底）/冲突 decisions 留痕保 ours/决策点 D1-D6（待批准实施）
- 《设计稿-J9-S2病理引擎并行.md》——J9 立项设计稿：引擎级四并发（线程池，并行实体是子进程）/vulture 地板决定收益上限（串行 55.6s→~40s）/逐 source 写序不变等价判据/mutate 三引擎不并行（#149 时序污染）/决策点 D1-D6（待批准实施）
- 《设计稿-A1-架构腐化时间线.md》——A1 立项设计稿：采样回放（默认 20 点恒含首尾）/单 detached worktree 串行推进+J8 增量链（用户树零接触）/链式单库步间查度量/Tornhill 度量向量十字段/timeline.json 确定性静态件/单样本失败中止全回放（跳样本=说谎）/决策点 D1-D6（待批准实施）
- 《渲染层方案.md》——archify 渲染层方案
- 《跨项目实测报告-2026-11-10.md》——跨项目实测
- 《契约/事务契约.md》——事务 schema/G 码对照/生产指南（AI 生产方必读）
- 《契约/梳理工件契约.md》——梳理工件（slices/structure）schema 唯一权威格式说明（AI 生产方必读，配置 skills/vrp-curation/ 生产指南）
- 《交接文档.md》——接手即读（环境/E2E 基准数字/代码地图/下一步）
- 《踩坑历史.md》——实测坑与纪律
- 《Checklist-并发归并确定性.md》——BB5 新并行代码点必过审查项（归并点显式排序/输出与串行恒等/优先复用 run_thunks/写面零交叉/过 census）+ CGC as_completed 完成序泄漏参照事故锚点 + 现有并行面审计表；census 守卫 tests/test_parallel_merge_census.py
- 《设计稿-B4-测试覆盖对账.md》——批⑤设计稿（用户授权自批 2026-11-21，实施 #203）：外部 coverage.py JSON 导入（生产者外部零依赖红线）→ test_coverage 表（file_path 键 OWNED 存续/per-label 整批替换）→ test_linkage 静态关联 vs 实际行覆盖五桶对账（body 区间 def 行排除——import 执行非测试执行；linked_not_covered_measured=「静态说有测试但从不执行」假关联强信号）/unresolved_files 诚实上报/决策点 D1-D5
- 《设计稿-F15BB3-查准查全基准与Golden审计矩阵.md》——批⑥设计稿（用户授权自批 2026-11-21，实施 #204）：Golden 审计矩阵——committed golden JSON 独立真值双面审计（CGC 循环完美门反面：导出与自己比=21/21 PASS 假象，真实 CALLS 准确率 84.6%）；violations 多集匹配（kind/file/line+R-AD payload 条件键）/symbols 五键精确近失成对；missing/spurious 双向计数+precision/recall 分母零→null 诚实；fixture 节一键重建可跑仓；七件真值集（5 s7 语言+2 ETL pin）零新增标注起步（F15 立项承诺兑现）；决策点 D1-D7
- 《设计稿-BB1V17-调用边置信度分层与schema迁移矩阵.md》——批⑦设计稿（用户授权自批 2026-11-21，实施 #205）：调用边置信度分层+schema 迁移矩阵合批——edges.resolution_label 列（confidence 双阈值 0.85/0.70→EXTRACTED/INFERRED/AMBIGUOUS；None→NULL 未知不惩罚；三仓真值探针实证 evidence/strategy 白名单不可枚举故否决）+MIGRATIONS 迁移链（PRAGMA user_version=2，先 ALTER 后 executescript 视图硬约束）+v_dead_code 拆分 AMBIGUOUS 过滤（IS NULL OR 显式防三值逻辑坑）；决策点 D1-D8
- 《schema版本承诺.md》——V18 政策（#205 rider）：facts.db schema 版本承诺——user_version 语义/N-1 库可迁移承诺/v0.x 破坏性变更随 minor 宽松政策/迁移链工程规则/反向风险记档/v1 保留基线槽说明
- 《SARIF-CI消费指南.md》——G17 出圈：外部项目 CI 消费 vrp SARIF 指南（GitHub Actions upload-sarif 样例/字段对照表/两层截断诚实链（vrp cap 2000+propertyBag 总数标注 × GitHub 5000 上限）/跨仓 dogfood 实测（lusiem 1064 全量 507KB、tree-sitter-analyzer 15741→2000 截断 979KB）/GitLab 通用 CI 提示；零服务端红线不动——上传动作归消费方）
- 《设计稿-C9-修复提案闭环.md》——批⑧设计稿（用户授权自批 2026-11-21，实施 #206）：修复提案闭环——单 finding 单文件 → LLM 产 transact 草案（事务契约 v1 载体零新 schema）/校验器 load_transaction 复用+三加验（WRONG_ID/WRONG_FILE/WRONG_OP 稳定回执码，拒收回执重试≤3）/单文件不可修 unfixable 诚实转义/D4 信任边界结构化（LLM 输出物理上只能变成草案文件，s6_transact 引用仅 load_transaction，源码守卫测试钉死）/防覆写 --force+run 目录同秒后缀/BB6 rider 合批；决策点 D1-D8
- 《A-B评估协议-防侧信道.md》——BB6 rider（#206）：A/B 评估方法论协议——双臂封 CLI 守卫（codegraph WITHOUT 臂 26/28 次 Bash 摸到 CLI"控制臂不是控制臂"教训）/每臂 ≥2 次取中位/三反馈指标（驻留上下文占用/下一步动作充分性/字节分配效率）/低显著度渠道别投入（3 措辞变体全回归教训）/诚实披露（residual-context-occupancy 先例）；脚本面诚实缓议（触发驱动，待首个真实 A/B 验收场景）
- 构想 v1-v6——历史方案，只读

## 平台

主开发环境 Windows + PowerShell 5.1；Python ≥3.11；产物尽可能落 D 盘。
