"""M1: when a re-indexed file replaces a symbol (same project+qualified_name,
different fid), edges of the replaced fid must die with it - no orphans."""

from __future__ import annotations

import sqlite3

import pytest

from vrp import etl


def _mk_cbm(db):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO store_meta VALUES (?,?)", [("db_uid", "uid-1"), ("mutation_gen", "7")])
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, properties TEXT)"
    )
    # node 1: stale generation of p.a.go (lines 1-2); node 2: re-indexed (1-3)
    con.execute("INSERT INTO nodes VALUES (1,'proj','Function','go','p.a.go','a.py',1,2,'{}')")
    con.execute("INSERT INTO nodes VALUES (2,'proj','Function','go','p.a.go','a.py',1,3,'{}')")
    con.execute("INSERT INTO nodes VALUES (3,'proj','Function','h','p.b.h','b.py',1,1,'{}')")
    con.execute("CREATE TABLE edges (source_id INT, target_id INT, type TEXT, properties TEXT, project TEXT)")
    con.executemany(
        "INSERT INTO edges VALUES (?,?,?,?, 'proj')",
        [(1, 3, "CALLS", "{}"), (3, 1, "CALLS", "{}"), (2, 3, "CALLS", "{}"), (3, 2, "CALLS", "{}")],
    )
    con.execute("CREATE TABLE index_coverage (project TEXT, rel_path TEXT, kind TEXT, detail TEXT)")
    con.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT)")
    con.commit()
    con.close()


@pytest.fixture()
def built(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def go():\n    return 1\n    return 2\n", encoding="utf-8")
    (repo / "b.py").write_text("def h():\n    pass\n", encoding="utf-8")
    cbm = tmp_path / "proj.db"
    _mk_cbm(cbm)
    monkeypatch.setattr(etl.s1_cbm, "cbm_db_path", lambda name: cbm)
    out_db = repo / ".vrp" / "facts.db"
    etl.run_etl(repo, "proj", out_db=out_db)
    con = sqlite3.connect(out_db)
    return con


def test_replaced_symbol_edges_are_cascaded(built):
    rows = built.execute("SELECT qualified_name, start_line, end_line FROM symbols ORDER BY qualified_name").fetchall()
    assert rows == [("p.a.go", 1, 3), ("p.b.h", 1, 1)], "only the surviving generation stays"
    assert built.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 2
    assert etl.orphan_edge_count(built) == 0


def test_other_file_edges_unaffected(built):
    # the new generation (fid of node 2) keeps both edges to p.b.h
    pairs = built.execute(
        "SELECT COUNT(*) FROM edges e"
        " JOIN symbols s ON s.fid = e.src_fid OR s.fid = e.dst_fid"
        " WHERE s.qualified_name = 'p.b.h'"
    ).fetchone()[0]
    assert pairs == 2
