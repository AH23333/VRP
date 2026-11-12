"""R-T 异常消息一致性横切审计守卫。

普查结论固化为回归测试（普查时点 120 raise 站点 / 16 文件全绿——守卫
防倒退）：
- 消息语言面：raise 消息字面部分必须纯 ASCII——运行时异常消息的消费
  者是 CLI 用户与 MCP 代理（_wrap 统一转 {"error": ...} JSON），全库
  约定英文消息（docstring/注释中文不受此限）；普查唯一红点
  curate.reject kind 消息（CJK）已修复，守卫防同类倒退；
- 静默失败面：SystemExit 不得无参裸 raise——无消息退出码 1 会静默
  吞掉失败原因（普查零站点，守卫防倒退）。

语义面（消息可操作性、截断宽度 [:500]/[:200]/[:16] 分域一致性、
raise...from 链、'vrp X' 命令引用与实际子命令对账 19/19）AST 不可守
卫或无硬边界，由 R-T 普查人工对账绿（见交接文档 #93）。
"""

import ast
from pathlib import Path

VRP_DIR = Path(__file__).parent.parent / "vrp"


def _raise_arg_literals(node: ast.Raise) -> list[str]:
    """收集 raise 调用第一参数内全部字符串字面量（含 f-string 字面部分/拼接片段）。"""
    if not (isinstance(node.exc, ast.Call) and node.exc.args):
        return []
    out: list[str] = []
    for sub in ast.walk(node.exc.args[0]):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return out


def test_exception_message_literals_ascii():
    """消息语言面：raise 消息字面部分不得含非 ASCII 字符。"""
    violations: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            for lit in _raise_arg_literals(node):
                bad = [ch for ch in lit if ord(ch) > 127]
                if bad:
                    violations.append(
                        f"{py.name}:{node.lineno} non-ASCII {bad!r} in exception message literal:"
                        f" {lit[:60]!r}"
                        " - runtime messages are consumed by CLI users and MCP agents; keep them English"
                    )
    assert not violations, "exception message language discipline regressed: " + "; ".join(violations)


def test_no_silent_systemexit():
    """静默失败面：SystemExit 不得无消息裸 raise（含零参调用）。"""
    violations: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            exc = node.exc
            if isinstance(exc, ast.Name) and exc.id == "SystemExit":
                violations.append(f"{py.name}:{node.lineno} bare 'raise SystemExit' (no message)")
            elif (
                isinstance(exc, ast.Call)
                and isinstance(exc.func, ast.Name)
                and exc.func.id == "SystemExit"
                and not exc.args
            ):
                violations.append(f"{py.name}:{node.lineno} 'raise SystemExit()' with no message")
    assert not violations, "silent-exit discipline regressed: " + "; ".join(violations)
