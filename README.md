# vrp — Vibe 项目重构分析管道

面向通用 AI 的 vibe 项目重构分析管道：把 AI 堆出来的仓库变成**有事实基础、可验证、可执行重构**的对象。

- **主用户：通用 AI agent**（不绑定特定 Agent/MCP 框架）——产物是文件与数据库，不是服务
- **辅用户：人类工程师**——产物同样可读可审计
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
| `transact` / `transact-batch` | 重构事务单发 / 目录批处理 |

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
python -m pytest -q                        # 快速层 257 例 ~40s（默认排除 deep）
python -m pytest -m deep -q                # 深度层 1402 例 ~10min（真实工具全外链 E2E）
python -m pytest -m "deep or not deep" -q  # 全量 1659 例（阶段性完工验收）
```

深度层含：双仓基准（TS 1887 符号 / Python 66132 符号·25 万边）、序贯事务 E2E、闸门假阳性性质（合法 rename 零误报×15、丢边加边必抓×25）、fuzz、五视图污染清扫、freeze 字节级复现、三语言变异执行 E2E、difftastic G4 E2E。

## 发布

开发与测试全在本地完成（工具链沙箱、verify 仓、CBM 缓存均为本地资源）；GitHub（`AH23333/VRP`）仅作 push 与 release 目标，**不做 CI/Actions**。

- **本地门禁**：`.githooks/pre-push`（`git config core.hooksPath .githooks` 激活）——推送前快速层 257 例全绿才放行（~40s）；跳过用 `git push --no-verify`
- **发布**：GitHub release API 打 tag 发版（v0.1.0 起）

## 外部工具（可选，缺失则降级）

CBM（索引）、code-atlas（交叉验证/MCP）、ast-grep、mutmut 2.4.5 / Stryker 10.1.1 / cargo-mutants（变异）、difftastic 0.72.0（结构 diff）、knip / vulture / jscpd、repomix（rawpack）。二进制沙箱化于 `work/`，路径可用环境变量覆盖（`VRP_*_BIN` / `VRP_DIFFTASTIC_BIN` 等）。

## 文档（docs/）

- 《产品目标.md》——定位与成功判据
- 《构想v6-核验后S1-S5定稿.md》——**权威技术方案**（§12-13 为实测/实施记录，含各批次 E2E）
- 《交接文档.md》——接手即读（环境/E2E 基准数字/代码地图/下一步）
- 《踩坑历史.md》——80 条实测坑与纪律
- 构想 v0-v5——历史方案，只读

## 平台

主开发环境 Windows + PowerShell 5.1；Python ≥3.11；产物尽可能落 D 盘。
