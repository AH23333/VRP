"""R-AB: SQLite 边界异常完备性——损坏 facts.db 必须诚实降级，不得裸 traceback。

双边界族: cli.main（进程边界 → error: + rc=1）与 mcp_server._guard
（server 边界 → {"error": ...}，兑现"不炸 server"承诺）。
修前两处捕获元组均缺 sqlite3.Error，垃圾/空/异构 facts.db 直接穿透。
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

from vrp import cli, mcp_server

VRP_DIR = Path(cli.__file__).parent


def test_main_catches_corrupt_facts_db(tmp_path, capsys):
    """垃圾字节 facts.db → error: + rc=1，非 DatabaseError 裸 traceback 出 main。"""
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    (repo / ".vrp" / "facts.db").write_bytes(b"definitely not a sqlite db\n" * 16)
    rc = cli.main(["status", str(repo)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "file is not a database" in err


def test_main_catches_empty_facts_db(tmp_path, capsys):
    """0 字节 facts.db（sqlite 视为合法空库）→ no such table 转诚实 error rc=1。"""
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    (repo / ".vrp" / "facts.db").write_bytes(b"")
    rc = cli.main(["status", str(repo)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "no such table" in err


def test_mcp_guard_wraps_sqlite_errors():
    """_guard 捕 sqlite3.Error → {"error": ...} JSON，兑现"不炸 server"承诺。"""

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: meta")

    wrapped = mcp_server._guard(_boom)
    out = json.loads(wrapped())
    assert "error" in out
    assert "no such table" in out["error"]


def _except_tuple_names(fn_name: str, path: Path, inner: str | None = None) -> set[str]:
    """提取指定函数（可选内层函数）try 块 except 元组的异常全名字符串集合。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    target: ast.AST = tree
    if inner is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == fn_name:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.FunctionDef) and sub.name == inner:
                        target = sub
                break
    names: set[str] = set()
    for node in ast.walk(target):
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Tuple):
            for elt in node.type.elts:
                if isinstance(elt, ast.Attribute) and isinstance(elt.value, ast.Name):
                    names.add(f"{elt.value.id}.{elt.attr}")
                elif isinstance(elt, ast.Name):
                    names.add(elt.id)
    return names


def test_sqlite_error_in_boundary_catch_tuples():
    """AST 守卫：cli.main 与 mcp_server._guard 两处边界元组必含 sqlite3.Error，防漂移。"""
    cli_names = _except_tuple_names("main", VRP_DIR / "cli.py")
    assert "sqlite3.Error" in cli_names, f"cli.main 边界元组缺 sqlite3.Error: {sorted(cli_names)}"
    guard_names = _except_tuple_names("_guard", VRP_DIR / "mcp_server.py", inner="wrapped")
    assert "sqlite3.Error" in guard_names, f"mcp _guard 边界元组缺 sqlite3.Error: {sorted(guard_names)}"
