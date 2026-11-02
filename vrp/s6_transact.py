"""S6 transact: refactor transaction consumer (v4 loop + v2-A gate matrix MVP).

Transaction format is JSON (v2-C sketched YAML; JSON keeps the zero-dependency
rule since the stdlib ships no YAML parser):

    {"id": "tx-0001", "type": "T1", "summary": "rename x",
     "declared_moves": [{"from": "pkg.old", "to": "pkg.new"}],
     "edits": [{"file": "pkg/a.py", "op": "modify", "content": "..."}]}

Flow: clean-tree precheck -> register tx -> apply edits -> G1 rescan
(CBM reindex + ETL to a side facts.db; same-name reindex prunes deleted
symbols, verified empirically) -> G2 lint/typecheck -> G3 test -> G4
declared_moves / dangling references -> G5 dead-code monotonic (T2/T4) ->
first failure rolls the working tree back; all pass -> git commit + promote
facts.db. T4 requires --confirm. Every run writes .vrp/verdicts/<tid>.json.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import s0_precheck, s1_cbm, s2_deadcode, s4_freeze, etl

TX_TYPES = ("T1", "T2", "T3", "T4")
EDIT_OPS = ("create", "modify", "delete")
USAGE_EDGE_TYPES = ("CALLS", "CALL_REFERENCE", "MENTIONS", "SPAWNS")
TID_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_rel(file_value) -> Path:
    if not isinstance(file_value, str) or not file_value.strip():
        raise ValueError("edit.file must be a non-empty string")
    path = Path(file_value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"edit.file must be repo-relative: {file_value}")
    return path


def load_transaction(path: Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("transaction must be a JSON object")
    tid = raw.get("id")
    if not isinstance(tid, str) or not tid.strip():
        raise ValueError("transaction.id must be a non-empty string")
    tx_type = raw.get("type")
    if tx_type not in TX_TYPES:
        raise ValueError(f"transaction.type must be one of {TX_TYPES}, got: {tx_type!r}")
    edits = raw.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ValueError("transaction.edits must be a non-empty list")
    normalized = []
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("each edit must be an object")
        op = edit.get("op")
        if op not in EDIT_OPS:
            raise ValueError(f"edit.op must be one of {EDIT_OPS}, got: {op!r}")
        rel = _norm_rel(edit.get("file"))
        content = edit.get("content")
        if op in ("create", "modify") and not isinstance(content, str):
            raise ValueError(f"edit.op {op} requires string content: {rel}")
        normalized.append({"file": rel, "op": op, "content": content})
    moves = raw.get("declared_moves", [])
    if not isinstance(moves, list):
        raise ValueError("declared_moves must be a list")
    declared = []
    for move in moves:
        if not isinstance(move, dict) or not isinstance(move.get("from"), str) or not isinstance(move.get("to"), str):
            raise ValueError("each declared_move needs string 'from' and 'to'")
        declared.append({"from": move["from"], "to": move["to"]})
    return {
        "id": tid.strip(),
        "type": tx_type,
        "summary": str(raw.get("summary", "")),
        "declared_moves": declared,
        "edits": normalized,
    }


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def git_clean(repo: Path) -> bool:
    proc = _git(repo, "status", "--porcelain")
    return proc.returncode == 0 and not proc.stdout.strip()


def git_head(repo: Path) -> str:
    proc = _git(repo, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def git_commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    proc = _git(repo, "commit", "-m", message)
    if proc.returncode != 0:
        raise RuntimeError(f"git commit failed: {proc.stderr.strip()[:500]}")
    return git_head(repo)


def apply_edits(repo: Path, edits: list[dict]) -> None:
    for edit in edits:
        target = repo / edit["file"]
        if edit["op"] == "create":
            if target.exists():
                raise RuntimeError(f"create target already exists: {edit['file']}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit["content"], encoding="utf-8", newline="\n")
        elif edit["op"] == "modify":
            if not target.is_file():
                raise RuntimeError(f"modify target missing: {edit['file']}")
            target.write_text(edit["content"], encoding="utf-8", newline="\n")
        else:
            if not target.is_file():
                raise RuntimeError(f"delete target missing: {edit['file']}")
            target.unlink()


def rollback_edits(repo: Path, edits: list[dict]) -> list[str]:
    restored = []
    for edit in reversed(edits):
        target = repo / edit["file"]
        if edit["op"] == "create":
            if target.exists():
                target.unlink()
            restored.append(f"deleted {edit['file']}")
        else:
            proc = _git(repo, "checkout", "HEAD", "--", edit["file"].as_posix())
            if proc.returncode != 0:
                restored.append(f"RESTORE FAILED {edit['file']}: {proc.stderr.strip()[:200]}")
            else:
                restored.append(f"restored {edit['file']}")
    return restored


def _qn_set(con: sqlite3.Connection) -> set[str]:
    return {qn for qn, in con.execute("SELECT qualified_name FROM symbols")}


def _pre_edges(con: sqlite3.Connection) -> list[tuple[str, str]]:
    placeholders = ",".join("?" for _ in USAGE_EDGE_TYPES)
    return [
        (src, dst)
        for src, dst in con.execute(
            "SELECT ss.qualified_name, sd.qualified_name FROM edges e"
            " JOIN symbols ss ON ss.fid = e.src_fid JOIN symbols sd ON sd.fid = e.dst_fid"
            f" WHERE e.type IN ({placeholders})",
            USAGE_EDGE_TYPES,
        )
    ]


def _parse_gaps(con: sqlite3.Connection, edited_files: set[str]) -> list[dict]:
    rows = []
    for file_path, kind, detail in con.execute("SELECT file_path, kind, detail FROM coverage_gaps"):
        norm = (file_path or "").replace("\\", "/")
        if norm in edited_files:
            rows.append({"file": norm, "kind": kind, "detail": detail})
    return rows


def _gate_g4(tx: dict, pre_qns: set[str], post_qns: set[str], pre_edges: list[tuple[str, str]]) -> tuple[bool, dict]:
    declared_from = {move["from"] for move in tx["declared_moves"]}
    declared_to = {move["to"] for move in tx["declared_moves"]}
    for move in tx["declared_moves"]:
        if move["from"] not in pre_qns:
            return False, {"reason": "undeclared-change", "detail": f"declared from missing in pre facts: {move['from']}"}
        if move["to"] not in post_qns:
            return False, {"reason": "undeclared-change", "detail": f"declared to missing in post facts: {move['to']}"}
    removed = pre_qns - post_qns
    added = post_qns - pre_qns
    if tx["type"] == "T1":
        if removed != declared_from:
            return False, {
                "reason": "undeclared-change",
                "detail": f"removed symbols not declared: {sorted(removed - declared_from)[:10]}",
                "removed": sorted(removed),
                "added": sorted(added),
            }
        if added != declared_to:
            return False, {
                "reason": "undeclared-change",
                "detail": f"added symbols not declared: {sorted(added - declared_to)[:10]}",
                "removed": sorted(removed),
                "added": sorted(added),
            }
        return True, {"removed": sorted(removed), "added": sorted(added), "declared": len(tx["declared_moves"])}
    if tx["type"] == "T2":
        dangling = sorted(
            {
                f"{src} -> {dst}"
                for src, dst in pre_edges
                if dst in removed and dst not in declared_from and src in post_qns and src != dst
            }
        )
        if dangling:
            return False, {"reason": "dangling-reference", "detail": "removed symbols still referenced by surviving code", "dangling": dangling[:20]}
        return True, {"removed": sorted(removed)[:50], "removed_count": len(removed), "dangling": []}
    return False, {"reason": "skipped"}


def _record(con: sqlite3.Connection, tx: dict, status: str, gates: list[dict]) -> None:
    con.execute(
        "UPDATE transactions SET status = ? WHERE tid = ? AND id = (SELECT MAX(id) FROM transactions WHERE tid = ?)",
        (status, tx["id"], tx["id"]),
    )
    for gate in gates:
        con.execute(
            "INSERT INTO gate_results (gate, status, detail_json) VALUES (?, ?, ?)",
            (gate["gate"], gate["status"], json.dumps(gate.get("detail", {}), ensure_ascii=False)),
        )
    con.commit()


def transact(
    repo: Path,
    tx_path: Path,
    name: str,
    confirm: bool = False,
    timeout: int = 900,
) -> dict:
    tx = load_transaction(tx_path)
    tid_safe = TID_SAFE.sub("_", tx["id"])
    facts_db = repo / ".vrp" / "facts.db"
    post_db = repo / ".vrp" / f"facts.db.post-{tid_safe}"
    verdict_path = repo / ".vrp" / "verdicts" / f"{tid_safe}.json"
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    if post_db.exists():
        post_db.unlink()
    con = sqlite3.connect(facts_db)
    try:
        pre_qns = _qn_set(con)
        pre_edges = _pre_edges(con)
        pre_con = sqlite3.connect(f"file:{facts_db}?mode=ro", uri=True)
        try:
            pre_dead = len(s2_deadcode.dead_code_candidates(pre_con))
        finally:
            pre_con.close()
    finally:
        con.close()

    def finish(status: str, gates: list[dict], failed_gate: str | None, extra: dict) -> dict:
        con = sqlite3.connect(facts_db)
        try:
            _record(con, tx, status, gates)
        finally:
            con.close()
        verdict = {
            "tid": tx["id"],
            "type": tx["type"],
            "summary": tx["summary"],
            "status": status,
            "verdict_at": _utc_now(),
            "failed_gate": failed_gate,
            "gates": gates,
            **extra,
        }
        verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
        return verdict

    con = sqlite3.connect(facts_db)
    try:
        con.execute(
            "INSERT INTO transactions (tid, kind, declared_moves, status, payload_json) VALUES (?, ?, ?, ?, ?)",
            (
                tx["id"],
                tx["type"],
                json.dumps(tx["declared_moves"], ensure_ascii=False),
                "running",
                json.dumps({"summary": tx["summary"], "edits": len(tx["edits"])}, ensure_ascii=False),
            ),
        )
        con.commit()
    finally:
        con.close()

    if tx["type"] == "T4" and not confirm:
        return finish(
            "needs_confirmation",
            [{"gate": "G0", "status": "skipped", "detail": {"reason": "T4 requires --confirm"}}],
            None,
            {"confidence": "low"},
        )

    try:
        apply_edits(repo, tx["edits"])
    except (RuntimeError, OSError) as exc:
        rollback = rollback_edits(repo, tx["edits"])
        return finish(
            "rolled_back",
            [{"gate": "G0", "status": "fail", "detail": {"reason": "apply-edits", "error": str(exc)[:500]}}],
            "G0",
            {"rollback": rollback},
        )

    gates: list[dict] = []
    edited_files = {edit["file"].as_posix() for edit in tx["edits"]}
    failed = None

    s1_cbm.index_repository(repo, name, reindex=True)
    etl.run_etl(repo, name, out_db=post_db)
    post = sqlite3.connect(f"file:{post_db}?mode=ro", uri=True)
    try:
        post_qns = _qn_set(post)
        parse_gaps = _parse_gaps(post, edited_files)
    finally:
        post.close()
    if parse_gaps:
        gates.append({"gate": "G1", "status": "fail", "detail": {"reason": "parse-errors", "gaps": parse_gaps[:20]}})
        failed = failed or "G1"
    else:
        gates.append({"gate": "G1", "status": "pass", "detail": {"symbols_post": len(post_qns)}})

    if failed is None:
        commands = s0_precheck.discover_commands(repo)
        static_commands = {slot: commands.get(slot, []) for slot in ("lint", "typecheck") if commands.get(slot)}
        if any(static_commands.values()):
            results = s4_freeze.run_commands(repo, static_commands, timeout=timeout)
            bad = [r for runs in results.values() for r in runs if r["rc"] != 0]
            if bad:
                gates.append({"gate": "G2", "status": "fail", "detail": {"reason": "static-check", "failed": [{"command": r["command"], "rc": r["rc"]} for r in bad]}})
                failed = failed or "G2"
            else:
                gates.append({"gate": "G2", "status": "pass", "detail": {"commands": [r["command"] for runs in results.values() for r in runs]}})
        else:
            gates.append({"gate": "G2", "status": "skipped", "detail": {"reason": "no lint/typecheck commands discovered"}})

    confidence = "degraded"
    if failed is None:
        test_commands = {"test": commands.get("test", [])} if commands.get("test") else {}
        if test_commands:
            results = s4_freeze.run_commands(repo, test_commands, timeout=timeout)
            bad = [r for r in results.get("test", []) if r["rc"] != 0]
            if bad:
                gates.append({"gate": "G3", "status": "fail", "detail": {"reason": "test", "failed": [{"command": r["command"], "rc": r["rc"], "stderr_tail": r["stderr_tail"][:500]} for r in bad]}})
                failed = failed or "G3"
            else:
                gates.append({"gate": "G3", "status": "pass", "detail": {"commands": [r["command"] for r in results["test"]]}})
                confidence = "high"
        else:
            gates.append({"gate": "G3", "status": "skipped", "detail": {"reason": "no test commands discovered"}})

    if failed is None and tx["type"] in ("T1", "T2"):
        ok, detail = _gate_g4(tx, pre_qns, post_qns, pre_edges)
        gates.append({"gate": "G4", "status": "pass" if ok else "fail", "detail": detail})
        if not ok:
            failed = failed or "G4"
    elif failed is None:
        gates.append({"gate": "G4", "status": "skipped", "detail": {"reason": f"no equivalence claim for {tx['type']}"}})

    if failed is None and tx["type"] in ("T2", "T4"):
        post = sqlite3.connect(f"file:{post_db}?mode=ro", uri=True)
        try:
            post_dead = len(s2_deadcode.dead_code_candidates(post))
        finally:
            post.close()
        if post_dead > pre_dead:
            gates.append({"gate": "G5", "status": "fail", "detail": {"reason": "dead-code-increase", "pre": pre_dead, "post": post_dead}})
            failed = failed or "G5"
        else:
            gates.append({"gate": "G5", "status": "pass", "detail": {"pre": pre_dead, "post": post_dead}})

    if failed is not None:
        rollback = rollback_edits(repo, tx["edits"])
        post_db.unlink(missing_ok=True)
        return finish("rolled_back", gates, failed, {"rollback": rollback, "confidence": confidence})

    post_db.unlink(missing_ok=True)
    con = sqlite3.connect(facts_db)
    try:
        _record(con, tx, "committed", gates)
    finally:
        con.close()
    etl.run_etl(repo, name, out_db=facts_db)
    verdict = {
        "tid": tx["id"],
        "type": tx["type"],
        "summary": tx["summary"],
        "status": "committed",
        "verdict_at": _utc_now(),
        "failed_gate": None,
        "gates": gates,
        "confidence": confidence,
    }
    verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    verdict["commit"] = git_commit_all(repo, f"vrp {tx['id']}: {tx['summary']} [{tx['type']}]")
    verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    return verdict
