"""R-S SQL 字符串构造面横切审计守卫。

普查结论固化为回归测试（普查时点 285 execute 站点 + 21 file: URI 连接
站点全绿——守卫防倒退）：
- 连接面：sqlite3.connect 第一参数不得为 f-string/字符串拼接——SQLite
  file: URI 给 '?'（query 分隔）'#'（fragment 起点）'%'（百分号转义）
  赋予结构含义，裸内插路径含这些字符即错库/报错（'#' 截断后静默打开
  错误/空库=最坏失败类；修复=R-S dbpath.ro_connect 统一 percent-encode）；
- execute 面：SQL 文本参数不得为 '+' 拼接或 str.format() 调用（普查零
  站点）；
- execute 面：'%' 格式化仅允许占位符生成形态（",".join("?" * N)，值仍
  参数绑定；普查 21 站点全部此形态——s7_enforce 族 IN (...) 查询）；
- execute 面：f-string SQL 的内插表达式仅允许经审计的标识符名（闭集：
  表名/列名/占位符串/常量查询片段）或占位符生成调用——新增名字=新增
  内插面，必须先过人工审计再进白名单。
值绑定面（B 面：全部值经 '?' 参数绑定）与动态 SQL 入口（C 面：
nav.query_facts 白名单）为语义面，AST 不可守卫，由 R-S 普查人工对账
绿（见交接文档 #92）。
"""

import ast
from pathlib import Path

VRP_DIR = Path(__file__).parent.parent / "vrp"

# f-string SQL 内插标识符经审计白名单（R-S 普查时点全量）：
# table/collist/placeholders/marks/join_on/where/c0/c1/k——表名（OWNED_TABLES
# 等模块常量）、快照列名、占位符串、查询片段字面量。
_FSTRING_NAME_ALLOWLIST = frozenset(
    {"table", "collist", "placeholders", "marks", "join_on", "where", "c0", "c1", "k"}
)

_EXECUTE_METHODS = ("execute", "executemany", "executescript")


def _is_placeholder_join(node: ast.AST) -> bool:
    """占位符生成形态：`X.join("?" * N)` / `",".join("?" * N)`。"""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr == "join"):
        return False
    for arg in node.args:
        if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mult):
            if isinstance(arg.left, ast.Constant) and isinstance(arg.left.value, str) and set(arg.left.value) <= {"?", ","}:
                continue
            return False
        elif isinstance(arg, ast.GeneratorExp) or isinstance(arg, ast.ListComp):
            continue
        else:
            return False
    return True


def test_no_raw_file_uri_connects():
    """连接面：sqlite3.connect 第一参数不得为 f-string（裸 URI 构造禁令）。"""
    violations: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "connect" or not node.args:
                continue
            if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "sqlite3"):
                continue
            arg = node.args[0]
            bad = None
            if isinstance(arg, ast.JoinedStr):
                bad = "f-string argument (raw file: URI construction - use vrp.dbpath.ro_connect)"
            elif isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
                bad = "string concatenation argument (use vrp.dbpath.ro_connect or a bound parameter)"
            if bad:
                violations.append(f"{py.name}:{node.lineno} sqlite3.connect {bad}")
    assert not violations, "raw sqlite URI construction discipline regressed: " + "; ".join(violations)


def test_no_sql_concat_or_format():
    """execute 面：SQL 文本不得为 '+' 拼接或 str.format() 调用。"""
    violations: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _EXECUTE_METHODS
                and node.args
            ):
                continue
            arg = node.args[0]
            bad = None
            if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
                bad = "string concatenation SQL text"
            elif (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "format"
            ):
                bad = "str.format() SQL text"
            if bad:
                violations.append(f"{py.name}:{node.lineno} .{node.func.attr}() {bad}")
    assert not violations, "SQL string-building discipline regressed: " + "; ".join(violations)


def test_sql_percent_format_only_placeholder_generation():
    """execute 面：'%' 格式化仅允许占位符生成形态（值仍参数绑定）。"""
    violations: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _EXECUTE_METHODS
                and node.args
            ):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mod):
                if not _is_placeholder_join(arg.right):
                    violations.append(
                        f"{py.name}:{node.lineno} .{node.func.attr}() percent-format SQL"
                        " with non-placeholder-generation right side"
                    )
    assert not violations, "SQL percent-format discipline regressed: " + "; ".join(violations)


def test_sql_fstring_interpolations_audited():
    """execute 面：f-string SQL 内插仅限经审计标识符/占位符生成调用。"""
    violations: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _EXECUTE_METHODS
                and node.args
            ):
                continue
            arg = node.args[0]
            if not isinstance(arg, ast.JoinedStr):
                continue
            for value in arg.values:
                if not isinstance(value, ast.FormattedValue):
                    continue
                expr = value.value
                if _is_placeholder_join(expr):
                    continue
                if isinstance(expr, ast.Name) and expr.id in _FSTRING_NAME_ALLOWLIST:
                    continue
                violations.append(
                    f"{py.name}:{node.lineno} .{node.func.attr}() f-string SQL interpolates"
                    f" unaudited expression ({ast.dump(expr)[:80]}...)"
                    " - extend _FSTRING_NAME_ALLOWLIST only after a manual audit"
                )
    assert not violations, "SQL f-string interpolation discipline regressed: " + "; ".join(violations)
