"""S2 P4: dead-code candidates from facts.db in-degree join (v6 section 4).

BB2 (构想v13, #197): candidates carry low/medium/high confidence + readable
reason instead of a binary "unreferenced == dead" verdict. Name-level
false-positive classes (dunder / test hook / entry-point name) are reported
as low rather than silently dropped; matching is whole-word — CGC's substring
incident (`name CONTAINS 'main'` killed `domain_check`/`remainder`/
`maintain_index`) cannot recur (code_finder.py:20-25, #1332). Structural
exclusions (synthetic/test paths) stay hard — flipping the test tree open
measured +23k noise rows on tree-sitter-analyzer.
"""

from __future__ import annotations

import json
import sqlite3

from . import testsig

FINDINGS_SOURCE = "s2-deadcode"
TARGET_LABELS = ("Function", "Method")
ENTRY_NAMES = frozenset({"main", "run", "app", "index", "setup", "teardown", "handler", "cli"})
USAGE_EDGE_TYPES = ("CALLS", "CALL_REFERENCE", "MENTIONS", "SPAWNS")
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def is_candidate(name: str, file_path: str) -> bool:
    """Structural universe test (post-BB2): test/synthetic trees stay out,
    name-level classes flow through for classify() to tier."""
    if not name or not file_path:
        return False
    if testsig.is_synthetic_path(file_path):
        return False
    if testsig.is_test_path(file_path):
        return False
    return True


def called_names(con: sqlite3.Connection) -> set[str]:
    """Names of symbols that have at least one usage in-edge anywhere."""
    return {
        row[0]
        for row in con.execute(
            "SELECT DISTINCT s.name FROM symbols s WHERE s.name != ''"
            " AND EXISTS (SELECT 1 FROM edges e WHERE e.dst_fid = s.fid AND e.type IN (?,?,?,?))",
            USAGE_EDGE_TYPES,
        )
    }


def classify(name: str, called: set[str]) -> tuple[str, str]:
    """(confidence, reason) for a zero-caller candidate (CGC #1332 form).

    Precondition: symbol already passed is_candidate (structural universe).
    Order matters: low classes first, then medium, then high.
    """
    if name.startswith("__") and name.endswith("__"):
        return "low", "dunder - invoked implicitly by the runtime"
    if name.startswith("test_") or name.endswith("_test") or name.lower() == "test":
        return "low", "test hook/prefix - invoked by a test runner"
    if name in ENTRY_NAMES:
        return "low", "entry-point name - typically called externally"
    if name in called:
        return "medium", "same-name symbol elsewhere has callers - possible dynamic dispatch or override"
    return "high", "no callers, no usage hints, no excluding signal"


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
    called = called_names(facts_db)
    by_confidence = {"high": 0, "medium": 0, "low": 0}
    rows = []
    for fid, qn, name, fp, start, end, label in candidates:
        confidence, reason = classify(name, called)
        by_confidence[confidence] += 1
        payload = json.dumps(
            {"qualified_name": qn, "label": label, "confidence": confidence, "reason": reason},
            ensure_ascii=False,
        )
        rows.append(("P4", "dead-code-candidate", FINDINGS_SOURCE, fid, None, fp, start, end, payload))
    facts_db.execute("DELETE FROM findings WHERE source = ?", (FINDINGS_SOURCE,))
    facts_db.executemany(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid, file_path, start_line, end_line, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    facts_db.commit()
    return {"total": len(rows), "by_confidence": by_confidence}
