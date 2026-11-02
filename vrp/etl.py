"""S1.5 ETL: CBM SQLite -> facts.db (v6 section 3 + 12)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from . import evidence as ev
from . import s1_cbm, testsig

SCHEMA_PATH = Path(__file__).parent / "schema" / "facts_schema.sql"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(path: str | None) -> str:
    return (path or "").replace("\\", "/")


class _ContentHasher:
    def __init__(self, repo: Path):
        self._repo = repo
        self._lines: dict[str, list[str] | None] = {}

    def _load(self, rel: str) -> list[str] | None:
        if rel in self._lines:
            return self._lines[rel]
        lines: list[str] | None = None
        try:
            full = self._repo / rel
            if full.is_file():
                text = full.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()
        except OSError:
            lines = None
        self._lines[rel] = lines
        return lines

    def definition_hash(self, rel: str, start_line, end_line) -> str:
        if not rel or not start_line or not end_line:
            return ""
        lines = self._load(rel)
        if not lines:
            return ""
        start = max(int(start_line) - 1, 0)
        end = min(int(end_line), len(lines))
        if start >= end:
            return ""
        chunk = "\n".join(lines[start:end])
        return hashlib.sha256(chunk.encode("utf-8", errors="replace")).hexdigest()


def cast_fid(project: str, qualified_name: str, content_hash: str) -> str:
    raw = f"{project}|{qualified_name}|{content_hash}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _decode_props(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _copy_table_simple(dst: sqlite3.Connection, rows, table: str, columns: int):
    placeholders = ",".join("?" * columns)
    dst.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})", rows)


def run_etl(repo: Path, cbm_name: str, out_db: Path | None = None) -> dict:
    repo = repo.resolve()
    db_path = s1_cbm.cbm_db_path(cbm_name)
    if not db_path.exists():
        raise FileNotFoundError(f"CBM db not found for project '{cbm_name}': {db_path}")

    store_meta = s1_cbm.read_store_meta(db_path)
    out_db = out_db or (repo / ".vrp" / "facts.db")
    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()
    dst = sqlite3.connect(out_db)
    dst.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    src = s1_cbm.connect_ro(db_path)
    hasher = _ContentHasher(repo)

    stats: dict[str, int] = {}
    try:
        fid_by_id, file_by_id = _etl_symbols(dst, src, hasher, cbm_name, repo)
        stats["symbols"] = len(fid_by_id)
        stats.update(_etl_edges(dst, src, cbm_name, fid_by_id, file_by_id))
        stats.update(_etl_coverage(dst, src, cbm_name))
        _write_meta(dst, cbm_name, store_meta, repo, stats)
        dst.commit()
    finally:
        src.close()
        dst.close()

    stats["facts_db"] = str(out_db)
    return stats


def _etl_symbols(dst: sqlite3.Connection, src: sqlite3.Connection, hasher: _ContentHasher, project: str, repo: Path):
    rows = src.execute(
        "SELECT id, label, name, qualified_name, file_path, start_line, end_line, properties"
        " FROM nodes WHERE project = ?",
        (project,),
    ).fetchall()
    symbol_rows = []
    fid_by_id: dict[int, str] = {}
    file_by_id: dict[int, str] = {}
    for r in rows:
        node_id, label, name, qn, file_path, start_line, end_line, props = r
        rel = _norm(file_path)
        content_hash = hasher.definition_hash(rel, start_line, end_line)
        fid = cast_fid(project, qn or name, content_hash)
        fid_by_id[node_id] = fid
        file_by_id[node_id] = rel
        symbol_rows.append(
            (
                fid,
                project,
                qn or name,
                label,
                name,
                rel,
                start_line,
                end_line,
                content_hash,
                props,
            )
        )
    dst.executemany(
        "INSERT OR REPLACE INTO symbols (fid, project, qualified_name, label, name, file_path,"
        " start_line, end_line, content_hash, properties_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        symbol_rows,
    )
    return fid_by_id, file_by_id


def _etl_edges(dst: sqlite3.Connection, src: sqlite3.Connection, project: str, fid_by_id: dict[int, str], file_by_id: dict[int, str]) -> dict:
    stats: dict[str, int] = {
        "edges": 0,
        "usage_hints": 0,
        "history_coupling": 0,
        "test_linkage_cbm": 0,
        "test_linkage_js_ts": 0,
        "findings_p2": 0,
        "dangling_edges": 0,
    }

    edge_rows = []
    usage_rows = []
    history_rows = []
    tests_rows = []
    findings_rows = []
    seen_js_ts: set[tuple[str, str]] = set()

    for r in src.execute(
        "SELECT source_id, target_id, type, properties FROM edges WHERE project = ?", (project,)
    ):
        src_id, dst_id, etype, props_raw = r
        src_fid = fid_by_id.get(src_id)
        dst_fid = fid_by_id.get(dst_id)
        if not src_fid or not dst_fid:
            stats["dangling_edges"] += 1
            continue
        props = _decode_props(props_raw)
        table, level = ev.route_edge(etype, props.get("strategy"), props.get("confidence"))

        if table == "findings_p2":
            payload = json.dumps({"jaccard": props.get("jaccard"), "same_file": props.get("same_file")})
            findings_rows.append(("P2", "similar_to", src_fid, dst_fid, None, None, None, payload))
        elif table == "history_coupling":
            history_rows.append((src_fid, dst_fid, props_raw))
        elif table == "test_linkage":
            if testsig.is_synthetic_path(file_by_id.get(dst_id, "")):
                continue
            tests_rows.append((src_fid, dst_fid, "cbm", props_raw))
        elif table == "usage_hints":
            usage_rows.append((src_fid, dst_fid, props_raw))
        else:
            edge_rows.append(
                (
                    src_fid,
                    dst_fid,
                    etype,
                    level,
                    props.get("strategy"),
                    props.get("confidence"),
                    props.get("line"),
                    props_raw,
                )
            )

        if etype in ("CALLS", "USAGE"):
            src_file = file_by_id.get(src_id, "")
            dst_file = file_by_id.get(dst_id, "")
            if testsig.should_self_build_tests(src_file, dst_file):
                key = (src_fid, dst_fid)
                if key not in seen_js_ts:
                    seen_js_ts.add(key)
                    tests_rows.append((src_fid, dst_fid, "etl-js-ts", props_raw))

    dst.executemany(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence, strategy, confidence, line, properties_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        edge_rows,
    )
    dst.executemany("INSERT INTO usage_hints (src_fid, dst_fid, properties_json) VALUES (?,?,?)", usage_rows)
    dst.executemany("INSERT INTO history_coupling (src_fid, dst_fid, properties_json) VALUES (?,?,?)", history_rows)
    dst.executemany("INSERT INTO test_linkage (test_fid, target_fid, source, properties_json) VALUES (?,?,?,?)", tests_rows)
    dst.executemany(
        "INSERT INTO findings (pathology, kind, anchor_fid, related_fid, file_path, start_line, end_line, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        findings_rows,
    )

    stats["edges"] = len(edge_rows)
    stats["usage_hints"] = len(usage_rows)
    stats["history_coupling"] = len(history_rows)
    stats["test_linkage_cbm"] = sum(1 for r in tests_rows if r[2] == "cbm")
    stats["test_linkage_js_ts"] = sum(1 for r in tests_rows if r[2] == "etl-js-ts")
    stats["findings_p2"] = len(findings_rows)
    return stats


def _etl_coverage(dst: sqlite3.Connection, src: sqlite3.Connection, project: str) -> dict:
    out = []
    rows = src.execute(
        "SELECT rel_path, kind, detail FROM index_coverage WHERE project = ?", (project,)
    ).fetchall()
    for r in rows:
        out.append((_norm(r[0]), r[1], r[2]))
    dst.executemany("INSERT OR REPLACE INTO coverage_gaps (file_path, kind, detail) VALUES (?,?,?)", out)

    hash_rows = []
    for r in src.execute(
        "SELECT rel_path, sha256 FROM file_hashes WHERE project = ?", (project,)
    ).fetchall():
        hash_rows.append((_norm(r[0]), r[1]))
    dst.executemany("INSERT OR REPLACE INTO file_hashes (file_path, hash) VALUES (?,?)", hash_rows)
    return {"coverage_gaps": len(out), "file_hashes": len(hash_rows)}


def _write_meta(dst: sqlite3.Connection, cbm_name: str, store_meta: dict, repo: Path, stats: dict):
    meta = {
        "vrp_version": __version__,
        "etl_at": _now(),
        "repo": str(repo),
        "cbm_project": cbm_name,
        "cbm_db_uid": store_meta.get("db_uid", ""),
        "cbm_mutation_gen": store_meta.get("mutation_gen", ""),
        "facts_schema": "1",
    }
    for key, value in stats.items():
        if not key.startswith("_"):
            meta[f"count_{key}"] = str(value)
    dst.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", list(meta.items()))
