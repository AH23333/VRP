"""I7 `run --watch`（批⑩ #208）：轮询 mtime+防抖自动重跑。

纯 stdlib（watchdog 判定=绕）；状态机时钟/快照全注入可测；驱动面
monkeypatch scan_tree。红线钉死：产物目录剪枝（不剪 .vrp 即自我触
发死循环）；rerun 失败/异常不杀 watch；初始 run 失败诚实传播（吞初
始失败=裸跑基线说谎）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vrp import cli
from vrp import watch as watch_mod


def _touch(path: Path, mtime_ns: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


# ---------- scan_tree ----------


def test_scan_tree_captures_files_posix_paths(tmp_path: Path) -> None:
    _touch(tmp_path / "a.py", 1_000_000_000)
    _touch(tmp_path / "sub" / "b.md", 2_000_000_000)
    state = watch_mod.scan_tree(tmp_path)
    assert set(state) == {"a.py", "sub/b.md"}
    assert state["a.py"] == (1_000_000_000, 1)


def test_scan_tree_excludes_noise_dirs(tmp_path: Path) -> None:
    _touch(tmp_path / "keep.py")
    for noise in (".vrp", ".git", "__pycache__", "node_modules", ".venv", "dist"):
        _touch(tmp_path / noise / "junk.bin")
    state = watch_mod.scan_tree(tmp_path)
    assert set(state) == {"keep.py"}


def test_scan_tree_missing_repo_empty(tmp_path: Path) -> None:
    assert watch_mod.scan_tree(tmp_path / "nope") == {}


def test_scan_tree_deterministic(tmp_path: Path) -> None:
    _touch(tmp_path / "a.py", 5_000_000_000)
    _touch(tmp_path / "b.py", 6_000_000_000)
    assert watch_mod.scan_tree(tmp_path) == watch_mod.scan_tree(tmp_path)


def test_scan_tree_sees_add_remove_modify(tmp_path: Path) -> None:
    _touch(tmp_path / "a.py", 1_000_000_000)
    _touch(tmp_path / "b.py", 2_000_000_000)
    before = watch_mod.scan_tree(tmp_path)
    (tmp_path / "a.py").unlink()  # removed
    os.utime(tmp_path / "b.py", ns=(9_000_000_000, 9_000_000_000))  # modified
    _touch(tmp_path / "c.py", 3_000_000_000)  # added
    after = watch_mod.scan_tree(tmp_path)
    diff = watch_mod.diff_state(before, after)
    assert diff["added"] == 1 and diff["added_samples"] == ["c.py"]
    assert diff["removed"] == 1 and diff["removed_samples"] == ["a.py"]
    assert diff["modified"] == 1 and diff["modified_samples"] == ["b.py"]


# ---------- diff_state ----------


def test_diff_state_identical_zero() -> None:
    state = {"a.py": (1, 1)}
    diff = watch_mod.diff_state(state, dict(state))
    assert diff["added"] == diff["removed"] == diff["modified"] == 0
    assert diff["added_samples"] == diff["removed_samples"] == diff["modified_samples"] == []


def test_diff_state_sample_cap() -> None:
    old: dict[str, tuple[int, int]] = {f"f{i}.py": (i, i) for i in range(6)}
    new: dict[str, tuple[int, int]] = {f"g{i}.py": (i, i) for i in range(6)}
    diff = watch_mod.diff_state(old, new)
    assert diff["added"] == 6 and diff["removed"] == 6
    assert len(diff["added_samples"]) == 3  # SAMPLE_CAP 截断防刷屏


# ---------- WatchLoop 状态机（fake clock） ----------


class _Clock:
    def __init__(self, *times: float) -> None:
        self._times = list(times)
        self._i = 0

    def __call__(self) -> float:
        t = self._times[min(self._i, len(self._times) - 1)]
        self._i += 1
        return t


def _state(tag: str) -> dict[str, tuple[int, int]]:
    return {"a.py": (hash(tag) % 10_000, 1)}


def test_loop_seed_then_steady() -> None:
    loop = watch_mod.WatchLoop(1.0, now=_Clock(0.0, 0.1, 0.2))
    s = _state("base")
    assert loop.feed(dict(s)) == "steady"
    assert loop.feed(dict(s)) == "steady"
    assert loop.feed(dict(s)) == "steady"


def test_loop_pending_then_fire_once() -> None:
    loop = watch_mod.WatchLoop(1.0, now=_Clock(0.0, 0.5, 1.2, 1.5, 1.9))
    base = _state("base")
    assert loop.feed(dict(base)) == "steady"
    changed = _state("changed")
    assert loop.feed(dict(changed)) == "pending"  # 变更起窗
    assert loop.feed(dict(changed)) == "pending"  # 静默 0.7s < 1.0
    assert loop.feed(dict(changed)) == "fire"  # 静默 1.4s >= 1.0
    assert loop.last_diff is not None and loop.last_diff["modified"] == 1
    assert loop.feed(dict(changed)) == "steady"  # 触发后复位——只发一次


def test_loop_continued_changes_slide_window() -> None:
    # 变更持续（t=0.0/1.0/2.0 每次都新内容）→ 窗口滑动永不触发
    clock = _Clock(0.0, 0.0, 1.0, 2.0, 2.1, 2.2)
    loop = watch_mod.WatchLoop(1.5, now=clock)
    loop.feed(_state("base"))
    assert loop.feed(_state("c1")) == "pending"
    assert loop.feed(_state("c2")) == "pending"
    assert loop.feed(_state("c3")) == "pending"
    # 静默但 0.1s/0.2s < 1.5 → 仍 pending
    assert loop.feed(_state("c3")) == "pending"
    assert loop.feed(_state("c3")) == "pending"


def test_loop_debounce_zero_fires_on_next_stable() -> None:
    loop = watch_mod.WatchLoop(0.0, now=_Clock(0.0, 1.0, 2.0))
    loop.feed(_state("base"))
    assert loop.feed(_state("c1")) == "pending"
    assert loop.feed(_state("c1")) == "fire"


def test_loop_fire_resets_baseline_new_cycle() -> None:
    loop = watch_mod.WatchLoop(0.0, now=_Clock(0.0, 1.0, 2.0, 3.0, 4.0, 5.0))
    loop.feed(_state("base"))
    assert loop.feed(_state("c1")) == "pending"
    assert loop.feed(_state("c1")) == "fire"
    assert loop.last_diff is not None and loop.last_diff["modified"] == 1
    # 第二轮：再变更 → 新 pending → fire；last_diff 只报新变更
    assert loop.feed(_state("c2")) == "pending"
    assert loop.feed(_state("c2")) == "fire"
    assert loop.last_diff is not None
    assert loop.last_diff["modified"] == 1 and loop.last_diff["modified_samples"] == ["a.py"]


# ---------- run_watch 驱动（monkeypatch scan_tree） ----------


class _Script:
    """注入编排：scan 序列逐个吐出；sleep 第 n 次抛 KI 收束循环。"""

    def __init__(self, scans: list[dict[str, tuple[int, int]]], stop_at_sleep: int) -> None:
        self._scans = list(scans)
        self._i = 0
        self._sleep_calls = 0
        self._stop_at = stop_at_sleep
        self.logs: list[str] = []

    def scan(self, repo: Path) -> dict[str, tuple[int, int]]:
        scan = self._scans[min(self._i, len(self._scans) - 1)]
        self._i += 1
        return dict(scan)

    def sleep(self, seconds: float) -> None:
        self._sleep_calls += 1
        if self._sleep_calls >= self._stop_at:
            raise KeyboardInterrupt

    def log(self, msg: str) -> None:
        self.logs.append(msg)


def test_run_watch_fires_and_survives_rc(monkeypatch, tmp_path: Path) -> None:
    s0 = _state("base")
    s1 = _state("changed")
    script = _Script(scans=[s0, s0, s1, s1, s1], stop_at_sleep=5)
    monkeypatch.setattr(watch_mod, "scan_tree", script.scan)
    calls: list[int] = []

    def run_once() -> int:
        calls.append(len(calls))
        return 1  # rc!=0：watch 不死，继续盯

    rc = watch_mod.run_watch(
        tmp_path, interval=0.01, debounce=0.0, run_once=run_once,
        sleep=script.sleep, now=_Clock(0, 1, 2, 3, 4, 5, 6), log=script.log,
    )
    assert rc == 0  # Ctrl+C 干净退出
    assert len(calls) == 2  # 初始 1 次 + fire 1 次（rc=1 不拦后续）
    assert any("pipeline rc=1" in m for m in script.logs)


def test_run_watch_pipeline_exception_continues(monkeypatch, tmp_path: Path) -> None:
    s0 = _state("base")
    s1 = _state("changed1")
    s2 = _state("changed2")
    # fire 轮1（run_once 抛）→ 继续盯 → fire 轮2 正常跑
    script = _Script(scans=[s0, s0, s1, s1, s1, s2, s2], stop_at_sleep=7)
    monkeypatch.setattr(watch_mod, "scan_tree", script.scan)
    calls: list[int] = []

    def run_once() -> int:
        calls.append(len(calls))
        if len(calls) == 2:
            raise ValueError("boom")
        return 0

    rc = watch_mod.run_watch(
        tmp_path, interval=0.01, debounce=0.0, run_once=run_once,
        sleep=script.sleep, now=_Clock(*range(20)), log=script.log,
    )
    assert rc == 0
    assert len(calls) == 3  # 初始 + fire1（炸）+ fire2（正常）
    assert any("pipeline error: boom" in m for m in script.logs)


def test_run_watch_initial_failure_propagates(monkeypatch, tmp_path: Path) -> None:
    script = _Script(scans=[_state("base")], stop_at_sleep=99)
    monkeypatch.setattr(watch_mod, "scan_tree", script.scan)

    def run_once() -> int:
        raise RuntimeError("no cbm")

    with pytest.raises(RuntimeError, match="no cbm"):
        watch_mod.run_watch(
            tmp_path, interval=0.01, debounce=0.0, run_once=run_once,
            sleep=script.sleep, now=_Clock(0, 1, 2), log=script.log,
        )


# ---------- CLI 接线 ----------


def test_run_parser_watch_flags_defaults() -> None:
    args = cli.build_parser().parse_args(["run", "somerepo"])
    assert args.watch is False
    assert args.interval == 2.0
    assert args.debounce == 1.0


def test_run_parser_watch_flags_explicit() -> None:
    args = cli.build_parser().parse_args(
        ["run", "somerepo", "--watch", "--interval", "5", "--debounce", "2.5"]
    )
    assert args.watch is True
    assert args.interval == 5.0
    assert args.debounce == 2.5


def test_cmd_run_watch_delegates(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_run_watch(repo, interval, debounce, run_once):
        captured["repo"] = repo
        captured["interval"] = interval
        captured["debounce"] = debounce
        captured["run_once"] = run_once
        return 42

    monkeypatch.setattr(cli.watch_mod, "run_watch", fake_run_watch)
    args = cli.build_parser().parse_args(["run", str(tmp_path), "--watch"])
    assert cli.cmd_run(args) == 42
    assert captured["repo"] == tmp_path.resolve()
    assert captured["interval"] == 2.0 and captured["debounce"] == 1.0
    assert callable(captured["run_once"])
