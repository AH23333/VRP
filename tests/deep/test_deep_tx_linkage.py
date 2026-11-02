import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s3, s6_transact
from tests.deep.conftest import CBM_CACHE, ROOT, git, read_verdict, run_vrp

pytestmark = pytest.mark.deep

CALC_ORIG = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
TEST_ORIG = "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
CALC_PLUS_SUB = "def plus(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
TEST_PLUS = "from pkg.calc import plus\n\n\ndef test_add():\n    assert plus(1, 2) == 3\n"
CALC_TOTAL_SUB = "def total(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


def _write_tx(repo: Path, name: str, tx: dict) -> Path:
    path = repo / ".vrp" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tx, ensure_ascii=False), encoding="utf-8")
    return path


def _facts_ro(repo: Path):
    return sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)


def _symbol_qns(con):
    return {row[0] for row in con.execute("SELECT qualified_name FROM symbols")}


@pytest.fixture(scope="module")
def link_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("deep-link")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "calc.py").write_text(CALC_ORIG, encoding="utf-8")
    (repo / "test_calc.py").write_text(TEST_ORIG, encoding="utf-8")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".vrp/\n__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "init")
    cache_db = CBM_CACHE / f"{repo.name}.db"
    if cache_db.exists():
        cache_db.unlink()
    run_vrp(["index", str(repo), "--reindex"], cwd=ROOT)
    run_vrp(["etl", str(repo)], cwd=ROOT)
    yield repo
    if cache_db.exists():
        cache_db.unlink()


def test_rolled_back_tx_leaves_symbols_untouched(link_repo):
    repo = link_repo
    con = _facts_ro(repo)
    try:
        qns_before = _symbol_qns(con)
    finally:
        con.close()
    tx = {
        "id": "tx-link-rb",
        "type": "T1",
        "summary": "undeclared rename add to total",
        "edits": [{"file": "pkg/calc.py", "op": "modify", "content": CALC_TOTAL_SUB}],
    }
    path = _write_tx(repo, "tx-link-rb.json", tx)
    run_vrp(["transact", str(repo), str(path)], cwd=ROOT, expect_rc=1)
    verdict = read_verdict(repo, "tx-link-rb.json")
    assert verdict["status"] == "rolled_back"
    con = _facts_ro(repo)
    try:
        assert _symbol_qns(con) == qns_before
    finally:
        con.close()
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_ORIG


@pytest.fixture(scope="module")
def committed_rename(link_repo):
    repo = link_repo
    db = repo / ".vrp" / "facts.db"
    con = sqlite3.connect(db)
    r1 = s3.add_annotation(con, repo, f"{repo.name}.pkg.calc.add", 1, 2, "purpose", "anchor on add")
    r2 = s3.add_annotation(con, repo, f"{repo.name}.pkg.calc.sub", 5, 6, "purpose", "anchor on sub")
    con.close()
    assert r1["ok"] is True
    assert r2["ok"] is True
    tx = {
        "id": "tx-link-rename",
        "type": "T1",
        "summary": "rename add to plus",
        "declared_moves": [{"from": f"{repo.name}.pkg.calc.add", "to": f"{repo.name}.pkg.calc.plus"}],
        "edits": [
            {"file": "pkg/calc.py", "op": "modify", "content": CALC_PLUS_SUB},
            {"file": "test_calc.py", "op": "modify", "content": TEST_PLUS},
        ],
    }
    path = _write_tx(repo, "tx-link-rename.json", tx)
    run_vrp(["transact", str(repo), str(path)], cwd=ROOT)
    verdict = read_verdict(repo, "tx-link-rename.json")
    assert verdict["status"] == "committed"
    return repo


def test_annotations_survive_promote(committed_rename):
    repo = committed_rename
    con = _facts_ro(repo)
    try:
        rows = con.execute("SELECT qualified_name FROM annotations ORDER BY id").fetchall()
    finally:
        con.close()
    assert {row[0] for row in rows} == {f"{repo.name}.pkg.calc.add", f"{repo.name}.pkg.calc.sub"}
    assert len(rows) == 2


def test_renamed_annotation_goes_stale_symbol_gone(committed_rename):
    repo = committed_rename
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        detail = s3.validate_anchors(con, repo)
    finally:
        con.close()
    assert detail["counts"]["active"] == 1
    assert detail["counts"]["stale"] == 1
    assert detail["stale_reasons"]["symbol-gone"] == 1
    con = _facts_ro(repo)
    try:
        statuses = dict(con.execute("SELECT qualified_name, status FROM annotations"))
    finally:
        con.close()
    assert statuses[f"{repo.name}.pkg.calc.sub"] == "active"
    assert statuses[f"{repo.name}.pkg.calc.add"] == "stale"


def test_new_symbol_promoted_and_history_preserved(committed_rename):
    repo = committed_rename
    con = _facts_ro(repo)
    try:
        qns = _symbol_qns(con)
        tx_rows = con.execute("SELECT tid, status FROM transactions").fetchall()
    finally:
        con.close()
    assert f"{repo.name}.pkg.calc.plus" in qns
    assert f"{repo.name}.pkg.calc.add" not in qns
    assert ("tx-link-rename", "committed") in tx_rows
    assert ("tx-link-rb", "rolled_back") in tx_rows


def _batch_tx_dir(repo: Path, dir_name: str, marker_a: str, marker_c: str) -> Path:
    tx_dir = repo / ".vrp" / dir_name
    tx_dir.mkdir(parents=True, exist_ok=True)
    txs = {
        "a.json": {
            "id": f"tx-{dir_name}-a",
            "type": "T2",
            "summary": "cosmetic comment",
            "edits": [{"file": "test_calc.py", "op": "modify", "content": TEST_PLUS + f"# {marker_a}\n"}],
        },
        "b.json": {
            "id": f"tx-{dir_name}-b",
            "type": "T1",
            "summary": "undeclared rename plus to total",
            "edits": [{"file": "pkg/calc.py", "op": "modify", "content": CALC_TOTAL_SUB}],
        },
        "c.json": {
            "id": f"tx-{dir_name}-c",
            "type": "T2",
            "summary": "cosmetic comment",
            "edits": [{"file": "test_calc.py", "op": "modify", "content": TEST_PLUS + f"# {marker_c}\n"}],
        },
    }
    for name, tx in txs.items():
        (tx_dir / name).write_text(json.dumps(tx, ensure_ascii=False), encoding="utf-8")
    return tx_dir


def test_transact_batch_stop_at_first_failure(committed_rename):
    repo = committed_rename
    tx_dir = _batch_tx_dir(repo, "batch7a", "marker-a", "marker-c")
    result = s6_transact.transact_batch(repo, tx_dir, repo.name)
    assert result["total"] == 3
    assert result["count"] == 2
    assert result["committed"] == 1
    assert result["statuses"] == ["committed", "rolled_back"]
    assert result["stopped_at"] == "b.json"
    assert (repo / "test_calc.py").read_text(encoding="utf-8") == TEST_PLUS + "# marker-a\n"
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS_SUB


def test_transact_batch_continue_on_fail(committed_rename):
    repo = committed_rename
    tx_dir = _batch_tx_dir(repo, "batch7b", "marker-a2", "marker-c2")
    result = s6_transact.transact_batch(repo, tx_dir, repo.name, continue_on_fail=True)
    assert result["total"] == 3
    assert result["count"] == 3
    assert result["committed"] == 2
    assert result["statuses"] == ["committed", "rolled_back", "committed"]
    assert result["stopped_at"] is None
    assert (repo / "test_calc.py").read_text(encoding="utf-8") == TEST_PLUS + "# marker-c2\n"
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS_SUB


def test_transact_batch_needs_confirmation_stops(committed_rename):
    repo = committed_rename
    tx_dir = repo / ".vrp" / "batch7c"
    tx_dir.mkdir(parents=True, exist_ok=True)
    (tx_dir / "a.json").write_text(
        json.dumps(
            {
                "id": "tx-batch7c-a",
                "type": "T4",
                "summary": "delete calc module",
                "edits": [{"file": "pkg/calc.py", "op": "delete"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = s6_transact.transact_batch(repo, tx_dir, repo.name)
    assert result["statuses"] == ["needs_confirmation"]
    assert result["stopped_at"] == "a.json"
    assert (repo / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS_SUB


def test_transact_batch_empty_dir(committed_rename):
    repo = committed_rename
    tx_dir = repo / ".vrp" / "batch7-empty"
    tx_dir.mkdir(parents=True, exist_ok=True)
    result = s6_transact.transact_batch(repo, tx_dir, repo.name)
    assert result["total"] == 0
    assert result["count"] == 0
    assert result["committed"] == 0
    assert result["statuses"] == []
    assert result["stopped_at"] is None
    assert result["verdicts"] == []
