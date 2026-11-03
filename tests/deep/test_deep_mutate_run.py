"""Deep E2E: mutate-run executes real mutmut mutations and reconciles against the plan (P8).

Fixture ground truth (probe-measured on mutmut 2.4.5, Windows):
calc/mathfun.py has 6 line-level mutants - 3 survive (clamp boundary `<`->`<=`,
argument default, raise removal), 3 get killed (safe_div `==`/`/` mutations).
Reconciliation maps mutants back onto the two budgeted plan targets by line
range; survivors become P8 findings (source='mutate-run'); the summary lands
in runtime_evidence under the 'mutate-run' key namespace; .mutmut-cache is
cleaned up and the git tree is left clean (mutmut restores sources).
"""
import json
import sqlite3
import subprocess

import pytest

from tests.deep.conftest import CBM_CACHE, ROOT, git, run_vrp

pytestmark = pytest.mark.deep


@pytest.fixture(scope="session")
def mutate_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("mutate-e2e")
    (repo / "calc").mkdir()
    (repo / "calc" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "calc" / "mathfun.py").write_text(
        "def clamp(value, low, high):\n"
        "    if value < low:\n"
        "        return low\n"
        "    if value > high:\n"
        "        return high\n"
        "    return value\n"
        "\n"
        "\n"
        "def safe_div(a, b):\n"
        "    if b == 0:\n"
        '        raise ValueError("zero")\n'
        "    return a / b\n",
        encoding="utf-8",
    )
    (repo / "test_mathfun.py").write_text(
        "import pytest\n"
        "\n"
        "from calc.mathfun import clamp, safe_div\n"
        "\n"
        "\n"
        "def test_clamp():\n"
        "    assert clamp(5, 0, 10) == 5\n"
        "    assert clamp(-5, 0, 10) == 0\n"
        "    assert clamp(15, 0, 10) == 10\n"
        "\n"
        "\n"
        "def test_safe_div():\n"
        "    assert safe_div(10, 2) == 5\n"
        "    with pytest.raises(ValueError):\n"
        "        safe_div(1, 0)\n",
        encoding="utf-8",
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
    yield repo
    if cache_db.exists():
        cache_db.unlink()


def test_mutate_run_e2e(mutate_repo):
    repo = mutate_repo
    out = json.loads(run_vrp(["mutate-run", str(repo), "--budget", "2"], cwd=ROOT, timeout=900))
    assert out["status"] == "ok"
    assert out["targets"] == 2
    assert out["files"] == ["calc/mathfun.py"]
    assert out["mutants_total"] >= 4
    # mutmut semantics: 'suspicious' == killed (rc 1) but slow; accept both as
    # caught - under full-suite load pytest runs drift past the time threshold
    assert out["killed"] + out["suspicious"] >= 1
    assert out["survived"] >= 1
    assert out["surviving_targets"] == out["findings"]
    assert out["findings"] >= 1
    assert out["survival_rate"] is not None
    assert 0.0 <= out["survival_rate"] <= 1.0
    report = json.loads((repo / ".vrp" / "mutation-report.json").read_text(encoding="utf-8"))
    assert len(report["per_target"]) == 2
    for target in report["per_target"]:
        counted = sum(target[k] for k in ("killed", "survived", "timeout", "suspicious", "skipped", "untested"))
        assert counted == target["mutants"]
    assert not (repo / ".mutmut-cache").exists()
    tree = git(repo, "status", "--porcelain").strip()
    assert tree == ""


def test_mutate_run_findings_and_evidence_written(mutate_repo):
    repo = mutate_repo
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT f.pathology, f.kind, f.source, f.anchor_fid, s.qualified_name, f.payload_json"
            " FROM findings f JOIN symbols s ON s.fid = f.anchor_fid"
            " WHERE f.source = 'mutate-run' ORDER BY s.qualified_name"
        ).fetchall()
        evidence = con.execute(
            "SELECT payload_json FROM runtime_evidence WHERE caller_key = 'mutate-run'"
        ).fetchall()
    finally:
        con.close()
    assert len(rows) >= 1
    for pathology, kind, source, _fid, qn, payload in rows:
        assert pathology == "P8" and kind == "mutation-survivors" and source == "mutate-run"
        detail = json.loads(payload)
        assert detail["survived"] >= 1
        assert detail["qualified_name"] == qn
        assert 0.0 <= detail["survival_rate"] <= 1.0
    assert len(evidence) == 1
    summary = json.loads(evidence[0][0])
    assert summary["engines"] == {"mutmut": "ok"}
    assert summary["survival_rate"] is not None


def test_mutate_run_idempotent(mutate_repo):
    repo = mutate_repo
    first = json.loads(run_vrp(["mutate-run", str(repo), "--budget", "2"], cwd=ROOT, timeout=900))
    second = json.loads(run_vrp(["mutate-run", str(repo), "--budget", "2"], cwd=ROOT, timeout=900))
    assert first["status"] == second["status"] == "ok"
    assert first["mutants_total"] == second["mutants_total"]
    assert first["survived"] == second["survived"]
    assert first["findings"] == second["findings"]
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        findings = con.execute(
            "SELECT COUNT(*) FROM findings WHERE source = 'mutate-run'"
        ).fetchone()[0]
        evidence = con.execute(
            "SELECT COUNT(*) FROM runtime_evidence WHERE caller_key = 'mutate-run'"
        ).fetchone()[0]
    finally:
        con.close()
    assert findings == second["findings"]
    assert evidence == 1
