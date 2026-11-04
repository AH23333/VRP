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
    assert out["slices"] == []  # 无入库业务线（curate 前默认空）
    drill = nav.nav_symbol(repo, out["callers"][0]["fid"])
    assert drill["symbol"]["qualified_name"] == "pkg.a.x"
    try:
        nav.nav_symbol(repo, "missing")
        raise AssertionError("unknown fid must raise")
    except SystemExit:
        pass


def test_nav_callers_callees_paging(tmp_path):
    repo = _mk_repo(tmp_path)
    target = _add_symbol(repo, "t", "pkg.t.go", "go", "pkg/t.py", 1)
    for i in range(5):
        _add_symbol(repo, f"c{i}", f"pkg.c.f{i}", f"f{i}", "pkg/c.py", i + 1)
        _add_edge(repo, f"c{i}", "t", "CALLS")
    one = nav.nav_callers(repo, target, limit=3)
    assert one["count"] == 3
    assert one["truncated"] is True
    assert [n["fid"] for n in one["neighbors"]] == ["c0", "c1", "c2"]
    full = nav.nav_callers(repo, target, limit=50)
    assert full["truncated"] is False
    assert full["count"] == 5
    assert full["neighbors"][0]["qualified_name"] == "pkg.c.f0"
    assert full["neighbors"][0]["type"] == "CALLS"
    # callees：t 无出边
    none = nav.nav_callees(repo, target)
    assert none["neighbors"] == []
    assert none["truncated"] is False
    try:
        nav.nav_callers(repo, " ")
        raise AssertionError("empty fid must raise")
    except SystemExit:
        pass


def test_nav_slice_lookup(tmp_path):
    repo = _mk_repo(tmp_path)
    f1 = _add_symbol(repo, "f1", "pkg.a.x", "x", "pkg/a.py", 1)
    f2 = _add_symbol(repo, "f2", "pkg.b.z", "z", "pkg/b.py", 1)
    _add_edge(repo, "f1", "f2")
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO slices (slice_id, name, summary, payload_json, status) VALUES (?, ?, ?, ?, 'active')",
        ("flow-1", "业务线", "摘要", json.dumps({
            "steps": [{"fid": "f1", "role": "entry"}, {"fid": "f2", "role": "persist"}],
            "acts": [{"name": "全程", "from_step": 0, "to_step": 1}],
        })),
    )
    con.commit()
    con.close()
    out = nav.nav_slice(repo, "flow-1")
    assert out["level"] == "slice"
    assert out["status"] == "active"
    assert [s["fid"] for s in out["steps"]] == ["f1", "f2"]
    assert out["steps"][0]["role"] == "entry"
    assert out["steps"][1]["qualified_name"] == "pkg.b.z"
    assert out["acts"] == [{"name": "全程", "from_step": 0, "to_step": 1}]
    try:
        nav.nav_slice(repo, "ghost")
        raise AssertionError("unknown slice must raise")
    except SystemExit:
        pass


def test_nav_architecture_structure_branch(tmp_path):
    repo = _mk_repo(tmp_path)
    f1 = _add_symbol(repo, "f1", "pkg.a.x", "x", "pkg/a.py", 1)
    f2 = _add_symbol(repo, "f2", "pkg.b.z", "z", "pkg/b.py", 1)
    f3 = _add_symbol(repo, "f3", "pkg.b.w", "w", "pkg/b.py", 5)
    _add_edge(repo, "f1", "f2")
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO structure (payload_json, status) VALUES (?, 'active')",
        (json.dumps({
            "modules": [
                {"module_id": "m1", "name": "甲", "responsibility": "r1", "symbol_fids": ["f1"], "layer": "domain"},
                {"module_id": "m2", "name": "乙", "responsibility": "r2", "symbol_fids": ["f2", "f3"], "layer": "service"},
            ],
            "layers": [
                {"name": "domain", "may_depend_on": []},
                {"name": "service", "may_depend_on": ["domain"]},
            ],
        }),),
    )
    con.commit()
    con.close()
    out = nav.nav_architecture(repo)
    assert out["source"] == "structure"
    assert [m["module_id"] for m in out["modules"]] == ["m1", "m2"]
    assert out["modules"][0]["symbols"] == 1
    assert out["modules"][0]["sample_fids"] == ["f1"]
    assert out["module_edges"] == [{"from": "m1", "to": "m2", "count": 1}]
    assert out["unassigned_symbols"] == 0
    assert out["layers"][1]["may_depend_on"] == ["domain"]


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
