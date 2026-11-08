# vrp — Vibe 项目重构分析管道

面向通用 AI 的 vibe 项目重构分析管道：把 AI 堆出来的仓库变成**有事实基础、可验证、可执行重构**的对象。

- **主用户：通用 AI agent**（不绑定特定 Agent/MCP 框架）——产物是文件与数据库，不是服务
- **辅用户：vibe 开发者（人类）**——两条消费线分开：AI 消费深层数据（facts.db/MCP），vibe 开发者消费 report/chain 用户产物（AI 梳理职责/层规则/病理体检报告），目标是「理解项目、判断梳理意见、做决定」
- 纯 Python 标准库（≥3.11，零第三方运行时依赖），全管道 CLI 子进程化、可审计可重放

## 管道一览

```
S0 precheck ─→ S1 CBM 索引 ─→ S1.5 ETL(facts.db) ─→ xcheck 交叉验证
                    │
                    ├─→ S2 病理检测（ast-grep/死代码/变异三引擎/P4·P2 补位）
                    ├─→ S3 校验器组（锚定/覆盖率/证据闸门）
                    ├─→ S4 freeze（基线快照 + 变更预筛）
                    ├─→ S5 dossier（五视图装配 + repomap + 任务型预算）
                    └─→ S6 重构事务（G1-G5 闸门 + git 原子提交/回滚）
```

核心产物：`<repo>/.vrp/facts.db`（规范化事实层 + 注解宿主）与 `dossier.json`（信息架构装配）；重构事务（T1 重命名/T2 删死码/T3 建调用方/T4 内容微调）在其上声明、验证、执行。

## 安装

```powershell
pip install -e .                       # 纯标准库，无第三方依赖
$env:CBM_CACHE_DIR = "D:\cbm-cache"    # CBM 缓存目录（必须显式设置）
```

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
vrp s2 <repo>                           # 病理检测（findings + 差异登记簿）
vrp mutate-run <repo> --budget N        # 变异执行（Python/TS/Rust 三引擎）
vrp dossier <repo> --task T1            # 五视图装配（T1-T4 预算）
vrp transact <repo> tx.json             # 重构事务（声明→闸门→原子提交或回滚）
vrp transact <repo> tx.json --preview   # diff 预览（difflib+difftastic 工具组，无闸门不改树）
vrp verdicts <repo>                     # verdict 历史（open 项自动回馈进 dossier evidence）
vrp curate <repo>                       # AI 梳理工件验收入库（.vrp/ai/ slices/structure；--reject 用户否决留痕）
vrp curate-auto <repo>                  # 内置 LLM 梳理循环（零 agent 路径）：配好 .vrp/config.json 的 API key 即自动跑 dossier→slices→structure→enforce→drift（契约 §6；全自动+全留痕 .vrp/auto/）
vrp drift <repo>                       # 梳理工件漂移查询面（只读）：stale 原因 + fid_ledger 反查重定位候选 remap；stored active 实际漂移 → rc=1
vrp remap <repo>                       # 漂移恢复半自动环：drift 的唯一候选 remap 一键写回 .vrp/ai/*.json（unresolved 诚实上报 → rc=1；再跑 vrp curate 复活）
vrp refresh <repo>                     # 漂移恢复一键环：S0→S1→ETL→xcheck→drift→remap→curate→report（代码变更后工件全链路自动追平）
vrp enforce <repo>                      # S7 架构执法：curate 入库的 structure 按语言路由执行（.py→import-linter；.ts/.js→dependency-cruiser；.go→go list 原生导入图；.rs→内置 use 解析器；.java→内置 package/import 解析器；.cs→内置 namespace/using 解析器；.kt→内置 package/import 解析器；.c/.cc/.cpp→内置 #include 解析器；.php→内置 namespace/use 解析器；.scala→内置 package/import 解析器；.rb→内置 module/require 解析器；.dart→内置 import 解析器；.swift→内置 Package.swift target 解析器；.zig→内置 @import 解析器；.lua→内置 require 解析器；.m/.mm→内置 #import 解析器），违例落 P9 findings
vrp report <repo>                       # S8 静态报告（.vrp/report/：IR+SARIF+Mermaid+archify html；R7' 多级颗粒度——archify 图链成功出 chain/（替代 L1/L2），否则 L1 模块页+L2 符号页；archify 缺失降级不阻塞；用户产物信息架构——How to read/Project overview（AI 梳理职责+层规则）/Pathology checkup（P 码人话体检）/Deep data 折叠（AI 材料归拢））
vrp session <repo> begin|end|report     # 全信任模式（§3.3）：初末 facts.db 快照 → 计数 diff + 双 L0 图并排 + verdict 时间线
```

## CLI 命令

| 命令 | 用途 |
|---|---|
| `precheck` | git 快照/语言普查/命令发现 |
| `index` / `status` | CBM 索引（缓存）/ 状态查询 |
| `etl` | CBM → facts.db 五级证据路由 |
| `xcheck` | code-atlas 四元组交叉验证 → 差异登记簿 |
| `run` | S0→S1→ETL→xcheck 一发 |
| `s2` | 病理检测：ast-grep 9 规则 + 死代码 + knip/vulture + jscpd |
| `mutate-plan` / `mutate-run` | 变异预算采样 / 变异执行（mutmut·Stryker·cargo-mutants） |
| `validate` / `gate` | 锚定+覆盖率校验 / AI claim 证据闸门 |
| `freeze` | 基线命令执行 + graph 快照 + 变更预筛 |
| `dossier` | 七节装配（架构/简报/repomap/五视图/证据/摘要/rawpack 指针） |
| `transact` / `transact-batch` | 重构事务单发（`--preview` diff 预览）/ 目录批处理 |
| `verdicts` | verdict 历史查询（`--tid` 单条详情；dossier evidence 自动内嵌 open 项） |
| `curate` | AI 梳理工件验收（slices/structure 确定性验证全过才入库；`--reject` 否决留痕 decisions 表；复验失效标 stale） |
| `curate-auto` | 内置 LLM 梳理循环（零 agent 路径，构想v7 §13）：`.vrp/config.json` 配 OpenAI-compatible API key → 自动六步（dossier 取材→slices→structure→enforce→drift→summary）；无 function calling（确定性校验器即工具，拒收回执重试≤3）；循环只走 curate 数据面不触重构事务；全自动+全留痕 `.vrp/auto/`（key 不落盘） |
| `drift` | 梳理工件漂移统一查询面（只读）：每次 etl/事务 promote/MCP 提交自动复验（F2 假 active 窗口已关）；drift 逐条给 fresh/missing_fids/missing_edges + fid_ledger（fid 身份史，跨 etl 存续）反查重定位候选——qn 未变给 remap 映射，qn 消亡给同文件域候选（cap 10）；remap 是提议不旁路，重提仍全量重验；stored active 实际漂移 → 退出码 1 |
| `remap` | 漂移恢复半自动环：drift 的唯一候选 remap 一键写回 `.vrp/ai/*.json` 工作工件（只改 fid 引用不触 facts.db）；多候选/无候选/文件缺失 → `unresolved` 诚实上报 rc=1；写回后 `vrp curate` 全量重验复活 active |
| `refresh` | 漂移恢复一键环：S0→S1→ETL→xcheck→drift→remap→curate→report（代码变更后 curate 工件全链路自动追平；remap unresolved 或 curate 拒收 → rc=1） |
| `enforce` | S7 架构执法（构想v7 §4/§8.2/§9）：structure(active) → 层 DAG+rules 生成 forbidden 契约 → 按语言路由执行——.py→import-linter（VRP_IMPORT_LINTER_BIN 覆盖）；.ts/.tsx/.js/.jsx/.mjs/.cjs→dependency-cruiser（typescript 5.9.3 钉版沙箱，VRP_DEPCRUISE_BIN 覆盖）；.go→go list 原生导入图（`go list -e -json`，传递闭包 BFS 最短链，VRP_GO_BIN 覆盖）；.rs→内置源级 use 解析器（纯 stdlib 零工具链依赖，mod 树+传递闭包 BFS 最短链，cfg(test) 排除，orphan .rs 诚实计数）；.java→内置源级 package/import 解析器（纯 stdlib 零工具链依赖，容器=Java 包，`/test/java/` 测试源根排除，传递闭包 BFS 最短链，orphan 无包文件诚实计数）；.cs→内置源级 namespace/using 解析器（纯 stdlib 零工具链依赖，容器=C# 命名空间，tests 测试目录排除，传递闭包 BFS 最短链，orphan 无命名空间文件诚实计数）；.kt→内置源级 package/import 解析器（纯 stdlib 零工具链依赖，容器=Kotlin 包，`/test/kotlin/` 测试源根排除，嵌套块注释/raw string/分号可选/`as` 别名词法，传递闭包 BFS 最短链，orphan 无包文件诚实计数）；.c/.cc/.cpp/.cxx/.c++/.h/.hh/.hpp/.hxx/.h++→内置源级 #include 解析器（纯 stdlib 零工具链依赖，容器=目录（C 无语言级模块系统 include 才是依赖边界），引号目标近似 -I 解析+尖括号系统头不入图，续行合并，不可嵌套注释/raw string 词法，test/tests 目录或文件名约定排除，传递闭包 BFS 最短链，锚定链尾 include 真行号）；.php→内置源级 namespace/use 解析器（纯 stdlib 零工具链依赖，容器=PHP 命名空间，use 边 only（deptrac 同口径，require/include/inline FQN 不入图），岛模型词法（HTML 岛外空白化/attribute 非注释/heredoc-nowdoc），tests 目录段或 `*Test.php` 约定排除（命名空间仍入表），传递闭包 BFS 最短链，锚定链尾 use 真行号，orphan 无命名空间文件诚实计数）；.scala→内置源级 package/import 解析器（纯 stdlib 零工具链依赖，容器=Scala 包，package 三形态（平铺/packaging 块/嵌套链拼接首链），Scala 2 `._`/Scala 3 `.*`/选择器 `{C, D}`/重命名 `{C => D}` import 全形态，嵌套块注释/三引号串词法，`/test/scala/` 测试源根排除，传递闭包 BFS 最短链，锚定链尾 import 真行号，orphan 无包文件诚实计数）；.rb→内置源级 module/require 解析器（纯 stdlib 零工具链依赖，容器=module/class 嵌套全路径（compact `A::B`/嵌套 `module A; module B` 同构，`class Foo < Bar` 超类忽略），require/require_relative 声明边（require_relative 相对文件目录/require $LOAD_PATH 近似根，动态 require（插值/变量实参）不入图），heredoc 四形态/% 字面量嵌套定界/正则位置启发式词法，test/tests/spec 目录或 `test_*.rb`/`*_test.rb`/`*_spec.rb` 约定排除（minitest+rspec，容器仍入表），传递闭包 BFS 最短链，锚定链尾 require 真行号，orphan 无声明文件诚实计数）；.dart→内置源级 import 解析器（纯 stdlib 零工具链依赖，容器=Dart 文件（每文件默认一 library；part 文件归并 owner 文件），依赖边=import/export URI（package: 就近 pubspec 判包、dart: SDK 跳过、相对 URI 归一；part 指令非依赖边），raw 串/三引号串/嵌套块注释/插值词法，test/tests/integration_test 目录或 `*_test.dart` 约定排除，传递闭包 BFS 最短链，锚定链尾 import 真行号）；.swift→内置源级 Package.swift target 解析器（纯 stdlib 零工具链依赖，容器=SwiftPM target 名（Package.swift targets 权威清单——path 实参或默认 Sources/<名>/Tests/<名>；testTarget 测试排除），依赖边=`import <TargetName>`（@testable/限定形态 import class/struct/... 取模块段；未声明系统模块跳过），raw 串（# 定界计数）/多行三引号串/嵌套块注释词法，传递闭包 BFS 最短链，锚定链尾 import 真行号，orphan 未归属 .swift 诚实计数）；.zig→内置源级 @import 解析器（纯 stdlib 零工具链依赖，容器=Zig 文件（repo 相对 posix 路径），依赖边=`@import("*.zig")` 相对路径（无 .zig 后缀=包名/命名模块→外部跳过；build.zig 程序化声明不解析），多行字符串（行首 `\\` 前缀）/行注释（Zig 无块注释）/单行串/字符字面量词法，test 块内 @import 排除（Zig 测试主形态——文件内嵌），传递闭包 BFS 最短链，锚定链尾 @import 真行号）；.lua→内置源级 require 解析器（纯 stdlib 零工具链依赖，容器=Lua 文件（repo 相对 posix 路径），依赖边=require 实参字符串（括号/无括号/level-0 长括号全形态，点转斜杠+repo 根/init.lua 候选近似 package.path，动态 require（变量/拼接实参）不入图），长括号 level 配对/`\z` 续串/贪心 `[[` 长字符串词法，spec/test/tests 目录或 `*_spec.lua`/`*_test.lua`/`test_*.lua` 约定排除（busted/luaunit，容器仍入表），传递闭包 BFS 最短链，锚定链尾 require 真行号）；.m/.mm→内置源级 #import 解析器（纯 stdlib 零工具链依赖，容器=目录（同 C——ObjC 无语言级模块系统 #import 才是依赖边界，@class 前向声明非依赖边），依赖边=引号 #import/#include（尖括号框架/系统头与 @import clang 模块不入图；源文件目录→仓根→header_dirs 解析=等价 Xcode header map 近似），词法底座同 C，test/tests 目录（大小写不敏感）或驼峰 *Test.m/*Tests.m 约定排除，传递闭包 BFS 最短链，锚定链尾 #import 真行号）；.pl/.pm/.t→内置源级 package/use 解析器（纯 stdlib 零工具链依赖，容器=Perl 包（首 package 声明，use/require 目标即包名；引号 .pm 目标 lib/→仓根→源目录三候选），`#` 注释（$# sigil 守卫）/POD/q·qq·qw·qr·m·s·tr·y 算符/heredoc 全形态/`__END__`/`__DATA__` 词法，t/xt/test/tests 目录或 `.t` 后缀约定排除（TAP，包仍入表），传递闭包 BFS 最短链，锚定链尾 use/require 真行号）；.erl→内置源级 module/远程调用解析器（纯 stdlib 零工具链依赖，容器=Erlang 模块（OTP 扁平命名空间，首 -module 声明，远程调用目标即模块名免 code path 猜测），依赖边=`mod:fun` 远程调用（fun 侧后随 `(`/`/arity` 才认定——map `:=`/typespec 不误触）+`-behaviour(mod)`+`-import(mod,...)`（外部模块 gen_server/lists 等跳过），`%` 注释/`$c` 字符字面量/双引号串/带引号原子原文保留词法，test/tests 目录或 `_test/_tests/_suite.erl` 约定排除（EUnit/Common Test，模块仍入表），传递闭包 BFS 最短链，锚定链尾调用真行号，orphan 无 -module 文件诚实计数）；.r/.R→内置源级 source/包引用解析器（纯 stdlib 零工具链依赖，容器=R 包目录（DESCRIPTION `Package:` 声明最近祖先目录，根包收编全部散文件；无包归属散 .R=文件自身），依赖边=source() 字符串实参（文件目录/仓根双候选解析）+library/require/loadNamespace/requireNamespace（裸词/串/`package=` 命名）+`pkg::`/`:::` 命名空间引用（本地包索引命中→包目录，CRAN 外部跳过），`#` 注释含 roxygen/单双引号串/反引号标识符词法，test/tests/testthat 目录或 `test-*.R`/`test_*.R` 约定排除（testthat，容器仍入表），传递闭包 BFS 最短链，锚定链尾 library/source 真行号）；.clj/.cljc→内置源级 ns/require 解析器（纯 stdlib 零工具链依赖，容器=Clojure 命名空间（首 `(ns name)` 声明（reader 元数据 `^`/`#^` 前缀跳过）——require 目标即命名空间；无 ns 文件=文件自身可出边不可入边叶可见），依赖边=ns 子句 :require/:use/:require-macros+require/use 函数形态（嵌套 require 递归提取——条件 require 支持）+reader 条件 `#?`/`#?@` 形树级展开（.cljc 分支不区分全分支按序拼接）+load/load-file 串实参（classpath 候选解析），`;` 注释/`#!` shebang/`#"..."` 正则字面量/`\c` 字符字面量（`\"` 防误开串）/`#_` 词法，test/tests 目录或 `*_test.clj`/`*_test.cljc` 约定排除（ns 仍入表），传递闭包 BFS 最短链，锚定链尾 require 真行号，orphan 无 ns 散文件诚实计数）；.ml/.mli→内置源级 module/open 解析器（纯 stdlib 零工具链依赖，容器=OCaml 编译单元文件（repo 相对 posix 路径，模块名=文件名词干首字母大写，引用路径头→模块名索引→命中文件，.ml 优先 .mli），依赖边=open/let open/include+module 别名（具名/括号署名提取）+functor 应用实参（`F(X)` 别名/include RHS 后上下文限定防构造器 `Some (x)` 误触）+`Mod.` 大写路径访问（路径头解析；外部模块 Stdlib 等索引未命中跳过；同词干 .mli↔.ml 非依赖），嵌套块注释（注释内字符串字面量整体跳过）/可跨行串/`{id|...|id}` quoted string/字符字面量·类型变量歧义消解词法，test/tests 目录或 `*_test.ml`/`*_test.mli` 约定排除（alcotest，容器仍入表），传递闭包 BFS 最短链，锚定链尾 open 真行号）；混语言诚实 not-enforceable；配置运行后清理；违例 = P9 findings（`source='s7-enforce'`，幂等 replace-all）；无 structure/非 active 诚实 skipped |
| `report` | S8 静态报告：三 IR（architecture/sequence/timeline）→ archify validate+render（缺失降级 Mermaid-only）→ `.vrp/report/`（report.html 无时间戳字节级复现+SARIF 2.1.0）；R7' 多级颗粒度（构想v7 §11）：archify 模块交互图链成功 → `chain/` 静态多页互链（dataflow/目录树/符号网格/整文件源码页=唯一代码视图，三段式原子提交失败降级）替代 L1/L2；失败/缺失 → L1 模块页（modules/）+ L2 符号页（symbols/）互链兜底；渲染 chrome 全英文，数据（模块名/AI 梳理职责）原样 |
| `session` | 全信任模式（构想v7 §3.3）：begin/end 出初末 facts.db 快照（sqlite backup 安全复制），report 出初末计数 diff（四组）+ 双 L0 架构图并排 + 会话窗口 verdict 时间线；不做图 diff（诚实版） |
| `mcp` | AI 接口层：MCP server over stdio（per-repo；`pip install vrp[mcp]`；nav_*/curate_*/tx_*/render_*/session_*/enforce/query_facts/drift_report/dossier 二十七件） |

## 重构事务与闸门

事务失败即整体回滚（git 原子性），verdict 落 `.vrp/verdicts/`：

| 闸门 | 检查 | 稳定规则码 |
|---|---|---|
| G0 | apply 前置校验 | `gate/G0/apply-edits` |
| G1 | 提交后重索引（旁路 ETL） | `gate/G1/parse-errors` |
| G2 | lint/typecheck | `gate/G2/static-check` |
| G3 | 测试套件 | `gate/G3/test-failed` |
| G4 | 声明等价：**五信号**——符号集/使用边/悬垂引用/verify_changes/**difftastic 结构 diff**（同符号体内偷改可抓） | `undeclared-change` `edge-mismatch` `dangling-reference` `verify-broken` `structural-change` |
| G5 | 死代码单调性 | `gate/G5/deadcode-increase` |

外部信号缺失/超时一律诚实降级（skipped / 诚实记录），不伪造结果。

## 测试

```powershell
python -m pytest -q                        # 快速层 402 例 ~60-100s（默认排除 deep）
python -m pytest -m deep -q                # 深度层 1410 例 ~10min（真实工具全外链 E2E）
python -m pytest -m "deep or not deep" -q  # 全量 1812 例（阶段性完工验收）
```

深度层含：双仓基准（TS 1887 符号 / Python 66132 符号·25 万边）、序贯事务 E2E、闸门假阳性性质（合法 rename 零误报×15、丢边加边必抓×25）、fuzz、五视图污染清扫、freeze 字节级复现、三语言变异执行 E2E、difftastic G4 E2E。

## 发布

开发与测试全在本地完成（工具链沙箱、verify 仓、CBM 缓存均为本地资源）；GitHub（`AH23333/VRP`）仅作 push 与 release 目标，**不做 CI/Actions**。

- **本地门禁**：`.githooks/pre-push`（`git config core.hooksPath .githooks` 激活）——推送前快速层 268 例全绿才放行（~35-40s）；跳过用 `git push --no-verify`
- **发布**：GitHub release API 打 tag 发版（v0.1.0 起）

## 外部工具（可选，缺失则降级）

CBM（索引）、code-atlas（交叉验证/MCP）、ast-grep、mutmut 2.4.5 / Stryker 10.1.1 / cargo-mutants（变异）、difftastic 0.72.0（结构 diff）、knip / vulture / jscpd、repomix（rawpack）、[archify](https://github.com/tt-a1i/archify) v3.2.0（S8 渲染，`bootstrap.py --with-archify` 装，`VRP_ARCHIFY_MJS` 覆盖）。二进制沙箱化于 `work/`，路径可用环境变量覆盖（`VRP_*_BIN` / `VRP_DIFFTASTIC_BIN` 等）。

## 文档（docs/）

- 《产品目标.md》——定位与成功判据（主流程四环）
- 《构想v6-核验后S1-S5定稿.md》——S1-S5 权威技术方案（§12-13 为实测/实施记录）
- 《构想v7-AI梳理层与S7-S8定稿.md》——**当前权威**：主流程四环/MCP 接口层/curate/S7-S8/批次 R1'-R4'
- 《契约/事务契约.md》——事务 schema/G 码对照/生产指南（AI 生产方必读）
- 《交接文档.md》——接手即读（环境/E2E 基准数字/代码地图/下一步）
- 《踩坑历史.md》——实测坑与纪律
- 构想 v0-v6——历史方案，只读

## 平台

主开发环境 Windows + PowerShell 5.1；Python ≥3.11；产物尽可能落 D 盘。
