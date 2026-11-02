"""S5 dossier: SQL view assembly + CBM architecture + budget allocation (v6 s7).

v1 scope (self-built views only): five facts.db SQL views (hotspots / cycles /
dead_code / dup_clusters / coupling), Python-side Tarjan SCC for real cycles,
CBM get_architecture thin wrapper, budget-capped assembly into dossier_views
+ .vrp/dossier.json. repomix / repomap / code-atlas views come in a later batch.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import s1_cbm, s2_deadcode

ARCH_SHARE = 0.4
VIEWS_SHARE = 0.4
SUMMARY_SHARE = 0.2
DEFAULT_BUDGET = 32000
SCC_REPORT_CAP = 20


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rows(con: sqlite3.Connection, sql: str, limit: int | None) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall() if limit is None else cur.fetchmany(limit)
    return [dict(zip(cols, row)) for row in rows]


def hotspots(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return _rows(con, "SELECT * FROM v_hotspots ORDER BY score DESC, qualified_name", limit)


def mutual_cycles(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return _rows(
        con,
        "SELECT sa.qualified_name AS symbol_a, sb.qualified_name AS symbol_b"
        " FROM v_cycles c JOIN symbols sa ON sa.fid = c.fid_a JOIN symbols sb ON sb.fid = c.fid_b"
        " ORDER BY symbol_a, symbol_b",
        limit,
    )


def dead_code(con: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = _rows(con, "SELECT qualified_name, label, file_path, name FROM v_dead_code", None)
    filtered = [r for r in rows if s2_deadcode.is_candidate(r["name"], r["file_path"])]
    return filtered[:limit]


def dup_clusters(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return _rows(
        con,
        "SELECT copies, members, files FROM v_dup_clusters ORDER BY copies DESC, content_hash",
        limit,
    )


def coupling(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return _rows(con, "SELECT file_a, file_b, edge_count FROM v_coupling ORDER BY edge_count DESC, file_a", limit)


def _fid_to_qn(con: sqlite3.Connection) -> dict[str, str]:
    return {fid: qn for fid, qn in con.execute("SELECT fid, qualified_name FROM symbols")}


def strongly_connected(con: sqlite3.Connection, report_cap: int = SCC_REPORT_CAP) -> dict:
    adjacency: dict[str, list[str]] = {}
    for src, dst in con.execute("SELECT src_fid, dst_fid FROM edges"):
        adjacency.setdefault(src, []).append(dst)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    components: list[list[str]] = []
    for root in adjacency:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, ptr = work[-1]
            if ptr == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            descended = False
            neighbors = adjacency.get(node, [])
            while ptr < len(neighbors):
                nxt = neighbors[ptr]
                ptr += 1
                if nxt not in index:
                    work[-1] = (node, ptr)
                    work.append((nxt, 0))
                    descended = True
                    break
                if nxt in on_stack and index[nxt] < low[node]:
                    low[node] = index[nxt]
            if descended:
                continue
            work.pop()
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(component)
            if work:
                parent = work[-1][0]
                if low[node] < low[parent]:
                    low[parent] = low[node]
    qn = _fid_to_qn(con)
    named = sorted(
        (sorted(qn.get(fid, fid) for fid in component) for component in components),
        key=lambda c: (-len(c), c),
    )
    return {
        "scc_count": len(components),
        "max_size": max((len(c) for c in components), default=0),
        "largest": named[:report_cap],
    }


def get_architecture(name: str, timeout: int = 300) -> dict:
    return s1_cbm.run_cli(
        ["get_architecture", "--project", name, "--format", "json"], timeout=timeout
    )


def _findings_summary(con: sqlite3.Connection) -> dict:
    by_source = {source: count for source, count in con.execute("SELECT source, COUNT(*) FROM findings GROUP BY source")}
    by_pathology = {
        pathology: count
        for pathology, count in con.execute("SELECT pathology, COUNT(*) FROM findings GROUP BY pathology")
    }
    return {"by_source": by_source, "by_pathology": by_pathology}


def _coverage_summary(con: sqlite3.Connection) -> dict:
    rows = con.execute("SELECT status, COUNT(*) FROM annotations GROUP BY status").fetchall()
    return {status: count for status, count in rows}


def _fit(payload: dict, cap: int) -> dict:
    if len(json.dumps(payload, ensure_ascii=False)) <= cap:
        return payload
    trimmed = {k: (list(v) if isinstance(v, list) else v) for k, v in payload.items()}
    while len(json.dumps(trimmed, ensure_ascii=False)) > cap:
        lists = [(k, len(v)) for k, v in trimmed.items() if isinstance(v, list) and v]
        if not lists:
            break
        key = max(lists, key=lambda kv: kv[1])[0]
        trimmed[key] = trimmed[key][: max(1, len(trimmed[key]) // 2)]
    return trimmed


def build_dossier(
    repo: Path,
    facts_db: Path,
    name: str,
    budget: int = DEFAULT_BUDGET,
    architecture: dict | None = None,
) -> dict:
    con = sqlite3.connect(f"file:{facts_db}?mode=ro", uri=True)
    con.row_factory = None
    try:
        views_payload = {
            "hotspots": hotspots(con),
            "mutual_cycles": mutual_cycles(con),
            "strongly_connected": strongly_connected(con),
            "dead_code": dead_code(con),
            "dup_clusters": dup_clusters(con),
            "coupling": coupling(con),
        }
        summary_payload = {
            "findings": _findings_summary(con),
            "annotation_status": _coverage_summary(con),
            "counts": {
                table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("symbols", "edges", "test_linkage")
            },
        }
    finally:
        con.close()
    if architecture is None:
        architecture = get_architecture(name)
    arch_cap = max(1, int(budget * ARCH_SHARE))
    views_cap = max(1, int(budget * VIEWS_SHARE))
    summary_cap = max(1, int(budget * SUMMARY_SHARE))
    sections = {
        "architecture": _fit(architecture, arch_cap),
        "sql_views": _fit(views_payload, views_cap),
        "summary": _fit(summary_payload, summary_cap),
    }
    dossier = {"generated_at": _utc_now(), "project": name, "budget": budget, "views": sections}
    con = sqlite3.connect(facts_db)
    try:
        for section, payload in sections.items():
            con.execute(
                "INSERT INTO dossier_views (view, payload_json) VALUES (?, ?)",
                (section, json.dumps(payload, ensure_ascii=False)),
            )
        con.commit()
    finally:
        con.close()
    out = repo / ".vrp" / "dossier.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dossier, ensure_ascii=False, indent=2), encoding="utf-8")
    dossier["out"] = str(out)
    return dossier
