"""R-N git/subprocess 调用族横切审计守卫。

普查结论固化为回归测试（普查时点全绿——守卫防倒退）：
- literal `subprocess.run(["git", ...])` 必须 pin encoding+errors+timeout+
  capture_output（6 站点：etl / render×2 / s4_freeze / s6_difftastic /
  s6_transact）——R-N 普查的 git 调用族纪律；
- 所有 `subprocess.run(..., text=True)` 必须 pin encoding（平台默认编码
  漂移免疫；二进制模式豁免）。动态 cmd 包装（s0._run、s6_transact._git
  等）在其唯一 subprocess.run 处统一 pin，由此条兜底。
"""

import ast
from pathlib import Path

VRP_DIR = Path(__file__).parent.parent / "vrp"


def _is_subprocess_run(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "run"
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def _first_arg_const(node: ast.Call):
    """首参列表字面量的首元素常量值；非该形态返回 None。"""
    if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)) and node.args[0].elts:
        first = node.args[0].elts[0]
        if isinstance(first, ast.Constant):
            return first.value
    return None


def test_git_literal_subprocess_discipline():
    """literal git subprocess.run 必 pin encoding/errors/timeout/capture_output。"""
    violations: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_subprocess_run(node)):
                continue
            if _first_arg_const(node) != "git":
                continue
            kws = {k.arg for k in node.keywords}
            missing = {"encoding", "errors", "timeout", "capture_output"} - kws
            if missing:
                violations.append(f"{py.name}:{node.lineno} git run missing {sorted(missing)}")
    assert not violations, "git subprocess discipline regressed: " + "; ".join(violations)


def test_subprocess_run_text_mode_pins_encoding():
    """所有 subprocess.run text 模式必须显式 pin encoding。"""
    violations: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_subprocess_run(node)):
                continue
            kws = {k.arg for k in node.keywords}
            if "text" in kws and "encoding" not in kws:
                violations.append(f"{py.name}:{node.lineno} text mode without encoding=")
    assert not violations, "subprocess text-mode encoding discipline regressed: " + "; ".join(violations)
