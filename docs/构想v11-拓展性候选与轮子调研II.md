# 构想v11 — 拓展性候选与轮子调研 II

> 状态：发散清单（未立项）。与《构想v10-拓展性候选与轮子调研.md》正交的第二批维度（H-Q 七组 25 项）。
> 方法论同 v10：**先找轮子再决定造/学/绕**；轮子名单只收确认存在的工具（宁缺勿错）。
> 优先级直觉：亮点首推 P23（原创无轮子）；甜点区=小时级成本；硬核区=需设计稿。

## H. 安全/合规面（全新维度）

### H1. secrets 独立化
- **是什么**：report 链已有密钥探测器（高置信云 token/JWT/私钥块/URL 凭据，SECRETS.local.md）——扩成独立 CLI `vrp secrets <repo>`：全仓扫描+git 历史扫描（全 blob 遍历）+monitor 模式。
- **轮子**：**gitleaks**（正则+熵组合，历史扫描先例）、**trufflehog**（验证器设计——密钥活性验证我们不做，只检测）、**detect-secrets**（Python 生态基线文件思想）。
- **判定**：**学**（gitleaks 的规则组织+熵阈值）+**造**（探测器代码已在 render 链，只差 CLI 化+历史面）。
- **成本**：低。优先级：甜点（顺路捎带——底座已在）。

### H2. SBOM+离线漏洞对账
- **是什么**：依赖清单提取（requirements/package.json/go.mod 等）→CycloneDX SBOM 生成；漏洞库不内置（零第三方红线）——**用户自备 OSV JSON 导入**后离线匹配。
- **轮子**：**osv-scanner**（OSV 数据格式与匹配语义）、**pip-audit**（Python 依赖审计）、**CycloneDX 规范**（SBOM 格式权威）。
- **判定**：**学**（OSV JSON schema 与 purl 坐标）+**造**（导入器+匹配器纯 stdlib）。
- **成本**：中。优先级：硬核（离线约束是差异化，但用户导入 OSV 的意愿待验证）。

### H3. 危险 API 规则族
- **是什么**：ast-grep 已有 9 规则（s2 产物）——扩 SQL 拼接/eval/exec/反序列化等危险模式规则集（yml 扩展，不引入新引擎）。
- **轮子**：**semgrep**（规则语法与模式库组织——只学规则思想不引入引擎）、**bandit**（Python 危险 API 清单参照）。
- **判定**：**绕**引擎（ast-grep yml 已在，规则是数据不是代码）。
- **成本**：低。优先级：甜点偏后（按需扩规则）。
- **状态**：**已完成**（2026-11-21 甜点穿插批 #209）——3 条新规则 9→12（规则是数据不是代码，yml 扩展零引擎改动）：`py-eval-exec`（builtin eval/exec 全 arity `$$$ARGS`；方法调用 `m.eval()` callee 是 attribute 不误报）/`py-unsafe-deserialize`（pickle.loads/pickle.load/yaml.load；`yaml.load(Loader=…)` 经 argument_list→keyword_argument name 字段下钻排除——`has` 只看直接子节点，call 直接子是 argument_list，单层下钻够不着）/`py-sql-concat`（execute/executemany 首参二元 + 拼接；`$A + $B, $$$REST` 字面逗号要求第二参——单参/双参双形态各补一模式）；**P5 病理码启用**（dangerous API——render.PATHOLOGY_LEGEND 补位 P1-P4/P8/P9 空档，explain 零复制直引自动流入，色位 #00695c 先前已预留）；semgrep/bandit 判定兑现：只学规则思想（bandit B301/B506 清单参照）零引擎引入；langmatrix astgrep 面 py 4→7 实时跟随；doclint readme-astgrep-rules 标记对账 9→12 同步。

## I. CLI/交互工程

### I4. `vrp doctor`
- **是什么**：环境自诊断——git/python/CBM/difftastic/ast-grep/21 引擎外链逐项可用性检测+修复建议（每次环境坑先跑它）。
- **轮子**：**npm doctor**（检测项组织形态）、**rustup doctor**（自检+指引风格）。
- **判定**：**造**（各依赖探测代码散在各模块的 fail-silent 守卫里，doctor 是聚合暴露面）。
- **成本**：很低。优先级：**甜点**（dogfood 直接受益；VS 实例坑 #91 一类问题先跑 doctor）。

### I5. `vrp explain <code>`
- **是什么**：错误码人话查询——G 码/S 码/P 码/SLICE_*/STRUCT_* 收据码全文检索（码表已在代码常量，只差 CLI 暴露）。
- **轮子**：**git help**（错误提示内嵌建议先例）、**cargo explain**（错误码→长文说明，Rust 编译器错误码体系最像）。
- **判定**：**造**（码表单源抽取——从代码常量生成，防第二处漂移）。
- **成本**：极低。优先级：**甜点**。

### I6. shell 补全生成
- **是什么**：argparse → bash/zsh/pwsh 补全脚本。
- **轮子**：**argcomplete**（Python argparse 补全生态标杆——只学其 argparse 内省法，运行时依赖不引入）。
- **判定**：**学**（内省实现）+**造**（生成静态脚本，零运行时依赖）。
- **成本**：低。优先级：探索（CLI 面稳定后再做，避免补全跟着命令变）。

### I7. `run --watch`
- **是什么**：文件变更自动重跑管道（开发时实时反馈）。与 v10-E13（MCP drift 订阅）区分：这是 CLI 消费面。
- **轮子**：**pytest-watch/ptw**（测试重跑形态）、**entr**（管道式文件事件工具）。
- **判定**：**绕**（轮询 mtime+防抖即可，实时性非刚需）。
- **成本**：中。优先级：探索（全管道重跑太重，配合 J8 增量化才有意义——**前置=J8**）。
- **状态**：**已完成**（2026-11-21 甜点穿插批 #208）——`vrp run <repo> --watch`：os.walk 快照（mtime_ns+size；产物/噪音目录剪枝——.vrp 不剪即自我触发死循环）→ WatchLoop 防抖状态机（变更持续窗口滑动，静默 ≥debounce（默认 1.0s）才触发；时钟/快照全注入纯逻辑可测）→ run_watch 驱动（初始全跑建基线→轮询→变更触发重跑；Ctrl+C 干净退出 rc=0；rerun 失败/异常不杀 watch——实时反馈面生存性优先；初始失败诚实传播）；J8 增量链（#190）成本前提兑现；cmd_run 提取 _run_pipeline 共用（单次面行为零动）；零依赖纯 stdlib（watchdog/entr 判定绕兑现）；18 件新测试（快照剪枝/确定性/增删改差分/状态机四态/驱动三场景/CLI 接线）。

## J. 增量与性能

### J8. ETL 增量化
- **是什么**：现在每次 etl 全量重建 facts.db（CBM 有缓存层但 ETL 侧全量）——变更文件重提取+图边局部更新。**大仓 dogfood 慢的根因**。
- **轮子**：**CBM 缓存层**（本仓已用——rebuild_reason 溯源设计可平移到 ETL 层）；**git ls-files -m** 增量检测；无现成"SQLite 图增量更新"轮子。
- **判定**：**造**（需要设计稿：fid 稳定性×增量窗口×级联失效（一个文件变→其符号边全失效）×OWNED_TABLES 回灌语义在增量路径下的行为）。**风险最高**：fid 身份/ledger/remap 全依赖 ETL 语义，增量路径破坏身份连续性=灾难。
- **成本**：高（需完整设计稿）。优先级：**硬核**（最高痛点×最高成本）。

### J9. S2 病理引擎并行
- **是什么**：各病理引擎互相独立，run_tree 进程治理底座已齐（#147 优先级边界版图）——进程池并行扇出。
- **轮子**：**cargo test**（并行调度+线程数默认值设计）、**concurrent.futures**（stdlib ProcessPoolExecutor——零依赖可用）。
- **判定**：**绕**（stdlib 进程池；注意 #147 边界版图：mutate 三引擎低优先级语义在并行下保持）。
- **成本**：中。优先级：硬核偏后（先 J10 落性能基线，有数据再决定并行收益）。

### J10. 管道性能回归追踪
- **是什么**：每批 dogfood run 耗时落档→性能回归检测（哪批变慢一目了然）。
- **轮子**：**rustc perf tracker**（性能数据版本化+回归报警思想）；**hyperfine**（基准测量形态参照）。
- **判定**：**造**（run 各阶段已有计时，落 JSONL+批次对账即可）。**J8/J9 的前置**（先有基线数据）。
- **成本**：低。优先级：甜点。

## K. 发布工程

### K11. release notes 半自动生成
- **是什么**：从交接文档条目+commit 历史生成 release notes 草稿；#153 数字教训落实——生成时**实时核码**（调内置计数函数，不信摘要快照）。
- **轮子**：**changesets**（变更累积→版本→notes 流程先例）、**conventional-changelog**（commit 规范→notes 生成）。
- **判定**：**学**（changesets 的累积模型）+**造**（交接文档是我们独有的第一手素材，比 commit message 信息密度高）。
- **成本**：低-中。优先级：甜点（下个版本发布直接受益）。

### K12. verify 仓冒烟集一键化
- **是什么**：发布前自动跑全部 verify 仓冒烟（跨仓回归）+结果汇总。
- **轮子**：**烟囱式自建**（verify 仓清单+run 调度+汇总表，无现成轮子匹配此形态）。
- **判定**：**造**（发布纪律脚本化——v0.1.2 是手工跑的）。
- **成本**：低。优先级：甜点。

## L. 数据治理与修复

### L13. `vrp repair`
- **是什么**：facts.db 损坏/迁移失败诊断修复——schema 版本检查+损坏定位+重建向导。
- **轮子**：**sqlite3 .recover**（损坏恢复权威）、**PRAGMA integrity_check**（健康检查入口）。
- **判定**：**学**（诊断序列：integrity_check→schema_version→逐表 count）+**造**（向导）。事实：facts.db 可全量重建（ETL 幂等），repair 主要是**诊断+指导重建**而非修数据。
- **成本**：低。优先级：探索（遇到真实损坏案例再做——坑驱动）。

### L14. 缓存 GC
- **是什么**：CBM 缓存库陈旧审计+清理（#132 清理轮手工做的）。
- **轮子**：**npm cache clean**（缓存治理 CLI 形态）。
- **判定**：**造**（审计查询+--dry-run 预览+确认清理三段式）。
- **成本**：极低。优先级：甜点。

### L15. flake 登记表
- **是什么**：flake 复发追踪（test_impact flake 先例 #153+fd0d7db 先例）——哪测试历史上抖过几次→抖王名单→隔离候选。
- **轮子**：**GitHub flaky bot**（flake 报告自动化先例）；**pytest-rerunfailures**（重试插件——不引入，只记录不掩盖）。
- **判定**：**造**（JSONL 登记+统计面；**纪律红线：登记不是掩盖**——flake 修复前必须留痕）。
- **成本**：低。优先级：甜点（已有两个真实案例打底）。

## M. 多代理协作（真实需求：本项目本身多会话）

### M16. curate 工件三方合并
- **是什么**：两代理各自梳理同一仓→slices/structure 冲突检测+合并——学 git merge 语义：共同祖先+双方变更+冲突标记留人工裁决。decisions 表已有否决留痕底座。
- **轮子**：**git merge**（三方合并语义权威——base/ours/theirs）；**Operational Transformation**（协作编辑理论，冲突分类参照：非冲突=自动合，冲突=标记）。
- **判定**：**造**（slices 按 slice_id 三方合并、structure 单行按字段三方——fid 层面 rebase 语义依赖 remap 环）。**场景真实**：多会话并行梳理（本项目自身）。
- **成本**：中-高。优先级：硬核（需设计稿：祖先快照存哪/冲突呈现形态）。

### M17. verdict 存档同步规范
- **是什么**：JSONL 存档跨机器交换格式规范（现单机 .vrp/）。
- **轮子**：**JSON Lines**（行级追加语义天然适合交换）；**NDJSON 生态**。
- **判定**：**绕**（格式已是 JSONL，只差文档化+去机器本地路径的可移植性检查）。
- **成本**：低。优先级：探索（跨机器场景出现再定）。

## N. 语言引擎深化

### N18. 语言能力矩阵
- **是什么**：21 语言在 S2/S3/S5/dossier 各面覆盖不均——矩阵文档化（哪些语言没有 deadcode/mutate/repomap）+缺口补齐清单。
- **轮子**：**tree-sitter 语法定义矩阵**（其 README 的语言支持表形态）。
- **判定**：**造**（矩阵表+守卫测试锁——防"新语言接引擎漏面"）。**#153 同款纪律**：矩阵数字实时核码生成。
- **成本**：文档低/补齐中-高。优先级：文档面甜点（补齐按需立项）。

### N19. 跨语言 API 边界对账

> **状态注记**：**触发条件验证完成（2026-11-22 #216 本机样例战役，6 真实仓实测）**：HTTP_CALLS 面=MarketTwin 572/restruct-music-player 82（JS→Go 路由跨界+测试打路由）/archify 109/excalidraw 8/StarMarkDesktop 1/CookMyFridge 0——方法标注边（GET/POST/DELETE+真实路由符号）质量达标；噪音面记档：ANY 方法边把路径形字符串（.env/文件系统路径/URL/打包产物/嵌码压缩文件）误判为路由（excalidraw 8 条中 5 条、archify findChrome→/Applications/...）。路由符号已可经 nav callers/callees 通用导航。
> **前置已满足（2026-11-22 #217 签名数据入库，schema v3）**：callee 面=symbols.signature/return_type/param_count 三列（CBM nodes.properties 提取，覆盖 Function/Method ~99%——MarketTwin 407/407、CookMyFridge 1092/1094 实测）；caller 面=CALLS 边 properties_json 的 args[{i,e,k,v}]（CBM 原样转存本就在库，#216 记档"签名不在 facts.db"不准确——原始 JSON 一直在，真差距是可查询列，已补）。**参数级对账双面就绪可立项**：映射=CALLS 边实参（args）对被调方签名（signature/param_count）的参数数/键名核对；上游边噪音（ANY 路由误判）归 CBM 上游记档非本仓修复面。

- **是什么**：py↔ts 的 HTTP/FFI 调用点参数对账（调用方传参与被调方签名漂移检测）。
- **轮子**：**OpenAPI spec 验证**（契约对账思想）；**grpcurl/proto 校验**（强契约先例）。
- **判定**：**学**（契约对比语义）。依赖 HTTP_CALLS 边提取精度，当前证据面可能不够。
- **成本**：高。优先级：探索（先看 HTTP_CALLS 边质量再定）。

### N20. Markdown 代码块提取
- **是什么**：文档中代码块的符号索引（CBM 索引 md 符号但代码块内是纯文本）——文档示例代码与真实 API 签名的漂移检测。
- **轮子**：**doctest**（文档内代码可验证先例——stdlib）；**litprog/literate programming**（文档即代码思想）。
- **判定**：**学**（doctest 的"文档示例必须可执行"哲学）+**造**（ fenced block 语言标注→对应引擎轻量解析→签名比对）。dogfood 受益：本项目文档全是示例代码。
- **成本**：低-中。优先级：甜点偏后。
- **状态**：**已完成**（2026-11-21 甜点穿插批 #210）——`vrp mdcode <repo>`（报告非门 rc 恒 0——langmatrix 先例）：fence 解析（未闭合照提取 closed 计数暴露）→路由 py（stdlib ast 真解析；ast.walk 是 BFS——按 (lineno,name) 排序恢复文档序）/ts·js（正则轻量五形状，树库不引边界记档）→name+label 对账：doc-only（db 无同名——改名/删除/虚构 API 嫌疑）/kind-mismatch（仅 py 清晰面 def↔Class；Method≈Function 同族不误报；ts 词表未钉 name-only 诚实边界）；无语言标注/未认知语言块不路由不猜；排除目录复用 watch.EXCLUDE_DIRS 单源；doctest 哲学落地=文档示例与 facts.db 可信度对账。**参数级比对前置已解锁**（#217 修档+补路）：原文"name 级比对（参数级签名不在 facts.db）"不准确——签名 JSON 一直在 symbols.properties_json，#217 提取为 signature/return_type/param_count 三列（schema v3）后参数级比对可立项（扩面归 N19 参数级对账同批评估，不在 mdcode 单独扩）。同名碰撞可能记档照旧。

## O. 契约互操作（降低接入成本）

### O21. 契约导入器
- **是什么**：import-linter/deptrac/ArchUnit 配置文件→structure JSON 转换器（已有仓的架构契约直接迁移，接入零成本）。
- **轮子**：**import-linter**（contracts.yml 格式）、**deptrac**（depfile.yaml 格式）、**json2csv 等格式转换器**（转换器工程形态）。
- **判定**：**造**（三格式各一个只读解析器——格式本身简单；风险=上游格式演进，钉版本+宽容解析）。
- **成本**：中。优先级：探索（"已有契约的仓"是假想用户，先验证真实需求）。

### O22. 契约导出
- **是什么**：structure JSON→其他工具格式（互操作出圈）。
- **轮子**：同 O21 反向。
- **判定**：**造**。优先级：探索（与 v10-G17 SARIF 出圈同批验证——挑一个真实外部消费场景再定）。

## P. 文档守卫泛化（亮点：自家纪律产品化）

### P23. 活文档防漂移功能
- **是什么**：本仓 test_docs_drift 守卫（README 可证伪断言对账/文档清单覆盖/计数同步）泛化成 vrp 功能给外部仓——文档中的数字断言（测试数/工具数/版本号）自动对账真实值。
- **轮子**：**无现成轮子**（原创空缺点——docs-as-code 概念有"文档测试"（doctest）但没有"活文档数字对账"工具；#148/#153 是自家两次实战教训的产品化）。
- **判定**：**造**。设计要点：①断言标记语法（文档内可证伪数字怎么标记——本仓用守卫测试硬编码正则，泛化需声明式标记）②真值源注册（数字从哪来——CLI 输出/文件计数/常量内省，与 vrp 自仓 collect-only 对账同构）③失败呈现（断言→期望→实际→修法指引）。
- **成本**：中。优先级：**亮点首推**（差异化叙事最强：与 21 语言/S8/AI 层全正交，"文档说谎检测器"独立成章；自家 1507/1523/3030 三计数守卫就是活样例）。

## Q. 可解释性

### Q24. glossary 一站式
- **是什么**：fid/remap/drift/verdict/evidence chain/OWNED_TABLES 等概念术语表（现散在各契约文档）。
- **轮子**：**Diátaxis 框架**（文档分类学：tutorial/how-to/reference/explanation——glossary 属 reference）。
- **判定**：**造**（纯文档工程）。Face C 守卫会要求 README 登记。
- **成本**：极低。优先级：甜点（新读者接手成本直接降低）。

### Q25. finding 叙事化
- **是什么**：xcheck agreement 低时人话解释差异来源（哪引擎与 CBM 不一致+为什么）。
- **轮子**：**compiler error explain**（rustc --explain 的叙事结构：现象→原因→修法）。
- **判定**：**造**（xcheck 差异明细数据已在，差叙事模板）。
- **成本**：中。优先级：探索（先收集真实困惑案例再模板化）。
- **状态**：**已完成**（2026-11-21 甜点穿插批 #211）——`vrp xcheck-explain <repo>`（Q25 落地；前置已满足：真实困惑案例已收集——xcheck.py:103 实录 27.7% agreement/159 CA calls 真实仓面）：只读 facts.db（discrepancy_ledger 四 category+meta xcheck_*）→逐类 rustc --explain 形态模板（现象→原因→修法：ca_only=目标未被 CBM 索引诸因/内建=CBM 刻意不索引/cbm_only=usage 弱证据或覆盖差异/测试源=CA 面排除）+top-3 示例（rowid 序）+总述行（agreement<60% 标 LOW）；xcheck 未跑过→诚实提示（ledger 空且 meta 无 xcheck_agreement_pct 键——"None" 值也是跑过，键存在判 ran）；rc 恒 0（解释面非门）。

## 优先级总表

| 梯队 | 项 | 一句话 |
|---|---|---|
| 亮点首推 | P23 活文档防漂移 | 原创无轮子+#148/#153 教训产品化 |
| 甜点 | I4 vrp doctor | 环境坑先跑它（#91 类问题） |
| 甜点 | I5 vrp explain | 错误码人话，码表单源抽取 |
| 甜点 | J10 性能追踪 | J8/J9 前置基线 |
| 甜点 | K11 notes 半自动 | 下版本发布直接受益 |
| 甜点 | K12 冒烟一键化 | 发布纪律脚本化 |
| 甜点 | L14 缓存 GC | #132 手工轮脚本化 |
| 甜点 | L15 flake 登记 | 两真实案例打底 |
| 甜点 | N18 能力矩阵 | #153 纪律：矩阵核码生成 |
| 甜点 | Q24 glossary | 接手成本降低 |
| 甜点（顺路） | H1 secrets 独立化 | 探测器已在 report 链 |
| 硬核 | J8 ETL 增量化 | 最高痛点×最高成本，需设计稿 |
| 硬核 | M16 curate 三方合并 | 多会话真实场景，需设计稿 |
| 硬核 | H2 SBOM | 离线约束差异化 |
| 硬核偏后 | J9 引擎并行 | 先 J10 基线再定收益 |
| 探索 | H3 危险 API 规则 | 按需扩规则 |
| 探索 | I6 补全生成 | CLI 面稳定后再做 |
| 探索 | I7 run --watch | 前置=J8 |
| 探索 | L13 vrp repair | 坑驱动 |
| 探索 | M17 存档同步 | 跨机器场景出现再定 |
| 探索 | N19 跨语言对账 | 先看 HTTP_CALLS 边质量 |
| 探索 | N20 md 代码块 | doctest 哲学，dogfood 受益 |
| 探索 | O21/O22 契约互操作 | 假想用户，先验真实需求 |
| 探索 | Q25 finding 叙事化 | 先收集困惑案例 |

## 两批关系与立项约定

- 本批与 v10 十八项**正交**（v10：时间/证据源/执法面/重构执行/AI 消费/度量/出圈；本批：安全合规/CLI 工程/增量性能/发布工程/数据治理/多代理协作/语言深化/互操作/文档守卫/可解释性）。
- 立项约定同 v10：清单不自动立项；单项走设计稿→批准→实施；立项前复轮子调研（本文档调研时点 2026-11-16）。
- 依赖链提示：J10→J8/J9（性能基线先行）；J8→I7（增量是 watch 的前提）；N18 矩阵先行可防"新语言接引擎漏面"债累积。
