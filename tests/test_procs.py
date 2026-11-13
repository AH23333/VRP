"""procs 树型超时击杀共享层（R-L 批）。

- POSIX 分支：killpg 整组信号（stub 静态验证——Windows-only 机器无法
  执行真 syscall；同型实现自 s4_freeze 冻结流程移植，已被其真超时测试
  覆盖）。
- Windows 分支：真树杀 E2E——孙进程存活的超时树必须全灭（subprocess
  超时只杀直子的孤儿面，实证见 work/repro_rl_tree.py R1a/R1b）。
- 解码纪律：utf-8 + errors=replace（读线程 strict 解码错误静默吞掉
  整段输出，stdout=None——R-L R2a 实证）。

R-W 批（计时确定性）：
- _pid_alive 改 ctypes OpenProcess 探针——旧 tasklist /FI 每次探测
  spawn 一个进程（满载下单次 >1s），是 deadline 预算的隐形吞吃者；
  ctypes 探针 µs 级、零 spawn、负载免疫。
- _wait_dead 改无 sleep 自旋——run_tree 超时分支的 communicate() 返回
  ⇔ 管道 EOF ⇔ 全部写端句柄释放 ⇔ 孙进程已死（R-L 批孤儿持管道实证
  的逆命题），自旋只容忍句柄拆除与进程表更新的内核滞后窗口；绿径
  首查即返，红径有界 2s 响亮失败（deadline 永不掩盖真回归）。
- parent 脚本加 Popen 重试环（满载/内存压力下瞬时 spawn 失败会令
  parent 提前退出 → timed_out=False 假红——重试使 parent 只能被击杀
  或自然走完，两轮全量各抖 1 次的加固面）；run_tree timeout 2→6s
  给满载解释器启动留 3x 余量（纯 CPU 满载实测单轮 ~2.9s 全绿，
  work/probe_rw_stress.py 12/12）。
"""

from __future__ import annotations

import ctypes
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
    """ctypes liveness 探针（R-W）：OpenProcess + GetExitCodeProcess。

    零进程 spawn、µs 级、负载免疫；进程不存在/已退出/句柄拒开 → False。
    STILL_ACTIVE(259) 语义沿用 Win32 惯例（受害进程被 taskkill /F 击杀，
    真退出码非 259，无误判面）。
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _wait_dead(pid: int, seconds: float = 2.0) -> bool:
    """拆除滞后容忍自旋（无 sleep——绿径首查即返）。

    按构造：run_tree 超时分支返回时管道已 EOF ⇒ 全部写端句柄已释放
    ⇒ 孙进程已死；本自旋只覆盖句柄拆除与进程表更新之间的内核滞后
    窗口。超 seconds 仍活 = 树杀语义真回归（直子击杀留下孤儿持管道），
    响亮红——deadline 不决定绿，只给红设上限。
    """
    deadline = time.monotonic() + seconds
    while True:
        if not _pid_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 真树杀 E2E")
def test_run_tree_timeout_kills_whole_windows_tree(tmp_path):
    gc = tmp_path / "gc.py"
    gc.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        f"gc_path = r'{gc}'\n"
        # 瞬时 spawn 失败（满载/内存压力）重试而非崩溃退出——parent 只能
        # 被击杀或走完，timed_out=False 假红面（R-W）随之关闭。
        "while True:\n"
        "    try:\n"
        "        p = subprocess.Popen([sys.executable, gc_path])\n"
        "        break\n"
        "    except OSError:\n"
        "        time.sleep(0.05)\n"
        f"open(r'{tmp_path / 'gc.pid'}', 'w').write(str(p.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    # timeout 6s（原 2s）：满载解释器启动可达数秒，2s 会赶在 gc.pid 写出
    # 前击杀 parent（FileNotFoundError 假红面）；树杀语义与 timeout 精度
    # 无关（精度由 test_s0_timeout 独立钉），6s 给启动留 3x 余量。
    rr = procs.run_tree([sys.executable, str(parent)], cwd=tmp_path, timeout=6)
    assert rr["timed_out"] is True, (
        f"parent exited before timeout (regression or victim crash); "
        f"returncode={rr['returncode']} stderr tail: {rr['stderr'][-500:]}"
    )
    gc_pid = int((tmp_path / "gc.pid").read_text())
    # 孤儿孙进程必须随树全灭（直子击杀会留下它继续持管道/目录锁）
    assert _wait_dead(gc_pid), f"grandchild pid={gc_pid} survived the tree kill"


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
