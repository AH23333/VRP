import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s4_freeze
from tests.deep.conftest import CBM_CACHE, ROOT, git, run_vrp

pytestmark = pytest.mark.deep

RUN_SLOTS = ("build", "test", "lint", "typecheck")

RUN_KEYS = {"command", "rc", "duration_s", "timed_out", "stdout_tail", "stderr_tail"}


def _con_ro(db: Path):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _count(db: Path, table: str) -> int:
    con = _con_ro(db)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _history_tuples(repo: Path):
    db = repo / ".vrp" / "facts.db"
    con = _con_ro(db)
    try:
        return con.execute(
            "SELECT src_fid, dst_fid, properties_json FROM history_coupling ORDER BY src_fid, dst_fid, properties_json"
        ).fetchall()
    finally:
        con.close()


def test_history_coupling_fk_clean_and_payload(lusiem_repo):
    db = lusiem_repo / ".vrp" / "facts.db"
    con = _con_ro(db)
    try:
        tuples = con.execute(
            "SELECT src_fid, dst_fid, properties_json FROM history_coupling"
        ).fetchall()
        fids = {row[0] for row in con.execute("SELECT fid FROM symbols")}
    finally:
        con.close()
    assert len(tuples) > 0
    for src, dst, _props in tuples:
        assert src in fids
        assert dst in fids
    for _src, _dst, props in tuples:
        payload = json.loads(props)
        assert {"co_changes", "coupling_score", "last_co_change"} <= set(payload)
        assert 0.0 <= payload["coupling_score"] <= 1.0
        assert payload["co_changes"] >= 1


def test_history_coupling_survives_etl_rerun(lusiem_repo):
    before = _history_tuples(lusiem_repo)
    assert len(before) > 0
    run_vrp(["etl", str(lusiem_repo)], cwd=ROOT)
    after = _history_tuples(lusiem_repo)
    assert after == before


@pytest.fixture(scope="module")
def freeze_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("deep-frz")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "test_core.py").write_text(
        "from pkg.core import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
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
    facts_db = repo / ".vrp" / "facts.db"
    summary = s4_freeze.freeze(repo, facts_db, repo.name)
    yield {"repo": repo, "db": facts_db, "summary": summary}
    if cache_db.exists():
        cache_db.unlink()


def test_runtime_evidence_shape(freeze_repo):
    db = freeze_repo["db"]
    con = _con_ro(db)
    try:
        rows = con.execute("SELECT caller_key, callee_key, payload_json FROM runtime_evidence").fetchall()
    finally:
        con.close()
    assert len(rows) >= 1
    for caller, slot, payload_json in rows:
        assert caller is None
        assert slot in RUN_SLOTS
        payload = json.loads(payload_json)
        assert set(payload) == RUN_KEYS
        assert payload["command"] == "pytest"
        assert payload["rc"] == 0
        assert payload["timed_out"] is False
        assert len(payload["stdout_tail"]) <= 2000
        assert len(payload["stderr_tail"]) <= 2000
        assert payload["duration_s"] >= 0


def test_freeze_summary_mirrors_runtime_evidence(freeze_repo):
    summary = freeze_repo["summary"]
    db = freeze_repo["db"]
    con = _con_ro(db)
    try:
        rows = con.execute(
            "SELECT callee_key, payload_json FROM runtime_evidence"
        ).fetchall()
    finally:
        con.close()
    slot_rows = [r for r in rows if r[0] == "test"]
    assert len(slot_rows) == len(summary["commands"]["test"])
    assert summary["commands"]["test"][0].startswith("pytest -> rc=0 (")
    assert summary["frozen_at"]
    assert summary["graph_baseline"]["status"] == "written"
    assert {json.loads(r[1])["command"] for r in slot_rows} == {"pytest"}


def test_freeze_writes_baseline_row(freeze_repo):
    db = freeze_repo["db"]
    con = _con_ro(db)
    try:
        rows = con.execute("SELECT kind, payload_json FROM baseline ORDER BY id").fetchall()
    finally:
        con.close()
    assert len(rows) >= 1
    payload = json.loads(rows[-1][1])
    assert {"frozen_at", "git", "commands", "mutation_plan", "graph_baseline", "test_impact"} <= set(payload)
    assert payload["commands"]["test"][0]["command"] == "pytest"


def test_freeze_runtime_evidence_append_semantics(freeze_repo):
    repo = freeze_repo["repo"]
    db = freeze_repo["db"]
    before = _count(db, "runtime_evidence")
    s4_freeze.freeze(repo, db, repo.name, skip_graph=True)
    after = _count(db, "runtime_evidence")
    assert after == before + 1
    con = _con_ro(db)
    try:
        latest = con.execute(
            "SELECT payload_json FROM runtime_evidence ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    assert set(json.loads(latest[0])) == RUN_KEYS


def test_freeze_skip_commands_writes_no_runtime_rows(freeze_repo):
    repo = freeze_repo["repo"]
    db = freeze_repo["db"]
    before = _count(db, "runtime_evidence")
    summary = s4_freeze.freeze(repo, db, repo.name, skip_commands=True, skip_graph=True)
    assert _count(db, "runtime_evidence") == before
    assert summary["commands"] == {}
