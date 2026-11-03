"""S2 P8 execution: mutmut-based mutation runner reconciled against the mutation plan.

mutmut 2.x executes line-level mutations and runs the test suite per mutant
(pinned to 2.4.5 - mutmut 3.x requires WSL on Windows, upstream issue #397).
Results are cached in `.mutmut-cache` (pony sqlite: Mutant/Line/SourceFile).

This adapter stitches mutmut onto the vrp plan (v6 §4 P8: Stryker/mutmut/
cargo-mutants 预算采样):
- select budgeted targets via s2_mutate.mutation_targets (same sampler as
  `vrp mutate-plan`; plan artifact rewritten for traceability; Python-only
  - mutmut cannot mutate other languages, non-.py targets are filtered out)
- run mutmut ONCE over the union of target files (`--paths-to-mutate`,
  file-granularity side mutants are expected and counted separately)
- reconcile cached mutants back onto plan targets by (file, line range)
- per-target survivors become findings (pathology='P8',
  source='mutate-run') - 同义反复测试 pathology items only
- summary row lands in runtime_evidence under the 'mutate-run' key namespace
  (freeze owns RUNTIME_SLOTS separately; both survive etl via OWNED_TABLES)
- full breakdown is persisted to .vrp/mutation-report.json; .mutmut-cache is
  read then deleted to keep the working tree clean
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

from . import s2_mutate

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
    try:
        proc = subprocess.run(
            [
                bin_path,
                "run",
                "--paths-to-mutate",
                ",".join(files),
                "--runner",
                runner,
                "--tests-dir",
                ".",
                "--no-progress",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "stderr_tail": f"mutmut timed out after {timeout}s"}
    return {
        "ok": rc_ok(proc.returncode),
        "returncode": proc.returncode,
        "stderr_tail": (proc.stderr or "")[-500:],
    }


def read_cache(cache: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{cache}?mode=ro", uri=True)
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
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def write_findings(con: sqlite3.Connection, per_target: list[dict]) -> int:
    rows = [t for t in per_target if t["survived"] > 0]
    con.execute("DELETE FROM findings WHERE source = ?", (SOURCE,))
    for t in rows:
        payload = json.dumps(
            {
                "qualified_name": t["qualified_name"],
                "file_path": t["file_path"],
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
            "runner": "mutmut",
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


def run(repo: Path, con: sqlite3.Connection, budget: int, timeout: int) -> dict:
    targets = s2_mutate.mutation_targets(con, budget)
    targets = [t for t in targets if t["file_path"].endswith(".py")]
    if not targets:
        return {"status": "no-targets"}
    bin_path = find_mutmut()
    if not bin_path:
        return {"status": "mutmut-not-found", "hint": "pip install mutmut==2.4.5 or set VRP_MUTMUT_BIN"}
    files = sorted({t["file_path"] for t in targets})
    s2_mutate.write_plan(repo, targets)
    rr = run_mutmut(bin_path, repo, files, timeout)
    if not rr["ok"]:
        return {
            "status": "mutmut-failed",
            "returncode": rr["returncode"],
            "stderr_tail": rr["stderr_tail"],
        }
    cache = repo / ".mutmut-cache"
    if not cache.is_file():
        return {"status": "mutmut-failed", "returncode": rr["returncode"], "stderr_tail": "no .mutmut-cache produced"}
    try:
        mutants = read_cache(cache)
    finally:
        cache.unlink(missing_ok=True)
    per_target, side = reconcile(targets, mutants)
    totals = Counter()
    for m in mutants:
        totals[m["status"]] += 1
    tested = totals["killed"] + totals["survived"] + totals["timeout"]
    surviving = [t for t in per_target if t["survived"] > 0]
    summary = {
        "status": "ok",
        "budget": budget,
        "targets": len(per_target),
        "files": files,
        "mutants_total": len(mutants),
        "side_mutants": side,
        **{k: totals.get(k, 0) for k in _COUNT_KEYS},
        "surviving_targets": len(surviving),
        "survival_rate": (totals["survived"] / tested) if tested else None,
    }
    report = write_report(repo, {**summary, "per_target": per_target})
    summary["report"] = str(report)
    summary["findings"] = write_findings(con, per_target)
    write_runtime_evidence(con, summary)
    return summary
