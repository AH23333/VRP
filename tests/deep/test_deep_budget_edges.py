import json

import pytest

from tests.deep.conftest import ROOT, run_vrp

pytestmark = pytest.mark.deep


def _read_dossier(repo) -> dict:
    return json.loads((repo / ".vrp" / "dossier.json").read_text(encoding="utf-8"))


def test_dossier_budget_zero(tx_scratch):
    run_vrp(["dossier", str(tx_scratch), "--task", "T1", "--skip-external", "--budget", "0"], cwd=ROOT)
    doc = _read_dossier(tx_scratch)
    assert doc["task"] == "T1"
    assert isinstance(doc["views"], dict)


def test_dossier_budget_one(tx_scratch):
    run_vrp(["dossier", str(tx_scratch), "--task", "T1", "--skip-external", "--budget", "1"], cwd=ROOT)
    doc = _read_dossier(tx_scratch)
    assert doc["task"] == "T1"


def test_dossier_budget_negative(tx_scratch):
    run_vrp(["dossier", str(tx_scratch), "--task", "T1", "--skip-external", "--budget", "-100"], cwd=ROOT)
    doc = _read_dossier(tx_scratch)
    assert doc["task"] == "T1"


def test_dossier_invalid_task_rejected(tx_scratch):
    run_vrp(
        ["dossier", str(tx_scratch), "--task", "T99", "--skip-external"],
        cwd=ROOT,
        expect_rc=2,
    )


def test_mutate_plan_budget_zero(tx_scratch):
    out = run_vrp(["mutate-plan", str(tx_scratch), "--budget", "0"], cwd=ROOT, timeout=1800)
    data = json.loads(out)
    assert data["budget"] == 0
    assert data["selected"] == 0
    plan_path = tx_scratch / ".vrp" / "mutation-plan.json"
    assert plan_path.exists()


def test_mutate_plan_budget_negative(tx_scratch):
    out = run_vrp(["mutate-plan", str(tx_scratch), "--budget", "-5"], cwd=ROOT, timeout=1800)
    data = json.loads(out)
    assert data["selected"] == 0
