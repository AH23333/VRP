import json
import sqlite3
from pathlib import Path

from vrp import nav

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    return repo


def _add_symbol(repo: Path, fid: str, qn: str, name: str, file_path: str, line: int, label: str = "Function") -> str:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES (?, 'p', ?, ?, ?, ?, ?, ?, 'h')",
        (fid, qn, label, name, file_path, line, line + 2),
    )
    con.commit()
    con.close()
    return fid


def _add_edge(repo: Path, src: str, dst: str, etype: str = "CALLS") -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?, ?, ?, 'structural')",
        (src, dst, etype),
    )
    con.commit()
    con.close()


def _add_finding(repo: Path, pathology: str, fid: str | None, file_path: str | None, line: int | None = None) -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, file_path, start_line, payload_json)"
        " VALUES (?, 'k', 's2-astgrep', ?, ?, ?, '{}')",
        (pathology, fid, file_path, line),
    )
    con.commit()
    con.close()


def test_nav_search_exact_first_and_fid_handle(tmp_path):
    repo = _mk_repo(tmp_path)
    f1 = _add_symbol(repo, "f1", "pkg.calc.total", "total", "pkg/calc.py", 1)
    _add_symbol(repo, "f2", "pkg.calc.total_all", "total_all", "pkg/calc.py", 5)
    _add_symbol(repo, "f3", "pkg.helpers.totalize", "totalize", "pkg/helpers.py", 1)
    _add_symbol(repo, "f4", "builtins.fake", "fake", "<python-builtins>", 1)
    out = nav.nav_search(repo, "total")
    assert out["count"] == 3
    assert out["results"][0]["fid"] == f1
    assert out["results"][0]["qualified_name"] == "pkg.calc.total"
    names = [r["name"] for r in out["results"]]
    assert names == ["total", "total_all", "totalize"]


def test_nav_search_empty_query_rejected_and_limit(tmp_path):
    repo = _mk_repo(tmp_path)
    for i in range(5):
        _add_symbol(repo, f"f{i}", f"pkg.m{i}", f"m{i}", "pkg/a.py", i)
    try:
        nav.nav_search(repo, "  ")
        raise AssertionError("empty query must raise")
    except SystemExit:
        pass
    out = nav.nav_search(repo, "m", limit=3)
    assert out["count"] == 3


def test_nav_architecture_dir_fallback(tmp_path):
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a.x", "x", "pkg/a.py", 1)
    _add_symbol(repo, "f2", "pkg.a.y", "y", "pkg/a.py", 9)
    _add_symbol(repo, "f3", "pkg.b.z", "z", "pkg/b.py", 1)
    _add_symbol(repo, "f4", "top.w", "w", "top.py", 1)
    _add_symbol(repo, "f5", "synth.s", "s", "<python-builtins>", 1)
    _add_finding(repo, "P1", "f1", "pkg/a.py", 3)
    out = nav.nav_architecture(repo)
    assert out["fallback"] == "dir-aggregation"
    assert out["total_symbols"] == 4
    assert out["total_findings"] == 1
    mods = {m["path"]: m for m in out["modules"]}
    assert mods["pkg"]["symbols"] == 3
    assert mods["pkg"]["files"] == 2
    assert mods["pkg"]["findings"] == 1
    assert mods["."]["files"] == 1
    assert set(mods) == {".", "pkg"}


def test_nav_module_symbols_findings_edges(tmp_path):
    repo = _mk_repo(tmp_path)
    f1 = _add_symbol(repo, "f1", "pkg.a.x", "x", "pkg/a.py", 1)
    f2 = _add_symbol(repo, "f2", "pkg.a.y", "y", "pkg/a.py", 9)
    _add_symbol(repo, "f3", "pkg.b.z", "z", "pkg/b.py", 1)
    _add_edge(repo, "f1", "f2")
    _add_edge(repo, "f3", "f1")
    _add_finding(repo, "P3", f1, "pkg/a.py", 2)
    out = nav.nav_module(repo, "pkg/a.py")
    assert out["module_id"] == "pkg/a.py"
    assert [s["fid"] for s in out["symbols"]] == ["f1", "f2"]
    assert out["findings_by_pathology"] == {"P3": 1}
    assert out["edges_out"] == 1
    assert out["edges_in"] == 2  # f3->f1 与 f1->f2 的 dst 均在本文件
    try:
        nav.nav_module(repo, "nope.py")
        raise AssertionError("unknown module must raise")
    except SystemExit:
        pass


def test_nav_symbol_full_chain(tmp_path):
    repo = _mk_repo(tmp_path)
    f1 = _add_symbol(repo, "f1", "pkg.a.x", "x", "pkg/a.py", 1)
    f2 = _add_symbol(repo, "f2", "pkg.b.z", "z", "pkg/b.py", 1)
    _add_edge(repo, "f1", "f2")
    _add_finding(repo, "P1", f2, "pkg/b.py", 3)
    out = nav.nav_symbol(repo, f2)
    assert out["level"] == "L2"
    assert out["symbol"]["qualified_name"] == "pkg.b.z"
    assert out["callers"][0]["fid"] == f1
    assert out["callers"][0]["type"] == "CALLS"
    assert out["callees"] == []
    assert out["findings"][0]["pathology"] == "P1"
    assert out["slices"] is None
    drill = nav.nav_symbol(repo, out["callers"][0]["fid"])
    assert drill["symbol"]["qualified_name"] == "pkg.a.x"
    try:
        nav.nav_symbol(repo, "missing")
        raise AssertionError("unknown fid must raise")
    except SystemExit:
        pass


def test_query_facts_select_only_guard(tmp_path):
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a.x", "x", "pkg/a.py", 1)
    ok = nav.query_facts(repo, "SELECT COUNT(*) AS n FROM symbols")
    assert ok["rows"] == [{"n": 1}]
    with_cte = nav.query_facts(
        repo, "WITH t AS (SELECT 1 AS v) SELECT v FROM t"
    )
    assert with_cte["rows"] == [{"v": 1}]
    for bad in (
        "INSERT INTO symbols VALUES ('x')",
        "DROP TABLE symbols",
        "UPDATE symbols SET name='x'",
        "PRAGMA journal_mode",
        "SELECT 1; DROP TABLE symbols",
        "",
    ):
        try:
            nav.query_facts(repo, bad)
            raise AssertionError(f"must reject: {bad!r}")
        except SystemExit:
            pass


def test_query_facts_params_and_truncation(tmp_path):
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a.x", "x", "pkg/a.py", 1)
    _add_symbol(repo, "f2", "pkg.a.y", "y", "pkg/a.py", 5)
    out = nav.query_facts(
        repo, "SELECT qualified_name FROM symbols WHERE name = ?", json.dumps(["x"])
    )
    assert out["rows"] == [{"qualified_name": "pkg.a.x"}]
    try:
        nav.query_facts(repo, "SELECT 1", params='{"not":"list"}')
        raise AssertionError("non-list params must raise")
    except SystemExit:
        pass
    trunc = nav.query_facts(repo, "SELECT fid FROM symbols", max_rows=1)
    assert trunc["truncated"] is True
    assert trunc["rowcount"] == 1
    full = nav.query_facts(repo, "SELECT fid FROM symbols", max_rows=10)
    assert full["truncated"] is False
    assert full["rowcount"] == 2
