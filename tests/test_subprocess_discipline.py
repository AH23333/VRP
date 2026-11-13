"""R-Y 批守卫：subprocess 调用纪律（非 git 全 API 面）。

census（work/probe_ry_census.py → work/census_ry.txt）裁决的落地：
- Face A/C：26 站点（23 run + 3 Popen）text 模式 encoding+errors 全 pin
  （run 侧 test_git_census 只守 run 不守 Popen，本批补 Popen 面）。
- Face B 活缺陷：procs.py kill_tree 的 taskkill 无 timeout——杀路径自身
  可挂（AV/系统压力），挂的是 run_tree 超时契约本身的执行面；修
  =timeout=30。守卫：vrp/ 全域 subprocess.run 必 pin timeout=。
- Face D 记实绿：rc 全消费（直查或 helper 返回给调用方）；taskkill
  best-effort 击杀设计（失败由后续 communicate/_wait_dead 显形）。
- Face E 活缺陷：s5_cabrief watchdog proc.kill() 单杀直子——R-L 孤儿
  持管道机制复发面（node 子进程持 stdout 写端 -> EOF 永不到达 ->
  _read_response 永久挂死，watchdog 只触发一次）；修 =kill_tree +
  POSIX start_new_session。行为回归测试见 test_s5_cabrief
  test_call_code_atlas_watchdog_kills_tree_not_just_child。
- Face F 记实不改：s4_freeze._run_one 全库唯一 shell=True——命令串来
  自被分析仓的 package.json（运行仓库自定义命令=功能本体，注入面是
  特性内在）；树杀语义正确（shell pid 被 /T 与 killpg 覆盖）。
  守卫：shell=True 只允许出现在 vrp/s4_freeze.py。
"""

from __future__ import annotations

import ast
from pathlib import Path

VRP = Path(__file__).resolve().parents[1] / "vrp"


def _iter_calls(py: Path):
    tree = ast.parse(py.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _is_subprocess_call(node: ast.Call, api: str) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == api
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def test_every_subprocess_run_pins_timeout():
    """Face B 守卫：vrp/ 全域 subprocess.run 必 pin timeout=（杀/等路径自身不可挂）。"""
    violations: list[str] = []
    for py in sorted(VRP.glob("*.py")):
        for node in _iter_calls(py):
            if not _is_subprocess_call(node, "run"):
                continue
            if "timeout" not in {k.arg for k in node.keywords}:
                violations.append(f"{py.name}:{node.lineno} run without timeout=")
    assert not violations, "subprocess.run timeout discipline regressed: " + "; ".join(violations)


def test_popen_text_mode_pins_encoding_and_errors():
    """Face C 守卫（Popen 面）：text 模式必 pin encoding；pin 了 encoding 必 pin errors。

    读线程 strict 解码错误会静默吞掉输出使 stdout=None（procs.py docstring
    教训）——errors=replace 是捕获语义的一部分，不是可选项。
    """
    violations: list[str] = []
    for py in sorted(VRP.glob("*.py")):
        for node in _iter_calls(py):
            if not _is_subprocess_call(node, "Popen"):
                continue
            kws = {k.arg for k in node.keywords}
            if "text" not in kws and "universal_newlines" not in kws:
                continue
            if "encoding" not in kws:
                violations.append(f"{py.name}:{node.lineno} Popen text mode without encoding=")
            elif "errors" not in kws:
                violations.append(f"{py.name}:{node.lineno} Popen text mode without errors=")
    assert not violations, "Popen text-mode codec discipline regressed: " + "; ".join(violations)


def test_shell_true_only_in_s4_freeze():
    """Face F 守卫：shell=True 全库只允许 s4_freeze._run_one（census 裁决的设计站点）。"""
    violations: list[str] = []
    for py in sorted(VRP.glob("*.py")):
        for node in _iter_calls(py):
            for kw in node.keywords:
                if (
                    kw.arg == "shell"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                    and py.name != "s4_freeze.py"
                ):
                    violations.append(f"{py.name}:{node.lineno} shell=True outside s4_freeze")
    assert not violations, "shell=True outside the sanctioned site: " + "; ".join(violations)
