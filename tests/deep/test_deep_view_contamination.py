import json
import sqlite3

import pytest

from vrp.etl import SCHEMA_PATH, ensure_schema
from tests.deep.conftest import ROOT, run_vrp

pytestmark = pytest.mark.deep


def _is_fake(path: str) -> bool:
    return path == "" or path.startswith("<") or path.startswith(">")


def _open_ro(repo) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)


def _make_con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return con


def _sym(con, fid, qn, label="Function", path="src/a.py", start_line=1, end_line=5, chash="h"):
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
        " start_line, end_line, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (fid, "p", qn, label, qn.split(".")[-1], path, start_line, end_line, chash),
    )


def _edge(con, src, dst, etype="CALLS"):
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,?,?)",
        (src, dst, etype, "E"),
    )


def test_v_hotspots_excludes_synthetic_and_empty_paths():
    con = _make_con()
    _sym(con, "b", "builtins.str", path="<python-builtins>")
    _sym(con, "d", "<decorator:x>", path="")
    _sym(con, "w", "weird", path=">synthetic<")
    _sym(con, "r1", "p.real1", path="src/real1.py")
    _sym(con, "r2", "p.real2", path="src/real2.py")
    _edge(con, "b", "r1")
    _edge(con, "d", "r1")
    _edge(con, "w", "r1")
    _edge(con, "r2", "r1")
    rows = {r[0]: r for r in con.execute("SELECT qualified_name, file_path, fan_in, score FROM v_hotspots")}
    assert set(rows) == {"p.real1", "p.real2"}
    assert rows["p.real1"][2] == 4
    assert rows["p.real1"][3] == 4


def test_v_hotspots_filter_keeps_real_symbol_counts_exact():
    con = _make_con()
    _sym(con, "b", "builtins.len", path="<python-builtins>")
    _sym(con, "r", "p.hub", path="src/hub.py")
    for i in range(7):
        _sym(con, f"u{i}", f"p.u{i}", path=f"src/u{i}.py")
        _edge(con, f"u{i}", "r")
    _edge(con, "r", "b")
    rows = {r[0]: r for r in con.execute("SELECT qualified_name, fan_in, fan_out, score FROM v_hotspots")}
    assert set(rows) == {"p.hub", *(f"p.u{i}" for i in range(7))}
    assert rows["p.hub"] == ("p.hub", 7, 1, 7)


def test_big_repo_hotspots_top20_all_real_paths(big_repo):
    con = _open_ro(big_repo)
    try:
        rows = list(
            con.execute(
                "SELECT qualified_name, file_path, score FROM v_hotspots ORDER BY score DESC LIMIT 20"
            )
        )
    finally:
        con.close()
    assert len(rows) == 20
    for qn, path, _score in rows:
        assert not _is_fake(path), f"fake path in hotspots top20: {qn} -> {path}"
        assert not qn.startswith("builtins."), qn
        assert not qn.startswith("<"), qn
    assert rows[0][2] >= 5000
    assert rows[0][0].startswith("tree-sitter-analyzer.")


def test_big_repo_hotspots_no_builtins_anywhere(big_repo):
    con = _open_ro(big_repo)
    try:
        bad = con.execute(
            "SELECT COUNT(*) FROM v_hotspots WHERE file_path = '' OR file_path GLOB '<*'"
            " OR file_path GLOB '>*'"
        ).fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM v_hotspots").fetchone()[0]
    finally:
        con.close()
    assert bad == 0
    assert total >= 10000


def test_big_repo_all_views_synthetic_sweep(big_repo):
    con = _open_ro(big_repo)
    try:
        bad_dead = con.execute(
            "SELECT COUNT(*) FROM v_dead_code WHERE file_path = '' OR file_path GLOB '<*'"
            " OR file_path GLOB '>*'"
        ).fetchone()[0]
        bad_coupling = con.execute(
            "SELECT COUNT(*) FROM v_coupling WHERE file_a = '' OR file_b = ''"
            " OR file_a GLOB '<*' OR file_a GLOB '>*' OR file_b GLOB '<*' OR file_b GLOB '>*'"
        ).fetchone()[0]
        bad_cycles = con.execute(
            "SELECT COUNT(*) FROM v_cycles c"
            " JOIN symbols sa ON sa.fid = c.fid_a JOIN symbols sb ON sb.fid = c.fid_b"
            " WHERE sa.file_path = '' OR sa.file_path GLOB '<*' OR sa.file_path GLOB '>*'"
            " OR sb.file_path = '' OR sb.file_path GLOB '<*' OR sb.file_path GLOB '>*'"
        ).fetchone()[0]
        bad_dup = con.execute(
            "SELECT COUNT(*) FROM v_dup_clusters d JOIN symbols s ON s.content_hash = d.content_hash"
            " WHERE s.file_path = '' OR s.file_path GLOB '<*' OR s.file_path GLOB '>*'"
        ).fetchone()[0]
    finally:
        con.close()
    assert bad_dead == 0
    assert bad_coupling == 0
    assert bad_cycles == 0
    assert bad_dup == 0


def test_big_repo_dossier_hotspots_section_all_real(big_repo):
    run_vrp(["dossier", str(big_repo), "--task", "T2", "--skip-external"], cwd=ROOT, timeout=1800)
    doc = json.loads((big_repo / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
    hot = doc["views"]["sql_views"]["hotspots"]
    assert isinstance(hot, list) and len(hot) >= 1
    for row in hot:
        assert not _is_fake(row["file_path"]), row
        assert not row["qualified_name"].startswith("builtins."), row
        assert not row["qualified_name"].startswith("<"), row


def test_ensure_schema_self_heals_dropped_views_and_tables():
    con = sqlite3.connect(":memory:")
    ensure_schema(con)
    con.execute("DROP VIEW v_hotspots")
    con.execute("DROP TABLE annotations")
    con.commit()
    with pytest.raises(sqlite3.OperationalError):
        con.execute("SELECT COUNT(*) FROM v_hotspots")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("SELECT COUNT(*) FROM annotations")
    ensure_schema(con)
    assert con.execute("SELECT COUNT(*) FROM v_hotspots").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 0
    views = {
        name
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
    }
    assert views == {"v_hotspots", "v_cycles", "v_dead_code", "v_dup_clusters", "v_coupling"}


def test_ensure_schema_idempotent_repeated():
    con = sqlite3.connect(":memory:")
    for _ in range(3):
        ensure_schema(con)
    counts = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("symbols", "edges", "annotations", "v_hotspots")
    }
    assert counts == {"symbols": 0, "edges": 0, "annotations": 0, "v_hotspots": 0}


def test_cli_etl_heals_damaged_facts_db(tmp_path):
    repo = tmp_path / "heal-repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    run_vrp(["index", str(repo), "--reindex"], cwd=ROOT)
    run_vrp(["etl", str(repo)], cwd=ROOT)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute("DROP VIEW v_hotspots")
    con.execute("DROP TABLE annotations")
    con.commit()
    con.close()
    run_vrp(["etl", str(repo)], cwd=ROOT)
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM v_hotspots").fetchone()[0] >= 0
        hot = list(con.execute("SELECT qualified_name FROM v_hotspots"))
        assert any("add" in qn for qn, in hot)
        assert con.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 0
    finally:
        con.close()
