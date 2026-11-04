"""S2 P8 execution: cargo-mutants adapter for Rust mutation runs.

cargo-mutants 27.1.0 (`cargo install cargo-mutants --locked`; VRP_CARGO_MUTANTS_BIN
overrides, ~/.cargo/bin/cargo-mutants.exe default) mutates Rust crates and runs
`cargo test` per mutant. Probed facts (work/rust-calc-probe, 2026-11-04):
- subcommand is `mutants`; file filter is a REPEATABLE `-f <glob>` (forward
  slashes, repo-relative); `-o <dir>` receives a `mutants.out/` DIRECTORY
  whose outcomes.json is pretty-printed JSON - NOT jsonl (missed.txt /
  caught.txt / log/ / diff/ sit beside it)
- rc: 0 = all caught, 2 = some missed (MISSED), 1 = engine error (broken
  baseline, cargo failure) - the mutmut bit trick (rc & 1) == 0 fits exactly
- default (no --in-place) copies the tree to a scratch dir and builds there:
  the source repo stays pristine (no target/, no Cargo.lock, scratch
  auto-removed) - Windows needs no readonly handling
- outcomes.json rows: scenario is the STRING "Baseline" or the DICT
  {"Mutant": {...}}; summary is Success / CaughtMutant / MissedMutant /
  TimeoutMutant / UnviableMutant; the mutant SITE line is the top-level
  span.start.line (function.span is the ENCLOSING function - not the site)
- untested functions report as MissedMutant too - cargo-mutants has no
  NotCovered state (greet with no covering test came back MISSED)

Status mapping onto the six-bucket reconcile contract (s2_mutate_run):
- CaughtMutant -> killed, MissedMutant -> survived, TimeoutMutant -> timeout
- UnviableMutant -> skipped (mutant does not even build - engine cannot
  evaluate it)
- unknown -> untested

The adapter only finds/runs/parses/cleans; orchestration (dispatch, reconcile,
findings, evidence) stays in s2_mutate_run.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

OUT_DIR_NAME = ".vrp/cargo-mutants-out"
REPORT_NAME = ".vrp/cargo-mutants-out/mutants.out/outcomes.json"

CARGO_MUTANTS_STATUS_MAP = {
    "CaughtMutant": "killed",
    "MissedMutant": "survived",
    "TimeoutMutant": "timeout",
    "UnviableMutant": "skipped",
}


def find_cargo_mutants() -> str | None:
    from os import environ
    from shutil import which

    env = environ.get("VRP_CARGO_MUTANTS_BIN")
    if env and Path(env).is_file():
        return env
    default = Path.home() / ".cargo" / "bin" / "cargo-mutants.exe"
    if default.is_file():
        return str(default)
    return which("cargo-mutants")


def run_cargo_mutants(bin_path: str, repo: Path, files: list[str], timeout: int) -> dict:
    cmd = [bin_path, "mutants", "-o", OUT_DIR_NAME, "--no-times"]
    for f in files:
        cmd += ["-f", f]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": f"cargo-mutants timed out after {timeout}s",
        }
    return {
        # rc 0 = all caught, 2 = some missed, 1 = engine error
        "ok": proc.returncode >= 0 and (proc.returncode & 1) == 0,
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-500:],
        "stderr_tail": (proc.stderr or "")[-500:],
    }


def parse_outcomes(report: Path) -> tuple:
    data = json.loads(report.read_text(encoding="utf-8"))
    records: list[dict] = []
    raw: Counter = Counter()
    baseline_ok = True
    for outcome in data.get("outcomes", []):
        scenario = outcome.get("scenario")
        if not isinstance(scenario, dict):
            # baseline row (scenario == "Baseline"): unmutated tree must pass,
            # else every mutant result is void
            if outcome.get("summary") != "Success":
                baseline_ok = False
            continue
        mutant = scenario.get("Mutant")
        if not mutant:
            continue
        summary = outcome.get("summary")
        raw[summary] += 1
        records.append(
            {
                "file": mutant["file"].replace("\\", "/"),
                "line": mutant["span"]["start"]["line"],
                "status": CARGO_MUTANTS_STATUS_MAP.get(summary, "untested"),
            }
        )
    return records, dict(raw), baseline_ok


def cleanup(repo: Path) -> None:
    import shutil

    shutil.rmtree(repo / OUT_DIR_NAME, ignore_errors=True)
