"""S3 validators: anchor validation (Unwind active/cosmetic/stale), coverage, evidence gate (v6 section 5)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .s2_astgrep import _anchor_symbol

ACTIVE = "active"
COSMETIC = "cosmetic"
STALE = "stale"
REJECTED = "rejected"
VALID_STATUSES = (ACTIVE, COSMETIC)
FINDINGS_GATE_SOURCE = "ai-gate"
MAX_MISSING_LIST = 200


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def window_text(repo: Path, file_path: str, start_line: int, end_line: int) -> str | None:
    target = repo / file_path
    if not target.is_file():
        return None
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    if start_line < 1 or start_line > len(lines):
        return None
    end = min(end_line, len(lines))
    return "\n".join(lines[start_line - 1 : end])


def symbol_by_qn(con: sqlite3.Connection, qualified_name: str):
    return con.execute(
        "SELECT fid, qualified_name, file_path, start_line, end_line, content_hash FROM symbols"
        " WHERE qualified_name = ? LIMIT 1",
        (qualified_name,),
    ).fetchone()


def add_annotation(
    con: sqlite3.Connection,
    repo: Path,
    qualified_name: str,
    start_line: int,
    end_line: int,
    kind: str,
    body: str,
    author: str = "ai",
) -> dict:
    symbol = symbol_by_qn(con, qualified_name)
    if symbol is None:
        return {"ok": False, "reason": "unknown-qualified_name"}
    file_path = symbol[2]
    if not file_path:
        return {"ok": False, "reason": "symbol-has-no-file"}
    text = window_text(repo, file_path, start_line, end_line)
    if text is None:
        return {"ok": False, "reason": "window-outside-file"}
    sym_start, sym_end = symbol[3], symbol[4]
    if sym_start is not None and sym_end is not None:
        if end_line < sym_start or start_line > sym_end:
            return {"ok": False, "reason": "window-outside-symbol"}
    cur = con.execute(
        "INSERT INTO annotations (qualified_name, file_path, start_line, end_line, symbol_hash, window_hash, author, kind, body, status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (qualified_name, file_path, start_line, end_line, symbol[5], _hash_text(text), author, kind, body, ACTIVE),
    )
    con.commit()
    return {"ok": True, "id": cur.lastrowid}


def _definition_hash_now(repo: Path, file_path: str, start_line, end_line) -> str:
    if not file_path or not start_line or not end_line:
        return ""
    target = repo / file_path
    if not target.is_file():
        return ""
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(int(start_line) - 1, 0)
    end = min(int(end_line), len(lines))
    if start >= end:
        return ""
    return _hash_text("\n".join(lines[start:end]))


def validate_anchors(con: sqlite3.Connection, repo: Path) -> dict:
    counts = {ACTIVE: 0, COSMETIC: 0, STALE: 0}
    reasons: dict[str, int] = {}
    rows = con.execute(
        "SELECT id, qualified_name, file_path, start_line, end_line, symbol_hash, window_hash, status FROM annotations"
    ).fetchall()
    for ann_id, qn, file_path, start, end, symbol_hash, window_hash, status in rows:
        if status == REJECTED:
            continue
        symbol = symbol_by_qn(con, qn)
        if symbol is None:
            new_status, reason = STALE, "symbol-gone"
        else:
            text = window_text(repo, file_path, start, end)
            if text is None:
                new_status, reason = STALE, "window-unreadable"
            elif _hash_text(text) != window_hash:
                new_status, reason = STALE, "window-changed"
            else:
                current_def = _definition_hash_now(repo, symbol[2], symbol[3], symbol[4])
                if symbol_hash and current_def and current_def != symbol_hash:
                    new_status, reason = COSMETIC, ""
                else:
                    new_status, reason = ACTIVE, ""
        if new_status != status:
            con.execute("UPDATE annotations SET status = ? WHERE id = ?", (new_status, ann_id))
        counts[new_status] += 1
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    con.commit()
    detail = {"counts": counts, "stale_reasons": reasons}
    con.execute(
        "INSERT INTO gate_results (gate, status, detail_json) VALUES (?,?,?)",
        ("S3-anchor", "pass" if counts[STALE] == 0 else "warn", json.dumps(detail, ensure_ascii=False)),
    )
    con.commit()
    return detail


def coverage(con: sqlite3.Connection) -> dict:
    total = con.execute(
        "SELECT COUNT(DISTINCT qualified_name) FROM symbols"
        " WHERE label IN ('Function','Method','Class','Interface','Struct','Enum') AND file_path != ''"
    ).fetchone()[0]
    annotated = con.execute(
        "SELECT COUNT(DISTINCT qualified_name) FROM annotations WHERE status IN (?, ?)",
        VALID_STATUSES,
    ).fetchone()[0]
    missing = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT s.qualified_name FROM symbols s"
            " WHERE s.label IN ('Function','Method','Class','Interface','Struct','Enum') AND s.file_path != ''"
            " AND s.qualified_name NOT IN (SELECT qualified_name FROM annotations WHERE status IN (?, ?))"
            " ORDER BY s.qualified_name LIMIT ?",
            (*VALID_STATUSES, MAX_MISSING_LIST),
        )
    ]
    detail = {"total": total, "annotated": annotated, "missing_sample": missing}
    status = "pass" if total > 0 and annotated >= total else "info"
    con.execute(
        "INSERT INTO gate_results (gate, status, detail_json) VALUES (?,?,?)",
        ("S3-coverage", status, json.dumps(detail, ensure_ascii=False)),
    )
    con.commit()
    return detail


def gate_claims(con: sqlite3.Connection, repo: Path, claims: list[dict]) -> dict:
    accepted = 0
    rejected: list[dict] = []
    for i, claim in enumerate(claims):
        verdict = _gate_one(con, repo, claim)
        if verdict is None:
            accepted += 1
        else:
            rejected.append({"index": i, "reason": verdict})
    detail = {"accepted": accepted, "rejected": rejected}
    con.execute(
        "INSERT INTO gate_results (gate, status, detail_json) VALUES (?,?,?)",
        ("S3-evidence-gate", "pass" if not rejected else "warn", json.dumps(detail, ensure_ascii=False)),
    )
    con.commit()
    return detail


def _gate_one(con: sqlite3.Connection, repo: Path, claim: dict) -> str | None:
    kind = claim.get("kind")
    if kind == "annotation":
        result = add_annotation(
            con,
            repo,
            claim.get("qualified_name", ""),
            int(claim.get("start_line", 0)),
            int(claim.get("end_line", 0)),
            claim.get("note_kind", "note"),
            claim.get("body", ""),
        )
        return None if result.get("ok") else result.get("reason", "unknown")
    if kind == "finding":
        file_path = claim.get("file_path", "")
        line = int(claim.get("line", 0))
        quote = str(claim.get("quote", ""))
        target = repo / file_path
        if not file_path or not target.is_file():
            return "unknown-file"
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        if line < 1 or line > len(lines):
            return "line-out-of-range"
        if quote.strip() not in lines[line - 1]:
            return "quote-mismatch"
        anchor = _anchor_symbol(con, file_path, line)
        payload = json.dumps(
            {"quote": quote, "body": claim.get("body", "")}, ensure_ascii=False
        )
        con.execute(
            "INSERT INTO findings (pathology, kind, source, anchor_fid, file_path, start_line, end_line, payload_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                claim.get("pathology", "P1"),
                claim.get("note_kind", "ai-note"),
                FINDINGS_GATE_SOURCE,
                anchor,
                file_path,
                line,
                line,
                payload,
            ),
        )
        con.commit()
        return None
    return "unknown-claim-kind"
