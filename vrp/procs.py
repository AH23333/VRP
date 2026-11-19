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
- low_priority_creationflags()/low_priority_argv_prefix(): 子进程低
  CPU 优先级（#147，学 TSA local_low_impact 的 nice -n 15 模式）——
  重型子进程（CBM 索引/mutate 引擎/freeze 基线/archify）不抢占交互
  前台；nice 语义=空闲机满速、争时机让位，只影响调度不影响结果
  （确定性红线不触）。Windows 走 creationflags 优先级类位（纯
  stdlib，TSA 因走 nice(1) 前缀而跳过 Windows——直接 spawn 可设）；
  POSIX 走 nice 前缀（which 探测，缺失诚实回退）。逃生门
  VRP_CHILD_PRIORITY=normal 恢复普通优先级（专用 CI 机/基准场景）。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000  # Win32 CreateProcess dwCreationFlags 优先级类位（API 文档值）
NICE_LEVEL = 15  # TSA local_low_impact 同值（nice -n 15）


def _priority_escape() -> bool:
    """逃生门：VRP_CHILD_PRIORITY=normal 恢复普通优先级；其余值/未设=低。"""
    return os.environ.get("VRP_CHILD_PRIORITY", "").lower() == "normal"


def low_priority_creationflags() -> int:
    """Windows 子进程低 CPU 优先级 creationflags（BELOW_NORMAL）。

    非零仅 win32；POSIX 返回 0（Popen 的 creationflags 仅 Windows 平台）；
    逃生门恢复 0（NORMAL，父进程缺省）。
    """
    if sys.platform != "win32" or _priority_escape():
        return 0
    return BELOW_NORMAL_PRIORITY_CLASS


def low_priority_argv_prefix() -> list[str]:
    """POSIX 子进程低优先级 argv 前缀（nice -n 15，TSA 同值）。

    which("nice") 探测，缺失回退 []（诚实降级不阻塞）；win32 返回 []；
    逃生门恢复 []。
    """
    if sys.platform == "win32" or _priority_escape():
        return []
    if shutil.which("nice") is None:
        return []
    return ["nice", "-n", str(NICE_LEVEL)]


def kill_tree(proc: subprocess.Popen) -> None:
    """整树击杀：Windows taskkill /T /F；POSIX killpg 整进程组。"""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=30,  # 杀路径自身不可挂（R-Y）：AV/系统压力下 taskkill 迟滞
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            # AU-1（R-AU）：taskkill 自身超时不可穿透调用方——run_tree 超时
            # 路径、s5_cabrief watchdog 线程均无二次防线（watchdog 线程内抛=
            # 线程死且树不灭=整链挂死）。降级直杀：至少直子死、主管道写端释放。
            try:
                proc.kill()
            except OSError:
                pass
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
    normal_priority: bool = False,
) -> dict:
    """subprocess.run 替身：超时杀整树而非只杀直子。

    env=None 继承父进程环境；显式 env 透传子进程（R-AN：s7_enforce._execute
    钉 PYTHONUTF8/COLUMNS 的共享层入口，#89 修在共享层不留本地副本）。

    normal_priority=True 显式跳过低优先级（#149）：mutate 引擎是时序敏感
    测量——per-mutant 超时基于各自 dry-run 基线，BELOW_NORMAL 在饱和负载
    （全量 3028 连跑）下挤压测量使 borderline 变异体在 Killed/Timeout 间
    漂移（stryker idempotent 实测 7!=4，单跑绿）——测量场景不吃降速，
    与 VRP_CHILD_PRIORITY=normal 逃生门同语义但按调用点显式声明。

    返回 dict（超时不抛异常）：
    - returncode: 进程退出码；超时树杀后为被杀码（Windows taskkill /F 为 -1）
    - timed_out: bool —— 调用方据此走各自的超时诚实上报路径
    - stdout/stderr: 捕获文本（utf-8 + replace，同族编码纪律——读线程
      strict 解码错误会静默吞掉输出使 stdout=None，必须 replace）
    """
    kwargs: dict = {}
    argv = cmd
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
        if not normal_priority:
            argv = [*low_priority_argv_prefix(), *cmd]
    else:
        kwargs["creationflags"] = 0 if normal_priority else low_priority_creationflags()
    proc = subprocess.Popen(
        argv,
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
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            # AU-1（R-AU）：树杀后 15s 管道仍不 EOF=有幸存者持写端（杀降级/
            # 不可杀系统进程）。返回诚实 survived dict 而非挂死——"超时不抛"
            # 契约对幸存路径同样成立。
            return {
                "returncode": -1,
                "timed_out": True,
                "stdout": "",
                "stderr": f"timed out after {timeout}s (process tree survived kill; output lost)",
            }
        return {
            "returncode": proc.returncode,
            "timed_out": True,
            "stdout": out or "",
            "stderr": (err or "") + f"\ntimed out after {timeout}s (process tree killed)",
        }


def stream_lines(
    cmd: list[str],
    cwd: Path | str | None = None,
    input_text: str | None = None,
    timeout: int = 3600,
):
    """流式行通道（H1 secrets 历史面）：stdout 逐行 yield——大体量输出
    禁止 run_tree 全量捕获（git cat-file --batch 全 blob 流可超内存）。

    - input_text 经**写线程**异步喂 stdin 后关闭（#178 自检实录：同步
      "先写完再读"在真仓死锁——2948 oid×41B=120KB stdin 超管道缓冲，
      子进程 stdout 8KB 满阻塞→停读 stdin→写端阻塞，双管道互锁，600s
      看门杀树才解；fixture 仓 oid 数小永不触发=dogfood 盲区。写线程
      与主线程读并行，双管道永不互锁）；input_text=None 时 stdin=DEVNULL
    - stderr=DEVNULL（读线程缺位下 PIPE 会写满死锁；流式场景错误面
      由调用方先行枚举校验承载——secrets 先 --batch-check 过滤 oid）
    - timeout 看门计时器整窗计时到点杀树（迭代慢=消费方处理慢也计入）
    - 耗尽路径 rc 诚实红：正常耗尽后 returncode!=0（看门杀/崩溃）→
      RuntimeError——流可能被截断，静默部分结果比死锁更糟（#178：
      被杀后 135/2948 行静默漏扫实录）
    - 生成器耗尽/close/异常时 finally 回收进程（kill_tree+wait）；
      Windows 低优先级同 run_tree 纪律
    """
    argv = cmd
    kwargs: dict = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
        argv = [*low_priority_argv_prefix(), *cmd]
    else:
        kwargs["creationflags"] = low_priority_creationflags()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )
    timer = threading.Timer(timeout, kill_tree, args=(proc,))
    timer.daemon = True
    timer.start()
    writer: threading.Thread | None = None
    if input_text is not None:
        def _feed() -> None:
            try:
                proc.stdin.write(input_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        writer = threading.Thread(target=_feed, daemon=True)
        writer.start()
    try:
        for line in proc.stdout:
            yield line
        proc.stdout.close()
        proc.wait(timeout=30)
        rc = proc.poll()
        if rc != 0:
            raise RuntimeError(
                f"stream command {' '.join(cmd)} exited rc={rc}"
                " (watchdog kill or crash; output may be truncated)"
            )
    finally:
        timer.cancel()
        if proc.poll() is None:
            kill_tree(proc)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        if writer is not None:
            writer.join(timeout=5)


def run_thunks(thunks: list, max_workers: int | None = None) -> list:
    """定界线程池扇出（J9 引擎级四并发；S2 各病理引擎互相独立——读写面
    零交叉是设计稿 §1 实核的结构性前提）。

    线程池而非进程池（设计稿 D2）：并行实体本就是各引擎 spawn 的子进程
    （ast-grep/jscpd/vulture/knip），父侧线程只在 communicate 等待——GIL
    释放即真并行；ProcessPoolExecutor 需 pickle 面纯增复杂度零增并行度。

    契约（设计稿 D4）：
    - 全任务跑完不抢跑取消——取消使失败复现面噪声化；
    - 结果按提交序返回（与串行装配恒等）；
    - 异常按任务序抛首个——与串行首错同引擎同消息同 traceback 面
      （with 退出时 shutdown(wait=True) 保证已抛异常时未触任务仍跑完，
      不留半途孤儿线程）；
    - max_workers=None 自动=任务数；<=1 串行直跑（零线程开销+全同序）。
    """
    if not thunks:
        return []
    workers = len(thunks) if max_workers is None else max_workers
    if workers <= 1:
        return [t() for t in thunks]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(t) for t in thunks]
        # 逐序 result()：先到序先取——首个失败任务序的异常被抛出时，
        # 更高序任务已在池中跑完或随 with 退出等完，永不裸奔。
        return [f.result() for f in futures]
