"""S6 transact: refactor transaction consumer (v4 loop + v2-A gate matrix MVP).

Transaction format is JSON (v2-C sketched YAML; JSON keeps the zero-dependency
rule since the stdlib ships no YAML parser):

    {"id": "tx-0001", "type": "T1", "summary": "rename x",
     "declared_moves": [{"from": "pkg.old", "to": "pkg.new"}],
     "edits": [{"file": "pkg/a.py", "op": "modify", "content": "..."}]}

Flow: clean-tree precheck (enforced inside transact itself: RuntimeError on a
dirty tree -- CLI, MCP tx_run and direct callers all pass through this single
gate; `git add -A` must never sweep unrelated WIP into the tx commit) ->
register tx -> apply edits -> G1 rescan
(CBM reindex + ETL to a side facts.db; same-name reindex prunes deleted
symbols, verified empirically) -> G2 lint/typecheck -> G3 test -> G4
declared_moves / usage-edge equivalence (T1) / dangling references (T2)
cross-checked by code-atlas verify_changes (BROKEN findings fail) and the
difftastic structural signal (content-level: undeclared structural changes
in modified files fail as gate/G4/structural-change; both degrade
non-blocking when unavailable) -> G5 dead-code monotonic (T2/T4); first
failure rolls the working tree back; all pass -> git commit + promote
facts.db (the commit precedes the committed bookkeeping: a failed commit
rolls the tree back and writes an honest "aborted" verdict under gate G6 --
facts.db must never claim committed without a git commit). T4 requires
--confirm. Every run writes .vrp/verdicts/<tid>.json
with stable rule codes (RULE_CODES) on every failing gate. transact_batch
consumes a directory of transaction files in sorted order, stopping at the
first non-committed verdict.

R1' additions: load_verdicts (verdict files -> sorted list; the dossier
evidence section embeds the open ones so the next AI round must see them,
and `vrp verdicts` exposes them on the CLI) and preview_transaction
(--preview: per-edit unified diff via difflib plus the difftastic
structural signal as a tool group - difflib always, difftastic when the
binary exists, structural per-file degrade never blocks; no gates, no
tree mutation, no git).
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import s0_precheck, s1_cbm, s2_deadcode, s4_freeze, s6_difftastic, etl
from . import dbpath, procs
from .s5_cabrief import call_code_atlas

TX_TYPES = ("T1", "T2", "T3", "T4")
EDIT_OPS = ("create", "modify", "delete")
USAGE_EDGE_TYPES = ("CALLS", "CALL_REFERENCE", "MENTIONS", "SPAWNS")
TID_SAFE = re.compile(r"[^A-Za-z0-9._-]")

RULE_CODES = {
    ("G0", "apply-edits"): "gate/G0/apply-edits",
    ("G1", "parse-errors"): "gate/G1/parse-errors",
    ("G2", "static-check"): "gate/G2/static-check",
    ("G3", "test"): "gate/G3/test",
    ("G4", "undeclared-change"): "gate/G4/undeclared-change",
    ("G4", "dangling-reference"): "gate/G4/dangling-reference",
    ("G4", "edge-mismatch"): "gate/G4/edge-mismatch",
    ("G4", "verify-broken"): "gate/G4/verify-broken",
    ("G4", "structural-change"): "gate/G4/structural-change",
    ("G5", "dead-code-increase"): "gate/G5/dead-code-increase",
    ("G6", "commit-failed"): "gate/G6/commit-failed",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_rel(file_value) -> Path:
    if not isinstance(file_value, str) or not file_value.strip():
        raise ValueError("edit.file must be a non-empty string")
    file_value = file_value.strip()
    normalized = file_value.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError(f"edit.file must be repo-relative: {file_value}")
    path = Path(normalized)
    if path.drive or path.root or path.is_absolute() or ".." in path.parts:
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


def _git(repo: Path, *args: str) -> dict:
    """git 单步调用（procs.run_tree 树杀收编，R-AP）。

    超时（timed_out）响亮 RuntimeError——事务路径（git_clean 前置 /
    commit 三步 / 回滚 checkout）任何一步挂死都不许静默半步；修前裸
    TimeoutExpired 会穿 mcp _guard 捕获元组（SystemExit/ValueError/
    RuntimeError/sqlite3.Error）炸 server。
    """
    res = procs.run_tree(["git", *args], cwd=repo, timeout=60)
    if res["timed_out"]:
        raise RuntimeError(f"git {' '.join(args)} timed out after 60s (tree killed)")
    return res


VRP_ARTIFACT_DIRS = (".vrp/", ".codebase-memory/", ".code-atlas/")


def _is_vrp_artifact(path: str) -> bool:
    norm = path.strip().strip('"')
    if " -> " in norm:
        norm = norm.split(" -> ")[-1]
    return norm.startswith(VRP_ARTIFACT_DIRS)


def git_clean(repo: Path) -> bool:
    # -c core.quotepath=false：非 ASCII 路径原样输出（默认会被引号+八进制
    # 转义，解析出的路径是乱码——R-N）。verdict 两态下本就正确，此 flag
    # 保真路径字面量。
    res = _git(repo, "-c", "core.quotepath=false", "status", "--porcelain")
    if res["returncode"] != 0:
        return False
    for line in res["stdout"].splitlines():
        if line.strip() and not _is_vrp_artifact(line[3:]):
            return False
    return True


def git_head(repo: Path) -> str:
    res = _git(repo, "rev-parse", "HEAD")
    return res["stdout"].strip() if res["returncode"] == 0 else ""


def git_commit_all(repo: Path, message: str) -> str:
    """add/reset/commit 三步 rc 全查（R-N：add rc 未查的事务完整性洞）。

    git add -A 失败（index.lock 争用等）若静默放过，commit 会提交旧索引——
    索引里若有别人 staged 未提交的内容就被静默毒化进事务提交；即使索引
    干净也会以误导性的 "nothing to commit" 报错掩盖真根因。reset 失败
    同理（工件目录混进事务提交）。三步任一失败响亮报错，事务不做半步。
    """
    res = _git(repo, "add", "-A")
    if res["returncode"] != 0:
        raise RuntimeError(f"git add failed: {res['stderr'].strip()[:500]}")
    for artifact in (".vrp", ".codebase-memory", ".code-atlas"):
        res = _git(repo, "reset", "-q", "HEAD", "--", artifact)
        if res["returncode"] != 0:
            raise RuntimeError(f"git reset failed ({artifact}): {res['stderr'].strip()[:500]}")
    res = _git(repo, "commit", "-m", message)
    if res["returncode"] != 0:
        raise RuntimeError(f"git commit failed: {res['stderr'].strip()[:500]}")
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
            # Path() 包一层：apply_edits 接受 str file（JSON 直出形态），
            # 回滚是恢复路径，不能在类型上崩（R-N API 对称性）。
            proc = _git(repo, "checkout", "HEAD", "--", Path(edit["file"]).as_posix())
            if proc["returncode"] != 0:
                restored.append(f"RESTORE FAILED {edit['file']}: {proc['stderr'].strip()[:200]}")
            else:
                restored.append(f"restored {edit['file']}")
    return restored


def _qn_set(con: sqlite3.Connection) -> set[str]:
    return {qn for qn, in con.execute("SELECT qualified_name FROM symbols")}


def _usage_edges(con: sqlite3.Connection) -> list[tuple[str, str, str]]:
    placeholders = ",".join("?" for _ in USAGE_EDGE_TYPES)
    return [
        (src, etype, dst)
        for src, etype, dst in con.execute(
            "SELECT ss.qualified_name, e.type, sd.qualified_name FROM edges e"
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


def _gate_g4(
    tx: dict,
    pre_qns: set[str],
    post_qns: set[str],
    pre_edges: list[tuple[str, str, str]],
    post_edges: list[tuple[str, str, str]],
) -> tuple[bool, dict]:
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
        moved = {move["from"]: move["to"] for move in tx["declared_moves"]}
        names = set(moved) | set(moved.values())
        pre_incident = {(s, t, d) for s, t, d in pre_edges if s in moved or d in moved}
        post_incident = {(s, t, d) for s, t, d in post_edges if s in names or d in names}
        expected = {(moved.get(s, s), t, moved.get(d, d)) for s, t, d in pre_incident}
        missing = expected - post_incident
        unexpected = post_incident - expected
        if missing or unexpected:
            return False, {
                "reason": "edge-mismatch",
                "detail": "usage edges around declared moves not preserved",
                "missing": sorted(f"{s} -{t}-> {d}" for s, t, d in missing)[:10],
                "unexpected": sorted(f"{s} -{t}-> {d}" for s, t, d in unexpected)[:10],
            }
        return True, {"removed": sorted(removed), "added": sorted(added), "declared": len(tx["declared_moves"])}
    if tx["type"] == "T2":
        dangling = sorted(
            {
                f"{src} -> {dst}"
                for src, _, dst in pre_edges
                if dst in removed and dst not in declared_from and src in post_qns and src != dst
            }
        )
        if dangling:
            return False, {"reason": "dangling-reference", "detail": "removed symbols still referenced by surviving code", "dangling": dangling[:20]}
        return True, {"removed": sorted(removed)[:50], "removed_count": len(removed), "dangling": []}
    return False, {"reason": "skipped"}


def _verify_changes(repo: Path, timeout: int = 180) -> dict:
    result = call_code_atlas(repo, "verify_changes", {"max_tokens": 20000}, timeout)
    if result["status"] != "ok":
        return {"status": result["status"], "reason": result.get("reason", "")[:300]}
    broken = []
    checks = []
    for line in result["text"].splitlines():
        stripped = line.strip()
        if stripped.startswith("BROKEN "):
            broken.append(stripped[len("BROKEN "):][:300])
        elif stripped.startswith("CHECK "):
            checks.append(stripped[len("CHECK "):][:300])
    return {"status": "ok", "broken": broken, "checks": checks, "chars": result["chars"]}


def _attach_codes(gates: list[dict]) -> list[dict]:
    for gate in gates:
        if gate.get("status") != "fail":
            continue
        detail = gate.get("detail") or {}
        code = RULE_CODES.get((gate.get("gate"), detail.get("reason")))
        if code:
            detail["code"] = code
    return gates


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
    if not git_clean(repo):
        # 脏树前置：git add -A 会把无关 WIP 卷进 tx 提交——CLI 有预检但
        # MCP tx_run / 直调没有，故在唯一代码路径 transact 内强制（2026-11-09
        # 二轮 review 实证：脏树下 T2 可一路走到 committed，用户 WIP 被提交）。
        raise RuntimeError(f"working tree not clean - commit or stash first: {repo}")
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
        pre_edges = _usage_edges(con)
        pre_con = dbpath.ro_connect(facts_db)
        try:
            pre_dead = len(s2_deadcode.dead_code_candidates(pre_con))
        finally:
            pre_con.close()
    finally:
        con.close()

    def finish(status: str, gates: list[dict], failed_gate: str | None, extra: dict) -> dict:
        _attach_codes(gates)
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
        verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
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
    except (RuntimeError, OSError, KeyboardInterrupt) as exc:
        # Ctrl-C 与异常同样走恢复边界（KI 非 Exception 子类，窄捕会让
        # apply 窗口的 Ctrl-C 绕过 rollback 裸逃——running 残行 + 无 verdict）。
        # KI 恢复后仍写 verdict 再原样重抛（保 Ctrl-C 退出语义）。
        if isinstance(exc, KeyboardInterrupt):
            rollback = rollback_edits(repo, tx["edits"])
            finish(
                "rolled_back",
                [{"gate": "G0", "status": "fail", "detail": {"reason": "apply-edits", "error": "KeyboardInterrupt (user interrupt)"}}],
                "G0",
                {"rollback": rollback},
            )
            raise
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

    try:
        s1_cbm.index_repository(repo, name, reindex=True)
        etl.run_etl(repo, name, out_db=post_db)
        post = dbpath.ro_connect(post_db)
        try:
            post_qns = _qn_set(post)
            post_edges = _usage_edges(post)
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
            ok, detail = _gate_g4(tx, pre_qns, post_qns, pre_edges, post_edges)
            if ok:
                verify = _verify_changes(repo)
                if verify["status"] == "ok":
                    detail["verify_changes"] = {"broken": len(verify["broken"]), "checks": len(verify["checks"])}
                    if verify["broken"]:
                        ok = False
                        detail = {"reason": "verify-broken", "broken": verify["broken"][:20], "checks": verify["checks"][:20]}
                else:
                    detail["verify_changes"] = verify
            if ok:
                structural = s6_difftastic.check_edits(repo, tx, facts_db, post_db)
                if structural["status"] == "fail":
                    ok = False
                    detail = {
                        "reason": "structural-change",
                        "unaccounted": structural["unaccounted"][:10],
                        "checked": structural.get("checked", 0),
                    }
                else:
                    detail["difftastic"] = structural
            gates.append({"gate": "G4", "status": "pass" if ok else "fail", "detail": detail})
            if not ok:
                failed = failed or "G4"
        elif failed is None:
            gates.append({"gate": "G4", "status": "skipped", "detail": {"reason": f"no equivalence claim for {tx['type']}"}})

        if failed is None and tx["type"] in ("T2", "T4"):
            post = dbpath.ro_connect(post_db)
            try:
                post_dead = len(s2_deadcode.dead_code_candidates(post))
            finally:
                post.close()
            if post_dead > pre_dead:
                gates.append({"gate": "G5", "status": "fail", "detail": {"reason": "dead-code-increase", "pre": pre_dead, "post": post_dead}})
                failed = failed or "G5"
            else:
                gates.append({"gate": "G5", "status": "pass", "detail": {"pre": pre_dead, "post": post_dead}})
    except (Exception, KeyboardInterrupt) as exc:
        # Crash between apply_edits and commit: restore the tree, mark the
        # transaction aborted, and still write a verdict (H1).
        # Ctrl-C（KeyboardInterrupt）同样走恢复边界——KI 非 Exception 子类，
        # 只捕 Exception 会让 gate 窗口的 Ctrl-C 绕过 rollback 裸逃：
        # 脏树 + "running" 残行 + 无 verdict。恢复后原样重抛（保 Ctrl-C
        # 退出语义），内部异常仍按原路径 return verdict。
        rollback = rollback_edits(repo, tx["edits"])
        post_db.unlink(missing_ok=True)
        crashed = f"G{len(gates) + 1}"
        error_text = str(exc)[:500] if not isinstance(exc, KeyboardInterrupt) else "KeyboardInterrupt (user interrupt)"
        gates.append({"gate": crashed, "status": "fail", "detail": {"reason": "gate-crash", "error": error_text}})
        verdict = finish("aborted", gates, crashed, {"rollback": rollback})
        if isinstance(exc, KeyboardInterrupt):
            raise
        return verdict

    if failed is not None:
        rollback = rollback_edits(repo, tx["edits"])
        post_db.unlink(missing_ok=True)
        return finish("rolled_back", gates, failed, {"rollback": rollback, "confidence": confidence})

    post_db.unlink(missing_ok=True)
    try:
        commit = git_commit_all(repo, f"vrp {tx['id']}: {tx['summary']} [{tx['type']}]")
    except RuntimeError as exc:
        # 提交先于记账：commit 失败（如全 no-op edits 无可提交）绝不能留下
        # DB=committed 却无 git 提交的假象——回滚树 + 诚实 aborted 裁决（G6）。
        rollback = rollback_edits(repo, tx["edits"])
        gates.append({"gate": "G6", "status": "fail", "detail": {"reason": "commit-failed", "error": str(exc)[:500]}})
        return finish("aborted", gates, "G6", {"rollback": rollback})
    _attach_codes(gates)
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
        "commit": commit,
    }
    verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return verdict


def transact_batch(
    repo: Path,
    tx_dir: Path,
    name: str,
    confirm: bool = False,
    timeout: int = 900,
    continue_on_fail: bool = False,
) -> dict:
    paths = sorted(Path(tx_dir).glob("*.json"))
    verdicts = []
    stopped_at = None
    for path in paths:
        verdict = transact(repo, path, name, confirm=confirm, timeout=timeout)
        verdicts.append(verdict)
        if verdict["status"] != "committed" and not continue_on_fail:
            stopped_at = path.name
            break
    return {
        "total": len(paths),
        "count": len(verdicts),
        "committed": sum(1 for v in verdicts if v["status"] == "committed"),
        "statuses": [v["status"] for v in verdicts],
        "stopped_at": stopped_at,
        "verdicts": verdicts,
    }


def load_verdicts(repo: Path) -> list[dict]:
    """Read .vrp/verdicts/*.json sorted oldest-first by verdict_at.

    Unreadable files become an honest placeholder entry (status
    "unreadable") instead of being silently dropped.
    """
    vdir = repo / ".vrp" / "verdicts"
    out: list[dict] = []
    if not vdir.is_dir():
        return out
    for path in sorted(vdir.glob("*.json")):
        try:
            verdict = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            out.append({"tid": path.stem, "status": "unreadable", "verdict_at": "", "gates": []})
            continue
        if not isinstance(verdict, dict):
            # R-AF: valid-JSON-non-dict files (list/str/number) must get the
            # same honest placeholder as broken JSON -- the docstring promise
            # "instead of being silently dropped" covers shape failures too.
            out.append({"tid": path.stem, "status": "unreadable", "verdict_at": "", "gates": []})
            continue
        out.append(verdict)
    out.sort(key=lambda v: (v.get("verdict_at") or "", v.get("tid") or ""))
    return out


STRUCTURAL_PREVIEW_CAP = 10
STRUCTURAL_PREVIEW_TIMEOUT = 20


def _unified(old: str | None, new: str | None, fromfile: str, tofile: str) -> str:
    a = [] if old is None else old.splitlines(keepends=True)
    b = [] if new is None else new.splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile=fromfile, tofile=tofile))


def preview_transaction(repo: Path, tx_path: Path) -> dict:
    """R1' --preview: per-edit unified diff, no gates, no tree mutation.

    Tool group (v7 r4 1.6): difflib always produces the diff; difftastic,
    when the binary exists, adds a per-modify-file structural signal
    ("unchanged" = whitespace/comment-only churn) capped at
    STRUCTURAL_PREVIEW_CAP files; any per-file degrade never blocks.
    """
    tx = load_transaction(tx_path)
    binary = s6_difftastic.find_difftastic()
    structural_runs = 0
    edits_report: list[dict] = []
    for edit in tx["edits"]:
        rel = edit["file"]
        target = repo / rel
        entry: dict = {"file": rel.as_posix(), "op": edit["op"]}
        if edit["op"] == "create":
            if target.exists():
                entry["ok"] = False
                entry["error"] = "create target already exists"
            else:
                entry["ok"] = True
                entry["diff"] = _unified(None, edit["content"], "/dev/null", f"b/{rel.as_posix()}")
        elif edit["op"] == "modify":
            if not target.is_file():
                entry["ok"] = False
                entry["error"] = "modify target missing"
            else:
                entry["ok"] = True
                old = target.read_text(encoding="utf-8", errors="replace")
                entry["diff"] = _unified(old, edit["content"], f"a/{rel.as_posix()}", f"b/{rel.as_posix()}")
                if not binary:
                    entry["structural"] = "unavailable"
                elif structural_runs >= STRUCTURAL_PREVIEW_CAP:
                    entry["structural"] = "skipped-cap"
                else:
                    structural_runs += 1
                    with tempfile.TemporaryDirectory(prefix="vrp-preview-") as tmp:
                        new_path = Path(tmp) / rel.name
                        new_path.write_text(edit["content"], encoding="utf-8", newline="\n")
                        run = s6_difftastic.run_difftastic(binary, target, new_path, timeout=STRUCTURAL_PREVIEW_TIMEOUT)
                    if run.get("ok"):
                        entry["structural"] = run["parsed"].get("status", "degraded")
                    else:
                        entry["structural"] = "degraded"
        else:
            if not target.is_file():
                entry["ok"] = False
                entry["error"] = "delete target missing"
            else:
                entry["ok"] = True
                old = target.read_text(encoding="utf-8", errors="replace")
                entry["diff"] = _unified(old, None, f"a/{rel.as_posix()}", "/dev/null")
        edits_report.append(entry)
    return {
        "tid": tx["id"],
        "type": tx["type"],
        "summary": tx["summary"],
        "declared_moves": tx["declared_moves"],
        "edits": edits_report,
        "structural_engine": "difftastic" if binary else "difflib-only",
        "ok": all(entry.get("ok") for entry in edits_report),
    }
