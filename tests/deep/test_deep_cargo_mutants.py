"""Deep E2E: mutate-run dispatches Rust targets to real cargo-mutants 27.1.0.

Fixture ground truth (probe-measured on cargo-mutants 27.1.0, Windows,
work/rust-calc-probe): src/lib.rs (add/greet/mul) yields 12 mutants - 8
CaughtMutant, 4 MissedMutant (two greet mutants from the untested function
count as side mutants, two mul survivors come from the deliberately weak
(1,1) assertions). The mutate-run entry point dispatches .rs targets to the
cargo-mutants engine, parses mutants.out/outcomes.json (pretty JSON - the
mutant SITE line is the top-level span.start.line, not function.span),
reconciles mutants onto the two budgeted plan targets, writes P8 findings
with engine='cargo-mutants' and runtime_evidence under the 'mutate-run'
namespace, then removes .vrp/cargo-mutants-out. cargo-mutants builds in its
own scratch copy by default (no --in-place), so the fixture never grows a
target/ dir and the git tree stays clean.
"""
import json
import sqlite3

import pytest

from tests.deep.conftest import CBM_CACHE, ROOT, git, rmtree_retry, run_vrp
from vrp.s2_cargo_mutants import find_cargo_mutants

pytestmark = pytest.mark.deep


@pytest.fixture(scope="session")
def cargo_repo():
    if not find_cargo_mutants():
        pytest.skip("cargo-mutants not installed")
    repo = ROOT / "work" / "_deep_cargo_mutants"
    if repo.exists():
        rmtree_retry(repo)
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "lib.rs").write_text(
        "pub fn add(a: i32, b: i32) -> i32 {\n"
        "    a + b\n"
        "}\n"
        "\n"
        "pub fn greet(name: &str) -> String {\n"
        "    format!(\"hello {}\", name)\n"
        "}\n"
        "\n"
        "/// Deliberately weak assertions in tests/calc.rs: `*` -> `/` survives on (1,1).\n"
        "pub fn mul(a: i32, b: i32) -> i32 {\n"
        "    a * b\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "tests" / "calc.rs").write_text(
        "use rust_calc::{add, mul};\n"
        "\n"
        "#[test]\n"
        "fn add_works() {\n"
        "    assert_eq!(add(1, 1), 2);\n"
        "    assert_eq!(add(2, 3), 5);\n"
        "}\n"
        "\n"
        "#[test]\n"
        "fn mul_works() {\n"
        "    // weak: 1*1 == 1/1, so a `*` -> `/` mutant survives this assertion\n"
        "    assert_eq!(mul(1, 1), 1);\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "Cargo.toml").write_text(
        "[package]\n"
        'name = "rust-calc"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n'
        "\n"
        "[lib]\n"
        'name = "rust_calc"\n'
        'path = "src/lib.rs"\n',
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("/target\n.vrp/\n", encoding="utf-8")
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
    rmtree_retry(repo)


def test_cargo_mutants_e2e(cargo_repo):
    repo = cargo_repo
    out = json.loads(run_vrp(["mutate-run", str(repo), "--budget", "2"], cwd=ROOT, timeout=900))
    assert out["status"] == "ok"
    assert out["engines"] == {"cargo-mutants": "ok"}
    assert out["targets"] == 2
    assert out["files"] == ["src/lib.rs"]
    # 27.1.0 ground truth: 12 mutants, 8 caught, 4 missed (2 greet side + 2 mul)
    assert out["mutants_total"] >= 10
    assert out["killed"] >= 5
    assert out["survived"] >= 3
    assert out["side_mutants"] >= 2
    assert out["surviving_targets"] == out["findings"] == 1
    assert out["survival_rate"] is not None
    assert 0.0 <= out["survival_rate"] <= 1.0
    assert out["cargo_mutants_raw"].get("CaughtMutant", 0) >= 1
    assert out["cargo_mutants_raw"].get("MissedMutant", 0) >= 1
    assert not (repo / ".vrp" / "cargo-mutants-out").exists()
    # cargo-mutants builds in its own scratch copy - the fixture stays pristine
    assert not (repo / "target").exists()
    assert not (repo / "Cargo.lock").exists()
    report = json.loads((repo / ".vrp" / "mutation-report.json").read_text(encoding="utf-8"))
    assert len(report["per_target"]) == 2
    for target in report["per_target"]:
        assert target["engine"] == "cargo-mutants"
        counted = sum(target[k] for k in ("killed", "survived", "timeout", "suspicious", "skipped", "untested"))
        assert counted == target["mutants"]
    tree = git(repo, "status", "--porcelain").strip()
    assert tree == ""


def test_cargo_mutants_findings_and_evidence_written(cargo_repo):
    repo = cargo_repo
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT f.pathology, f.kind, f.source, s.qualified_name, f.payload_json"
            " FROM findings f JOIN symbols s ON s.fid = f.anchor_fid"
            " WHERE f.source = 'mutate-run' ORDER BY s.qualified_name"
        ).fetchall()
        evidence = con.execute(
            "SELECT payload_json FROM runtime_evidence WHERE caller_key = 'mutate-run'"
        ).fetchall()
    finally:
        con.close()
    # only mul survives (add assertions are strong, greet is outside the plan)
    assert len(rows) == 1
    pathology, kind, source, qn, payload = rows[0]
    assert pathology == "P8" and kind == "mutation-survivors" and source == "mutate-run"
    assert qn.endswith(".mul")
    detail = json.loads(payload)
    assert detail["engine"] == "cargo-mutants"
    assert detail["survived"] >= 1
    assert detail["qualified_name"] == qn
    assert 0.0 <= detail["survival_rate"] <= 1.0
    assert len(evidence) == 1
    summary = json.loads(evidence[0][0])
    assert summary["engines"] == {"cargo-mutants": "ok"}
    assert summary["survival_rate"] is not None


def test_cargo_mutants_idempotent(cargo_repo):
    repo = cargo_repo
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
