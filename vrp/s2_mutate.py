"""S2 P8: mutation budget sampler using test_linkage pre-filter (v6 section 4)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import s2_deadcode, testsig

TARGET_LABELS = s2_deadcode.TARGET_LABELS


def mutation_targets(con: sqlite3.Connection, budget: int) -> list[dict]:
    if budget <= 0:
        return []
    rows = con.execute(
        "SELECT s.fid, s.qualified_name, s.name, s.file_path, s.start_line, s.end_line,"
        " (SELECT COUNT(*) FROM test_linkage t WHERE t.target_fid = s.fid) AS tests,"
        " (SELECT COUNT(*) FROM edges e WHERE e.dst_fid = s.fid AND e.type IN ('CALLS','CALL_REFERENCE')) AS callers"
        " FROM symbols s"
        " WHERE s.label IN (?, ?) AND s.file_path != ''"
        " AND EXISTS (SELECT 1 FROM test_linkage t WHERE t.target_fid = s.fid)"
        " ORDER BY callers DESC, tests DESC, s.qualified_name",
        TARGET_LABELS,
    ).fetchall()
    out: list[dict] = []
    for fid, qn, name, fp, start, end, tests, callers in rows:
        if testsig.is_synthetic_path(fp) or testsig.is_test_path(fp):
            continue
        if name.startswith("test_") or (name.startswith("__") and name.endswith("__")):
            continue
        out.append(
            {
                "fid": fid,
                "qualified_name": qn,
                "file_path": fp,
                "start_line": start,
                "end_line": end,
                "tests": tests,
                "callers": callers,
            }
        )
        if len(out) >= budget:
            break
    return out


def write_plan(repo: Path, targets: list[dict]) -> Path:
    out_dir = repo / ".vrp"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "mutation-plan.json"
    out.write_text(json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
