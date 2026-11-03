import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s5_dossier

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


@pytest.fixture
def con():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    yield con
    con.close()


def _ledger(con, category, src, dst, ca_conf=None, provenance=None):
    con.execute(
        "INSERT INTO discrepancy_ledger (category, src_key, dst_key, cbm_edge_type, ca_confidence, ca_provenance, detail_json)"
        " VALUES (?, ?, ?, NULL, ?, ?, '{}')",
        (category, src, dst, ca_conf, provenance),
    )


def _runtime(con, callee, payload='{"exit_code": 0}'):
    con.execute(
        "INSERT INTO runtime_evidence (caller_key, callee_key, payload_json) VALUES (NULL, ?, ?)",
        (callee, payload),
    )


def test_evidence_in_section_order():
    assert "evidence" in s5_dossier.SECTION_ORDER
    assert s5_dossier.SECTION_ORDER.index("evidence") < s5_dossier.SECTION_ORDER.index("summary")


def test_task_profiles_cover_evidence_and_balance():
    for task, profile in s5_dossier.TASK_PROFILES.items():
        assert "evidence" in profile, task
        assert abs(sum(profile.values()) - 1.0) < 1e-9, task
        assert set(profile) == set(s5_dossier.SECTION_ORDER), task


def test_discrepancy_summary_empty(con):
    out = s5_dossier._discrepancy_summary(con)
    assert out["total"] == 0
    assert out["by_category"] == {}
    assert out["samples"] == []


def test_discrepancy_summary_stratified(con):
    for i in range(12):
        _ledger(con, "ca_only", f"s{i}", f"d{i}", 0.6, "index")
    _ledger(con, "cbm_only", "x", "y", None, None)
    _ledger(con, "cbm_only", "x2", "y2", None, None)
    con.commit()
    out = s5_dossier._discrepancy_summary(con)
    assert out["total"] == 14
    assert out["by_category"] == {"ca_only": 12, "cbm_only": 2}
    cats_in_samples = {s["category"] for s in out["samples"]}
    assert cats_in_samples == {"ca_only", "cbm_only"}
    ca_samples = [s for s in out["samples"] if s["category"] == "ca_only"]
    assert len(ca_samples) <= 10
    assert all(s["ca_confidence"] == 0.6 for s in ca_samples)
    assert all("src_key" in s and "dst_key" in s for s in out["samples"])


def test_runtime_evidence_summary_empty(con):
    out = s5_dossier._runtime_evidence_summary(con)
    assert out["total"] == 0
    assert out["samples"] == []


def test_runtime_evidence_summary_rows(con):
    _runtime(con, "test", '{"exit_code": 0, "command": "pytest"}')
    _runtime(con, "lint", '{"exit_code": 1}')
    con.commit()
    out = s5_dossier._runtime_evidence_summary(con)
    assert out["total"] == 2
    keys = {s["callee_key"] for s in out["samples"]}
    assert keys == {"test", "lint"}
    payload = json.loads(next(s for s in out["samples"] if s["callee_key"] == "test")["payload_json"])
    assert payload["exit_code"] == 0
