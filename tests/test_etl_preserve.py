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
    con.execute(
        "INSERT INTO fid_ledger (fid, project, qualified_name, file_path, first_seen_at)"
        " VALUES ('f-old', 'p', 'pkg.old', 'pkg/x.py', '2026-11-06T00:00:00Z')"
    )
    con.commit()
    con.close()


def test_snapshot_and_restore_roundtrip(tmp_path):
    db = tmp_path / "facts.db"
    _build_db(db)
    snap = snapshot_owned(db)
    # R-D E1：快照每表 (cols, rows)——列名随行记录
    ann_cols, ann_rows = snap["annotations"]
    assert "qualified_name" in ann_cols and "status" in ann_cols
    assert len(ann_rows) == 1
    assert len(snap["gate_results"][1]) == 1
    assert len(snap["runtime_evidence"][1]) == 1
    rt_cols, rt_rows = snap["runtime_evidence"]
    assert rt_rows[0][rt_cols.index("callee_key")] == "test"
    assert len(snap["findings_kept"][1]) == 1
    fk_cols, fk_rows = snap["findings_kept"]
    assert fk_rows[0][fk_cols.index("source")] == "s2-astgrep"

    db.unlink()
    fresh = sqlite3.connect(db)
    fresh.executescript(SCHEMA.read_text(encoding="utf-8"))
    warnings = restore_owned(fresh, snap)
    fresh.commit()
    assert warnings == []  # 同 schema 恢复零警告
    ann = fresh.execute("SELECT qualified_name, status FROM annotations").fetchall()
    assert ann == [("m.go", "active")]
    srcs = [r[0] for r in fresh.execute("SELECT source FROM findings ORDER BY source")]
    assert srcs == ["s2-astgrep"]
    gates = fresh.execute("SELECT gate, status FROM gate_results").fetchall()
    assert gates == [("G1", "pass")]
    ev = fresh.execute("SELECT callee_key, payload_json FROM runtime_evidence").fetchall()
    assert ev == [("test", '{"exit_code": 0}')]
    ledger = fresh.execute(
        "SELECT fid, project, qualified_name, file_path, first_seen_at FROM fid_ledger"
    ).fetchall()
    assert ledger == [("f-old", "p", "pkg.old", "pkg/x.py", "2026-11-06T00:00:00Z")]
    fresh.close()


def test_restore_aligns_by_column_name_across_reordered_schema(tmp_path):
    """R-D E1：schema 列序演化（列数同、顺序不同）——按名对齐不错位。"""
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute(
        "CREATE TABLE gate_results (id INTEGER PRIMARY KEY, detail_json TEXT, gate TEXT, status TEXT)"
    )
    con.execute("INSERT INTO gate_results (detail_json, gate, status) VALUES ('{}', 'G9', 'pass')")
    con.commit()
    con.close()
    snap = snapshot_owned(old)
    fresh = sqlite3.connect(tmp_path / "fresh.db")
    fresh.executescript(SCHEMA.read_text(encoding="utf-8"))
    warnings = restore_owned(fresh, snap)
    fresh.commit()
    row = fresh.execute("SELECT gate, status, detail_json FROM gate_results").fetchone()
    fresh.close()
    assert warnings == []
    assert row == ("G9", "pass", "{}")  # gate/status 不错位


def test_restore_skips_not_null_spanning_table_with_warning(tmp_path):
    """R-D E1：缺源列撞 NOT NULL 无默认（schema 演化跨列）——整表跳过+诚实警告。"""
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    # 假想旧版 fid_ledger 无 first_seen_at（新 schema NOT NULL 无默认）
    con.execute(
        "CREATE TABLE fid_ledger (fid TEXT PRIMARY KEY, project TEXT,"
        " qualified_name TEXT, file_path TEXT)"
    )
    con.execute("INSERT INTO fid_ledger VALUES ('f-1', 'p', 'pkg.a', 'a.py')")
    con.commit()
    con.close()
    snap = snapshot_owned(old)
    fresh = sqlite3.connect(tmp_path / "fresh.db")
    fresh.executescript(SCHEMA.read_text(encoding="utf-8"))
    warnings = restore_owned(fresh, snap)
    fresh.commit()
    rows = fresh.execute("SELECT COUNT(*) FROM fid_ledger").fetchone()[0]
    fresh.close()
    assert rows == 0  # 违约束表不落位
    assert any("fid_ledger" in w for w in warnings)


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
        "fid_ledger",
    }
