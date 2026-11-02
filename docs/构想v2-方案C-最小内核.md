# 方案 C：最小内核（工件与闸门优先）

## 产品判断

回看 v1 的"需要自己补的核心缺口"：统一闸门、证据消费、流程换壳、语言扩展。其中真正不可替代、也不会被上游吞掉的是**工件（dossier / facts.db / freeze pack）和闸门**。编排逻辑（grill/plan 的 prompt 工程）会被各家通用 AI 自己进化掉，语言扩展会被上游工具迭代掉。

所以方案 C 反过来：**不建管道，建协议**。把自建面压到 5 个 CLI 命令 + 2 个文件格式，其余全部让给通用 AI 编排。

## 组件清单

5 个命令，每个都是纯函数式的：输入磁盘状态，输出工件，不持有会话。

| 命令 | 产出 | 说明 |
|---|---|---|
| `precheck` | capability-manifest.json | 同方案 A Stage 0 |
| `index` | facts.db | 包装单一上游 provider，落自有 schema |
| `freeze` | freeze pack | 表征 harness；不可运行时输出降级说明 |
| `transact <transaction.yaml>` | commit 或 rollback | 执行编辑 → 重扫 → gate → git 原子提交或回滚 |
| `dossier [--budget N]` | 分层视图 | 按 token 预算：模块地图/函数卡片/问题清单/证据边 |

2 个格式：

**facts.db schema**：canonical ID（file path + qualified name + content hash）+ 证据等级。

**transaction.yaml**：

```yaml
id: tx-0007
type: T1-equivalence        # T1/T2/T3/T4，强制单类型
declared_moves:             # 图匹配提示，闸门按此验证
  - from: fn:app.py:42:9f3a
    to: fn:services/auth.py:10:b7c1
edits:
  - file: services/auth.py
    op: create
    content_hash: ...
expected_delta:
  removed_edges: [...]
  added_edges: [...]
behavior:
  run_freeze_pack: true
```

## 通用 AI 的两种驱动方式

- **批处理模式（默认）**：AI 读 dossier，离线产出事务批次；人把 transaction.yaml 喂回 CLI。grill 问卷由 AI 自答并标注置信度，管道不等待。
- **会话模式**：AI 按需调用 5 个命令，自己控制循环与预算。

"面向通用 AI"的解耦主张在 C 里最彻底：工具对消费者的唯一要求是**会读 JSON、会写 YAML**。

## 为什么这可能才是对的

- 方案 A 最大的工程风险是编排层与 LLM 能力耦合：prompt 工程投入会随模型换代作废。C 把 prompt 完全外置，工具只做确定性部分。
- 闸门的事务分类学、ID 稳定性策略、降级矩阵全部继承自方案 A，只是宿主从"管道"变成"CLI 协议"。
- 最快拿到端到端闭环，用真实消费者的用法数据反过来决定管道（方案 A/B）要不要建。

## 边界与风险

- 编排质量完全取决于消费者 AI：烂 AI 产出烂事务。闸门能挡住危险部分，但 T4 类需要人工兜底。
- dossier 的信息架构成为核心体验，需要单独的迭代周期——它本质上是给 LLM 的"上下文工程"产品。
- 没有自己的执行回路，做不了方案 B 的 probe/trace。后续可把 `probe`/`trace` 作为第 6/7 个命令加入，格式不变。
