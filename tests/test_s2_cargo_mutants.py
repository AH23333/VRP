import json

from vrp import s2_cargo_mutants
from vrp.s2_cargo_mutants import (
    OUT_DIR_NAME,
    REPORT_NAME,
    cleanup,
    find_cargo_mutants,
    parse_outcomes,
    run_cargo_mutants,
)


def test_find_cargo_mutants_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "cargo-mutants.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("VRP_CARGO_MUTANTS_BIN", str(fake))
    assert find_cargo_mutants() == str(fake)


def test_find_cargo_mutants_home_default_then_which(tmp_path, monkeypatch):
    monkeypatch.delenv("VRP_CARGO_MUTANTS_BIN", raising=False)
    home_bin = tmp_path / ".cargo" / "bin" / "cargo-mutants.exe"
    home_bin.parent.mkdir(parents=True)
    home_bin.write_text("", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert find_cargo_mutants() == str(home_bin)
    home_bin.unlink()
    monkeypatch.setattr("shutil.which", lambda name: "global-cargo-mutants")
    assert find_cargo_mutants() == "global-cargo-mutants"
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert find_cargo_mutants() is None


def test_run_cargo_mutants_cmd_and_rc2_ok(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_run_tree(cmd, cwd=None, timeout=None):
        seen["cmd"] = list(cmd)
        seen["cwd"] = cwd
        seen["timeout"] = timeout
        return {"returncode": 2, "timed_out": False,
                "stdout": "12 mutants tested: 4 missed, 8 caught", "stderr": ""}

    monkeypatch.setattr("vrp.procs.run_tree", fake_run_tree)
    rr = run_cargo_mutants("fake-cargo-mutants", tmp_path, ["src/a.rs", "src/b.rs"], 300)
    # rc 2 = some missed - still a valid run (rc 1 = engine error is not)
    assert rr["ok"] is True and rr["returncode"] == 2
    assert seen["cmd"] == [
        "fake-cargo-mutants", "mutants", "-o", OUT_DIR_NAME, "--no-times",
        "-f", "src/a.rs", "-f", "src/b.rs",
    ]
    assert seen["cwd"] == tmp_path
    assert seen["timeout"] == 300


def test_run_cargo_mutants_rc1_not_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vrp.procs.run_tree",
        lambda cmd, cwd=None, timeout=None: {
            "returncode": 1, "timed_out": False, "stdout": "", "stderr": "baseline tests failed",
        },
    )
    rr = run_cargo_mutants("fake-cargo-mutants", tmp_path, ["src/a.rs"], 300)
    assert rr["ok"] is False
    assert rr["returncode"] == 1
    assert rr["stderr_tail"] == "baseline tests failed"


def test_run_cargo_mutants_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vrp.procs.run_tree",
        lambda cmd, cwd=None, timeout=None: {
            "returncode": -1, "timed_out": True, "stdout": "", "stderr": "",
        },
    )
    rr = run_cargo_mutants("fake-cargo-mutants", tmp_path, ["src/a.rs"], 300)
    assert rr["ok"] is False
    assert rr["returncode"] is None
    assert "timed out" in rr["stderr_tail"]


def _mutant(site_line, fn_line=1):
    return {
        "name": "src/lib.rs:{}:5: replace f with 0".format(site_line),
        "package": "p",
        "file": "src\\lib.rs",
        "function": {"function_name": "f", "return_type": "-> i32",
                     "span": {"start": {"line": fn_line, "column": 1},
                              "end": {"line": fn_line + 2, "column": 2}}},
        "span": {"start": {"line": site_line, "column": 5},
                 "end": {"line": site_line, "column": 10}},
        "replacement": "0",
        "genre": "FnValue",
    }


def _outcomes(tmp_path, outcomes):
    report = tmp_path / REPORT_NAME
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"outcomes": outcomes}), encoding="utf-8")
    return report


def test_parse_outcomes_maps_statuses_and_site_line(tmp_path):
    report = _outcomes(tmp_path, [
        {"scenario": "Baseline", "summary": "Success"},
        {"scenario": {"Mutant": _mutant(2)}, "summary": "CaughtMutant"},
        {"scenario": {"Mutant": _mutant(6, fn_line=5)}, "summary": "MissedMutant"},
        {"scenario": {"Mutant": _mutant(11, fn_line=10)}, "summary": "TimeoutMutant"},
        {"scenario": {"Mutant": _mutant(12, fn_line=10)}, "summary": "UnviableMutant"},
        {"scenario": {"Mutant": _mutant(13, fn_line=10)}, "summary": "WeirdStatus"},
    ])
    records, raw, baseline_ok = parse_outcomes(report)
    # the SITE line is the top-level span.start.line, NOT function.span
    assert [(r["file"], r["line"], r["status"]) for r in records] == [
        ("src/lib.rs", 2, "killed"),
        ("src/lib.rs", 6, "survived"),
        ("src/lib.rs", 11, "timeout"),
        ("src/lib.rs", 12, "skipped"),
        ("src/lib.rs", 13, "untested"),
    ]
    assert raw == {"CaughtMutant": 1, "MissedMutant": 1, "TimeoutMutant": 1,
                   "UnviableMutant": 1, "WeirdStatus": 1}
    assert baseline_ok is True


def test_parse_outcomes_baseline_failure_voids_run(tmp_path):
    report = _outcomes(tmp_path, [{"scenario": "Baseline", "summary": "TestFailure"}])
    records, raw, baseline_ok = parse_outcomes(report)
    assert records == [] and raw == {}
    assert baseline_ok is False


def test_cleanup_removes_scratch_tolerant(tmp_path):
    out = tmp_path / OUT_DIR_NAME / "mutants.out"
    out.mkdir(parents=True)
    (out / "outcomes.json").write_text("{}", encoding="utf-8")
    cleanup(tmp_path)
    assert not (tmp_path / OUT_DIR_NAME).exists()
    # idempotent on a missing dir
    cleanup(tmp_path)


def test_module_docstring_documents_probe():
    text = s2_cargo_mutants.__doc__ or ""
    for fact in ("27.1.0", "-f", "mutants.out", "outcomes.json", "span.start.line"):
        assert fact in text
