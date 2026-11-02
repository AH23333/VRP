import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s2_deadcode, s5_dossier
from tests.deep.conftest import CBM_CACHE, ROOT, git, run_vrp

pytestmark = pytest.mark.deep

SCHEMA = ROOT / "vrp" / "schema" / "facts_schema.sql"

CODE_A = (
    "def used_fn(a):\n"
    "    return a + 1\n\n\n"
    "def dead_fn(a):\n"
    "    return a - 1\n\n\n"
    "def entry():\n"
    "    return used_fn(1)\n"
)
TEST_A = "from pkg.core import entry\n\n\ndef test_entry():\n    assert entry() == 2\n"


@pytest.fixture(scope="module")
def inv_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("deep-inv")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text(CODE_A, encoding="utf-8")
    (repo / "test_core.py").write_text(TEST_A, encoding="utf-8")
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


def _table_fingerprint(db: Path, table: str) -> set:
    con = sqlite3.connect(db)
    try:
        cols = [d[1] for d in con.execute(f"PRAGMA table_info({table})")]
        rows = {tuple(repr(v) for v in row) for row in con.execute(f"SELECT * FROM {table}")}
        _ = cols
        return rows
    finally:
        con.close()


def test_etl_idempotent(inv_repo):
    db = inv_repo / ".vrp" / "facts.db"
    before_symbols = _table_fingerprint(db, "symbols")
    before_edges = _table_fingerprint(db, "edges")
    run_vrp(["etl", str(inv_repo)], cwd=ROOT)
    assert _table_fingerprint(db, "symbols") == before_symbols
    assert _table_fingerprint(db, "edges") == before_edges


def test_s2_idempotent_and_view_alignment(inv_repo):
    out1 = run_vrp(["s2", str(inv_repo)], cwd=ROOT)
    first = json.loads(out1)
    assert first["dead_code_candidates"] == 1
    assert first["p4"]["vulture"] == 1
    out2 = run_vrp(["s2", str(inv_repo)], cwd=ROOT)
    second = json.loads(out2)
    assert first == second
    con = sqlite3.connect(inv_repo / ".vrp" / "facts.db")
    try:
        view_rows = {row[0] for row in con.execute("SELECT qualified_name FROM v_dead_code")}
        func_rows = {row[1] for row in s2_deadcode.dead_code_candidates(con)}
    finally:
        con.close()
    assert view_rows == func_rows
    assert any("dead_fn" in qn for qn in func_rows)
    assert not any("used_fn" in qn or "entry" in qn for qn in func_rows)


def test_xcheck_reproducible(inv_repo):
    out1 = json.loads(run_vrp(["xcheck", str(inv_repo)], cwd=ROOT))
    out2 = json.loads(run_vrp(["xcheck", str(inv_repo)], cwd=ROOT))
    assert out1["ca_calls"] == out2["ca_calls"]
    assert out1["cbm_calls"] == out2["cbm_calls"]
    assert out1["agreement"] == out2["agreement"]
    assert out1["agreement"] == out1["ca_calls"]


def test_validate_idempotent(inv_repo):
    out1 = run_vrp(["validate", str(inv_repo)], cwd=ROOT)
    out2 = run_vrp(["validate", str(inv_repo)], cwd=ROOT)
    assert out1 == out2


def test_dossier_schema_stable(inv_repo):
    out = run_vrp(["dossier", str(inv_repo), "--task", "T2", "--skip-external"], cwd=ROOT)
    payload = json.loads(out.split("== S5 dossier ==")[1].strip())
    assert payload["task"] == "T2"
    doc = json.loads((inv_repo / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
    assert set(doc["views"].keys()) == {
        "architecture",
        "repo_map_brief",
        "repomap",
        "sql_views",
        "summary",
        "raw_pack",
    }
    shares = s5_dossier.TASK_PROFILES["T2"]
    assert abs(sum(shares.values()) - 1.0) < 1e-9
    assert set(shares.keys()) == {
        "architecture",
        "repo_map_brief",
        "repomap",
        "sql_views",
        "summary",
        "raw_pack",
    }


@pytest.mark.parametrize("task", ["T1", "T2", "T3", "T4"])
def test_task_profiles_share_sums(task):
    shares = s5_dossier.TASK_PROFILES[task]
    assert abs(sum(shares.values()) - 1.0) < 1e-9


def test_dossier_skip_external_matches_with_external_fields(inv_repo):
    run_vrp(["dossier", str(inv_repo), "--task", "T3", "--skip-external"], cwd=ROOT)
    doc = json.loads((inv_repo / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
    views = doc["views"]
    assert views["architecture"].get("status") in ("skipped", "ok", None)
    assert isinstance(views["summary"], dict)
    assert "counts" in json.dumps(views["summary"]) or views["summary"]
