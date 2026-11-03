import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from vrp import s5_dossier

pytestmark = pytest.mark.deep

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_evidence_survives_etl_rebuild(tx_scratch):
    repo = tx_scratch
    facts = repo / ".vrp" / "facts.db"
    con = sqlite3.connect(facts)
    con.execute(
        "INSERT INTO runtime_evidence (caller_key, callee_key, payload_json)"
        " VALUES (NULL, 'test', '{\"exit_code\": 0}')"
    )
    con.commit()
    con.close()
    proc = subprocess.run(
        [sys.executable, "-m", "vrp.cli", "etl", str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        env={**__import__("os").environ, "CBM_CACHE_DIR": r"D:\cbm-cache"},
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    con = sqlite3.connect(f"file:{facts}?mode=ro", uri=True)
    rows = con.execute("SELECT callee_key FROM runtime_evidence").fetchall()
    con.close()
    assert rows == [("test",)]


@pytest.mark.parametrize("repo_fixture", ["big_repo", "lusiem_repo"])
def test_dossier_evidence_section_real_repos(repo_fixture, request):
    repo = request.getfixturevalue(repo_fixture)
    facts = repo / ".vrp" / "facts.db"
    name = repo.name
    dossier = s5_dossier.build_dossier(
        repo, facts, name, task="T2", skip_external=True
    )
    evidence = dossier["views"]["evidence"]
    discrepancies = evidence["discrepancies"]
    assert discrepancies["total"] >= 0
    assert isinstance(discrepancies["by_category"], dict)
    assert sum(discrepancies["by_category"].values()) == discrepancies["total"]
    for sample in discrepancies["samples"]:
        assert "category" in sample and "src_key" in sample
    runtime = evidence["runtime"]
    assert runtime["total"] >= 0
    assert isinstance(runtime["samples"], list)
    persisted = json.loads((repo / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
    assert "evidence" in persisted["views"]
    con = sqlite3.connect(f"file:{facts}?mode=ro", uri=True)
    rows = con.execute("SELECT view FROM dossier_views ORDER BY rowid").fetchall()
    con.close()
    assert [r[0] for r in rows] == list(s5_dossier.SECTION_ORDER)
