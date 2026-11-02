import json
import sqlite3
from pathlib import Path

import pytest

from tests.deep.conftest import ROOT, run_vrp
from vrp import testsig

pytestmark = pytest.mark.deep


def _run_plan(repo, budget):
    out = json.loads(run_vrp(["mutate-plan", str(repo), "--budget", str(budget)], cwd=ROOT))
    plan_path = Path(out["plan"])
    targets = json.loads(plan_path.read_text(encoding="utf-8"))
    return out, targets


def test_mutate_plan_big_repo_targets(big_repo):
    summary, targets = _run_plan(big_repo, 50)
    assert summary["budget"] == 50
    assert summary["selected"] == len(targets)
    assert 1 <= len(targets) <= 50
    con = sqlite3.connect(f"file:{big_repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        fids = {row[0] for row in con.execute("SELECT fid FROM symbols")}
    finally:
        con.close()
    for target in targets:
        assert set(target) == {
            "fid",
            "qualified_name",
            "file_path",
            "start_line",
            "end_line",
            "tests",
            "callers",
        }
        assert target["fid"] in fids
        assert target["tests"] >= 1
        assert target["callers"] >= 0
        assert (big_repo / target["file_path"]).is_file()
        assert not testsig.is_test_path(target["file_path"])
        assert not testsig.is_synthetic_path(target["file_path"])
        name = target["qualified_name"].rsplit(".", 1)[-1]
        assert not name.startswith("test_")
        assert not (name.startswith("__") and name.endswith("__"))
        assert 1 <= target["start_line"] <= target["end_line"]
    callers = [t["callers"] for t in targets]
    assert callers == sorted(callers, reverse=True)


def test_mutate_plan_budget_caps(big_repo):
    _s5, five = _run_plan(big_repo, 5)
    assert 1 <= len(five) <= 5
    _s1, one = _run_plan(big_repo, 1)
    assert len(one) == 1
    assert one[0]["qualified_name"] == five[0]["qualified_name"]
    _s50, fifty = _run_plan(big_repo, 50)
    assert one[0]["qualified_name"] == fifty[0]["qualified_name"]


def test_mutate_plan_budget_zero(big_repo):
    summary, targets = _run_plan(big_repo, 0)
    assert summary["budget"] == 0
    assert summary["selected"] == 0
    assert targets == []


def test_mutate_plan_file_location(big_repo):
    summary, _targets = _run_plan(big_repo, 3)
    assert Path(summary["plan"]) == big_repo / ".vrp" / "mutation-plan.json"
    assert Path(summary["plan"]).is_file()
