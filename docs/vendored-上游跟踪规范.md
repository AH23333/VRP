# vendored 上游跟踪规范（Z26）

> **是什么**：本仓 vendored/钉版本的外部组件上游新版本周期审查流程——离线世界的手动 dependabot。
> **为什么**：所有外部工具钉版本（bootstrap.py 是唯一钉位），上游修复/破坏性变更不会自动到达我们；不跟踪则钉死即腐化。
> **范围**：bootstrap.py 钉的一切 + in-repo vendored（vrp/vendor/、vrp/assets/）。

## 跟踪对象清单

钉位=bootstrap.py 常量/表项（改版本只改这里，一处钉一处改）。

| 组件 | 当前钉值 | 钉位 | 上游 |
|---|---|---|---|
| @ast-grep/cli | 0.51.0 | `NPM_SANDBOXES` | github.com/ast-grep/ast-grep |
| knip | 6.44.1 | `NPM_SANDBOXES` | github.com/webpro-nl/knip |
| jscpd | 5.4.2 | `NPM_SANDBOXES` | github.com/kucherenko/jscpd |
| @stryker-mutator/core | 10.1.1 | `NPM_SANDBOXES` | github.com/stryker-mutator/stryker-js |
| repomix | 1.18.1 | `NPM_SANDBOXES` | github.com/yamadashy/repomix |
| @lusiem/code-atlas | 0.6.0 | `NPM_SANDBOXES` | github.com/lusiem/code-atlas |
| difftastic | 0.72.0 | `DIFFT_VERSION` | github.com/Wilfred/difftastic |
| archify | 25cf3708（v3.2.0） | `ARCHIFY_COMMIT` | github.com/tt-a1i/archify |
| CBM（codebase-memory-mcp） | 0.12.1 | `CBM_VERSION`（系统级安装，提示式） | github.com/DeusData/codebase-memory-mcp |
| cargo-mutants | 27.1.0 | `CARGO_MUTANTS_VERSION`（可选） | github.com/sourcefrog/cargo-mutants |
| ECharts | 5.6.0 | `vrp/assets/echarts.min.js`（in-repo 文件） | github.com/apache/echarts |
| pytest 系（pip） | 见 bootstrap.py `PIP_CORE`/`PIP_DEEP` | pip 钉版本 | 各上游 |

## 检查周期

- **例行**：每季度首周（1/4/7/10 月）过一遍上表上游 release notes。
- **事件驱动**：上游安全修复公告 / 破坏性变更公告 / 本仓遇到疑似上游 bug 时立即检查。

## 升级评估清单

升级一个组件前逐项过（全部记入交接条目）：

1. **变更分级**：读上游 release notes——安全修复（优先升）/ bug 修复（评估是否命中我们的使用面）/ 新特性（不追新，按需）/ 破坏性变更（升前必读迁移指南）。
2. **事实面影响**：该组件是否参与 facts.db 生成（CBM/code-atlas/ast-grep/knip/jscpd/difftastic 参与；archify/ECharts 只影响渲染）？
   - 参与者升级会改变 findings 内容——这是合法的**环境变化**（确定性红线是"同环境同输入同产物"，环境指纹含工具版本），但必须：重建 lusiem 基准对比、在 facts.db meta 核对工具版本记录、交接记档"环境指纹变化"。
   - 渲染面升级（archify/ECharts）需检查 render/render_chain/render_fusion 的契约测试与快照。
3. **契约面**：组件 CLI/stdout 契约是否变化——R-X/R-AB 守卫族覆盖的 stdout 形状漂移面重新冒烟（s2_p4/s2_cargo_mutants/s2_stryker/s7_enforce_ts 先例）。
4. **测试面**：快速层全绿（pre-push 门禁）+ 受影响模块专项；必要时受影响测试红面自证。
5. **license 复核**：上游 license 是否变化——同步更新《第三方许可登记.md》。
6. **钉位更新**：bootstrap.py 钉值 + 本文档表格 + 《第三方许可登记.md》版本列，三处同批更新。

## 红线

- 不追新：例行检查只**记录**新版本可用，升级须有动因（安全修复/命中我方 bug/立项需要）。
- 升级必须单批可回滚：一个组件一个交接条目，混批升级无法归因。
- 本 SOP 不自动化：离线手动流程是设计选择（依赖清单小、变更频率低，自动化成本大于收益；真需要时再立项）。

## 记录义务

每次例行检查（含"无变化"）在交接文档记一行；升级按上述清单记条目。本文表格与 bootstrap.py 钉值不一致即文档漂移——发现即修。
