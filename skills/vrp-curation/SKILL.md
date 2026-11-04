---
name: vrp-curation
description: vrp 梳理生产指南——AI 产出 slices/structure 工件、经 curate 验收入库的操作协议。当需要梳理业务线（slices）、整体结构（structure）、处理 curate 收据拒绝码时使用。
---

# vrp-curation：梳理生产指南

VRP 主流程②环（AI 梳理）操作协议。前置阅读：`docs/契约/梳理工件契约.md`（schema 权威）。

## 定位

把 AI 对代码库的理解**结构化沉淀**为可验证数据：
- **线（slices）**：业务脉络——步骤序列，每步锚定真实符号，相邻步骤必须有真实调用边
- **面（structure）**：整体结构——模块划分 + 层序 DAG + 架构规则（S7 执法原料）

验证是**确定性的**（机器验，全过才入库）；**价值判断**留给用户（`--reject` 否决留痕）。
你（AI）生产，curate 验收，用户拍板。

## 生产流程（六步）

```
1. 取材     dossier / nav_search 定位目标业务
2. 核实     nav_callers / nav_callees 找真实相邻符号（不许跨跳、不许虚构 fid）
3. 产出     按 §1.1/§2.1 schema 写 slices.json / structure.json
4. 提交     curate_submit(kind, payload_json) 或落 .vrp/ai/ 后 vrp curate <repo>
5. 读收据   accepted → 完成；rejected → 读 errors[].code 逐条修正后整体重提
6. 下钻     nav_slice(slice_id) 复核入库结果
```

## 验证红线（被拒的常见原因）

| 你犯的错 | 收据码 |
|---|---|
| steps 相邻符号间没有真实边（跨跳/想当然） | `SLICE_MISSING_EDGE` |
| fid 过期（事务提交后没重新 nav_search） | `SLICE_UNKNOWN_FID` |
| 同一符号分给两个模块 | `STRUCT_FID_OVERLAP` |
| 层依赖成环 | `STRUCT_LAYER_CYCLE` |
| module.layer / rule 层引用不在 layers | `STRUCT_UNKNOWN_LAYER` / `STRUCT_RULE_LAYER` |
| acts 分幕不衔接/不覆盖全程 | `SLICE_ACT_BOUNDS` |

## 规则

1. **fid 只能来自 nav 族查询**——虚构的 fid 一定过不了存在性验证
2. **相邻必须有边**：`CALLS`/`CALL_REFERENCE`/`HTTP_CALLS` 任一方向；先 nav_callers/nav_callees 核实再写 steps
3. **原子性**：一个文件里任何一条不过，整个文件都拒——修完再整体重提
4. **幂等**：同 slice_id 重提交覆盖旧版；structure 单行覆盖——大胆改版重提
5. **诚实**：业务线画不出真实边就缩短范围；不许为了凑步骤硬编
6. 用户否决（status=rejected）是终态——不许重提同 id 对抗用户决定；换 slice_id 重新梳理需用户明示

## 拒绝码速查

slices：`SLICE_SCHEMA` / `SLICE_EMPTY_NAME` / `SLICE_EMPTY_SUMMARY` / `SLICE_UNKNOWN_FID` / `SLICE_MISSING_EDGE` / `SLICE_ACT_BOUNDS`
structure：`STRUCT_SCHEMA` / `STRUCT_EMPTY_NAME` / `STRUCT_EMPTY_RESPONSIBILITY` / `STRUCT_UNKNOWN_FID` / `STRUCT_FID_OVERLAP` / `STRUCT_LAYER_CYCLE` / `STRUCT_UNKNOWN_LAYER` / `STRUCT_RULE_LAYER`
文件级：`FILE_NOT_JSON` / `FILE_WRONG_SHAPE`

## 工具面

| 工具 | 用途 |
|---|---|
| `curate_submit(kind, payload_json)` | 提交验证入库（kind=slices/structure） |
| `curate_slices()` / `curate_structure()` | 读已入库工件与状态 |
| `curate_receipt()` | 读最近验收收据 |
| `curate_reject(kind, target_id, note)` | 代转达用户否决（保守方向允许 agent 转） |
| `nav_slice(slice_id)` | 业务线下钻核对 |
| CLI `vrp curate <repo>` / `--reject <id> --note ...` | 人工入口 |
