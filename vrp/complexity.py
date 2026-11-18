"""R3（构想v12）：复杂度度量族 v1——py/ts 先行验证形态。

学（度量定义与阈值惯例）：圈复杂度 CC 口径学 radon/lizard（CC=1+决策点：
if/elif/for/while/except/ternary/assert/逻辑运算符每个额外操作数/
comprehension for+每个 if 子句/match case；嵌套函数独立成条——复杂度归
自己不归外层）；阈值惯例圈复杂度>10 告警线。认知复杂度学 SonarQube
白皮书核心（嵌套惩罚+线性流+递归）——**简化口径（docstring 即声明）**：
  + (1 + depth) 每个：if/for/while/except/ternary/comprehension/case
  + 1 每个：elif、else（if 链平级延伸，不加深）
  + 1 每个逻辑运算符（and/or/&&/||/??——白皮书原文按序列 +1，简化按
    运算符，与 CC 口径对齐便于人对照）
  + 1 每次直接递归自调用
  depth 只由控制流结构体（if/for/while/except/case）子块贡献
可维护性指数 MI **不做**（诚实边界：SEI 公式需 Halstead volume——操作数/
操作符全统计，先攒案例再立项，宁缺勿错）。

造（不引依赖）：py 面 stdlib ast 完整 AST；ts 面 node + 沙箱 typescript
（work/dep-cruise-probe 既有面，零新依赖）真 AST——_TS_SCAN_SRC JS 扫描
器内嵌模块常量（build_pyz._LAUNCHER_SRC 先例），临时 .mjs + node 跑，
stdout JSON 回传；沙箱缺 typescript 时 ts 面诚实降级 unavailable（assess
enforcement unavailable 先例），py 面照常。

v1 范围：只读现算查询（`vrp complexity`），不落库不入 findings——pathology
面接入（P 码/ETL）待形态验证后立项（构想想定终态，本批先行验证）。

确定性（复现性红线）：同输入文件集同输出；排序 (-cc, -cognitive, file,
line) 纯确定；不碰 facts.db（无 .vrp 依赖，纯文件面工具）。
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
import warnings
from pathlib import Path

from . import procs, s0_precheck
from .s7_enforce_ts import TS_SUFFIXES

PY_SUFFIXES = (".py",)
CC_THRESHOLD = 10  # 圈复杂度告警线（radon/lizard 惯例：>10 告警）
NODE_TIMEOUT = 120
_SCAN_CHUNK = 150  # argv 分批上限（自仓冒烟实录 WinError 206：绝对路径 ~100B×400 超 Windows 32KB 命令行）
DEFAULT_LIMIT = 30

_PY_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
_MATCH_CASE = getattr(ast, "match_case", None)
_HAS_MATCH = hasattr(ast, "Match")

_TS_SCAN_SRC = r"""import { createRequire } from "module";
const require = createRequire(import.meta.url);
const ts = require(process.argv[2]);
const fs = require("fs");
const files = process.argv.slice(3);
const out = [];
const FUNC_KINDS = new Set([
  "FunctionDeclaration", "MethodDeclaration", "ArrowFunction", "FunctionExpression",
  "GetAccessorDeclaration", "SetAccessorDeclaration", "Constructor",
]);
const kindOf = (node) => ts.SyntaxKind[node.kind];
const isFunc = (node) => FUNC_KINDS.has(kindOf(node));
function nameOf(fn, inheritedName) {
  if (fn.name && fn.name.text) return fn.name.text;
  if (inheritedName) return inheritedName;
  return "(anonymous)";
}
function lineOf(fn, sf) {
  return sf.getLineAndCharacterOfPosition(fn.getStart(sf)).line + 1;
}
function ccOf(fn) {
  let cc = 1;
  const walk = (node) => {
    ts.forEachChild(node, (child) => {
      if (isFunc(child)) return; // 嵌套函数独立成条
      const k = kindOf(child);
      if (k === "IfStatement" || k === "ForStatement" || k === "ForOfStatement"
        || k === "ForInStatement" || k === "WhileStatement" || k === "DoStatement"
        || k === "ConditionalExpression" || k === "CatchClause") cc += 1;
      else if (k === "CaseClause") cc += 1;
      else if (k === "BinaryExpression") {
        const op = ts.SyntaxKind[child.operatorToken.kind];
        if (op === "AmpersandAmpersandToken" || op === "BarBarToken"
          || op === "QuestionQuestionToken") cc += 1;
      }
      walk(child);
    });
  };
  walk(fn);
  return cc;
}
function cogOf(fn) {
  let score = 0;
  const bump = (n) => { score += n; };
  const stmtsOf = (node) => (node.statements ? Array.from(node.statements) : [node]);
  function visitExpr(node, depth) {
    if (!node || node.kind === undefined) return;
    const k = kindOf(node);
    if (isFunc(node)) return;
    if (k === "ConditionalExpression") {
      bump(1 + depth);
      ts.forEachChild(node, (c) => visitExpr(c, depth + 1));
      return;
    }
    if (k === "BinaryExpression") {
      const op = ts.SyntaxKind[node.operatorToken.kind];
      if (op === "AmpersandAmpersandToken" || op === "BarBarToken"
        || op === "QuestionQuestionToken") bump(1);
      ts.forEachChild(node, (c) => visitExpr(c, depth));
      return;
    }
    if (k === "CallExpression") {
      if (node.expression && kindOf(node.expression) === "Identifier"
        && node.expression.text === fname) bump(1);
      ts.forEachChild(node, (c) => visitExpr(c, depth));
      return;
    }
    ts.forEachChild(node, (c) => visitExpr(c, depth));
  }
  const LOOP_KINDS = new Set(["ForStatement", "ForOfStatement", "ForInStatement", "WhileStatement", "DoStatement"]);
  function visitElseIf(ifNode, depth) {
    visitExpr(ifNode.expression, depth);
    for (const s of stmtsOf(ifNode.thenStatement)) visitStmt(s, depth + 1);
    if (ifNode.elseStatement) {
      if (kindOf(ifNode.elseStatement) === "IfStatement") {
        bump(1); // else-if 平级 +1 不加深
        visitElseIf(ifNode.elseStatement, depth);
      } else {
        bump(1); // else +1
        for (const s of stmtsOf(ifNode.elseStatement)) visitStmt(s, depth + 1);
      }
    }
  }
  function visitStmt(node, depth) {
    if (!node || node.kind === undefined) return;
    if (isFunc(node)) return;
    const k = kindOf(node);
    if (k === "IfStatement") {
      bump(1 + depth);
      visitExpr(node.expression, depth);
      for (const s of stmtsOf(node.thenStatement)) visitStmt(s, depth + 1);
      if (node.elseStatement) {
        if (kindOf(node.elseStatement) === "IfStatement") {
          bump(1);
          visitElseIf(node.elseStatement, depth);
        } else {
          bump(1);
          for (const s of stmtsOf(node.elseStatement)) visitStmt(s, depth + 1);
        }
      }
      return;
    }
    if (LOOP_KINDS.has(k)) {
      bump(1 + depth);
      for (const s of stmtsOf(node.statement)) visitStmt(s, depth + 1);
      return;
    }
    if (k === "CaseClause") {
      bump(1 + depth);
      for (const s of node.statements) visitStmt(s, depth + 1);
      return;
    }
    if (k === "CatchClause") {
      bump(1 + depth);
      for (const s of node.block.statements) visitStmt(s, depth + 1);
      return;
    }
    if (k === "SwitchStatement" || k === "TryStatement" || k === "Block") {
      ts.forEachChild(node, (c) => visitStmt(c, depth));
      return;
    }
    // 其他语句：子语句走语句面，子表达式走表达式面
    ts.forEachChild(node, (c) => {
      const ck = kindOf(c);
      if (isFunc(c)) return;
      if (ck.endsWith("Statement") || ck.endsWith("Clause")) visitStmt(c, depth);
      else visitExpr(c, depth);
    });
  }
  const fname = fn.name && fn.name.text ? fn.name.text : ""; // 匿名函数递归不识别（v1 诚实边界）
  for (const s of stmtsOf(fn.body === undefined ? fn : fn.body)) visitStmt(s, 0);
  return score;
}
for (const file of files) {
  const text = fs.readFileSync(file, "utf8");
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const visit = (node, inheritedName) => {
    let childName = null; // const arrow = ... 箭头函数继承变量名
    if (kindOf(node) === "VariableDeclaration" && node.name && node.name.text) {
      childName = node.name.text;
    }
    if (isFunc(node)) {
      out.push({ file, name: nameOf(node, inheritedName), line: lineOf(node, sf), cc: ccOf(node), cognitive: cogOf(node) });
    }
    ts.forEachChild(node, (c) => visit(c, childName));
  };
  visit(sf, null);
}
console.log(JSON.stringify(out));
"""


def _iter_own(node: ast.AST) -> "iter":
    """own 面遍历：不进入嵌套函数（嵌套函数独立成条，radon 口径）。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _PY_FUNC):
            continue
        yield child
        yield from _iter_own(child)


def py_cc(fn: ast.AST) -> int:
    """圈复杂度（radon 口径）：1+决策点；嵌套函数不计入。"""
    cc = 1
    for node in _iter_own(fn):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.Assert)):
            cc += 1
        elif isinstance(node, ast.BoolOp):
            cc += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            cc += 1 + len(node.ifs)
        elif _MATCH_CASE is not None and isinstance(node, _MATCH_CASE):
            cc += 1
    return cc


def py_cognitive(fn: ast.AST) -> int:
    """认知复杂度（SonarQube 白皮书简化口径，见模块 docstring）；嵌套函数不计入。"""
    score = 0
    fname = fn.name

    def bump(delta: int) -> None:
        nonlocal score
        score += delta

    def visit_expr(node: ast.AST | None, depth: int) -> None:
        if node is None:
            return
        if isinstance(node, ast.IfExp):
            bump(1 + depth)
            for child in ast.iter_child_nodes(node):
                visit_expr(child, depth + 1)
        elif isinstance(node, ast.BoolOp):
            bump(len(node.values) - 1)
            for child in ast.iter_child_nodes(node):
                visit_expr(child, depth)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == fname:
                bump(1)
            for child in ast.iter_child_nodes(node):
                visit_expr(child, depth)
        elif isinstance(node, ast.comprehension):
            bump(1 + depth + len(node.ifs))
            for child in ast.iter_child_nodes(node):
                visit_expr(child, depth + 1)
        else:
            for child in ast.iter_child_nodes(node):
                visit_expr(child, depth)

    def visit_stmts(stmts: list, depth: int) -> None:
        for stmt in stmts:
            visit_stmt(stmt, depth)

    def visit_if_tail(if_node: ast.If, depth: int) -> None:
        """elif/else 链：平级 +1 不加深（SonarQube 线性流延伸）。"""
        orelse = if_node.orelse
        if not orelse:
            return
        if len(orelse) == 1 and isinstance(orelse[0], ast.If):
            sub = orelse[0]
            bump(1)
            visit_expr(sub.test, depth)
            visit_stmts(sub.body, depth + 1)
            visit_if_tail(sub, depth)
        else:
            bump(1)
            visit_stmts(orelse, depth + 1)

    def visit_stmt(stmt: ast.AST, depth: int) -> None:
        if isinstance(stmt, ast.If):
            bump(1 + depth)
            visit_expr(stmt.test, depth)
            visit_stmts(stmt.body, depth + 1)
            visit_if_tail(stmt, depth)
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            bump(1 + depth)
            if isinstance(stmt, (ast.For, ast.AsyncFor)):
                visit_expr(stmt.iter, depth)
            else:
                visit_expr(stmt.test, depth)
            visit_stmts(stmt.body, depth + 1)
        elif isinstance(stmt, ast.ExceptHandler):
            bump(1 + depth)
            visit_stmts(stmt.body, depth + 1)
        elif _HAS_MATCH and isinstance(stmt, ast.Match):
            for case in stmt.cases:
                bump(1 + depth)
                visit_stmts(case.body, depth + 1)
        elif isinstance(stmt, _PY_FUNC):
            return
        else:
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, (ast.stmt, ast.ExceptHandler)):
                    visit_stmt(child, depth)
                else:
                    visit_expr(child, depth)

    visit_stmts(fn.body, 0)
    return score


def scan_py_file(path: Path) -> tuple[list[dict], bool]:
    """单 py 文件函数抽取；SyntaxError 诚实返回 (rows=False) 供 unparseable 计数。
    被扫文件的 invalid escape 序列只产 SyntaxWarning 不影响解析——抑制（自仓
    冒烟实录：被扫 docstring 的 '\\*' 警告泄进 CLI stderr 污染人读面）。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(path.read_bytes().decode("utf-8", "replace"))
    except SyntaxError:
        return [], False
    rows = [
        {
            "file": str(path),
            "name": fn.name,
            "line": fn.lineno,
            "cc": py_cc(fn),
            "cognitive": py_cognitive(fn),
        }
        for fn in ast.walk(tree)
        if isinstance(fn, _PY_FUNC)
    ]
    return rows, True


def find_typescript() -> Path | None:
    """沙箱 typescript 发现（VRP_TYPESCRIPT_BIN 覆盖 → dep-cruise 沙箱；
    typescript.js 是 CJS——JS 侧 createRequire 加载绝对路径）。"""
    env = os.environ.get("VRP_TYPESCRIPT_BIN")
    if env:
        return Path(env)
    cand = (
        Path(__file__).resolve().parent.parent
        / "work" / "dep-cruise-probe" / "node_modules"
        / "typescript" / "lib" / "typescript.js"
    )
    return cand if cand.exists() else None


def scan_ts_files(files: list[Path], ts_path: Path) -> list[dict]:
    """ts 面批量扫描：临时 .mjs + node 真跑（沙箱 typescript AST），
    stdout JSON 回传；rc!=0/超时诚实 RuntimeError（测量失败不是零复杂度）。
    """
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="vrp-ts-scan-") as td:
        script = Path(td) / "ts-scan.mjs"
        script.write_text(_TS_SCAN_SRC, encoding="utf-8", newline="\n")
        for i in range(0, len(files), _SCAN_CHUNK):
            batch = files[i : i + _SCAN_CHUNK]
            rr = procs.run_tree(
                ["node", str(script), str(ts_path), *[str(f) for f in batch]],
                timeout=NODE_TIMEOUT,
            )
            if rr["timed_out"] or rr["returncode"] != 0:
                raise RuntimeError(
                    f"ts complexity scan failed (rc={rr['returncode']},"
                    f" timed_out={rr['timed_out']}): {(rr.get('stderr') or '')[:300]}"
                )
            rows.extend(json.loads(rr["stdout"] or "[]"))
    return rows


def run_complexity(repo: Path, cc_threshold: int = CC_THRESHOLD, limit: int = DEFAULT_LIMIT) -> dict:
    """`vrp complexity` 入口：纯文件面（不碰 facts.db）；ts 沙箱缺失诚实降级。"""
    repo = Path(repo).resolve()
    py_files: list[Path] = []
    ts_files: list[Path] = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in s0_precheck.IGNORE_DIRS]
        for name in names:
            p = Path(root) / name
            if name.endswith(PY_SUFFIXES):
                py_files.append(p)
            elif name.endswith(TS_SUFFIXES):
                ts_files.append(p)
    rows: list[dict] = []
    unparseable = 0
    for p in py_files:
        file_rows, ok = scan_py_file(p)
        if not ok:
            unparseable += 1
        rows.extend(file_rows)
    ts_status = "skipped-no-ts-files"
    if ts_files:
        ts_path = find_typescript()
        if ts_path is None:
            ts_status = "unavailable-no-typescript"
        else:
            ts_status = "ok"
            rows.extend(scan_ts_files(ts_files, ts_path))
    rows.sort(key=lambda r: (-r["cc"], -r["cognitive"], r["file"], r["line"]))
    exceeds = [r for r in rows if r["cc"] > cc_threshold]
    shown = rows[: max(limit, 0)]
    cc_values = [r["cc"] for r in rows]
    return {
        "repo": str(repo),
        "summary": {
            "py_files": len(py_files),
            "ts_files": len(ts_files),
            "ts_status": ts_status,
            "functions": len(rows),
            "unparseable_py_files": unparseable,
            "cc_threshold": cc_threshold,
            "exceeds_threshold": len(exceeds),
            "avg_cc": round(sum(cc_values) / len(cc_values), 2) if cc_values else 0.0,
            "max_cc": max(cc_values) if cc_values else 0,
            "max_cognitive": max((r["cognitive"] for r in rows), default=0),
        },
        "limit": limit,
        "shown": len(shown),
        "functions": shown,
        "exceeds": exceeds[: max(limit, 0)],
    }


def render_text(result: dict) -> str:
    """人读呈现（chrome 英文，惯例同报告链）。"""
    s = result["summary"]
    lines = [f"vrp complexity - {Path(result['repo']).name}"]
    lines.append(
        f"  files      : {s['py_files']} py / {s['ts_files']} ts"
        f" (ts engine: {s['ts_status']})"
        f"  unparseable: {s['unparseable_py_files']}"
    )
    lines.append(
        f"  functions  : {s['functions']}  avg cc {s['avg_cc']}"
        f"  max cc {s['max_cc']}  max cognitive {s['max_cognitive']}"
    )
    lines.append(
        f"  threshold  : cc > {s['cc_threshold']} -> {s['exceeds_threshold']} function(s) over"
    )
    for e in result["exceeds"]:
        lines.append(
            f"    [over] {e['file']}:{e['line']}  {e['name']}  cc {e['cc']}  cognitive {e['cognitive']}"
        )
    lines.append(f"  top (by cc desc, showing {result['shown']}):")
    for r in result["functions"]:
        lines.append(f"    {r['file']}:{r['line']}  {r['name']}  cc {r['cc']}  cognitive {r['cognitive']}")
    return "\n".join(lines)
