"""S2 P8 execution: budgeted mutation runs reconciled against the mutation plan.

Two engines behind one entry point (`vrp mutate-run`), dispatched by target
language - Python targets go to mutmut (pinned 2.4.5; 3.x requires WSL on
Windows, upstream issue #397), JS/TS targets go to Stryker 10 (core +
vitest-runner sandbox, see vrp/s2_stryker.py for probed facts), Rust targets
go to cargo-mutants 27.1.0 (see vrp/s2_cargo_mutants.py). All engines
produce a common mutant record list ({file, line, status} on the six-bucket
contract killed/survived/timeout/suspicious/skipped/untested) which the shared
reconcile maps back onto plan targets by (file, line range):
- select budgeted targets via s2_mutate.mutation_targets (same sampler as
  `vrp mutate-plan`; plan artifact rewritten for traceability)
- per-target survivors become findings (pathology='P8',
  source='mutate-run', payload carries the engine) - 同义反复测试 pathology
  items only
- summary row lands in runtime_evidence under the 'mutate-run' key namespace
  (freeze owns RUNTIME_SLOTS separately; both survive etl via OWNED_TABLES)
- full breakdown is persisted to .vrp/mutation-report.json; engine scratch
  (.mutmut-cache, .vrp/stryker.conf.json, .vrp/stryker-report.json,
  .vrp/cargo-mutants-out) is read then deleted on every path so the working
  tree stays clean
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from . import procs, s2_cargo_mutants, s2_mutate, s2_stryker
from . import dbpath

SOURCE = "mutate-run"
PATHOLOGY = "P8"
KIND = "mutation-survivors"

STATUS_MAP = {
    "ok_killed": "killed",
    "bad_survived": "survived",
    "bad_timeout": "timeout",
    "ok_suspicious": "suspicious",
    "skipped": "skipped",
    "untested": "untested",
}

_COUNT_KEYS = ("killed", "survived", "timeout", "suspicious", "skipped", "untested")

_PY_EXTS = (".py",)
_TS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_RS_EXTS = (".rs",)

_ENGINE_PRIORITY = ("mutmut", "stryker", "cargo-mutants")


def _engine_for(file_path: str) -> str:
    if file_path.endswith(_PY_EXTS):
        return "mutmut"
    if file_path.endswith(_RS_EXTS):
        return "cargo-mutants"
    return "stryker"


def find_mutmut() -> str | None:
    from os import environ
    from shutil import which

    env = environ.get("VRP_MUTMUT_BIN")
    if env and Path(env).is_file():
        return env
    return which("mutmut")


def rc_ok(returncode: int) -> bool:
    """mutmut exit codes: bit-OR of 1 fatal / 2 survived / 4 timeout / 8 slow."""
    return returncode >= 0 and (returncode & 1) == 0


def run_mutmut(bin_path: str, repo: Path, files: list[str], timeout: int) -> dict:
    runner = f'"{sys.executable}" -m pytest -x -q'
    rr = procs.run_tree(
        [
            bin_path,
            "run",
            "--paths-to-mutate",
            ",".join(files),
            "--runner",
            runner,
            "--tests-dir",
            ".",
            # mutmut default 2.0 relabels killed-but-slow runs as
            # 'suspicious' under batch load (rc==1 yet >2x baseline);
            # 10.0 keeps reconcile deterministic in loaded pipelines
            "--test-time-multiplier",
            "10.0",
            "--no-progress",
        ],
        cwd=repo,
        timeout=timeout,
    )
    if rr["timed_out"]:
        return {"ok": False, "returncode": None, "stderr_tail": f"mutmut timed out after {timeout}s"}
    return {
        "ok": rc_ok(rr["returncode"]),
        "returncode": rr["returncode"],
        "stderr_tail": rr["stderr"][-500:],
    }


def read_cache(cache: Path) -> list[dict]:
    con = dbpath.ro_connect(cache)
    try:
        rows = con.execute(
            'SELECT sf.filename, l.line_number, m."index", m.status FROM Mutant m'
            " JOIN Line l ON m.line = l.id"
            " JOIN SourceFile sf ON l.sourcefile = sf.id"
        ).fetchall()
    finally:
        con.close()
    out: list[dict] = []
    for filename, line_number, _index, status in rows:
        out.append(
            {
                "file": filename.replace("\\", "/"),
                "line": line_number,
                "status": STATUS_MAP.get(status, "untested"),
            }
        )
    return out


def reconcile(targets: list[dict], mutants: list[dict]) -> tuple:
    per_target: list[dict] = []
    side = 0
    for t in targets:
        in_range = [
            m
            for m in mutants
            if m["file"] == t["file_path"] and t["start_line"] <= m["line"] <= t["end_line"]
        ]
        counts = Counter(m["status"] for m in in_range)
        tested = counts["killed"] + counts["survived"] + counts["timeout"]
        per_target.append(
            {
                **{k: t[k] for k in ("fid", "qualified_name", "file_path", "start_line", "end_line", "tests", "callers")},
                "engine": _engine_for(t["file_path"]),
                "mutants": len(in_range),
                **{k: counts.get(k, 0) for k in _COUNT_KEYS},
                "survival_rate": (counts["survived"] / tested) if tested else None,
            }
        )
    for m in mutants:
        inside = any(
            t["file_path"] == m["file"] and t["start_line"] <= m["line"] <= t["end_line"]
            for t in targets
        )
        if not inside:
            side += 1
    return per_target, side


def write_report(repo: Path, payload: dict) -> Path:
    out = repo / ".vrp" / "mutation-report.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return out


def write_findings(con: sqlite3.Connection, per_target: list[dict]) -> int:
    rows = [t for t in per_target if t["survived"] > 0]
    con.execute("DELETE FROM findings WHERE source = ?", (SOURCE,))
    for t in rows:
        payload = json.dumps(
            {
                "qualified_name": t["qualified_name"],
                "file_path": t["file_path"],
                "engine": t["engine"],
                "mutants": t["mutants"],
                **{k: t[k] for k in _COUNT_KEYS},
                "survival_rate": t["survival_rate"],
                "tests": t["tests"],
                "callers": t["callers"],
            },
            ensure_ascii=False,
        )
        con.execute(
            "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid, file_path, start_line, end_line, payload_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (PATHOLOGY, KIND, SOURCE, t["fid"], None, t["file_path"], t["start_line"], t["end_line"], payload),
        )
    con.commit()
    return len(rows)


def write_runtime_evidence(con: sqlite3.Connection, summary: dict) -> None:
    con.execute("DELETE FROM runtime_evidence WHERE caller_key = ?", (SOURCE,))
    payload = json.dumps(
        {
            "engines": summary.get("engines"),
            "budget": summary.get("budget"),
            "targets": summary.get("targets"),
            "mutants": summary.get("mutants_total"),
            "survived": summary.get("survived"),
            "surviving_targets": summary.get("surviving_targets"),
            "survival_rate": summary.get("survival_rate"),
        },
        ensure_ascii=False,
    )
    con.execute(
        "INSERT INTO runtime_evidence (caller_key, callee_key, payload_json) VALUES (?,?,?)",
        (SOURCE, SOURCE, payload),
    )
    con.commit()


def _engine_mutmut(repo: Path, targets: list[dict], timeout: int) -> dict:
    bin_path = find_mutmut()
    if not bin_path:
        return {
            "status": "not-found",
            "mutants": [],
            "detail": {"hint": "pip install mutmut==2.4.5 or set VRP_MUTMUT_BIN"},
        }
    files = sorted({t["file_path"] for t in targets})
    rr = run_mutmut(bin_path, repo, files, timeout)
    if not rr["ok"]:
        # mutmut creates .mutmut-cache before its baseline run - remove it on
        # failure too, the target repo's tree must stay clean on every path
        (repo / ".mutmut-cache").unlink(missing_ok=True)
        return {"status": "failed", "mutants": [], "detail": rr}
    cache = repo / ".mutmut-cache"
    if not cache.is_file():
        return {
            "status": "failed",
            "mutants": [],
            "detail": {"returncode": rr["returncode"], "stderr_tail": "no .mutmut-cache produced"},
        }
    try:
        mutants = read_cache(cache)
    finally:
        cache.unlink(missing_ok=True)
    return {"status": "ok", "mutants": mutants}


def _engine_stryker(repo: Path, targets: list[dict], timeout: int) -> dict:
    bin_path = s2_stryker.find_stryker()
    if not bin_path:
        return {
            "status": "not-found",
            "mutants": [],
            "detail": {
                "hint": "npm install @stryker-mutator/core @stryker-mutator/vitest-runner"
                " into work/stryker or set VRP_STRYKER_BIN"
            },
        }
    files = sorted({t["file_path"] for t in targets})
    rr = s2_stryker.run_stryker(bin_path, repo, files, timeout)
    try:
        if not rr["ok"]:
            return {"status": "failed", "mutants": [], "detail": rr}
        report = repo / s2_stryker.REPORT_NAME
        if not report.is_file():
            return {
                "status": "failed",
                "mutants": [],
                "detail": {"returncode": rr["returncode"], "stderr_tail": "no stryker report produced"},
            }
        mutants, raw = s2_stryker.parse_report(report)
        return {"status": "ok", "mutants": mutants, "raw": raw}
    finally:
        s2_stryker.cleanup(repo)


def _engine_cargo_mutants(repo: Path, targets: list[dict], timeout: int) -> dict:
    bin_path = s2_cargo_mutants.find_cargo_mutants()
    if not bin_path:
        return {
            "status": "not-found",
            "mutants": [],
            "detail": {
                "hint": "cargo install cargo-mutants or set VRP_CARGO_MUTANTS_BIN"
            },
        }
    files = sorted({t["file_path"] for t in targets})
    rr = s2_cargo_mutants.run_cargo_mutants(bin_path, repo, files, timeout)
    try:
        if not rr["ok"]:
            return {"status": "failed", "mutants": [], "detail": rr}
        report = repo / s2_cargo_mutants.REPORT_NAME
        if not report.is_file():
            return {
                "status": "failed",
                "mutants": [],
                "detail": {
                    "returncode": rr["returncode"],
                    "stderr_tail": "no cargo-mutants outcomes produced",
                },
            }
        mutants, raw, baseline_ok = s2_cargo_mutants.parse_outcomes(report)
        if not baseline_ok:
            return {
                "status": "failed",
                "mutants": [],
                "detail": {
                    "returncode": rr["returncode"],
                    "stderr_tail": "cargo-mutants baseline tests failed",
                },
            }
        return {"status": "ok", "mutants": mutants, "raw": raw}
    finally:
        s2_cargo_mutants.cleanup(repo)


def run(repo: Path, con: sqlite3.Connection, budget: int, timeout: int) -> dict:
    targets = s2_mutate.mutation_targets(con, budget)
    py = [t for t in targets if t["file_path"].endswith(_PY_EXTS)]
    ts = [t for t in targets if t["file_path"].endswith(_TS_EXTS)]
    rs = [t for t in targets if t["file_path"].endswith(_RS_EXTS)]
    selected = py + ts + rs
    if not selected:
        return {"status": "no-targets"}
    s2_mutate.write_plan(repo, selected)
    engines: dict = {}
    if py:
        engines["mutmut"] = _engine_mutmut(repo, py, timeout)
    if ts:
        engines["stryker"] = _engine_stryker(repo, ts, timeout)
    if rs:
        engines["cargo-mutants"] = _engine_cargo_mutants(repo, rs, timeout)
    if not any(r["status"] == "ok" for r in engines.values()):
        first = next((n for n in _ENGINE_PRIORITY if n in engines), None)
        failed = engines[first]
        out: dict = {"status": f"{first}-{failed['status']}", "engines": {n: r["status"] for n, r in engines.items()}}
        if failed.get("detail"):
            detail = dict(failed["detail"])
            detail.pop("ok", None)
            out.update(detail)
        return out
    mutants = [m for r in engines.values() for m in r["mutants"]]
    per_target, side = reconcile(selected, mutants)
    totals = Counter()
    for m in mutants:
        totals[m["status"]] += 1
    tested = totals["killed"] + totals["survived"] + totals["timeout"]
    surviving = [t for t in per_target if t["survived"] > 0]
    summary = {
        "status": "ok",
        "budget": budget,
        "targets": len(selected),
        "files": sorted({t["file_path"] for t in selected}),
        "mutants_total": len(mutants),
        "side_mutants": side,
        **{k: totals.get(k, 0) for k in _COUNT_KEYS},
        "surviving_targets": len(surviving),
        "survival_rate": (totals["survived"] / tested) if tested else None,
        "engines": {n: r["status"] for n, r in engines.items()},
    }
    stryker_raw = engines.get("stryker", {}).get("raw")
    if stryker_raw:
        summary["stryker_raw"] = stryker_raw
    cargo_raw = engines.get("cargo-mutants", {}).get("raw")
    if cargo_raw:
        summary["cargo_mutants_raw"] = cargo_raw
    report = write_report(repo, {**summary, "per_target": per_target})
    summary["report"] = str(report)
    summary["findings"] = write_findings(con, per_target)
    write_runtime_evidence(con, summary)
    return summary
