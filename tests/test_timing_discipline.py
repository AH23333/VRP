"""R-W 批守卫：测试套件计时确定性纪律。

census（work/probe_rw_census.py）四象裁决的落地：
- Face D 真实 time.sleep 调用站点全域禁绝。唯一豁免 deep/conftest.py
  rmtree 退避：有界尝试封顶 6 次、deadline 不决定结果（所有尝试失败才
  失败），覆盖 Windows AV 实时扫描抓目录句柄窗口（2026-11-06 dogfood
  实证）。
- victim 脚本内的 sleep 在字符串字面量里（AST 不可见），不受本守卫
  约束——那是使 victim 长命、让 timeout 必然命中的正确 fixture 用法。
- 注入式 sleep= 依赖（auto_curate transport）确定性不走真实时钟，
  不在此列。
- ctypes liveness 探针行为 pin（test_procs._pid_alive/_wait_dead，
  R-W 改型前为 tasklist spawn 轮询——满载下每次探测 >1s 吃 deadline
  预算，是 deadline-decided assert 的脆弱面）。
- census 副发现钉死：tests/ 源文件禁 UTF-8 BOM（PS 5.1 Set-Content
  默认带 BOM 的防倒退；CPython 容忍但字节级复现与 diff 卫生不容）。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
VRP_ROOT = TESTS_ROOT.parent / "vrp"
SLEEP_ALLOWLIST = {"deep/conftest.py"}


def test_no_real_sleep_call_sites_in_tests():
    """Face D 禁绝：tests/ 全域无真实 time.sleep 调用站点（豁免见模块 docstring）。"""
    offenders: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        rel = path.relative_to(TESTS_ROOT).as_posix()
        if rel in SLEEP_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sleep"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"real time.sleep call site(s) in tests (R-W census Face D): {offenders}"


def test_no_bom_in_test_sources():
    """census 副发现钉死：tests/ 源文件禁 UTF-8 BOM。"""
    offenders = [
        p.relative_to(TESTS_ROOT).as_posix()
        for p in sorted(TESTS_ROOT.rglob("*.py"))
        if p.read_bytes().startswith(b"\xef\xbb\xbf")
    ]
    assert not offenders, f"UTF-8 BOM in test sources: {offenders}"


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes liveness 探针仅 Windows")
def test_pid_alive_ctypes_probe():
    """ctypes 探针行为 pin：活进程 True / 不可开句柄 False / 已退出 False。"""
    from tests.test_procs import _pid_alive

    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(0) is False  # System Idle Process 拒开句柄
    reaped = subprocess.Popen([sys.executable, "-c", "pass"])
    assert reaped.wait() == 0
    assert _pid_alive(reaped.pid) is False  # 已退出（退出码 0 != STILL_ACTIVE(259)）


@pytest.mark.skipif(sys.platform != "win32", reason="_wait_dead 红径上界 pin")
def test_wait_dead_red_path_bounded():
    """红径有界：真活进程必须在上界处响亮 False（不无限自旋、不依赖机速）。"""
    from tests.test_procs import _wait_dead

    assert _wait_dead(os.getpid(), seconds=0.2) is False


def test_vrp_wall_clocks_utc_and_elapsed_monotonic():
    """R-AC census 钉死：vrp/ 墙钟一律 tz-aware UTC、elapsed 一律 monotonic。

    - datetime.now() 无 tz 参（naive 本地墙钟）/ datetime.utcnow()（3.12
      弃用 + naive）/ datetime.today() 禁绝；datetime.now(tz) 豁免。
    - time.localtime()/time.ctime()（本地时区显示）禁绝。
    - time.time()（墙钟做 elapsed：NTP 回拨可倒退）禁绝——elapsed 走
      time.monotonic()（s7 二十一引擎 deadline 同源）。
    - 墙钟戳一律 UTC：strftime+gmtime（etl/auto_curate）或
      datetime.now(timezone.utc)（curate/s4/s5/s6/session）。
    """
    offenders: list[str] = []
    for path in sorted(VRP_ROOT.glob("*.py")):
        rel = path.relative_to(VRP_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            attr = node.func.attr
            recv = node.func.value
            recv_name = recv.id if isinstance(recv, ast.Name) else None
            if recv_name == "datetime":
                if attr in ("utcnow", "today") or (attr == "now" and not node.args):
                    offenders.append(f"{rel}:{node.lineno}: datetime.{attr}")
            elif recv_name == "time":
                if attr in ("localtime", "ctime", "time"):
                    offenders.append(f"{rel}:{node.lineno}: time.{attr}")
    assert not offenders, f"naive/local wall-clock or time.time() in vrp/ (R-AC census): {offenders}"
