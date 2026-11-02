import itertools
import re
import sqlite3
from pathlib import Path

import pytest

from vrp import s2_deadcode, testsig
from vrp.s2_deadcode import ENTRY_NAMES, TARGET_LABELS, USAGE_EDGE_TYPES, is_candidate
from tests.deep.conftest import ROOT

pytestmark = pytest.mark.deep

SCHEMA = ROOT / "vrp" / "schema" / "facts_schema.sql"
SCHEMA_TEXT = SCHEMA.read_text(encoding="utf-8")

NAMES = [
    "used_fn",
    "main",
    "run",
    "app",
    "index",
    "setup",
    "teardown",
    "handler",
    "cli",
    "Main",
    "MAIN",
    "test_fn",
    "fn_test",
    "Test",
    "test",
    "TEST",
    "_test",
    "test_",
    "__init__",
    "__dunder__",
    "_private",
    "fn%with_like",
    "fn_with'quote",
    "100%_x",
    "back\\slash",
    "中文函数",
    "a_b",
    "Handler",
    "cli_main",
    "main_sub",
]

PATHS = [
    "src/mod.py",
    "tests/t.py",
    "test/t.py",
    "spec/t.py",
    "__tests__/t.py",
    "src/tests/t.py",
    "src/test_x.py",
    "src/x_test.py",
    "t_test.go",
    "t_test.py",
    "t_test.rs",
    "t_test.cpp",
    "t_test.lua",
    "SomeTest.java",
    "SomeTest.kt",
    "SomeTest.cs",
    "SomeTest.php",
    "SomeSpec.scala",
    "x_spec.rb",
    "a.test.ts",
    "a.spec.js",
    "b.test.tsx",
    "b.spec.tsx",
    "<synthetic>",
    ">other-synth",
    "tests",
    "test",
    "spec",
    "TESTS/x.py",
    "Tests/x.py",
    "deep\\windows\\tests\\c.py",
    "deep/windows/test/d.py",
    "pkg\\__tests__\\e.ts",
]


def _mk_con():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_TEXT)
    return con


@pytest.mark.parametrize("name,file_path", list(itertools.product(NAMES, PATHS)))
def test_view_matches_is_candidate(name, file_path):
    con = _mk_con()
    try:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
            " VALUES (?,?,?,?,?,?,?)",
            ("f1", "p", f"p.{name}", "Function", name, file_path, "h"),
        )
        con.commit()
        sql_hit = con.execute("SELECT COUNT(*) FROM v_dead_code WHERE fid = 'f1'").fetchone()[0] == 1
    finally:
        con.close()
    py_hit = is_candidate(name, file_path)
    assert sql_hit == py_hit, f"view={sql_hit} python={py_hit} for name={name!r} path={file_path!r}"


def test_view_labels_match_constants():
    con = _mk_con()
    try:
        for label in ("Function", "Method", "Class", "Variable"):
            con.execute("DELETE FROM symbols")
            con.execute(
                "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
                " VALUES (?,?,?,?,?,?,?)",
                ("f1", "p", "p.x", label, "some_fn", "src/a.py", "h"),
            )
            con.commit()
            hit = con.execute("SELECT COUNT(*) FROM v_dead_code WHERE fid = 'f1'").fetchone()[0] == 1
            assert hit == (label in TARGET_LABELS), f"label={label} view={hit}"
    finally:
        con.close()


def test_view_uses_all_usage_edge_types():
    con = _mk_con()
    try:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
            " VALUES (?,?,?,?,?,?,?)",
            ("f1", "p", "p.x", "Function", "some_fn", "src/a.py", "h"),
        )
        for edge_type in ("CALLS", "CALL_REFERENCE", "MENTIONS", "SPAWNS", "IMPORTS", "CONTAINS"):
            for stmt in ("DELETE FROM edges", "DELETE FROM usage_hints", "DELETE FROM test_linkage"):
                con.execute(stmt)
            if edge_type:
                con.execute(
                    "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('f2','f1',?,'E')",
                    (edge_type,),
                )
            con.commit()
            hit = con.execute("SELECT COUNT(*) FROM v_dead_code WHERE fid = 'f1'").fetchone()[0] == 1
            assert hit == (edge_type not in USAGE_EDGE_TYPES), f"edge={edge_type} view={hit}"
    finally:
        con.close()


def test_schema_entry_names_match_python_constant():
    view_sql = SCHEMA_TEXT[SCHEMA_TEXT.find("CREATE VIEW v_dead_code"):]
    match = re.search(r"name NOT IN \(([^)]*)\)", view_sql)
    assert match is not None
    sql_names = {m.strip().strip("'") for m in match.group(1).split(",")}
    assert sql_names == set(ENTRY_NAMES), f"drift: sql={sorted(sql_names)} py={sorted(ENTRY_NAMES)}"


def test_schema_edge_types_match_python_constant():
    view_sql = SCHEMA_TEXT[SCHEMA_TEXT.find("CREATE VIEW v_dead_code"):]
    match = re.search(r"type IN \(([^)]*)\)", view_sql)
    assert match is not None
    sql_types = {m.strip().strip("'") for m in match.group(1).split(",")}
    assert sql_types == set(USAGE_EDGE_TYPES)


def test_usage_hints_and_test_linkage_block_view():
    con = _mk_con()
    try:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
            " VALUES (?,?,?,?,?,?,?)",
            ("f1", "p", "p.x", "Function", "some_fn", "src/a.py", "h"),
        )
        con.execute("INSERT INTO usage_hints (src_fid, dst_fid) VALUES ('f2','f1')")
        con.commit()
        assert con.execute("SELECT COUNT(*) FROM v_dead_code WHERE fid='f1'").fetchone()[0] == 0
        con.execute("DELETE FROM usage_hints")
        con.execute("INSERT INTO test_linkage (test_fid, target_fid, source) VALUES ('f2','f1','T')")
        con.commit()
        assert con.execute("SELECT COUNT(*) FROM v_dead_code WHERE fid='f1'").fetchone()[0] == 0
        con.execute("DELETE FROM test_linkage")
        con.commit()
        assert con.execute("SELECT COUNT(*) FROM v_dead_code WHERE fid='f1'").fetchone()[0] == 1
    finally:
        con.close()
