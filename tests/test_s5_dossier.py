import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s5_dossier

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

VIEW_NAMES = {"v_hotspots", "v_cycles", "v_dead_code", "v_dup_clusters", "v_coupling"}


@pytest.fixture
def con():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('fa','p','mod.a','Function','a','src/one.ts',1,2,'hash-1')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('fb','p','mod.b','Function','b','src/two.ts',1,2,'hash-2')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('fc','p','mod.c','Function','c','src/three.ts',1,2,'hash-1')"
    )
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fa','fb','CALLS','E2')")
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fb','fa','CALLS','E2')")
    con.commit()
    yield con
    con.close()


def test_views_created(con):
    names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert VIEW_NAMES <= names


def test_hotspots(con):
    rows = s5_dossier.hotspots(con)
    top = rows[0]
    assert top["qualified_name"] in ("mod.a", "mod.b")
    assert top["fan_in"] == 1
    assert top["fan_out"] == 1
    assert top["score"] == 1


def test_mutual_cycles(con):
    pairs = s5_dossier.mutual_cycles(con)
    assert pairs == [{"symbol_a": "mod.a", "symbol_b": "mod.b"}]


def test_strongly_connected(con):
    result = s5_dossier.strongly_connected(con)
    assert result["scc_count"] == 1
    assert result["max_size"] == 2
    assert result["largest"] == [["mod.a", "mod.b"]]


def test_dead_code_view(con):
    rows = s5_dossier.dead_code(con)
    assert [r["qualified_name"] for r in rows] == ["mod.c"]


def test_dead_code_entry_name_excluded(con):
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
        " VALUES ('fm','p','mod.main','Function','main','src/main.ts','hash-m')"
    )
    con.commit()
    names = [r["qualified_name"] for r in s5_dossier.dead_code(con)]
    assert "mod.main" not in names


def test_dup_clusters(con):
    clusters = s5_dossier.dup_clusters(con)
    assert len(clusters) == 1
    assert clusters[0]["copies"] == 2
    assert set(clusters[0]["members"].split("|")) == {"mod.a", "mod.c"}


def test_coupling(con):
    rows = s5_dossier.coupling(con)
    assert len(rows) == 2
    pair = {(r["file_a"], r["file_b"]) for r in rows}
    assert pair == {("src/one.ts", "src/two.ts"), ("src/two.ts", "src/one.ts")}
    assert all(r["edge_count"] == 1 for r in rows)


def test_fit_trims_longest_list():
    payload = {"small": "x", "big": [f"row-{i}" for i in range(500)]}
    fitted = s5_dossier._fit(payload, cap=2000)
    assert len(json.dumps(fitted, ensure_ascii=False)) <= 2000
    assert fitted["small"] == "x"
    assert len(fitted["big"]) < 500


def test_fit_noop_when_small():
    payload = {"a": [1, 2, 3]}
    assert s5_dossier._fit(payload, cap=1000) is payload


def test_build_dossier(tmp_path, con, monkeypatch):
    facts_db = tmp_path / "facts.db"
    dump = sqlite3.connect(facts_db)
    dump.executescript(SCHEMA.read_text(encoding="utf-8"))
    for row in con.execute("SELECT fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash FROM symbols"):
        dump.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            row,
        )
    for row in con.execute("SELECT src_fid, dst_fid, type, evidence FROM edges"):
        dump.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,?,?)", row)
    dump.commit()
    dump.close()
    monkeypatch.setattr("vrp.s5_dossier.get_architecture", lambda name: {"modules": ["one", "two"]})
    dossier = s5_dossier.build_dossier(tmp_path, facts_db, "p", budget=32000)
    assert dossier["project"] == "p"
    assert dossier["views"]["architecture"] == {"modules": ["one", "two"]}
    assert dossier["views"]["sql_views"]["hotspots"][0]["qualified_name"].startswith("mod.")
    assert dossier["views"]["summary"]["counts"]["symbols"] == 3
    assert dossier["out"].endswith("dossier.json")
    persisted = json.loads((tmp_path / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
    assert persisted["project"] == "p"
    check = sqlite3.connect(facts_db)
    rows = check.execute("SELECT view FROM dossier_views ORDER BY view").fetchall()
    check.close()
    assert [r[0] for r in rows] == ["architecture", "sql_views", "summary"]
