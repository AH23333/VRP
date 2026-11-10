"""procs 树型超时击杀共享层（R-L 批）。

- POSIX 分支：killpg 整组信号（stub 静态验证——Windows-only 机器无法
  执行真 syscall；同型实现自 s4_freeze 冻结流程移植，已被其真超时测试
  覆盖）。
- Windows 分支：真树杀 E2E——孙进程存活的超时树必须全灭（subprocess
  超时只杀直子的孤儿面，实证见 work/repro_rl_tree.py R1a/R1b）。
- 解码纪律：utf-8 + errors=replace（读线程 strict 解码错误静默吞掉
  整段输出，stdout=None——R-L R2a 实证）。
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from vrp import procs

FAKE_SIGKILL = 9  # stand-in for signal.SIGKILL on platforms lacking it


class _FakeProc:
    pid = 4242


@pytest.fixture()
def posix(monkeypatch):
    monkeypatch.setattr(procs.sys, "platform", "linux")
    monkeypatch.setattr(procs.signal, "SIGKILL", FAKE_SIGKILL, raising=False)
    calls: list[tuple] = []
    monkeypatch.setattr(
        procs.os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)), raising=False
    )
    return calls


def test_kill_tree_posix_signals_process_group(posix, monkeypatch):
    monkeypatch.setattr(
        procs.os, "getpgid", lambda pid: (posix.append(("getpgid", pid)), 9999)[1], raising=False
    )
    procs.kill_tree(_FakeProc())
    assert ("getpgid", 4242) in posix
    assert ("killpg", 9999, FAKE_SIGKILL) in posix


def test_kill_tree_posix_tolerates_dead_process(posix, monkeypatch):
    def _raise(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(procs.os, "getpgid", _raise, raising=False)
    procs.kill_tree(_FakeProc())  # must not raise
    assert not any(c[0] == "killpg" for c in posix)


def _pid_alive(pid: int) -> bool:
    r = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True,
    )
    return str(pid) in r.stdout


def _wait_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 真树杀 E2E")
def test_run_tree_timeout_kills_whole_windows_tree(tmp_path):
    gc = tmp_path / "gc.py"
    gc.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, r'{gc}'])\n"
        f"open(r'{tmp_path / 'gc.pid'}', 'w').write(str(p.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    rr = procs.run_tree([sys.executable, str(parent)], cwd=tmp_path, timeout=2)
    assert rr["timed_out"] is True
    gc_pid = int((tmp_path / "gc.pid").read_text())
    # 孤儿孙进程必须随树全灭（直子击杀会留下它继续持管道/目录锁）
    assert _wait_dead(gc_pid)


def test_run_tree_success_captures_output(tmp_path):
    rr = procs.run_tree([sys.executable, "-c", "print('ok-42')"], cwd=tmp_path, timeout=30)
    assert rr["timed_out"] is False
    assert rr["returncode"] == 0
    assert "ok-42" in rr["stdout"]


def test_run_tree_decodes_invalid_utf8_with_replace(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import sys\nsys.stdout.buffer.write(b'\\x80\\xff end')\n", encoding="utf-8")
    rr = procs.run_tree([sys.executable, str(bad)], cwd=tmp_path, timeout=30)
    assert rr["timed_out"] is False
    assert "end" in rr["stdout"]
    assert "\ufffd" in rr["stdout"]  # U+FFFD：errors=replace 的证据
