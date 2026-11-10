"""R-M 文件 IO 族横切审计守卫：encoding 纪律普查 + 源码零 SyntaxWarning。

两道守卫把 R-M 普查结论固化为回归测试（普查时点全绿——守卫防倒退）：
- text IO（read_text/write_text/open）必须显式 pin encoding（二进制模式豁免）；
  open 双签名：内建 open(file, mode, ...) mode=args[1]，Path.open(mode) mode=args[0]
- vrp/*.py 逐文件 warnings-as-errors 编译：模块 docstring（非 raw）内的单反斜杠
  无效转义（如 `\\z`/`\\s`）会在 import 时发 SyntaxWarning——R-M 侧发现
  s7_enforce_lua:38 / s7_enforce_scala:20 已修，此处防再犯。
"""

import ast
import warnings
from pathlib import Path

VRP_DIR = Path(__file__).parent.parent / "vrp"


def test_text_io_encoding_pinned():
    """vrp/*.py 全部 text IO 显式 pin encoding（二进制豁免）；非常数 mode 也必须 pin。"""
    violations: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                continue
            if name not in ("read_text", "write_text", "open"):
                continue
            kws = {k.arg for k in node.keywords}
            if name in ("read_text", "write_text"):
                if "encoding" not in kws:
                    violations.append(f"{py.name}:{node.lineno} {name} without encoding=")
                continue
            if isinstance(func, ast.Attribute):
                mode_arg = node.args[0] if node.args else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
            else:
                mode_arg = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
            if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
                if "b" not in mode_arg.value and "encoding" not in kws:
                    violations.append(f"{py.name}:{node.lineno} open text-mode without encoding=")
            elif "encoding" not in kws:
                violations.append(f"{py.name}:{node.lineno} open non-constant mode without encoding=")
    assert not violations, "text IO encoding discipline regressed: " + "; ".join(violations)


def test_vrp_sources_compile_without_syntax_warnings():
    """逐文件 warnings-as-errors 编译：docstring 单反斜杠无效转义（SyntaxWarning）即红。"""
    offenders: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", SyntaxWarning)
            compile(py.read_text(encoding="utf-8-sig"), str(py), "exec")
        for w in caught:
            if issubclass(w.category, SyntaxWarning):
                offenders.append(f"{py.name}: {w.message}")
    assert not offenders, "SyntaxWarning in vrp sources (docstring escapes need \\\\): " + "; ".join(offenders)
