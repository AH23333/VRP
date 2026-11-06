"""Deep E2E: mutate-run dispatches TS targets to real Stryker (sandboxed 10.1.1).

Fixture ground truth (probe-measured on Stryker 10.1.1 + vitest 4.1.x,
Windows): src/mathfun.ts yields ~15 mutants - roughly 12 killed, 3 survive
(clamp boundary `>`->`>=`, string literal, comparison relaxation). The
mutate-run entry point dispatches .ts targets to the Stryker engine, parses
the JSON report (schemaVersion 1.0), reconciles mutants onto the two budgeted
plan targets, writes P8 findings with engine='stryker' and runtime_evidence
under the 'mutate-run' namespace, then removes .vrp/stryker.conf.json and
.vrp/stryker-report.json leaving the git tree clean.

The fixture repo lives under work/ (D drive) because vitest+typescript
node_modules are heavy; teardown removes the whole fixture and its cache db.
"""
import json
import shutil
import sqlite3
import subprocess

import pytest

from tests.deep.conftest import CBM_CACHE, ROOT, git, rmtree_retry, run_vrp

pytestmark = pytest.mark.deep

SHIM = ROOT / "work" / "stryker" / "node_modules" / ".bin" / "stryker.cmd"


@pytest.fixture(scope="session")
def stryker_repo():
    if not SHIM.is_file():
        pytest.skip("work/stryker sandbox not installed")
    repo = ROOT / "work" / "_deep_stryker"
    if repo.exists():
        rmtree_retry(repo)
    (repo / "src").mkdir(parents=True)
    (repo / "test").mkdir()
    (repo / "src" / "mathfun.ts").write_text(
        "export function clamp(value: number, low: number, high: number): number {\n"
        "  if (value < low) {\n"
        "    return low;\n"
        "  }\n"
        "  if (value > high) {\n"
        "    return high;\n"
        "  }\n"
        "  return value;\n"
        "}\n"
        "\n"
        "export function safeDiv(a: number, b: number): number {\n"
        "  if (b === 0) {\n"
        "    return 0;\n"
        "  }\n"
        "  return a / b;\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "test" / "mathfun.test.ts").write_text(
        "import { describe, expect, it } from 'vitest';\n"
        "import { clamp, safeDiv } from '../src/mathfun';\n"
        "\n"
        "describe('mathfun', () => {\n"
        "  it('clamps', () => {\n"
        "    expect(clamp(5, 0, 10)).toBe(5);\n"
        "    expect(clamp(-5, 0, 10)).toBe(0);\n"
        "    expect(clamp(15, 0, 10)).toBe(10);\n"
        "  });\n"
        "  it('divides safely', () => {\n"
        "    expect(safeDiv(10, 2)).toBe(5);\n"
        "    expect(safeDiv(1, 0)).toBe(0);\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )
    (repo / "package.json").write_text(
        json.dumps({
            "name": "mathfun-fixture",
            "type": "module",
            "scripts": {"test": "vitest run"},
            "devDependencies": {"typescript": "^5.9.3", "vitest": "^4.1.9"},
        }),
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("node_modules/\n.vrp/\npackage-lock.json\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "init")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    assert npm, "npm not on PATH"
    proc = subprocess.run(
        [npm, "install", "--no-audit", "--no-fund"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    assert proc.returncode == 0, f"npm install failed: {proc.stdout[-2000:]} {proc.stderr[-2000:]}"
    cache_db = CBM_CACHE / f"{repo.name}.db"
    if cache_db.exists():
        cache_db.unlink()
    run_vrp(["index", str(repo), "--reindex"], cwd=ROOT)
    run_vrp(["etl", str(repo)], cwd=ROOT)
    yield repo
    if cache_db.exists():
        cache_db.unlink()
    rmtree_retry(repo)


def test_stryker_e2e(stryker_repo):
    repo = stryker_repo
    out = json.loads(run_vrp(["mutate-run", str(repo), "--budget", "2"], cwd=ROOT, timeout=900))
    assert out["status"] == "ok"
    assert out["engines"] == {"stryker": "ok"}
    assert out["targets"] == 2
    assert out["files"] == ["src/mathfun.ts"]
    assert out["mutants_total"] >= 5
    assert out["killed"] + out["suspicious"] >= 1
    assert out["survived"] >= 1
    assert out["surviving_targets"] == out["findings"]
    assert out["findings"] >= 1
    assert out["survival_rate"] is not None
    assert 0.0 <= out["survival_rate"] <= 1.0
    assert out["stryker_raw"].get("Killed", 0) >= 1
    assert not (repo / ".vrp" / "stryker.conf.json").exists()
    assert not (repo / ".vrp" / "stryker-report.json").exists()
    assert not (repo / ".vrp" / "stryker-tmp").exists()
    report = json.loads((repo / ".vrp" / "mutation-report.json").read_text(encoding="utf-8"))
    assert len(report["per_target"]) == 2
    for target in report["per_target"]:
        assert target["engine"] == "stryker"
        counted = sum(target[k] for k in ("killed", "survived", "timeout", "suspicious", "skipped", "untested"))
        assert counted == target["mutants"]
    tree = git(repo, "status", "--porcelain").strip()
    assert tree == ""


def test_stryker_findings_and_evidence_written(stryker_repo):
    repo = stryker_repo
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
        assert detail["engine"] == "stryker"
        assert detail["survived"] >= 1
        assert detail["qualified_name"] == qn
        assert 0.0 <= detail["survival_rate"] <= 1.0
    assert len(evidence) == 1
    summary = json.loads(evidence[0][0])
    assert summary["engines"] == {"stryker": "ok"}
    assert summary["survival_rate"] is not None


def test_stryker_idempotent(stryker_repo):
    repo = stryker_repo
    first = json.loads(run_vrp(["mutate-run", str(repo), "--budget", "2"], cwd=ROOT, timeout=900))
    second = json.loads(run_vrp(["mutate-run", str(repo), "--budget", "2"], cwd=ROOT, timeout=900))
    assert first["status"] == second["status"] == "ok"
    assert first["mutants_total"] == second["mutants_total"]
    assert first["killed"] == second["killed"]
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
