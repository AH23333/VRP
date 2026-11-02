import json
import sqlite3

import pytest

from vrp import s2_deadcode
from tests.deep.conftest import ROOT, run_vrp

pytestmark = pytest.mark.deep


def test_big_repo_facts_baseline(big_repo):
    con = sqlite3.connect(f"file:{big_repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        symbols = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    finally:
        con.close()
    assert symbols == 66176
    assert edges == 251771


def test_big_repo_dead_code_view_aligns_with_function(big_repo):
    run_vrp(["validate", str(big_repo)], cwd=ROOT, timeout=1800)
    con = sqlite3.connect(f"file:{big_repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        view_rows = {row[0] for row in con.execute("SELECT qualified_name FROM v_dead_code")}
        func_rows = {row[1] for row in s2_deadcode.dead_code_candidates(con)}
    finally:
        con.close()
    assert view_rows == func_rows
    assert not any(qn.startswith("builtins.") for qn in view_rows)
    assert len(view_rows) >= 1000


def test_big_repo_s2_idempotent(big_repo):
    out1 = json.loads(run_vrp(["s2", str(big_repo)], cwd=ROOT, timeout=1800))
    assert out1["dead_code_candidates"] >= 1000
    assert out1["p4"]["vulture"] >= 700
    out2 = json.loads(run_vrp(["s2", str(big_repo)], cwd=ROOT, timeout=1800))
    assert out1 == out2


def test_big_repo_validate_baseline(big_repo):
    out = json.loads(run_vrp(["validate", str(big_repo)], cwd=ROOT, timeout=1800))
    coverage = out.get("coverage", out)
    total = coverage.get("total") if isinstance(coverage, dict) else coverage
    assert total >= 45000


def test_big_repo_dossier_t2_skip_external(big_repo):
    run_vrp(["dossier", str(big_repo), "--task", "T2", "--skip-external"], cwd=ROOT, timeout=1800)
    doc = json.loads((big_repo / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
    assert set(doc["views"].keys()) == {
        "architecture",
        "repo_map_brief",
        "repomap",
        "sql_views",
        "summary",
        "raw_pack",
    }
    sql_views = doc["views"]["sql_views"]
    assert len(json.dumps(sql_views, ensure_ascii=False)) > 5000


def test_lusiem_repo_smoke(lusiem_repo):
    facts = lusiem_repo / ".vrp" / "facts.db"
    if not facts.exists():
        pytest.skip("lusiem repo not indexed yet")
    con = sqlite3.connect(f"file:{facts}?mode=ro", uri=True)
    try:
        symbols = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    finally:
        con.close()
    assert symbols > 0
    out = json.loads(run_vrp(["s2", str(lusiem_repo)], cwd=ROOT, timeout=1800))
    assert "dead_code_candidates" in out
    run_vrp(["dossier", str(lusiem_repo), "--task", "T1", "--skip-external"], cwd=ROOT, timeout=1800)
    doc = json.loads((lusiem_repo / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
    assert doc["task"] == "T1"
