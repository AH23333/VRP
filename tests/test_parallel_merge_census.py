"""BB5 并发归并确定性 census（批④）：新并行代码点审查项的机械守卫面。

CGC 事故参照（verify/CodeGraphContext/.../pipeline.py:308-318）：asyncio.
as_completed 完成序泄漏进下游 map 构建，"any last-writer-wins or
first-match-wins choice becomes nondeterministic"。本 census 把教训钉进
测试面——新增并行原语使用点先过 docs/Checklist-并发归并确定性.md 再扩
允许面（census 红即审查对话的强制触发器）。
"""

from pathlib import Path

VRP_DIR = Path(__file__).parent.parent / "vrp"

_PY_FILES = sorted(VRP_DIR.glob("*.py"))


def _files_containing(token: str) -> set[str]:
    return {p.name for p in _PY_FILES if token in p.read_text(encoding="utf-8-sig")}


def test_no_as_completed():
    """checklist 第 1 条机械面：完成序（as_completed）禁止进入归并面。"""
    offenders = _files_containing("as_completed")
    assert not offenders, (
        f"as_completed 出现于 {sorted(offenders)}——完成序归并非确定，"
        "见 docs/Checklist-并发归并确定性.md（归并点必须显式排序）"
    )


def test_threadpool_only_in_procs():
    """checklist 第 3 条机械面：裸 ThreadPoolExecutor 仅允许 procs.py（run_thunks 单一归并原语）。"""
    assert _files_containing("ThreadPoolExecutor") == {"procs.py"}, (
        "新线程池使用点必须复用 procs.run_thunks（契约已定：提交序返回/首错按任务序/全跑完不取消），"
        "见 docs/Checklist-并发归并确定性.md"
    )


def test_threading_and_asyncio_allowed_sites_pinned():
    """threading/asyncio 允许面钉死：新线程/协程使用点必须过 checklist 后扩本断言。"""
    assert _files_containing("threading.") == {"procs.py", "s5_cabrief.py"}, (
        "新 threading 使用点必须过 docs/Checklist-并发归并确定性.md 审查后扩本 census 允许面"
    )
    assert not _files_containing("asyncio"), (
        "vrp 运行时 asyncio 零使用（BB5 审计面）——引入前过 checklist（as_completed 完成序泄漏教训）"
    )
