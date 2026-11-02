"""S4 freeze: run discovered commands -> runtime evidence + CBM graph baseline (v6 s6).

Pure orchestration (v5): no test-framework knowledge, no per-language coverage tools
in v1. CBM linkages per v6 s6:
- linkage 1: index_repository --persistence -> <repo>/.codebase-memory/graph.db.zst baseline
- linkage 2: detect_changes scope=tests -> change->test pre-filter signal (never a gate)
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import s0_precheck, s1_cbm

RUNTIME_SLOTS = ("build", "test", "lint", "typecheck")
TAIL_BYTES = 2000
IMPACT_CAP = 64 * 1024
FREEZE_KIND = "freeze"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        proc.kill()


def _run_one(repo: Path, command: str, timeout: int) -> dict:
    started = time.monotonic()
    timed_out = False
    popen_kwargs = {}
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = proc.communicate()
        timed_out = True
        rc = -1
        err = (err or "") + f"\ntimeout after {timeout}s (process tree killed)"
    return {
        "command": command,
        "rc": rc,
        "duration_s": round(time.monotonic() - started, 3),
        "timed_out": timed_out,
        "stdout_tail": (out or "")[-TAIL_BYTES:],
        "stderr_tail": (err or "")[-TAIL_BYTES:],
    }


def run_commands(repo: Path, commands: dict[str, list[str]], timeout: int = 900) -> dict[str, list[dict]]:
    results: dict[str, list[dict]] = {}
    for slot in RUNTIME_SLOTS:
        for command in commands.get(slot, []):
            results.setdefault(slot, []).append(_run_one(repo, command, timeout))
    return results


def write_runtime_evidence(con: sqlite3.Connection, results: dict[str, list[dict]]) -> int:
    written = 0
    for slot in RUNTIME_SLOTS:
        for run in results.get(slot, []):
            con.execute(
                "INSERT INTO runtime_evidence (caller_key, callee_key, payload_json) VALUES (NULL, ?, ?)",
                (slot, json.dumps(run, ensure_ascii=False)),
            )
            written += 1
    con.commit()
    return written


def graph_baseline(repo: Path, name: str, timeout: int = 3600) -> dict:
    result = s1_cbm.run_cli(
        ["index_repository", "--repo-path", str(repo), "--name", name, "--persistence"],
        timeout=timeout,
    )
    artifact = repo / ".codebase-memory" / "graph.db.zst"
    if not artifact.exists():
        raise RuntimeError(f"persistence index succeeded but artifact missing: {artifact}")
    return {
        "status": "written",
        "artifact": str(artifact),
        "bytes": artifact.stat().st_size,
        "cbm": result,
    }


def test_impact(repo: Path, name: str, timeout: int = 300) -> dict:
    try:
        return s1_cbm.run_cli(
            ["detect_changes", "--project", name, "--scope", "tests"],
            timeout=timeout,
        )
    except RuntimeError as exc:
        return {"error": str(exc)[:1000]}


def _cap_payload(payload: dict) -> dict:
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded) <= IMPACT_CAP:
        return payload
    return {"truncated": True, "bytes": len(encoded)}


def _git_head(repo: Path) -> dict:
    def run(args: list[str]) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except (subprocess.TimeoutExpired, OSError):
            return ""

    return {"commit": run(["rev-parse", "HEAD"]), "branch": run(["rev-parse", "--abbrev-ref", "HEAD"])}


def _mutation_archive(repo: Path) -> dict | None:
    plan = repo / ".vrp" / "mutation-plan.json"
    if not plan.is_file():
        return None
    return {"path": str(plan), "bytes": plan.stat().st_size}


def freeze(
    repo: Path,
    facts_db: Path,
    name: str,
    timeout: int = 900,
    graph_timeout: int = 3600,
    skip_commands: bool = False,
    skip_graph: bool = False,
) -> dict:
    commands = s0_precheck.discover_commands(repo)
    git = _git_head(repo)
    results: dict[str, list[dict]] = {}
    con = sqlite3.connect(facts_db)
    try:
        if not skip_commands:
            results = run_commands(repo, commands, timeout=timeout)
            write_runtime_evidence(con, results)
        graph = None
        impact = None
        if not skip_graph:
            graph = graph_baseline(repo, name, timeout=graph_timeout)
            impact = _cap_payload(test_impact(repo, name))
        payload = {
            "frozen_at": _utc_now(),
            "git": git,
            "commands": {
                slot: [
                    {"command": r["command"], "rc": r["rc"], "duration_s": r["duration_s"], "timed_out": r["timed_out"]}
                    for r in runs
                ]
                for slot, runs in results.items()
            },
            "mutation_plan": _mutation_archive(repo),
            "graph_baseline": graph,
            "test_impact": impact,
        }
        con.execute(
            "INSERT INTO baseline (kind, payload_json) VALUES (?, ?)",
            (FREEZE_KIND, json.dumps(payload, ensure_ascii=False)),
        )
        con.commit()
    finally:
        con.close()
    summary = {
        "frozen_at": payload["frozen_at"],
        "git": git,
        "commands": {
            slot: [f"{r['command']} -> rc={r['rc']} ({r['duration_s']}s)" for r in runs]
            for slot, runs in results.items()
        },
        "graph_baseline": {k: graph[k] for k in ("status", "artifact", "bytes")} if graph else None,
        "test_impact_truncated": bool(impact and impact.get("truncated")),
    }
    return summary
