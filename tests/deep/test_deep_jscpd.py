"""Deep E2E: jscpd P2 recall backstop (构想v6 §12.5 role swap - CBM main precision, jscpd recall).

Ground truth from the vibe-dup-eval fixture (§12.5 calibration):
- PAIR-1 verbatim duplicate -> both CBM SIMILAR_TO (jaccard=1.0) and jscpd token clone -> ledger agreed
- PAIR-2 identifier-renamed near-dup -> CBM only -> ledger cbm-only
- PAIR-3 heavy rewrite -> neither (below everyone's floor)

Real-repo recall backstop on code-atlas-lusiem: jscpd clones exceed the CBM P2
file-pair set (measured 109 clones vs 54 CBM P2 findings), jscpd-only ledger
rows prove recall beyond the ~0.95 emission floor.
"""
import json
import sqlite3

import pytest

from tests.deep.conftest import CBM_CACHE, ROOT, git, run_vrp
from tests.deep.test_deep_similar_to_eval import _write_eval_repo

pytestmark = pytest.mark.deep


@pytest.fixture(scope="session")
def jscpd_eval(tmp_path_factory):
    repo = tmp_path_factory.mktemp("jscpd-dup-eval")
    _write_eval_repo(repo)
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


def _ledger_rows(repo, category="p2-cross-check-jscpd"):
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT src_key, dst_key, detail_json FROM discrepancy_ledger WHERE category = ?",
            (category,),
        ).fetchall()
    finally:
        con.close()


def test_dup_eval_verbatim_clone_detected_and_agreed(jscpd_eval):
    repo = jscpd_eval
    out = json.loads(run_vrp(["s2", str(repo)], cwd=ROOT))
    p2 = out["p2"]
    assert p2["jscpd"] >= 1
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT file_path, anchor_fid, related_fid FROM findings WHERE source='s2-jscpd'"
        ).fetchall()
    finally:
        con.close()
    assert any(r[0] == "app/config_loader.py" and r[1] and r[2] for r in rows)
    ledger = _ledger_rows(repo)
    agreed = [r for r in ledger if "app/config_loader.py" in r[0] and r[1] is not None]
    assert len(agreed) == 1
    assert agreed[0][1].startswith("cbm-p2:")
    cbm_only = [r for r in ledger if r[1] == "jscpd:none"]
    assert any("report_render.py" in r[0] and "report_format.py" in r[0] for r in cbm_only)


def test_dup_eval_no_false_positive_on_controls(jscpd_eval):
    con = sqlite3.connect(f"file:{jscpd_eval / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT payload_json FROM findings WHERE source='s2-jscpd'"
        ).fetchall()
    finally:
        con.close()
    for (payload,) in rows:
        detail = json.loads(payload)
        for side in ("second_file",):
            assert "date_utils.py" not in detail.get(side, "")
            assert "list_utils.py" not in detail.get(side, "")


def test_lusiem_recall_backstop(lusiem_repo):
    repo = lusiem_repo
    out = json.loads(run_vrp(["s2", str(repo)], cwd=ROOT))
    p2 = out["p2"]
    assert p2["jscpd"] >= 50
    assert p2["ledger_rows"] >= p2["jscpd"]
    ledger = _ledger_rows(repo)
    jscpd_only = [r for r in ledger if r[1] is None]
    assert len(jscpd_only) >= 20
    agreed = [r for r in ledger if r[1] is not None and r[1].startswith("cbm-p2:")]
    assert len(agreed) >= 1
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        cbm_pairs = con.execute(
            "SELECT COUNT(*) FROM findings WHERE pathology='P2' AND source='etl'"
        ).fetchone()[0]
    finally:
        con.close()
    assert cbm_pairs > 0
    rerun = json.loads(run_vrp(["s2", str(repo)], cwd=ROOT))
    assert rerun["p2"] == p2
