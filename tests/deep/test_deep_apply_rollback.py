import json
import sqlite3
from pathlib import Path

import pytest

from vrp import etl as etl_mod, s1_cbm
from vrp.s6_transact import apply_edits, rollback_edits
from tests.deep.conftest import ROOT, git, read_verdict, run_vrp

pytestmark = pytest.mark.deep


@pytest.fixture()
def own_tx_repo(tmp_path):
    repo = tmp_path / "own-tx-repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (repo / "test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(".vrp/\n__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "init")
    s1_cbm.index_repository(repo, repo.name, reindex=True)
    etl_mod.run_etl(repo, repo.name)
    return repo


def _repo_with_git(tmp_path) -> Path:
    repo = tmp_path / "apply-repo"
    repo.mkdir()
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "init")
    return repo


def test_apply_edits_sequence(tmp_path):
    repo = _repo_with_git(tmp_path)
    (repo / "gone.py").write_text("old = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "two")
    apply_edits(
        repo,
        [
            {"file": Path("new.py"), "op": "create", "content": "new = 2\n"},
            {"file": Path("tracked.py"), "op": "modify", "content": "x = 42\n"},
            {"file": Path("gone.py"), "op": "delete"},
        ],
    )
    assert (repo / "new.py").read_text(encoding="utf-8") == "new = 2\n"
    assert (repo / "tracked.py").read_text(encoding="utf-8") == "x = 42\n"
    assert not (repo / "gone.py").exists()


def test_apply_edits_partial_state_on_mid_failure(tmp_path):
    repo = _repo_with_git(tmp_path)
    with pytest.raises(RuntimeError, match="modify target missing"):
        apply_edits(
            repo,
            [
                {"file": Path("created.py"), "op": "create", "content": "a = 1\n"},
                {"file": Path("missing.py"), "op": "modify", "content": "b = 2\n"},
            ],
        )
    assert (repo / "created.py").exists(), "apply_edits is non-atomic: earlier edits persist until transact-level rollback"


def test_apply_edits_preconditions(tmp_path):
    repo = _repo_with_git(tmp_path)
    (repo / "exists.py").write_text("a\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="create target already exists"):
        apply_edits(repo, [{"file": Path("exists.py"), "op": "create", "content": "x"}])
    with pytest.raises(RuntimeError, match="modify target missing"):
        apply_edits(repo, [{"file": Path("nope.py"), "op": "modify", "content": "x"}])
    with pytest.raises(RuntimeError, match="delete target missing"):
        apply_edits(repo, [{"file": Path("nope.py"), "op": "delete"}])


def test_rollback_edits_restores_git_tracked_state(tmp_path):
    repo = _repo_with_git(tmp_path)
    (repo / "todelete.py").write_text("bye = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "two")
    edits = [
        {"file": Path("extra.py"), "op": "create", "content": "e = 1\n"},
        {"file": Path("tracked.py"), "op": "modify", "content": "x = 99\n"},
        {"file": Path("todelete.py"), "op": "delete"},
    ]
    apply_edits(repo, edits)
    restored = rollback_edits(repo, edits)
    assert not (repo / "extra.py").exists()
    assert (repo / "tracked.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (repo / "todelete.py").read_text(encoding="utf-8") == "bye = 1\n"
    assert len(restored) == 3
    assert not any("RESTORE FAILED" in r for r in restored)
    assert git(repo, "status", "--porcelain").strip() == ""


def test_rollback_edits_untracked_modify_reports_restore_failed(tmp_path):
    repo = _repo_with_git(tmp_path)
    (repo / "untracked.py").write_text("original = 1\n", encoding="utf-8")
    edits = [{"file": Path("untracked.py"), "op": "modify", "content": "clobbered = 2\n"}]
    apply_edits(repo, edits)
    restored = rollback_edits(repo, edits)
    assert len(restored) == 1
    assert "RESTORE FAILED" in restored[0], "untracked modify must report honest restore failure (no git baseline)"
    assert (repo / "untracked.py").read_text(encoding="utf-8") == "clobbered = 2\n"


def test_transact_apply_failure_rolls_back_clean(own_tx_repo):
    repo = own_tx_repo
    tx = {
        "id": "tx-apply-fail",
        "type": "T1",
        "summary": "second edit targets missing file",
        "declared_moves": [],
        "edits": [
            {"file": "pkg/created.py", "op": "create", "content": "z = 1\n"},
            {"file": "pkg/missing.py", "op": "modify", "content": "y = 2\n"},
        ],
    }
    tx_path = repo / ".vrp" / "tx-apply-fail.json"
    tx_path.parent.mkdir(parents=True, exist_ok=True)
    tx_path.write_text(json.dumps(tx), encoding="utf-8")
    run_vrp(["transact", str(repo), str(tx_path)], cwd=ROOT, expect_rc=1)
    verdict = read_verdict(repo, "tx-apply-fail.json")
    assert verdict["status"] == "rolled_back"
    assert verdict["failed_gate"] == "G0"
    failed = next(g for g in verdict["gates"] if g["status"] == "fail")
    assert failed["detail"]["reason"] == "apply-edits"
    assert not (repo / "pkg" / "created.py").exists(), "mid-apply created file must be rolled back"
    assert (repo / "pkg" / "calc.py").exists()
    assert git(repo, "status", "--porcelain").strip() == ""
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        status = con.execute(
            "SELECT status FROM transactions WHERE tid = 'tx-apply-fail'"
        ).fetchone()
    finally:
        con.close()
    assert status is not None and status[0] == "rolled_back"
