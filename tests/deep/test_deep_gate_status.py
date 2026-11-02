import json
import sqlite3
from pathlib import Path

import pytest

from tests.deep.conftest import CBM_CACHE, ROOT, run_vrp

pytestmark = pytest.mark.deep

STATUS_TABLES = (
    "symbols",
    "edges",
    "usage_hints",
    "history_coupling",
    "test_linkage",
    "coverage_gaps",
    "findings",
    "discrepancy_ledger",
    "file_hashes",
)


@pytest.fixture(scope="module")
def gate_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("deep-gate")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "test_core.py").write_text(
        "from pkg.core import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".vrp/\n__pycache__/\n", encoding="utf-8")
    cache_db = CBM_CACHE / f"{repo.name}.db"
    if cache_db.exists():
        cache_db.unlink()
    run_vrp(["index", str(repo), "--reindex"], cwd=ROOT)
    run_vrp(["etl", str(repo)], cwd=ROOT)
    yield repo
    if cache_db.exists():
        cache_db.unlink()


def _write_claims(path: Path, claims) -> Path:
    path.write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")
    return path


def _run_gate(repo: Path, claims_path: Path) -> dict:
    return json.loads(run_vrp(["gate", str(repo), str(claims_path)], cwd=ROOT))


def _counts(repo: Path, table: str) -> int:
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def test_gate_accepts_annotation_claim(gate_repo, tmp_path):
    repo = gate_repo
    before = _counts(repo, "annotations")
    qn = f"{repo.name}.pkg.core.f"
    claims = _write_claims(
        tmp_path / "claims.json",
        [
            {
                "kind": "annotation",
                "qualified_name": qn,
                "start_line": 1,
                "end_line": 2,
                "note_kind": "purpose",
                "body": "deep gate e2e",
            }
        ],
    )
    result = _run_gate(repo, claims)
    assert result == {"accepted": 1, "rejected": []}
    assert _counts(repo, "annotations") == before + 1
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT qualified_name, status FROM annotations WHERE qualified_name = ?", (qn,)
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    assert row[1] == "active"


def test_gate_rejects_unknown_qn(gate_repo, tmp_path):
    repo = gate_repo
    before = _counts(repo, "annotations")
    claims = _write_claims(
        tmp_path / "claims.json",
        [
            {
                "kind": "annotation",
                "qualified_name": "no.such.symbol.anywhere",
                "start_line": 1,
                "end_line": 1,
                "note_kind": "note",
                "body": "x",
            }
        ],
    )
    result = _run_gate(repo, claims)
    assert result == {"accepted": 0, "rejected": [{"index": 0, "reason": "unknown-qualified_name"}]}
    assert _counts(repo, "annotations") == before


def test_gate_mixed_claims_indices_and_reasons(gate_repo, tmp_path):
    repo = gate_repo
    before = _counts(repo, "annotations")
    claims = _write_claims(
        tmp_path / "claims.json",
        [
            {
                "kind": "annotation",
                "qualified_name": "still.missing.one",
                "start_line": 1,
                "end_line": 1,
                "note_kind": "note",
                "body": "x",
            },
            {
                "kind": "annotation",
                "qualified_name": f"{repo.name}.pkg.core.f",
                "start_line": 1,
                "end_line": 2,
                "note_kind": "purpose",
                "body": "ok",
            },
            {
                "kind": "annotation",
                "qualified_name": "also.missing.two",
                "start_line": 1,
                "end_line": 1,
                "note_kind": "note",
                "body": "x",
            },
        ],
    )
    result = _run_gate(repo, claims)
    assert result["accepted"] == 1
    assert [r["index"] for r in result["rejected"]] == [0, 2]
    assert {r["reason"] for r in result["rejected"]} == {"unknown-qualified_name"}
    assert _counts(repo, "annotations") == before + 1


def test_gate_finding_quote_accepted(gate_repo, tmp_path):
    repo = gate_repo
    before = _counts(repo, "findings")
    claims = _write_claims(
        tmp_path / "claims.json",
        [
            {
                "kind": "finding",
                "file_path": "pkg/core.py",
                "line": 1,
                "quote": "def f():",
                "body": "entry point",
            }
        ],
    )
    result = _run_gate(repo, claims)
    assert result == {"accepted": 1, "rejected": []}
    assert _counts(repo, "findings") == before + 1
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT file_path, start_line FROM findings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    assert row[0] == "pkg/core.py"
    assert row[1] == 1


def test_gate_finding_quote_or_rejected_paths(gate_repo, tmp_path):
    repo = gate_repo
    before = _counts(repo, "findings")
    claims = _write_claims(
        tmp_path / "claims.json",
        [
            {"kind": "finding", "file_path": "nope.py", "line": 1, "quote": "x"},
            {"kind": "finding", "file_path": "pkg/core.py", "line": 0, "quote": "x"},
            {"kind": "finding", "file_path": "pkg/core.py", "line": 999, "quote": "x"},
            {"kind": "finding", "file_path": "pkg/core.py", "line": 1, "quote": "WRONG QUOTE"},
        ],
    )
    result = _run_gate(repo, claims)
    assert result["accepted"] == 0
    assert [r["reason"] for r in result["rejected"]] == [
        "unknown-file",
        "line-out-of-range",
        "line-out-of-range",
        "quote-mismatch",
    ]
    assert _counts(repo, "findings") == before


def test_gate_rejects_inverted_window(gate_repo, tmp_path):
    repo = gate_repo
    before = _counts(repo, "annotations")
    claims = _write_claims(
        tmp_path / "claims.json",
        [
            {
                "kind": "annotation",
                "qualified_name": f"{repo.name}.pkg.core.f",
                "start_line": 2,
                "end_line": 1,
                "note_kind": "note",
                "body": "x",
            },
            {
                "kind": "annotation",
                "qualified_name": f"{repo.name}.pkg.core.f",
                "start_line": 1,
                "end_line": 0,
                "note_kind": "note",
                "body": "x",
            },
        ],
    )
    result = _run_gate(repo, claims)
    assert result["accepted"] == 0
    assert [r["reason"] for r in result["rejected"]] == ["window-outside-file", "window-outside-file"]
    assert _counts(repo, "annotations") == before


def test_gate_accepts_eof_overhang_window(gate_repo, tmp_path):
    repo = gate_repo
    before = _counts(repo, "annotations")
    claims = _write_claims(
        tmp_path / "claims.json",
        [
            {
                "kind": "annotation",
                "qualified_name": f"{repo.name}.pkg.core.f",
                "start_line": 1,
                "end_line": 99999,
                "note_kind": "note",
                "body": "eof overhang",
            }
        ],
    )
    result = _run_gate(repo, claims)
    assert result == {"accepted": 1, "rejected": []}
    assert _counts(repo, "annotations") == before + 1


def test_gate_records_gate_results_row(gate_repo, tmp_path):
    repo = gate_repo
    claims = _write_claims(
        tmp_path / "claims-warn.json",
        [
            {"kind": "finding", "file_path": "pkg/core.py", "line": 1, "quote": "WRONG", "body": "x"},
            {"kind": "finding", "file_path": "pkg/core.py", "line": 2, "quote": "return 1", "body": "y"},
        ],
    )
    _run_gate(repo, claims)
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT gate, status, detail_json FROM gate_results WHERE gate = 'S3-evidence-gate' ORDER BY id"
        ).fetchall()
    finally:
        con.close()
    assert rows
    latest_status, latest_detail = rows[-1][1], json.loads(rows[-1][2])
    assert latest_status == "warn"
    assert latest_detail["accepted"] == 1
    assert len(latest_detail["rejected"]) == 1


def test_gate_claims_file_must_be_array(gate_repo, tmp_path):
    claims = tmp_path / "not-array.json"
    claims.write_text('{"not": "a list"}', encoding="utf-8")
    run_vrp(["gate", str(gate_repo), str(claims)], cwd=ROOT, expect_rc=1)


def test_gate_missing_claims_file(gate_repo, tmp_path):
    run_vrp(["gate", str(gate_repo), str(tmp_path / "missing.json")], cwd=ROOT, expect_rc=1)


def test_status_big_repo_counts_consistent(big_repo):
    out = json.loads(run_vrp(["status", str(big_repo)], cwd=ROOT))
    con = sqlite3.connect(f"file:{big_repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        expected_counts = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in STATUS_TABLES
        }
        expected_evidence = dict(
            con.execute("SELECT evidence, COUNT(*) FROM edges GROUP BY evidence")
        )
        meta_size = con.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    finally:
        con.close()
    assert out["counts"] == expected_counts
    assert expected_counts["symbols"] > 1000
    assert expected_counts["edges"] > 100000
    assert out["edges_by_evidence"] == expected_evidence
    assert len(out["meta"]) == meta_size
    assert meta_size > 0


def test_status_missing_facts_db(tmp_path):
    empty = tmp_path / "no-facts"
    empty.mkdir()
    out = json.loads(run_vrp(["status", str(empty)], cwd=ROOT))
    assert out == {"facts_db": None}
