"""子进程族共享工具（R-L 批）：树型超时击杀。

subprocess.run(timeout=...) 在 Windows 的 TimeoutExpired 处理器里只杀
直子进程再二次 communicate()：孙进程树（vitest workers / pytest runner /
rustc）不但成为孤儿继续运行（持 fixture 目录锁、吃内存——踩坑 #84 的
vitest 僵尸进程即此机制的观测症状），且孤儿持有管道写端使二次
communicate() 挂死——超时连墙钟都不守（work/repro_rl_tree.py R1a 双面
实证）。POSIX 同型：run() 超时 kill 直子，孙进程照常存活。

本模块提供树杀语义（s4_freeze._run_one 范式的共享化，#89 教训——修在
共享层不留本地副本）：

- kill_tree(proc): 跨平台整树击杀。Windows taskkill /PID /T /F；
  POSIX killpg 整进程组（要求子进程以 start_new_session=True 启动自成
  组长——run_tree 已内置）。
- run_tree(cmd, cwd, timeout): subprocess.run 的树杀替身。超时不抛
  TimeoutExpired 而是返回 timed_out=True；管道持有者随树全灭，
  communicate 即返（不挂死）。返回
  {"returncode", "timed_out", "stdout", "stderr"}。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def kill_tree(proc: subprocess.Popen) -> None:
    """整树击杀：Windows taskkill /T /F；POSIX killpg 整进程组。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            timeout=30,  # 杀路径自身不可挂（R-Y）：AV/系统压力下 taskkill 迟滞
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        # child runs with start_new_session=True -> it is its own process
        # group leader; kill the whole group so grandchildren die too.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_tree(
    cmd: list[str],
    cwd: Path | str | None = None,
    timeout: int | None = None,
    env: dict | None = None,
) -> dict:
    """subprocess.run 替身：超时杀整树而非只杀直子。

    env=None 继承父进程环境；显式 env 透传子进程（R-AN：s7_enforce._execute
    钉 PYTHONUTF8/COLUMNS 的共享层入口，#89 修在共享层不留本地副本）。

    返回 dict（超时不抛异常）：
    - returncode: 进程退出码；超时树杀后为被杀码（Windows taskkill /F 为 -1）
    - timed_out: bool —— 调用方据此走各自的超时诚实上报路径
    - stdout/stderr: 捕获文本（utf-8 + replace，同族编码纪律——读线程
      strict 解码错误会静默吞掉输出使 stdout=None，必须 replace）
    """
    kwargs: dict = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        **kwargs,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return {
            "returncode": proc.returncode,
            "timed_out": False,
            "stdout": out or "",
            "stderr": err or "",
        }
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        return {
            "returncode": proc.returncode,
            "timed_out": True,
            "stdout": out or "",
            "stderr": (err or "") + f"\ntimed out after {timeout}s (process tree killed)",
        }
