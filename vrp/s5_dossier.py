"""S5 dossier: view assembly + budget allocation (v6 s7).

Six sections assembled under a task-typed budget profile (T1 equivalent
refactor / T2 cleanup / T3 fix-up / T4 improvement, v2 gate-matrix taxonomy):
architecture (CBM get_architecture), repo_map_brief (code-atlas MCP),
repomap (aider-algorithm port on facts.db), sql_views (five facts.db views),
summary (findings/annotations/counts), raw_pack (repomix artifact pointer).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import s1_cbm, s2_deadcode, s5_cabrief, s5_rawpack, s5_repomap

DEFAULT_BUDGET = 32000
SCC_REPORT_CAP = 20
SCC_MEMBER_CAP = 30
DUP_MEMBERS_CHARS = 300
DEFAULT_TASK = "T4"
TASK_PROFILES: dict[str, dict[str, float]] = {
    "T1": {
        "repomap": 0.30,
        "sql_views": 0.25,
        "architecture": 0.15,
        "repo_map_brief": 0.10,
        "raw_pack": 0.05,
        "summary": 0.15,
    },
    "T2": {
        "sql_views": 0.40,
        "repomap": 0.20,
        "summary": 0.15,
        "architecture": 0.10,
        "repo_map_brief": 0.10,
        "raw_pack": 0.05,
    },
    "T3": {
        "raw_pack": 0.30,
        "sql_views": 0.20,
        "summary": 0.20,
        "repomap": 0.15,
        "architecture": 0.10,
        "repo_map_brief": 0.05,
    },
    "T4": {
        "architecture": 0.25,
        "sql_views": 0.20,
        "repomap": 0.20,
        "repo_map_brief": 0.15,
        "summary": 0.10,
        "raw_pack": 0.10,
    },
}
SECTION_ORDER = ("architecture", "repo_map_brief", "repomap", "sql_views", "summary", "raw_pack")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rows(con: sqlite3.Connection, sql: str, limit: int | None, params: tuple = ()) -> list[dict]:
    cur = con.execute(sql, params)
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
        "SELECT copies, substr(members, 1, ?) AS members, files FROM v_dup_clusters ORDER BY copies DESC, content_hash",
        limit,
        params=(DUP_MEMBERS_CHARS,),
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
        "largest": [
            {"size": len(c), "members": c[:SCC_MEMBER_CAP]}
            for c in named[:report_cap]
        ],
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
        lists = [(k, len(v)) for k, v in trimmed.items() if isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        key = max(lists, key=lambda kv: kv[1])[0]
        trimmed[key] = trimmed[key][: len(trimmed[key]) // 2]
    return trimmed


def _cap_brief(brief: dict, cap: int) -> dict:
    text = brief.get("text")
    if not isinstance(text, str) or len(text) <= cap:
        return brief
    return {**brief, "text": text[:cap] + "\n...[truncated]", "chars": len(text), "truncated": True}


def build_dossier(
    repo: Path,
    facts_db: Path,
    name: str,
    budget: int = DEFAULT_BUDGET,
    task: str = DEFAULT_TASK,
    architecture: dict | None = None,
    skip_external: bool = False,
) -> dict:
    if task not in TASK_PROFILES:
        raise ValueError(f"unknown task: {task!r} (expected one of {', '.join(TASK_PROFILES)})")
    caps = {section: max(1, int(budget * share)) for section, share in TASK_PROFILES[task].items()}
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
        repomap_payload = s5_repomap.render_repomap(con, char_budget=caps["repomap"])
    finally:
        con.close()
    if architecture is not None:
        arch_payload = architecture
    elif skip_external:
        arch_payload = {"status": "skipped", "reason": "external calls disabled"}
    else:
        arch_payload = get_architecture(name)
    if skip_external:
        brief_payload = {"status": "skipped", "reason": "external calls disabled"}
        raw_payload = {"status": "skipped", "reason": "external calls disabled"}
    else:
        brief_payload = _cap_brief(
            s5_cabrief.repo_map_brief(
                repo,
                max_tokens=max(200, min(20000, caps["repo_map_brief"] // 4)),
                timeout=180,
            ),
            caps["repo_map_brief"],
        )
        raw_payload = s5_rawpack.build_raw_pack(repo)
        if raw_payload.get("status") == "ok":
            raw_payload["recommended_read_chars"] = caps["raw_pack"]
    sections = {
        "architecture": _fit(arch_payload, caps["architecture"]),
        "repo_map_brief": brief_payload,
        "repomap": repomap_payload,
        "sql_views": _fit(views_payload, caps["sql_views"]),
        "summary": _fit(summary_payload, caps["summary"]),
        "raw_pack": raw_payload,
    }
    dossier = {
        "generated_at": _utc_now(),
        "project": name,
        "budget": budget,
        "task": task,
        "views": sections,
    }
    con = sqlite3.connect(facts_db)
    try:
        con.execute("DELETE FROM dossier_views")
        for section in SECTION_ORDER:
            con.execute(
                "INSERT INTO dossier_views (view, payload_json) VALUES (?, ?)",
                (section, json.dumps(sections[section], ensure_ascii=False)),
            )
        con.commit()
    finally:
        con.close()
    out = repo / ".vrp" / "dossier.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # L3: same-directory temp file + atomic swap, so a mid-write failure
    # leaves the previous dossier.json intact (H2 pattern).
    tmp = out.with_name(f"{out.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(dossier, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, out)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    dossier["out"] = str(out)
    return dossier
