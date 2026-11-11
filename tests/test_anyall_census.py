"""R-R any()/all() 调用族横切审计守卫。

普查结论固化为回归测试（普查时点 64 站点四面全绿——守卫防倒退）：
- D 面：any()/all() 不得直收 str/bytes/f-string/dict 字面量——迭代 str 得
  字符、迭代 dict 得键，与"容器判存在/判全称"的调用意图相悖（普查零站点）；
- 遮蔽面：any/all 内建名不得被任何绑定形态（赋值/注解/循环/walrus/
  with/except/参数/推导目标/import 别名/global·nonlocal 声明）遮蔽——
  遮蔽会静默改变全文件语义（普查零站点）。
A 面（空输入语义 any([])=False / all([])=True 是否符合调用点契约）与
B 面（短路部分消费生成器的副作用）为语义面，AST 不可守卫，由 R-R 普查
人工对账绿（见交接文档 #90）；C 面（成员测试性能）常量为 2-4 元
tuple/frozenset，量级免疫。
"""

import ast
from pathlib import Path

VRP_DIR = Path(__file__).parent.parent / "vrp"
_FUNCS = ("any", "all")


def _anyall_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS
            and node.args
        ):
            yield node


def _store_names(target: ast.AST) -> list[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]


def _bound_names(node: ast.AST) -> list[str]:
    """该节点绑定的名字（多形态绑定面，返回空列表表示不绑定）。"""
    if isinstance(node, ast.Assign):
        names: list[str] = []
        for t in node.targets:
            names.extend(_store_names(t))
        return names
    if isinstance(node, ast.AnnAssign):
        return _store_names(node.target) if isinstance(node.target, (ast.Name, ast.Tuple, ast.List)) else []
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return _store_names(node.target)
    if isinstance(node, ast.NamedExpr):
        return _store_names(node.target)
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return _store_names(node.optional_vars)
    if isinstance(node, ast.ExceptHandler) and node.name:
        return [node.name]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        names = []
        args = node.args
        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            names.append(arg.arg)
        if args.vararg:
            names.append(args.vararg.arg)
        if args.kwarg:
            names.append(args.kwarg.arg)
        return names
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [a.asname or a.name.split(".")[0] for a in node.names]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names)
    return []


def test_any_all_no_iterated_semantics_literals():
    """D 面：any/all 直收 str/bytes/f-string/dict 字面量即违规。"""
    violations: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in _anyall_calls(tree):
            arg = node.args[0]
            what = None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, (str, bytes)):
                what = "str/bytes literal (iterates chars)"
            elif isinstance(arg, ast.JoinedStr):
                what = "f-string (iterates chars)"
            elif isinstance(arg, ast.Dict):
                what = "dict literal (iterates keys)"
            if what:
                violations.append(f"{py.name}:{node.lineno} {node.func.id}() fed {what}")
    assert not violations, "any/all iterated-semantics discipline regressed: " + "; ".join(violations)


def test_any_all_not_shadowed():
    """遮蔽面：any/all 内建名不得被任何绑定形态遮蔽。"""
    violations: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            for name in _bound_names(node):
                if name in _FUNCS:
                    violations.append(f"{py.name}:{node.lineno} binds {name!r} ({type(node).__name__})")
    assert not violations, "any/all shadowing discipline regressed: " + "; ".join(violations)
