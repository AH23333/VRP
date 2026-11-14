"""S2 P8 execution: Stryker adapter for JS/TS mutation runs.

Stryker 10 (core + vitest-runner, sandboxed at work/stryker; VRP_STRYKER_BIN
overrides) executes mutants against the target repo's vitest suite. Probed
facts (work/stryker-probe, 2026-11-03):
- config file is a POSITIONAL argument (`stryker run <config>`), JSON must be
  BOM-free (PS 5.1 Set-Content utf8 writes a BOM - pitfall family #11)
- json report path knob is top-level `jsonReporter.fileName` (NOT nested
  reporterOptions - silently ignored there)
- `tempDirName` accepts a .vrp-relative path; Stryker removes it on success,
  but a FAILED run (e.g. ConfigError on broken baseline) leaks the full
  sandbox copy of the repo - cleanup() must rmtree it (readonly .git objects
  on Windows need chmod-on-retry)
- rc == 0 on success (survivors included); non-zero on any failure (missing
  test runner, broken baseline)
- report schemaVersion 1.0: files{relpath: {language, mutants: [{status,
  location: {start: {line, column}}, ...}]}}; 1-based lines; use start.line
- vitest >= 2.0.0 peer range, probed working on vitest 4.1.x

Status mapping onto the six-bucket reconcile contract (s2_mutate_run):
- Killed -> killed, Survived -> survived, Timeout -> timeout
- NoCoverage -> survived (the suite never executes the mutant - survivor by
  pathology, recorded raw for honesty)
- CompileError/RuntimeError -> suspicious (engine could not evaluate)
- unknown -> untested

The adapter only finds/runs/parses/cleans; orchestration (dispatch, reconcile,
findings, evidence) stays in s2_mutate_run.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections import Counter
from pathlib import Path

from . import procs

CONF_NAME = ".vrp/stryker.conf.json"
REPORT_NAME = ".vrp/stryker-report.json"
TMP_DIR_NAME = ".vrp/stryker-tmp"

STRYKER_STATUS_MAP = {
    "Killed": "killed",
    "Survived": "survived",
    "NoCoverage": "survived",
    "Timeout": "timeout",
    "CompileError": "suspicious",
    "RuntimeError": "suspicious",
}


def _shim() -> Path:
    return Path(__file__).parent.parent / "work" / "stryker" / "node_modules" / ".bin" / "stryker.cmd"


def find_stryker() -> str | None:
    from os import environ
    from shutil import which

    env = environ.get("VRP_STRYKER_BIN")
    if env and Path(env).is_file():
        return env
    shim = _shim()
    if shim.is_file():
        return str(shim)
    return which("stryker")


def write_config(repo: Path, files: list[str]) -> Path:
    conf = repo / CONF_NAME
    conf.parent.mkdir(exist_ok=True)
    conf.write_text(
        json.dumps(
            {
                "testRunner": "vitest",
                "reporters": ["json"],
                "mutate": sorted(files),
                "coverageAnalysis": "perTest",
                "tempDirName": TMP_DIR_NAME,
                "jsonReporter": {"fileName": REPORT_NAME},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return conf


def run_stryker(bin_path: str, repo: Path, files: list[str], timeout: int) -> dict:
    conf = write_config(repo, files)
    rr = procs.run_tree([bin_path, "run", CONF_NAME], cwd=repo, timeout=timeout)
    if rr["timed_out"]:
        return {
            "ok": False,
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": f"stryker timed out after {timeout}s",
        }
    return {
        "ok": rr["returncode"] == 0,
        "returncode": rr["returncode"],
        # Stryker logs the actual failure reason (e.g. which test broke the
        # initial dry-run) to stdout; the ConfigError stack lands on stderr
        "stdout_tail": rr["stdout"][-500:],
        "stderr_tail": rr["stderr"][-500:],
    }


def parse_report(report: Path) -> tuple:
    data = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        # 上游 stryker 报告形状漂移（非 JSON 对象）：诚实错误（与 JSONDecodeError 同语义）
        raise ValueError(f"stryker report is not a JSON object: {report}")
    records: list[dict] = []
    raw: Counter = Counter()
    for file_rel, entry in data.get("files", {}).items():
        for m in entry.get("mutants", []):
            status = m.get("status")
            raw[status] += 1
            records.append(
                {
                    "file": file_rel.replace("\\", "/"),
                    "line": m["location"]["start"]["line"],
                    "status": STRYKER_STATUS_MAP.get(status, "untested"),
                }
            )
    return records, dict(raw)


def cleanup(repo: Path) -> None:
    (repo / CONF_NAME).unlink(missing_ok=True)
    (repo / REPORT_NAME).unlink(missing_ok=True)
    tmp = repo / TMP_DIR_NAME
    if tmp.exists():
        # Stryker cleans tempDirName itself on success, but a failed run
        # (e.g. ConfigError on a broken baseline) leaks the full sandbox copy
        # of the repo; its .git objects carry the readonly bit on Windows
        def _clear_readonly(func, target, _exc):
            os.chmod(target, stat.S_IWRITE)
            func(target)

        shutil.rmtree(tmp, onexc=_clear_readonly)
