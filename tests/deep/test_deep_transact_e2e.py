import json
import sqlite3
from pathlib import Path

import pytest

from tests.deep.conftest import ROOT, git, read_verdict, run_vrp

pytestmark = pytest.mark.deep

CALC_BOTH = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
CALC_PLUS_SUB = "def plus(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
CALC_PLUS = "def plus(a, b):\n    return a + b\n"
CALC_PLUS_ZERO = "def plus(a, b):\n    return a + b + 0\n"
CALC_TOTAL = "def total(a, b):\n    return a + b\n"
CALC_GRAND = "def grand(a, b):\n    return a + b\n"
TEST_ADD = "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
TEST_PLUS = "from pkg.calc import plus\n\n\ndef test_add():\n    assert plus(1, 2) == 3\n"
TEST_TOTAL = "from pkg.calc import total\n\n\ndef test_add():\n    assert total(1, 2) == 3\n"
TEST_GRAND = "from pkg.calc import grand\n\n\ndef test_add():\n    assert grand(1, 2) == 3\n"
API_PLUS = "from pkg.calc import plus\n\n\ndef compute():\n    return plus(1, 2)\n"
API_TOTAL = "from pkg.calc import total\n\n\ndef compute():\n    return total(1, 2)\n"


def _write_tx(repo: Path, name: str, tx: dict) -> Path:
    path = repo / ".vrp" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tx, ensure_ascii=False), encoding="utf-8")
    return path


def _transact(repo, tx_file, expect_status, expect_rc=0, confirm=False, expect_gate=None, expect_code=None):
    args = ["transact", str(repo), str(tx_file)]
    if confirm:
        args.append("--confirm")
    run_vrp(args, cwd=ROOT, expect_rc=expect_rc)
    verdict = read_verdict(repo, Path(tx_file).name)
    assert verdict["status"] == expect_status
    if expect_gate is not None:
        assert verdict["failed_gate"] == expect_gate
    if expect_code is not None:
        failed = next(g for g in verdict["gates"] if g["status"] == "fail")
        assert failed["detail"]["code"] == expect_code
    return verdict


def test_tx001_t1_rename_committed(tx_scratch):
    repo = tx_scratch
    path = _write_tx(
        repo,
        "tx-001.json",
        {
            "id": "tx-001",
            "type": "T1",
            "summary": "rename add to plus",
            "declared_moves": [{"from": f"{repo.name}.pkg.calc.add", "to": f"{repo.name}.pkg.calc.plus"}],
            "edits": [
                {"file": "pkg/calc.py", "op": "modify", "content": CALC_PLUS_SUB},
                {"file": "test_calc.py", "op": "modify", "content": TEST_PLUS},
            ],
        },
    )
    verdict = _transact(repo, path, "committed")
    assert verdict["confidence"] == "high"
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS_SUB
    assert "vrp tx-001" in git(repo, "log", "-1", "--format=%s")


def test_tx002_t2_remove_dead_committed(tx_scratch):
    repo = tx_scratch
    path = _write_tx(
        repo,
        "tx-002.json",
        {
            "id": "tx-002",
            "type": "T2",
            "summary": "remove dead sub",
            "edits": [{"file": "pkg/calc.py", "op": "modify", "content": CALC_PLUS}],
        },
    )
    _transact(repo, path, "committed")
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS


def test_tx003_t1_undeclared_rolled_back(tx_scratch):
    repo = tx_scratch
    before = (repo / "pkg" / "calc.py").read_text(encoding="utf-8")
    path = _write_tx(
        repo,
        "tx-003.json",
        {
            "id": "tx-003",
            "type": "T1",
            "summary": "undeclared extra function",
            "declared_moves": [{"from": f"{repo.name}.pkg.calc.plus", "to": f"{repo.name}.pkg.calc.plus"}],
            "edits": [
                {
                    "file": "pkg/calc.py",
                    "op": "modify",
                    "content": "def plus(a, b):\n    return a + b\n\n\ndef extra():\n    return 9\n",
                }
            ],
        },
    )
    _transact(repo, path, "rolled_back", expect_rc=1, expect_gate="G4", expect_code="gate/G4/undeclared-change")
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == before
    assert not git(repo, "status", "--porcelain").strip()


def test_tx004_t4_needs_confirmation_then_committed(tx_scratch):
    repo = tx_scratch
    path = _write_tx(
        repo,
        "tx-004.json",
        {
            "id": "tx-004",
            "type": "T4",
            "summary": "touch plus behavior",
            "edits": [{"file": "pkg/calc.py", "op": "modify", "content": CALC_PLUS_ZERO}],
        },
    )
    _transact(repo, path, "needs_confirmation", expect_rc=1)
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS
    _transact(repo, path, "committed", confirm=True)
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS_ZERO


def test_tx005_t3_new_module_committed(tx_scratch):
    repo = tx_scratch
    path = _write_tx(
        repo,
        "tx-005.json",
        {
            "id": "tx-005",
            "type": "T3",
            "summary": "add api module using plus",
            "edits": [{"file": "pkg/api.py", "op": "create", "content": API_PLUS}],
        },
    )
    _transact(repo, path, "committed")
    assert (repo / "pkg" / "api.py").read_text(encoding="utf-8") == API_PLUS


def test_tx006_t1_forgot_caller_edge_mismatch(tx_scratch):
    repo = tx_scratch
    path = _write_tx(
        repo,
        "tx-006.json",
        {
            "id": "tx-006",
            "type": "T1",
            "summary": "rename plus to total, forgot api caller",
            "declared_moves": [{"from": f"{repo.name}.pkg.calc.plus", "to": f"{repo.name}.pkg.calc.total"}],
            "edits": [
                {"file": "pkg/calc.py", "op": "modify", "content": CALC_TOTAL},
                {"file": "test_calc.py", "op": "modify", "content": TEST_TOTAL},
            ],
        },
    )
    _transact(repo, path, "rolled_back", expect_rc=1, expect_gate="G4", expect_code="gate/G4/edge-mismatch")
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS_ZERO
    assert not git(repo, "status", "--porcelain").strip()


def test_tx007_t1_full_rename_with_real_verify_changes(tx_scratch):
    repo = tx_scratch
    path = _write_tx(
        repo,
        "tx-007.json",
        {
            "id": "tx-007",
            "type": "T1",
            "summary": "rename plus to total everywhere",
            "declared_moves": [{"from": f"{repo.name}.pkg.calc.plus", "to": f"{repo.name}.pkg.calc.total"}],
            "edits": [
                {"file": "pkg/calc.py", "op": "modify", "content": CALC_TOTAL},
                {"file": "test_calc.py", "op": "modify", "content": TEST_TOTAL},
                {"file": "pkg/api.py", "op": "modify", "content": API_TOTAL},
            ],
        },
    )
    _transact(repo, path, "committed")
    assert (repo / "pkg" / "api.py").read_text(encoding="utf-8") == API_TOTAL


def test_batch_two_transactions_committed(tx_scratch):
    repo = tx_scratch
    txs = repo / ".vrp" / "txs"
    txs.mkdir(parents=True, exist_ok=True)
    (txs / "01-remove-api.json").write_text(
        json.dumps(
            {
                "id": "tx-b01",
                "type": "T2",
                "summary": "remove dead api module",
                "edits": [{"file": "pkg/api.py", "op": "delete"}],
            }
        ),
        encoding="utf-8",
    )
    (txs / "02-total-to-grand.json").write_text(
        json.dumps(
            {
                "id": "tx-b02",
                "type": "T1",
                "summary": "rename total to grand",
                "declared_moves": [{"from": f"{repo.name}.pkg.calc.total", "to": f"{repo.name}.pkg.calc.grand"}],
                "edits": [
                    {"file": "pkg/calc.py", "op": "modify", "content": CALC_GRAND},
                    {"file": "test_calc.py", "op": "modify", "content": TEST_GRAND},
                ],
            }
        ),
        encoding="utf-8",
    )
    out = run_vrp(["transact-batch", str(repo), str(txs)], cwd=ROOT)
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("== "))
    summary = json.loads("\n".join(lines[start + 1:]))
    assert summary["statuses"] == ["committed", "committed"]
    assert summary["stopped_at"] is None
    assert not (repo / "pkg" / "api.py").exists()
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_GRAND
    assert "vrp tx-b02" in git(repo, "log", "-1", "--format=%s")


def test_tx008_t2_delete_dead_file_and_replay(tx_scratch):
    repo = tx_scratch
    (repo / "pkg" / "legacy.py").write_text("def orphan():\n    return 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "add legacy")
    run_vrp(["index", str(repo), "--reindex"], cwd=ROOT)
    run_vrp(["etl", str(repo)], cwd=ROOT)
    path = _write_tx(
        repo,
        "tx-008.json",
        {
            "id": "tx-008",
            "type": "T2",
            "summary": "remove dead legacy module",
            "edits": [{"file": "pkg/legacy.py", "op": "delete"}],
        },
    )
    _transact(repo, path, "committed")
    assert not (repo / "pkg" / "legacy.py").exists()
    _transact(repo, path, "rolled_back", expect_rc=1, expect_gate="G0")
    assert not (repo / "pkg" / "legacy.py").exists()
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_GRAND


def test_transactions_audit_trail(tx_scratch):
    if not (tx_scratch / ".vrp" / "verdicts").is_dir():
        pytest.skip("requires preceding sequential transact scenarios in the same session")
    con = sqlite3.connect(tx_scratch / ".vrp" / "facts.db")
    try:
        rows = con.execute("SELECT tid, status FROM transactions ORDER BY rowid").fetchall()
    finally:
        con.close()
    statuses = [status for _, status in rows]
    assert statuses == [
        "committed",
        "committed",
        "rolled_back",
        "needs_confirmation",
        "committed",
        "committed",
        "rolled_back",
        "committed",
        "committed",
        "committed",
        "committed",
        "rolled_back",
    ]
    assert len(rows) == 12


def test_verdict_files_persisted(tx_scratch):
    verdict_dir = tx_scratch / ".vrp" / "verdicts"
    if not verdict_dir.is_dir():
        pytest.skip("requires preceding sequential transact scenarios in the same session")
    expected = {
        "tx-001.json",
        "tx-002.json",
        "tx-003.json",
        "tx-004.json",
        "tx-005.json",
        "tx-006.json",
        "tx-007.json",
        "tx-008.json",
        "tx-b01.json",
        "tx-b02.json",
    }
    assert expected <= {p.name for p in verdict_dir.iterdir()}
    for name in expected:
        verdict = json.loads((verdict_dir / name).read_text(encoding="utf-8"))
        assert {"tid", "status", "gates", "failed_gate"} <= set(verdict.keys())
