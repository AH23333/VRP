import json
from pathlib import Path

import pytest

from tests.deep.conftest import ROOT, git, read_verdict, run_vrp

pytestmark = pytest.mark.deep

# Sequential fixture states (each test starts from the state the previous one
# left behind; rolls back restore the previous state exactly):
#   state0: calc.py = ADD_SUB          test_calc.py = TEST_ADD
#   t1 sneak   -> rolled_back          (state0 preserved)
#   t2 rename  -> committed            (state1: PLUS_SUB_COMMENT / TEST_PLUS)
#   t3 remove  -> committed            (state2: PLUS_ONLY)
#   t4 sneak   -> rolled_back          (state2 preserved)
ADD_SUB = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
PLUS_SUB_SNEAK = "def plus(a, b):\n    return a + b + 0\n\n\ndef sub(a, b):\n    return a - b\n"
PLUS_SUB_COMMENT = "def plus(a, b):\n    return a + b\n\n\n# renamed from add\ndef sub(a, b):\n    return a - b\n"
PLUS_ONLY = "def plus(a, b):\n    return a + b\n"
PLUS_ONLY_SNEAK = "def plus(a, b):\n    return a + b + 0\n"
TEST_ADD = "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
TEST_PLUS = "from pkg.calc import plus\n\n\ndef test_add():\n    assert plus(1, 2) == 3\n"


@pytest.fixture(scope="session")
def dift_scratch(tmp_path_factory):
    from vrp import s6_difftastic

    if s6_difftastic.find_difftastic() is None:
        pytest.skip("difftastic binary not installed")
    repo = tmp_path_factory.mktemp("deep-dift")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "calc.py").write_text(ADD_SUB, encoding="utf-8")
    (repo / "test_calc.py").write_text(TEST_ADD, encoding="utf-8")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".vrp/\n__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "init")
    cache_db = Path(r"D:\cbm-cache") / f"{repo.name}.db"
    if cache_db.exists():
        cache_db.unlink()
    run_vrp(["index", str(repo), "--reindex"], cwd=ROOT)
    run_vrp(["etl", str(repo)], cwd=ROOT)
    yield repo
    if cache_db.exists():
        cache_db.unlink()


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


def test_dif001_t1_sneaky_body_change_rolled_back(dift_scratch):
    """Flagship: rename passes set/edge/verify/G3 but the + 0 body sneak is
    unaccounted content -> difftastic structural-change."""
    repo = dift_scratch
    path = _write_tx(
        repo,
        "dift-001.json",
        {
            "id": "dift-001",
            "type": "T1",
            "summary": "rename add to plus with sneaky body change",
            "declared_moves": [{"from": f"{repo.name}.pkg.calc.add", "to": f"{repo.name}.pkg.calc.plus"}],
            "edits": [
                {"file": "pkg/calc.py", "op": "modify", "content": PLUS_SUB_SNEAK},
                {"file": "test_calc.py", "op": "modify", "content": TEST_PLUS},
            ],
        },
    )
    verdict = _transact(
        repo, path, "rolled_back", expect_rc=1, expect_gate="G4", expect_code="gate/G4/structural-change"
    )
    g4 = next(g for g in verdict["gates"] if g["gate"] == "G4")
    unaccounted = g4["detail"]["unaccounted"]
    assert unaccounted and unaccounted[0]["file"] == "pkg/calc.py"
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == ADD_SUB
    assert not git(repo, "status", "--porcelain").strip()


def test_dif002_t1_pure_rename_with_comment_churn_committed(dift_scratch):
    repo = dift_scratch
    path = _write_tx(
        repo,
        "dift-002.json",
        {
            "id": "dift-002",
            "type": "T1",
            "summary": "rename add to plus everywhere",
            "declared_moves": [{"from": f"{repo.name}.pkg.calc.add", "to": f"{repo.name}.pkg.calc.plus"}],
            "edits": [
                {"file": "pkg/calc.py", "op": "modify", "content": PLUS_SUB_COMMENT},
                {"file": "test_calc.py", "op": "modify", "content": TEST_PLUS},
            ],
        },
    )
    verdict = _transact(repo, path, "committed")
    g4 = next(g for g in verdict["gates"] if g["gate"] == "G4")
    assert g4["status"] == "pass"
    structural = g4["detail"]["difftastic"]
    assert structural["status"] == "ok"
    assert structural["checked"] >= 2
    assert set(structural["files"].values()) <= {"accounted", "unchanged"}
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == PLUS_SUB_COMMENT


def test_dif003_t2_dead_removal_committed(dift_scratch):
    repo = dift_scratch
    path = _write_tx(
        repo,
        "dift-003.json",
        {
            "id": "dift-003",
            "type": "T2",
            "summary": "remove dead sub",
            "edits": [{"file": "pkg/calc.py", "op": "modify", "content": PLUS_ONLY}],
        },
    )
    _transact(repo, path, "committed")
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == PLUS_ONLY


def test_dif004_t2_sneaky_addition_rolled_back(dift_scratch):
    """T2 claim is removal-only; + 0 in the surviving body is an addition ->
    structural-change (set-level T2 does not check added/modified content)."""
    repo = dift_scratch
    path = _write_tx(
        repo,
        "dift-004.json",
        {
            "id": "dift-004",
            "type": "T2",
            "summary": "remove plus with sneaky body change",
            "edits": [{"file": "pkg/calc.py", "op": "modify", "content": PLUS_ONLY_SNEAK}],
        },
    )
    _transact(repo, path, "rolled_back", expect_rc=1, expect_gate="G4", expect_code="gate/G4/structural-change")
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == PLUS_ONLY
    assert not git(repo, "status", "--porcelain").strip()
