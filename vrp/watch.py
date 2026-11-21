"""I7（构想v11）：`run --watch` 文件变更自动重跑管道（开发时实时反馈）。

判定=绕（轮询 mtime+防抖即可，实时性非刚需——watchdog/entr/ptw 形态
参照，零依赖纯 stdlib）。J8 增量化（#190）是成本前提：变更后重跑走
增量链（未变文件 content-hash 命中跳过），轮询触发才有实用性。

与 v10-E13（MCP drift 订阅推送）区分：这是 CLI 消费面，前台循环，
非守护进程（三红线：无服务端）。

形态：os.walk 快照（mtime_ns+size，排除目录剪枝）→ WatchLoop 防抖
状态机（变更持续则窗口滑动，静默 ≥debounce 才触发——编辑器保存风暴
不连发）→ run_watch 驱动（初始全跑一次建基线 → 轮询 → 变更触发
run_once 重跑；Ctrl+C 干净退出 rc=0；rerun 失败/异常不杀 watch，
诚实打印后继续盯）。
"""

from __future__ import annotations

import os
import stat as stat_module
import time
from pathlib import Path
from typing import Callable

DEFAULT_INTERVAL = 2.0
DEFAULT_DEBOUNCE = 1.0
SAMPLE_CAP = 3

# 轮询剪枝：产物目录（.vrp/.git——重跑自己写的面，不剪会自我触发）、
# 环境噪音目录（缓存/依赖）。缺一不可的自我触发红线：run_once 产物
# 全在 .vrp/ 下，剪掉即闭环。
EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".vrp",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
    }
)

FeedStatus = str  # "steady" | "pending" | "fire"
TreeState = dict[str, tuple[int, int]]  # rel posix path -> (mtime_ns, size)


def scan_tree(repo: Path) -> TreeState:
    """os.walk 快照：rel posix 路径 -> (st_mtime_ns, st_size)。

    非目录 → 空字典（诚实空，不炸——调用方 run_watch 前已有 repo 校验）。
    走查中被删文件（stat OSError）跳过不炸；非普通文件（套接字等）跳过。
    """
    state: TreeState = {}
    if not repo.is_dir():
        return state
    for dirpath, dirnames, filenames in os.walk(repo, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            try:
                st = fpath.stat()
            except OSError:
                continue  # 走查中被删/被锁：跳过
            if not stat_module.S_ISREG(st.st_mode):
                continue
            state[fpath.relative_to(repo).as_posix()] = (st.st_mtime_ns, st.st_size)
    return state


def diff_state(old: TreeState, new: TreeState, sample_cap: int = SAMPLE_CAP) -> dict:
    """两快照差分：added/removed/modified 计数+样本（cap 截断防刷屏）。"""
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    return {
        "added": len(added),
        "removed": len(removed),
        "modified": len(modified),
        "added_samples": added[:sample_cap],
        "removed_samples": removed[:sample_cap],
        "modified_samples": modified[:sample_cap],
    }


class WatchLoop:
    """防抖状态机：feed(快照) -> "steady" | "pending" | "fire"。

    - 变更快照：窗口滑动（pending_since=now）——变更持续则永不触发。
    - 静默快照：距上次变更 ≥debounce → "fire"（last_diff 填好，基线
      前移，窗口复位）；否则 "pending"。
    - 无窗口且无变更："steady"。
    时钟/快照全部注入——状态机纯逻辑可测（run_watch 只做 IO 编排）。
    """

    def __init__(self, debounce: float, now: Callable[[], float] = time.monotonic) -> None:
        self._debounce = float(debounce)
        self._now = now
        self._baseline: TreeState | None = None
        self._last: TreeState | None = None
        self._pending_since: float | None = None
        self.last_diff: dict | None = None

    def feed(self, snapshot: TreeState) -> FeedStatus:
        if self._baseline is None:
            self._baseline = dict(snapshot)
            self._last = dict(snapshot)
            return "steady"
        if snapshot != self._last:
            self._last = dict(snapshot)
            self._pending_since = self._now()
            return "pending"
        if self._pending_since is None:
            return "steady"
        if self._now() - self._pending_since >= self._debounce:
            self.last_diff = diff_state(self._baseline, snapshot)
            self._baseline = dict(snapshot)
            self._pending_since = None
            return "fire"
        return "pending"


def run_watch(
    repo: Path,
    interval: float,
    debounce: float,
    run_once: Callable[[], int],
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> int:
    """驱动：初始跑一次建基线 → 轮询 → 变更触发重跑。Ctrl+C 退出 rc=0。

    重跑失败（rc!=0）或异常不杀 watch——打印后继续盯（实时反馈面的
    生存性优先；真正的失败语义由单次 run 的调用方消费）。
    """
    log(f"watch: initial run on {repo}")
    run_once()
    loop = WatchLoop(debounce, now=now)
    loop.feed(scan_tree(repo))
    log(
        f"watch: watching {repo} (interval {interval}s, debounce {debounce}s,"
        f" excludes {len(EXCLUDE_DIRS)} dirs)"
    )
    log("watch: Ctrl+C to stop")
    try:
        while True:
            sleep(interval)
            status = loop.feed(scan_tree(repo))
            if status != "fire":
                continue
            assert loop.last_diff is not None
            log(f"watch: changes {loop.last_diff} - rerunning pipeline")
            try:
                rc = run_once()
                if rc != 0:
                    log(f"watch: pipeline rc={rc} (continuing to watch)")
            except Exception as exc:  # noqa: BLE001 - watch 必须比管道故障更长寿
                log(f"watch: pipeline error: {exc} (continuing to watch)")
    except KeyboardInterrupt:
        log("watch: stopped (Ctrl+C)")
        return 0
