"""s0 超时纪律（R-L 批；R-AP 收编 procs.run_tree 树杀）：git 快照 fail-fast / census 降级。

- _run：无 timeout 的挂死面（死盘/网络驱动器级）改为 120s 上限 +
  RuntimeError 响亮失败——is_repo/dirty 的任何降级取值都是静默谎言
  （脏树前置是 R-A 硬保证），让 cli 以 rc 1 诚实退出。R-AP 起底层为
  procs.run_tree（超时杀全树），timed_out → RuntimeError 语义不变。
- census_scc：300s 上限，超时返回 None 走 census_fallback（既有降级，
  census 是展示性数据不值得炸 precheck）。
"""

from __future__ import annotations

import sys
import time

from vrp import s0_precheck as s0


def test_run_timeout_raises_runtime_error_fast(tmp_path):
    t0 = time.monotonic()
    try:
        # victim 30s（原 5s）+ 上界 10.0s（原 4.0s）：无击杀回归会等满
        # victim 自然退出（30s > 10s 仍红）；满载下击杀+拆除滞后有 9s
        # 余量（原 margin 仅 3s，与 test_procs R-W 同族的墙上界脆弱面）。
        s0._run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, timeout=1)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "timed out" in str(exc)
    assert time.monotonic() - t0 < 10.0


def test_run_pins_default_timeout(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_tree(cmd, cwd=None, timeout=None, env=None):
        seen["timeout"] = timeout
        return {"returncode": 0, "timed_out": False, "stdout": "", "stderr": ""}

    monkeypatch.setattr(s0.procs, "run_tree", fake_tree)
    rc, _, _ = s0._run(["git", "rev-parse", "HEAD"], tmp_path)
    assert rc == 0
    assert seen.get("timeout") == 120


def test_census_scc_pins_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(s0.shutil, "which", lambda name: "fake-scc")
    seen: dict = {}

    def fake_tree(cmd, cwd=None, timeout=None, env=None):
        seen["timeout"] = timeout
        return {"returncode": 0, "timed_out": False, "stdout": "[]", "stderr": ""}

    monkeypatch.setattr(s0.procs, "run_tree", fake_tree)
    assert s0.census_scc(tmp_path) == {"tool": "scc", "languages": []}
    assert seen.get("timeout") == 300


def test_census_scc_timeout_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(s0.shutil, "which", lambda name: "fake-scc")

    def fake_tree(cmd, cwd=None, timeout=None, env=None):
        return {"returncode": -1, "timed_out": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(s0.procs, "run_tree", fake_tree)
    assert s0.census_scc(tmp_path) is None


def test_census_scc_nondict_json_returns_none(tmp_path, monkeypatch):
    """AU-5（R-AU）：scc 合法 JSON 非 list（未钉 PATH 版本形状漂移）——同降级
    None 走 census_fallback，不裸抛 AttributeError 击穿 guard 元组。"""
    monkeypatch.setattr(s0.shutil, "which", lambda name: "fake-scc")

    def fake_tree(cmd, cwd=None, timeout=None, env=None):
        return {"returncode": 0, "timed_out": False, "stdout": '{"languages": []}', "stderr": ""}

    monkeypatch.setattr(s0.procs, "run_tree", fake_tree)
    assert s0.census_scc(tmp_path) is None


def test_git_snapshot_ignores_stderr_pollution(tmp_path, monkeypatch):
    """AU-2（R-AU）：git rev-parse HEAD 歧义时 stderr 警告（rc 仍 0）不得
    混入 head 提取——旧 _run 合并 stdout+stderr 使 facts.git_head 污染。"""
    (tmp_path / ".git").mkdir()
    head_hash = "deadbeef" * 5

    def fake_tree(cmd, cwd=None, timeout=None, env=None):
        if "--abbrev-ref" in cmd:
            out, err = "main\n", "warning: reflog noise\n"
        elif "status" in cmd:
            out, err = "", "warning: worktree hint\n"
        else:
            out, err = head_hash + "\n", "warning: ignoring broken ref refs/heads/other\n"
        return {"returncode": 0, "timed_out": False, "stdout": out, "stderr": err}

    monkeypatch.setattr(s0.procs, "run_tree", fake_tree)
    snap = s0.git_snapshot(tmp_path)
    assert snap["head"] == head_hash  # 纯净：stderr 警告零混入
    assert snap["branch"] == "main"
    assert snap["dirty"] is False
    assert snap["dirty_files"] == []
