"""#147 测试：子进程低 CPU 优先级（学 TSA local_low_impact 的 nice -n 15 模式）。

覆盖：creationflags 平台分支/nice argv 前缀 which 探测/逃生门
VRP_CHILD_PRIORITY=normal/Windows 真测（run_tree 与 _run_one 子进程回读
自身优先级类断言 BELOW_NORMAL=0x4000）/POSIX 真测分支（skipif 守卫，
代码在测试在）。
"""

import shutil
import sys

import pytest

from vrp import procs, s4_freeze

# Win32 优先级类（kernel32 GetPriorityClass 回读值）
BELOW_NORMAL = 16384  # 0x4000
NORMAL = 32  # 0x20

# 子进程回读自身优先级类（Windows ctypes 纯 stdlib；GetCurrentProcess
# 伪句柄 -1 必须 wintypes.HANDLE restype——默认 c_int 32 位传递导致
# GetPriorityClass 失败返 0；无符号 64 位句柄传回须同步设 argtypes
# 否则 ctypes int 参数转换溢出）
_WIN_PRIORITY_PROBE = (
    "import ctypes; from ctypes import wintypes;"
    " k = ctypes.windll.kernel32; k.GetCurrentProcess.restype = wintypes.HANDLE;"
    " k.GetPriorityClass.argtypes = [wintypes.HANDLE];"
    " print(k.GetPriorityClass(k.GetCurrentProcess()))"
)


# ---------- 助手单测：low_priority_creationflags ----------


def test_creationflags_windows_below_normal(monkeypatch):
    """win32 默认：BELOW_NORMAL_PRIORITY_CLASS=0x4000。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("VRP_CHILD_PRIORITY", raising=False)
    assert procs.low_priority_creationflags() == 0x4000


def test_creationflags_posix_zero(monkeypatch):
    """非 win32：0（creationflags 仅 Windows 平台）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("VRP_CHILD_PRIORITY", raising=False)
    assert procs.low_priority_creationflags() == 0


def test_creationflags_escape_hatch_normal(monkeypatch):
    """逃生门 VRP_CHILD_PRIORITY=normal：win32 也恢复 0（NORMAL）。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("VRP_CHILD_PRIORITY", "normal")
    assert procs.low_priority_creationflags() == 0


def test_creationflags_escape_hatch_other_values_stay_low(monkeypatch):
    """逃生门只认 normal（大小写宽容）：其余乱值保持低。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("VRP_CHILD_PRIORITY", "fast")
    assert procs.low_priority_creationflags() == 0x4000
    monkeypatch.setenv("VRP_CHILD_PRIORITY", "NORMAL")  # 大小写宽容恢复
    assert procs.low_priority_creationflags() == 0


# ---------- 助手单测：low_priority_argv_prefix ----------


def test_argv_prefix_posix_with_nice(monkeypatch):
    """POSIX nice 在场：["nice", "-n", "15"]（TSA local_low_impact 同值）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("VRP_CHILD_PRIORITY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nice")
    assert procs.low_priority_argv_prefix() == ["nice", "-n", "15"]


def test_argv_prefix_posix_nice_missing_falls_back(monkeypatch):
    """POSIX nice 缺失：[]（诚实降级不阻塞）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("VRP_CHILD_PRIORITY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert procs.low_priority_argv_prefix() == []


def test_argv_prefix_windows_empty(monkeypatch):
    """win32：[]（优先级走 creationflags 通道）。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("VRP_CHILD_PRIORITY", raising=False)
    assert procs.low_priority_argv_prefix() == []


def test_argv_prefix_escape_hatch_normal(monkeypatch):
    """逃生门 normal：POSIX 也恢复 []（继承父优先级）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("VRP_CHILD_PRIORITY", "normal")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nice")
    assert procs.low_priority_argv_prefix() == []


# ---------- Windows 真测：子进程回读自身优先级类 ----------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows creationflags 通道")
def test_run_tree_child_below_normal_windows(monkeypatch, tmp_path):
    """run_tree spawn 的子进程优先级类=BELOW_NORMAL（16384）——真 spawn 回读。"""
    monkeypatch.delenv("VRP_CHILD_PRIORITY", raising=False)
    res = procs.run_tree([sys.executable, "-c", _WIN_PRIORITY_PROBE],
                         cwd=tmp_path, timeout=120)
    assert res["returncode"] == 0 and res["timed_out"] is False
    assert res["stdout"].strip() == str(BELOW_NORMAL)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows creationflags 通道")
def test_run_tree_escape_hatch_normal_windows(monkeypatch, tmp_path):
    """逃生门 normal：子进程优先级类=NORMAL（32，父缺省）。"""
    monkeypatch.setenv("VRP_CHILD_PRIORITY", "normal")
    res = procs.run_tree([sys.executable, "-c", _WIN_PRIORITY_PROBE],
                         cwd=tmp_path, timeout=120)
    assert res["returncode"] == 0
    assert res["stdout"].strip() == str(NORMAL)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows creationflags 通道")
def test_s4_freeze_run_one_low_priority_windows(monkeypatch, tmp_path):
    """s4_freeze._run_one（shell=True 流式通道）同降：回读 BELOW_NORMAL；
    返回 dict 的 command 字段保留原命令声明语义（前缀是实现细节）。"""
    monkeypatch.delenv("VRP_CHILD_PRIORITY", raising=False)
    command = f'"{sys.executable}" -c "{_WIN_PRIORITY_PROBE}"'
    out = s4_freeze._run_one(tmp_path, command, timeout=120)
    assert out["rc"] == 0 and out["timed_out"] is False
    assert out["stdout_tail"].strip() == str(BELOW_NORMAL)
    assert out["command"] == command  # 原命令声明语义不变


# ---------- POSIX 真测分支（本机 Windows skip；代码在测试在） ----------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX nice 前缀通道")
def test_run_tree_child_niced_posix(monkeypatch, tmp_path):
    """POSIX：nice 前缀下子进程 os.nice(0) 回读 >= 10。"""
    monkeypatch.delenv("VRP_CHILD_PRIORITY", raising=False)
    res = procs.run_tree(
        [sys.executable, "-c", "import os; print(os.nice(0))"],
        cwd=tmp_path, timeout=120,
    )
    assert res["returncode"] == 0
    assert int(res["stdout"].strip()) >= 10


# ---------- #149：normal_priority=True 测量场景显式豁免 ----------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows creationflags 通道")
def test_run_tree_normal_priority_windows(monkeypatch, tmp_path):
    """#149：normal_priority=True——子进程优先级类回读 NORMAL（32），
    与逃生门 env 同效但按调用点显式声明（mutate 引擎时序敏感测量）。"""
    monkeypatch.delenv("VRP_CHILD_PRIORITY", raising=False)
    res = procs.run_tree([sys.executable, "-c", _WIN_PRIORITY_PROBE],
                         cwd=tmp_path, timeout=120, normal_priority=True)
    assert res["returncode"] == 0 and res["timed_out"] is False
    assert res["stdout"].strip() == str(NORMAL)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX nice 前缀通道")
def test_run_tree_normal_priority_posix(monkeypatch, tmp_path):
    """#149：POSIX normal_priority=True——argv 无 nice 前缀，
    子进程继承父优先级（os.nice(0) < 10）。"""
    monkeypatch.delenv("VRP_CHILD_PRIORITY", raising=False)
    res = procs.run_tree(
        [sys.executable, "-c", "import os; print(os.nice(0))"],
        cwd=tmp_path, timeout=120, normal_priority=True,
    )
    assert res["returncode"] == 0
    assert int(res["stdout"].strip()) < 10
