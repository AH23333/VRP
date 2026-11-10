"""s0 超时纪律（R-L 批）：git 快照 fail-fast / census 降级。

- _run：无 timeout 的挂死面（死盘/网络驱动器级）改为 120s 上限 +
  RuntimeError 响亮失败——is_repo/dirty 的任何降级取值都是静默谎言
  （脏树前置是 R-A 硬保证），让 cli 以 rc 1 诚实退出。
- census_scc：300s 上限，超时返回 None 走 census_fallback（既有降级，
  census 是展示性数据不值得炸 precheck）。
"""

from __future__ import annotations

import subprocess
import sys
import time

from vrp import s0_precheck as s0


def test_run_timeout_raises_runtime_error_fast(tmp_path):
    t0 = time.monotonic()
    try:
        s0._run([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, timeout=1)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "timed out" in str(exc)
    assert time.monotonic() - t0 < 4.0


def test_run_pins_default_timeout(tmp_path, monkeypatch):
    seen: dict = {}

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return _P()

    monkeypatch.setattr(s0.subprocess, "run", fake_run)
    rc, _ = s0._run(["git", "rev-parse", "HEAD"], tmp_path)
    assert rc == 0
    assert seen.get("timeout") == 120


def test_census_scc_pins_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(s0.shutil, "which", lambda name: "fake-scc")
    seen: dict = {}

    class _P:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return _P()

    monkeypatch.setattr(s0.subprocess, "run", fake_run)
    assert s0.census_scc(tmp_path) == {"tool": "scc", "languages": []}
    assert seen.get("timeout") == 300


def test_census_scc_timeout_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(s0.shutil, "which", lambda name: "fake-scc")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(s0.subprocess, "run", fake_run)
    assert s0.census_scc(tmp_path) is None
