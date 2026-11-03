import hashlib
import sqlite3
from pathlib import Path

import pytest

from vrp import s3

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

SRC = "def go(x):\n    return x * 2\n\ndef other(y):\n    return y\n"
SRC_LINES = SRC.splitlines()


def _def_hash(start: int, end: int) -> str:
    return hashlib.sha256("\n".join(SRC_LINES[start - 1 : end]).encode()).hexdigest()


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(SRC, encoding="utf-8")
    return tmp_path


@pytest.fixture
def con():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1','proj','m.go','Function','go','src/m.py',1,2,?)",
        (_def_hash(1, 2),),
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2','proj','m.other','Function','other','src/m.py',4,5,?)",
        (_def_hash(4, 5),),
    )
    con.commit()
    yield con
    con.close()


def test_add_annotation_ok(repo, con):
    result = s3.add_annotation(con, repo, "m.go", 1, 2, "purpose", "doubles x")
    assert result["ok"] and result["id"] == 1
    row = con.execute(
        "SELECT file_path, start_line, end_line, symbol_hash, window_hash, status FROM annotations"
    ).fetchone()
    assert row[0] == "src/m.py"
    assert row[1] == 1 and row[2] == 2
    assert row[3] == _def_hash(1, 2)
    assert len(row[4]) == 64
    assert row[5] == "active"


def test_add_annotation_rejections(repo, con):
    assert s3.add_annotation(con, repo, "nope.qn", 1, 1, "k", "b") == {
        "ok": False,
        "reason": "unknown-qualified_name",
    }
    assert s3.add_annotation(con, repo, "m.go", 99, 100, "k", "b")["reason"] == "window-outside-file"
    assert s3.add_annotation(con, repo, "m.go", 0, 0, "k", "b")["reason"] == "window-outside-file"
    assert s3.add_annotation(con, repo, "m.other", 1, 1, "k", "b")["reason"] == "window-outside-symbol"


def test_validate_anchors_active(repo, con):
    s3.add_annotation(con, repo, "m.go", 1, 2, "purpose", "b")
    detail = s3.validate_anchors(con, repo)
    assert detail["counts"] == {"active": 1, "cosmetic": 0, "stale": 0}


def test_validate_anchors_cosmetic(repo, con):
    s3.add_annotation(con, repo, "m.go", 2, 2, "purpose", "b")
    (repo / "src" / "m.py").write_text(
        "def go(x, y=0):\n    return x * 2\n\ndef other(y):\n    return y\n", encoding="utf-8"
    )
    detail = s3.validate_anchors(con, repo)
    assert detail["counts"] == {"active": 0, "cosmetic": 1, "stale": 0}
    status = con.execute("SELECT status FROM annotations").fetchone()[0]
    assert status == "cosmetic"


def test_validate_anchors_stale_window_changed(repo, con):
    s3.add_annotation(con, repo, "m.go", 1, 2, "purpose", "b")
    (repo / "src" / "m.py").write_text("def go(x):\n    return x * 3\n\ndef other(y):\n    return y\n", encoding="utf-8")
    detail = s3.validate_anchors(con, repo)
    assert detail["counts"] == {"active": 0, "cosmetic": 0, "stale": 1}
    assert detail["stale_reasons"] == {"window-changed": 1}


def test_validate_anchors_symbol_gone(repo, con):
    s3.add_annotation(con, repo, "m.go", 1, 2, "purpose", "b")
    con.execute("DELETE FROM symbols")
    con.commit()
    detail = s3.validate_anchors(con, repo)
    assert detail["counts"]["stale"] == 1
    assert detail["stale_reasons"] == {"symbol-gone": 1}


def test_coverage(repo, con):
    s3.add_annotation(con, repo, "m.go", 1, 2, "purpose", "b")
    s3.add_annotation(con, repo, "m.other", 4, 5, "purpose", "b")
    detail = s3.coverage(con)
    assert detail["total"] == 2
    assert detail["annotated"] == 2
    assert detail["missing_sample"] == []
    con.execute("UPDATE annotations SET status='stale' WHERE qualified_name='m.other'")
    con.commit()
    detail = s3.coverage(con)
    assert detail["annotated"] == 1
    assert detail["missing_sample"] == ["m.other"]


def test_gate_claims_rejects_path_escape(repo, con, tmp_path):
    """H4: finding claims with paths escaping the repo must be rejected,
    never accepted and written into findings."""
    secret = tmp_path.parent / "secret-outside-repo.txt"
    secret.write_text("leak me\n", encoding="utf-8")
    claims = [
        {"kind": "finding", "pathology": "P1", "file_path": "../secret-outside-repo.txt", "line": 1, "quote": "leak me", "body": ""},
        {"kind": "finding", "pathology": "P1", "file_path": str(secret), "line": 1, "quote": "leak me", "body": ""},
        {"kind": "finding", "pathology": "P1", "file_path": "src/../src/m.py", "line": 2, "quote": "return x * 2", "body": ""},
    ]
    detail = s3.gate_claims(con, repo, claims)
    assert detail["accepted"] == 0
    assert [r["reason"] for r in detail["rejected"]] == ["path-outside-repo"] * 3
    assert con.execute("SELECT COUNT(*) FROM findings WHERE source = 'ai-gate'").fetchone()[0] == 0
    secret.unlink()


def test_gate_claims_accepts_normal_relative_path(repo, con):
    detail = s3.gate_claims(
        con,
        repo,
        [{"kind": "finding", "pathology": "P1", "file_path": "src/m.py", "line": 2, "quote": "return x * 2", "body": ""}],
    )
    assert detail["accepted"] == 1
    assert detail["rejected"] == []


def test_gate_claims_annotation_and_finding(repo, con):
    claims = [
        {"kind": "annotation", "qualified_name": "m.go", "start_line": 1, "end_line": 2, "note_kind": "purpose", "body": "b"},
        {"kind": "finding", "pathology": "P1", "note_kind": "smell", "file_path": "src/m.py", "line": 2, "quote": "return x * 2", "body": "magic op"},
        {"kind": "finding", "pathology": "P1", "file_path": "src/m.py", "line": 2, "quote": "wrong quote", "body": ""},
        {"kind": "finding", "pathology": "P1", "file_path": "src/nope.py", "line": 1, "quote": "x", "body": ""},
        {"kind": "finding", "pathology": "P1", "file_path": "src/m.py", "line": 99, "quote": "x", "body": ""},
        {"kind": "weird", "x": 1},
    ]
    detail = s3.gate_claims(con, repo, claims)
    assert detail["accepted"] == 2
    assert [r["reason"] for r in detail["rejected"]] == [
        "quote-mismatch",
        "unknown-file",
        "line-out-of-range",
        "unknown-claim-kind",
    ]
    gate = con.execute("SELECT gate, status FROM gate_results ORDER BY id").fetchall()
    assert ("S3-evidence-gate", "warn") in gate
    ann = con.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
    assert ann == 1
    finding = con.execute(
        "SELECT kind, source, anchor_fid, file_path, start_line FROM findings WHERE source = 'ai-gate'"
    ).fetchone()
    assert finding == ("smell", "ai-gate", "f1", "src/m.py", 2)


def test_gate_claims_isolates_malformed_claim(repo, con):
    """L2: one malformed claim (non-numeric line/start_line) must be
    rejected as claim-error, not crash the whole gate run."""
    claims = [
        {"kind": "finding", "pathology": "P1", "file_path": "src/m.py", "line": "abc", "quote": "return x * 2", "body": ""},
        {"kind": "annotation", "qualified_name": "m.go", "start_line": "x1", "end_line": 2, "note_kind": "note", "body": "b"},
        {"kind": "finding", "pathology": "P1", "file_path": "src/m.py", "line": 2, "quote": "return x * 2", "body": ""},
    ]
    detail = s3.gate_claims(con, repo, claims)
    assert detail["accepted"] == 1
    assert [(r["index"], r["reason"]) for r in detail["rejected"]] == [
        (0, "claim-error"),
        (1, "claim-error"),
    ]
    assert con.execute("SELECT COUNT(*) FROM findings WHERE source = 'ai-gate'").fetchone()[0] == 1
