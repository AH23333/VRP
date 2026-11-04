import sqlite3
from pathlib import Path

from vrp import etl
from vrp.etl import restore_owned, snapshot_owned

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _build_db(path: Path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO annotations (qualified_name, file_path, start_line, end_line, symbol_hash, window_hash, author, kind, body, status)"
        " VALUES ('m.go','src/m.py',1,3,'h1','w1','ai','purpose','does go','active')"
    )
    con.execute(
        "INSERT INTO findings (pathology, kind, source, payload_json) VALUES ('P1','x','s2-astgrep','{}')"
    )
    con.execute(
        "INSERT INTO findings (pathology, kind, source, payload_json) VALUES ('P2','similar_to','etl','{}')"
    )
    con.execute("INSERT INTO gate_results (gate, status, detail_json) VALUES ('G1','pass','{}')")
    con.execute(
        "INSERT INTO runtime_evidence (caller_key, callee_key, payload_json)"
        " VALUES (NULL, 'test', '{\"exit_code\": 0}')"
    )
    con.commit()
    con.close()


def test_snapshot_and_restore_roundtrip(tmp_path):
    db = tmp_path / "facts.db"
    _build_db(db)
    snap = snapshot_owned(db)
    assert len(snap["annotations"]) == 1
    assert len(snap["gate_results"]) == 1
    assert len(snap["runtime_evidence"]) == 1
    assert snap["runtime_evidence"][0][2] == "test"
    assert len(snap["findings_kept"]) == 1
    assert snap["findings_kept"][0][3] == "s2-astgrep"

    db.unlink()
    fresh = sqlite3.connect(db)
    fresh.executescript(SCHEMA.read_text(encoding="utf-8"))
    restore_owned(fresh, snap)
    fresh.commit()
    ann = fresh.execute("SELECT qualified_name, status FROM annotations").fetchall()
    assert ann == [("m.go", "active")]
    srcs = [r[0] for r in fresh.execute("SELECT source FROM findings ORDER BY source")]
    assert srcs == ["s2-astgrep"]
    gates = fresh.execute("SELECT gate, status FROM gate_results").fetchall()
    assert gates == [("G1", "pass")]
    ev = fresh.execute("SELECT callee_key, payload_json FROM runtime_evidence").fetchall()
    assert ev == [("test", '{"exit_code": 0}')]
    fresh.close()


def test_snapshot_missing_file(tmp_path):
    assert snapshot_owned(tmp_path / "nope.db") == {}


def test_snapshot_tolerates_old_schema(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.commit()
    con.close()
    snap = snapshot_owned(db)
    assert snap == {}


def test_owned_tables_constant():
    assert set(etl.OWNED_TABLES) == {
        "annotations",
        "transactions",
        "gate_results",
        "dossier_views",
        "baseline",
        "runtime_evidence",
        "slices",
        "structure",
        "decisions",
    }
