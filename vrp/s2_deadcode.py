"""S2 P4: dead-code candidates from facts.db in-degree join (v6 section 4)."""

from __future__ import annotations

import json
import sqlite3

from . import testsig

FINDINGS_SOURCE = "s2-deadcode"
TARGET_LABELS = ("Function", "Method")
ENTRY_NAMES = {"main", "run", "app", "index", "setup", "teardown", "handler", "cli"}
USAGE_EDGE_TYPES = ("CALLS", "CALL_REFERENCE", "MENTIONS", "SPAWNS")


def is_candidate(name: str, file_path: str) -> bool:
    if not name or not file_path:
        return False
    if testsig.is_synthetic_path(file_path):
        return False
    if testsig.is_test_path(file_path):
        return False
    if name.startswith("test_") or name.endswith("_test") or name.lower() == "test":
        return False
    if name.startswith("__") and name.endswith("__"):
        return False
    if name in ENTRY_NAMES:
        return False
    return True


def dead_code_candidates(con: sqlite3.Connection) -> list[tuple]:
    rows = con.execute(
        "SELECT s.fid, s.qualified_name, s.name, s.file_path, s.start_line, s.end_line, s.label"
        " FROM symbols s"
        " WHERE s.label IN (?, ?) AND s.file_path != ''"
        " AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst_fid = s.fid AND e.type IN (?, ?, ?, ?))"
        " AND NOT EXISTS (SELECT 1 FROM usage_hints u WHERE u.dst_fid = s.fid)"
        " AND NOT EXISTS (SELECT 1 FROM test_linkage t WHERE t.target_fid = s.fid)",
        (*TARGET_LABELS, *USAGE_EDGE_TYPES),
    ).fetchall()
    return [r for r in rows if is_candidate(r[2], r[3])]


def scan(facts_db: sqlite3.Connection) -> dict:
    candidates = dead_code_candidates(facts_db)
    rows = []
    for fid, qn, name, fp, start, end, label in candidates:
        payload = json.dumps({"qualified_name": qn, "label": label}, ensure_ascii=False)
        rows.append(("P4", "dead-code-candidate", FINDINGS_SOURCE, fid, None, fp, start, end, payload))
    facts_db.execute("DELETE FROM findings WHERE source = ?", (FINDINGS_SOURCE,))
    facts_db.executemany(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid, file_path, start_line, end_line, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    facts_db.commit()
    return {"total": len(rows)}
