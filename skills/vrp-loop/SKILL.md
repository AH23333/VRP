---
name: vrp-loop
description: 用 vrp MCP 工具执行 AI 重构循环：导航定位→下钻核实→事务生产→preview 自查→执行→读回执→失败修正重试（≤3 次/事务）。适用任何接入了 vrp MCP server 的 agent。
---

# vrp-loop：AI 重构循环协议 v1

## 前置

- MCP server 已接入（`vrp mcp <repo>`，stdio，per-repo 绑定；`pip install vrp[mcp]`）
- 事实层已建好（facts.db 存在；若 nav_* 报 facts.db missing，先请用户跑 `vrp run <repo>`）
- 事务契约：`docs/契约/事务契约.md`（schema/G 码/生产指南——生产前必读）

## 循环 8 步（构想v7 §1.5）

1. **定位**：`nav_search(query)` 拿候选符号（fid 句柄 + file_path）
   - 全局地图先看 `nav_architecture()`（目录聚合：符号数/病理数分布）
2. **下钻核实**：`nav_module(module_id=file_path)` → `nav_symbol(fid)`
   - **全链下钻**：callers/callees 的 fid 继续下钻，直到确认影响面闭合
   - 不读裸代码文件——认知来自分层接口（`query_facts`/dossier 补充）
3. **生产事务**：按契约组装 tx_json（`{"id","type","summary","declared_moves","edits"}`）
   - **最小事务**：一个事务一个意图；edits 全文覆写（先取现文件内容再产全文）
   - T1 的 declared_moves 必须完备（以 nav_symbol/query_facts 查到的 qualified_name 为准，勿凭记忆）
4. **preview 自查**：`tx_preview(tx_json)` 读 unified diff——确认改动面 = 声明意图
5. **执行**：`tx_run(tx_json)`
   - T4 会返回 needs_confirmation——**不要重试**，整理 diff 摘要请用户人工放行（CLI `vrp transact --confirm`）
6. **读回执**：`tx_verdicts()`；失败读 `failed_gate` + `failed_codes`
7. **修正重试**：按失败码修 tx_json 重新 preview→run；**同一事务 ≤3 次重试**，3 次不过换思路或放弃该目标并向用户说明
8. **回归**：目标批次完成后请用户跑 `vrp run <repo>` 重建事实层 + `vrp dossier` 复核病理变化（open verdict 会自动出现在 dossier evidence 节）

## 失败码速查（详见契约 §4）

| 码 | 含义 | 修正方向 |
|---|---|---|
| `gate/G0/apply-edits` | 目标缺失/已存在 | 核对 file 路径与 op；先 nav_module 看现状 |
| `gate/G1/parse-errors` | 编辑引入语法错误 | 重产全文 content |
| `gate/G2/static-check` | lint/typecheck 挂 | 读 detail 修 lint |
| `gate/G3/test` | 测试挂 | 读 detail.failed 测试名；判断是事务错还是测试需更新 |
| `gate/G4/undeclared-change` | T1 有未声明符号变更 | 补 declared_moves 或拆事务 |
| `gate/G4/dangling-reference` | T2 删了仍被引用的符号 | 缩小删除范围 |
| `gate/G4/edge-mismatch` | T1 使用边不保 | 漏改调用方；nav_symbol callees 核实 |
| `gate/G4/verify-broken` | code-atlas verify_changes BROKEN | 结构性破坏，重新设计 |
| `gate/G4/structural-change` | 未声明内容变化（difftastic） | 撤偷改或如实声明换档位 |
| `gate/G5/deadcode-increase` | T2/T4 引入新死码 | 删除新增的死路径 |

## 红线

- **不读裸代码**当认知来源（接口拿不到再用 query_facts）
- **不放行 T4**（needs_confirmation 即停，转人工）
- **重试 ≤3 次**/事务
- **不虚构 fid/qualified_name**（接口查不到就是不存在的处理路径）

## 关联

- ②环梳理（业务线/结构沉淀）走 `skills/vrp-curation/`（curate_submit 验收入库 → nav_slice 下钻）
- 事务 schema 权威：`docs/契约/事务契约.md`；梳理工件 schema 权威：`docs/契约/梳理工件契约.md`
