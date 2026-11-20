# Checklist：并发归并确定性（BB5）

> **状态**：已实施（2026-11-21 批④，交接 #202；构想v13 BB5）。
>
> 红线背景：同 facts.db → 下游产物字节级可复现。并行步骤（解析/查询/写入）
> 的产物归并处若继承完成序（`as_completed`/调度序），"last-writer-wins /
> first-match-wins"即非确定——字节复现从数据流剔除。

## 参照事故（CGC，防转述失真锚点）

`verify/CodeGraphContext/src/codegraphcontext/tools/indexing/pipeline.py`：

- `:234` 图写循环前按 path 排序——注释明言 "parsing remains concurrent, but
  graph writes are ordered"（共享节点 canonical 元数据确定性）；
- `:308-318` 后处理前再排——`as_completed` 完成序泄漏进下游 map 构建
  （global_class_bases / interface_implementors / class_method_index），
  "any last-writer-wins or first-match-wins choice becomes nondeterministic"。

## Checklist（新并行代码点必过审查项）

1. **归并点显式排序**：并行产物装配前按提交序或键序排序；完成序
   （`as_completed`/调度序）禁止进入下游任何 map/first-match/last-writer 面。
2. **输出与串行恒等**：并行路径的最终产物（findings/JSON/DB 行集）与串行
   路径等价（时点列 id/created_at 除外——J8 同裁定）。
3. **优先复用 `procs.run_thunks`**：契约已定（提交序返回/首错按任务序/全跑完
   不取消）；裸 `ThreadPoolExecutor`/自建线程池默认拒绝。
4. **写面零交叉**：并行单元只读或写互不交叉的面（J9 D1 前提：读写面零交叉
   是结构性前提，不是运行时巧合）；共享写面在归并排序后单线程写。
5. **过 census**：`tests/test_parallel_merge_census.py` 三条守卫
   （as_completed 禁用 / ThreadPoolExecutor 仅 procs.py / threading·asyncio
   允许面钉死）——新增并行原语使用点必须先过本 checklist 再有意扩 census。

## 现有并行面审计（批④，2026-11-21）

| 面 | 机制 | 归并确定性 |
|---|---|---|
| `procs.run_thunks` | 提交序 futures 逐序 `result()`（非 as_completed） | ✅ 契约内嵌（procs.py D4 注释；test_s2_parallel 契约测试） |
| `cli.cmd_s2`（J9 四引擎并发） | 固定引擎序装配（astgrep→deadcode→p4→jscpd），结果按提交序返回 | ✅ 输出与串行恒等（设计稿-J9 D6；串行 vs 并行对拍测试） |
| `procs.run_stream` watchdog/writer 线程 | 单子进程生命周期管理（Timer 树杀/stdin 喂入） | 非归并点 ✅ |
| `s5_cabrief` watchdog 线程 | 同上（Timer 树杀） | 非归并点 ✅ |
| `s7_enforce._run_mixed` 分治分块 | 单线程；分块 count 降序+键升序 | ✅ 确定性分块（批③ C7） |
| asyncio / multiprocessing / as_completed | vrp 运行时零使用 | census 钉死 ✅ |

审计结论：全部并行归并点确定性成立；唯一归并原语 `run_thunks` 的契约即
checklist 第 1/2/3 条的实例化。新并行面出现时按上表追加行并过 checklist。
