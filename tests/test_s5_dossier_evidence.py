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


def _write_verdict(repo: Path, tid: str, status: str, verdict_at: str, **extra) -> Path:
    vdir = repo / ".vrp" / "verdicts"
    vdir.mkdir(parents=True, exist_ok=True)
    verdict = {"tid": tid, "type": "T2", "status": status, "verdict_at": verdict_at, "gates": [], **extra}
    path = vdir / f"{tid}.json"
    path.write_text(json.dumps(verdict, ensure_ascii=False), encoding="utf-8")
    return path


def test_last_verdicts_summary_open_vs_committed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_verdict(repo, "tx-ok", "committed", "2026-11-04T00:00:00Z", commit="c1")
    _write_verdict(repo, "tx-fail", "rolled_back", "2026-11-04T01:00:00Z", failed_gate="G3",
                   gates=[{"gate": "G3", "status": "fail", "detail": {"reason": "test", "code": "gate/G3/test"}}])
    _write_verdict(repo, "tx-wait", "needs_confirmation", "2026-11-04T02:00:00Z")
    out = s5_dossier._last_verdicts_summary(repo)
    assert out["open_count"] == 2
    assert [r["tid"] for r in out["recent"]] == ["tx-wait", "tx-fail"]
    failed = out["recent"][1]
    assert failed["failed_gate"] == "G3"
    assert failed["failed_codes"] == ["gate/G3/test"]


def test_last_verdicts_summary_empty_and_cap(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert s5_dossier._last_verdicts_summary(repo) == {"open_count": 0, "recent": []}
    for i in range(s5_dossier.LAST_VERDICTS_CAP + 3):
        _write_verdict(repo, f"tx-{i:02d}", "rolled_back", f"2026-11-04T0{i // 10}:0{i % 10}:00Z")
    out = s5_dossier._last_verdicts_summary(repo)
    assert out["open_count"] == s5_dossier.LAST_VERDICTS_CAP + 3
    assert len(out["recent"]) == s5_dossier.LAST_VERDICTS_CAP
    assert out["recent"][0]["tid"] == f"tx-{s5_dossier.LAST_VERDICTS_CAP + 2:02d}"
