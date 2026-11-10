"""S1.5 ETL: CBM SQLite -> facts.db (v6 section 3 + 12)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from . import curate as curate_mod
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


OWNED_TABLES = (
    "annotations",
    "transactions",
    "gate_results",
    "dossier_views",
    "baseline",
    "runtime_evidence",
    "slices",
    "structure",
    "decisions",
    "fid_ledger",
)


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.commit()


def current_git_head(repo: Path) -> str:
    """HEAD commit of repo, or '' when repo is not a git checkout."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def check_facts_freshness(con: sqlite3.Connection, repo: Path) -> None:
    """Hard-fail (M4) when facts.db was built against a different HEAD.

    Raises RuntimeError - the CLI boundary turns that into an error exit.
    A db without a recorded git_head (pre-freshness build) counts as stale.
    """
    row = con.execute("SELECT value FROM meta WHERE key = 'git_head'").fetchone()
    if row is None:
        raise RuntimeError(
            f"facts.db is stale: no git_head recorded (built before freshness tracking) "
            f"- re-run 'vrp etl {repo}'"
        )
    current = current_git_head(repo)
    if row[0] != current:
        raise RuntimeError(
            f"facts.db is stale: recorded git_head={row[0] or '(none)'} != "
            f"current HEAD={current or '(none)'} - re-run 'vrp etl {repo}'"
        )


def snapshot_owned(db_path: Path) -> dict[str, tuple[list[str], list[tuple]]]:
    """OWNED_TABLES + 非 etl findings 快照。

    每表记录 (cols, rows)——恢复端按列名对齐写入：schema 演化（升级加列/
    调序）时旧快照按名落位，不错位、不硬崩（新列补 NULL，废弃列丢弃）。
    """
    if not db_path.exists():
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    snap: dict[str, tuple[list[str], list[tuple]]] = {}
    try:
        for table in OWNED_TABLES:
            try:
                cur = con.execute(f"SELECT * FROM {table}")
                cols = [d[0] for d in cur.description]
                snap[table] = (cols, cur.fetchall())
            except sqlite3.OperationalError:
                pass
        try:
            cur = con.execute("SELECT * FROM findings WHERE source != 'etl'")
            snap["findings_kept"] = ([d[0] for d in cur.description], cur.fetchall())
        except sqlite3.OperationalError:
            pass
    finally:
        con.close()
    return snap


def _restore_table(dst: sqlite3.Connection, table: str, cols: list[str], rows: list[tuple]) -> str | None:
    """按列名对齐恢复：快照列名 → 新库列序映射（缺源补 NULL，废列丢弃）。

    缺源列撞 NOT NULL 无默认（schema 演化跨列）时整表跳过（SAVEPOINT
    回滚半写）并返回警告——诚实上报优于部分恢复或硬崩。
    """
    new_cols = [r[1] for r in dst.execute(f"PRAGMA table_info({table})")]
    if not new_cols:
        return f"{table}: removed from current schema, snapshot skipped"
    pos = {c: i for i, c in enumerate(cols)}
    mapped = [tuple(row[pos[c]] if c in pos else None for c in new_cols) for row in rows]
    collist = ",".join(f'"{c}"' for c in new_cols)
    placeholders = ",".join("?" * len(new_cols))
    dst.execute("SAVEPOINT restore_table")
    try:
        dst.executemany(f"INSERT OR REPLACE INTO {table} ({collist}) VALUES ({placeholders})", mapped)
    except sqlite3.IntegrityError:
        dst.execute("ROLLBACK TO restore_table")
        dst.execute("RELEASE restore_table")
        return f"{table}: snapshot rows violate current schema constraints, skipped"
    dst.execute("RELEASE restore_table")
    return None


def restore_owned(dst: sqlite3.Connection, snap: dict[str, tuple[list[str], list[tuple]]]) -> list[str]:
    """恢复快照；返回诚实警告清单（schema 演化无法落位的表）。"""
    warnings: list[str] = []
    for table in OWNED_TABLES:
        entry = snap.get(table)
        if not entry:
            continue
        cols, rows = entry
        if rows:
            warn = _restore_table(dst, table, cols, rows)
            if warn:
                warnings.append(warn)
    kept = snap.get("findings_kept")
    if kept:
        cols, rows = kept
        if rows:
            warn = _restore_table(dst, "findings", cols, rows)
            if warn:
                warnings.append(warn)
    return warnings


def run_etl(repo: Path, cbm_name: str, out_db: Path | None = None) -> dict:
    repo = repo.resolve()
    db_path = s1_cbm.find_cbm_db(cbm_name, str(repo))
    if db_path is None:
        raise FileNotFoundError(
            f"CBM db not found for project '{cbm_name}' (nor by repo provenance): "
            f"{s1_cbm.cbm_db_path(cbm_name)}"
        )
    project = db_path.stem
    s1_cbm.verify_cbm_schema(db_path)
    store_meta = s1_cbm.read_store_meta(db_path)
    out_db = out_db or (repo / ".vrp" / "facts.db")
    out_db.parent.mkdir(parents=True, exist_ok=True)
    snap = snapshot_owned(out_db)
    # Build the new database in a same-directory temp file and only swap it
    # over the old one after a successful commit, so a mid-run failure leaves
    # the previous facts.db intact (H2).
    tmp_db = out_db.with_name(f"{out_db.name}.tmp-{os.getpid()}")
    tmp_db.unlink(missing_ok=True)
    dst = sqlite3.connect(tmp_db)
    try:
        dst.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        restore_warnings = restore_owned(dst, snap)

        src = s1_cbm.connect_ro(db_path)
        hasher = _ContentHasher(repo)

        stats: dict[str, int] = {}
        if restore_warnings:
            stats["restore_warnings"] = "; ".join(restore_warnings)
        try:
            fid_by_id, file_by_id, replaced = _etl_symbols(dst, src, hasher, project, repo)
            stats["symbols"] = len(fid_by_id)
            stats.update(_etl_edges(dst, src, project, fid_by_id, file_by_id, replaced))
            stats.update(_etl_coverage(dst, src, project))
            # P-1: 产物噪音提示（在 tmp 库上计算，落盘在 os.replace 之后）
            noise = detect_artifact_noise(dst)
            stats["artifact_hints"] = len(noise["hints"])
            # F2 (curate drift governance): this rebuild is THE moment fids rotate,
            # so the restored curate artifacts are revalidated here - drifted ones
            # get marked stale and the "fake active" window closes. Covers all
            # three write paths (etl / run / transact G1 promote), which all go
            # through run_etl.
            revalidated = curate_mod._revalidate_ingested(dst)
            stats["curate_stale_slices"] = len(revalidated["slices"])
            stats["curate_stale_structure"] = 1 if revalidated["structure"] else 0
            _write_meta(dst, project, store_meta, repo, stats)
            dst.commit()
        finally:
            src.close()
        dst.close()
        os.replace(tmp_db, out_db)
    except Exception:
        dst.close()
        tmp_db.unlink(missing_ok=True)
        raise

    # P-1: hints 落盘 .vrp/artifact-hints.json（提示性产物，每次 etl 覆写）
    (out_db.parent / "artifact-hints.json").write_text(
        json.dumps(noise, ensure_ascii=False, indent=2), encoding="utf-8"
    )

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
    # M1: rows sharing (project, qualified_name) collide with the symbols
    # UNIQUE constraint; INSERT OR REPLACE drops the earlier fid's row, so
    # track those replaced fids to cascade-delete their edges below.
    # Transaction boundary (ruled 2026-11-03): guaranteed by the whole build
    # running on the exclusive temp db with a single commit at the end of
    # run_etl - symbols and edges are already in one transaction; do NOT add
    # an explicit BEGIN here (it would mean switching isolation levels for
    # nominal compliance and new risk).
    # Cascade scope (ruled 2026-11-03): edges plus the edge-family tables
    # (usage_hints / test_linkage / history_coupling / findings_p2) - they
    # reference symbols the same way; do NOT narrow it to the edges table.
    replaced: set[str] = set()
    surviving: dict[tuple[str, str], str] = {}
    for r in rows:
        node_id, label, name, qn, file_path, start_line, end_line, props = r
        rel = _norm(file_path)
        content_hash = hasher.definition_hash(rel, start_line, end_line)
        fid = cast_fid(project, qn or name, content_hash)
        fid_by_id[node_id] = fid
        file_by_id[node_id] = rel
        key = (project, qn or name)
        prev = surviving.get(key)
        if prev is not None and prev != fid:
            replaced.add(prev)
        surviving[key] = fid
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
    # F1 (fid identity ledger): record every fid ever seen (fid -> qn/file) so
    # drifted curate artifacts can be relocated later (curate.drift_report).
    # INSERT OR IGNORE keeps first_seen_at stable and growth bounded by real
    # content churn; survives etl via OWNED_TABLES snapshot/restore.
    now = _now()
    dst.executemany(
        "INSERT OR IGNORE INTO fid_ledger (fid, project, qualified_name, file_path, first_seen_at)"
        " VALUES (?,?,?,?,?)",
        [(r[0], r[1], r[2], r[5], now) for r in symbol_rows],
    )
    return fid_by_id, file_by_id, replaced


def _etl_edges(
    dst: sqlite3.Connection,
    src: sqlite3.Connection,
    project: str,
    fid_by_id: dict[int, str],
    file_by_id: dict[int, str],
    replaced: set[str],
) -> dict:
    stats: dict[str, int] = {
        "edges": 0,
        "usage_hints": 0,
        "history_coupling": 0,
        "test_linkage_cbm": 0,
        "test_linkage_js_ts": 0,
        "test_linkage_rust": 0,
        "findings_p2": 0,
        "dangling_edges": 0,
    }

    edge_rows = []
    usage_rows = []
    history_rows = []
    tests_rows = []
    findings_rows = []
    seen_js_ts: set[tuple[str, str]] = set()
    seen_rust: set[tuple[str, str]] = set()

    for r in src.execute(
        "SELECT source_id, target_id, type, properties FROM edges WHERE project = ?", (project,)
    ):
        src_id, dst_id, etype, props_raw = r
        src_fid = fid_by_id.get(src_id)
        dst_fid = fid_by_id.get(dst_id)
        if not src_fid or not dst_fid:
            stats["dangling_edges"] += 1
            continue
        if src_fid in replaced or dst_fid in replaced:
            # M1 cascade: an edge of a replaced (re-indexed) symbol dies with it.
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
            elif testsig.should_self_build_tests_rust(src_file, dst_file):
                key = (src_fid, dst_fid)
                if key not in seen_rust:
                    seen_rust.add(key)
                    tests_rows.append((src_fid, dst_fid, "etl-rust", props_raw))

    # M1: delete any edge touching a replaced symbol before re-inserting, in
    # the same transaction as the symbol REPLACE above.
    for fid in replaced:
        dst.execute("DELETE FROM edges WHERE src_fid = ? OR dst_fid = ?", (fid, fid))
    dst.executemany(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence, strategy, confidence, line, properties_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        edge_rows,
    )
    dst.executemany("INSERT INTO usage_hints (src_fid, dst_fid, properties_json) VALUES (?,?,?)", usage_rows)
    dst.executemany("INSERT INTO history_coupling (src_fid, dst_fid, properties_json) VALUES (?,?,?)", history_rows)
    dst.executemany("INSERT INTO test_linkage (test_fid, target_fid, source, properties_json) VALUES (?,?,?,?)", tests_rows)
    dst.executemany(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid, file_path, start_line, end_line, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(r[0], r[1], "etl", *r[2:]) for r in findings_rows],
    )

    stats["edges"] = len(edge_rows)
    stats["usage_hints"] = len(usage_rows)
    stats["history_coupling"] = len(history_rows)
    stats["test_linkage_cbm"] = sum(1 for r in tests_rows if r[2] == "cbm")
    stats["test_linkage_js_ts"] = sum(1 for r in tests_rows if r[2] == "etl-js-ts")
    stats["test_linkage_rust"] = sum(1 for r in tests_rows if r[2] == "etl-rust")
    stats["findings_p2"] = len(findings_rows)
    return stats


def orphan_edge_count(con: sqlite3.Connection) -> int:
    """Read-only integrity check (M1): edges whose src/dst no longer resolve
    to a symbol. With the file-level cascade this must be 0."""
    return con.execute(
        "SELECT COUNT(*) FROM edges e"
        " WHERE NOT EXISTS (SELECT 1 FROM symbols s WHERE s.fid = e.src_fid)"
        "    OR NOT EXISTS (SELECT 1 FROM symbols s WHERE s.fid = e.dst_fid)"
    ).fetchone()[0]


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


# P-1（跨项目实测 10/15 项目命中）：产物噪音启发式——生成物/构建产物/压缩库
# 常占索引符号 50%-98%（archify 生成 HTML 页、docs/_build、*.min.js、site/
# web 双轨入库），是 similar_to 噪音与假热点的主体。只提示（hints 写
# .vrp/artifact-hints.json）绝不自动排除：排除决策留给用户，数据保持原样。
ARTIFACT_DIR_SEGMENTS = frozenset({"_build", "dist", "build", "site", "coverage"})
MINIFIED_SUFFIXES = (".min.js", ".min.css", ".min.mjs")
DENSE_FILE_MIN_SYMBOLS = 200
DENSE_FILE_MAX_LINE = 20
UNIFORM_HTML_MIN_FILES = 5
UNIFORM_HTML_MIN_SYMBOLS = 20
ARTIFACT_HINT_CAP = 20


def detect_artifact_noise(con: sqlite3.Connection) -> dict:
    """P-1 产物噪音提示（heuristic hints，非裁决，不排除任何数据）。

    四类独立规则（每类各自成 hint）：
    1. artifact_dir_name   路径含构建产物目录段（_build/dist/build/site/
                           coverage，按匹配段子树聚合）
    2. minified_or_dense   .min.js/.min.css/.min.mjs 命名，或 ≥200 符号挤在
                           ≤20 行（压缩/生成代码签名）
    3. uniform_html_pages  目录内 ≥5 个 HTML 文件符号数全等且 ≥20（生成页
                           签名，如 archify 358×N viewer 页）
    4. same_hash_copies    逐字节相同的多路径入库副本（file_hashes 重复
                           hash，单一聚合 hint + 代表例）

    已知局限（诚实边界）：单行超长的 minified 检测需逐文件读内容，成本不
    成比例，不做——仅靠命名与行密度近似。
    """
    total = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    if total == 0:
        return {"total_symbols": 0, "indexed_files": 0, "hints": []}

    rows = con.execute(
        "SELECT file_path, COUNT(*), MAX(start_line) FROM symbols"
        " WHERE file_path != '' AND file_path NOT GLOB '<*' AND file_path NOT GLOB '>*'"
        " GROUP BY file_path"
    ).fetchall()

    def share(n: int) -> float:
        return round(100.0 * n / total, 1)

    hints: list[dict] = []

    # 1) 构建产物目录段（子树聚合：site/ 覆盖 site/2020/01/... 深层页）
    subtree: dict[str, dict] = {}
    for fpath, n, _ml in rows:
        segs = _norm(fpath).split("/")
        hit = next((i for i, seg in enumerate(segs[:-1]) if seg in ARTIFACT_DIR_SEGMENTS), None)
        if hit is None:
            continue
        prefix = "/".join(segs[: hit + 1])
        entry = subtree.setdefault(prefix, {"files": 0, "symbols": 0})
        entry["files"] += 1
        entry["symbols"] += n
    for d, entry in sorted(subtree.items(), key=lambda kv: -kv[1]["symbols"])[:ARTIFACT_HINT_CAP]:
        hints.append(
            {
                "kind": "artifact_dir_name",
                "dir": d,
                "files": entry["files"],
                "symbols": entry["symbols"],
                "symbol_share_pct": share(entry["symbols"]),
                "note": "build-output directory name - likely generated content indexed as code;"
                " consider excluding from the index",
            }
        )

    # 2) minified / dense 文件（按直接父目录聚合）
    dense: dict[str, dict] = {}
    for fpath, n, ml in rows:
        norm = _norm(fpath)
        is_min = norm.lower().endswith(MINIFIED_SUFFIXES)
        is_dense = ml is not None and n >= DENSE_FILE_MIN_SYMBOLS and ml <= DENSE_FILE_MAX_LINE
        if not (is_min or is_dense):
            continue
        d = norm.rsplit("/", 1)[0] if "/" in norm else "."
        entry = dense.setdefault(d, {"files": 0, "symbols": 0, "examples": []})
        entry["files"] += 1
        entry["symbols"] += n
        if len(entry["examples"]) < 5:
            entry["examples"].append(norm.rsplit("/", 1)[-1])
    for d, entry in sorted(dense.items(), key=lambda kv: -kv[1]["symbols"])[:ARTIFACT_HINT_CAP]:
        hints.append(
            {
                "kind": "minified_or_dense_files",
                "dir": d,
                "files": entry["files"],
                "symbols": entry["symbols"],
                "symbol_share_pct": share(entry["symbols"]),
                "examples": entry["examples"],
                "note": "minified naming or 200+ symbols within 20 lines - bundled/generated code,"
                " weak signal for structural analysis",
            }
        )

    # 3) uniform HTML 生成页签名
    html_counts: dict[str, list[int]] = {}
    for fpath, n, _ml in rows:
        norm = _norm(fpath)
        if not norm.lower().endswith((".html", ".htm")):
            continue
        d = norm.rsplit("/", 1)[0] if "/" in norm else "."
        html_counts.setdefault(d, []).append(n)
    for d, counts in sorted(html_counts.items()):
        if len(counts) < UNIFORM_HTML_MIN_FILES or len(set(counts)) != 1:
            continue
        per_file = counts[0]
        if per_file < UNIFORM_HTML_MIN_SYMBOLS:
            continue
        hints.append(
            {
                "kind": "uniform_html_pages",
                "dir": d,
                "files": len(counts),
                "symbols_per_file": per_file,
                "symbols": len(counts) * per_file,
                "symbol_share_pct": share(len(counts) * per_file),
                "note": "multiple HTML pages with identical symbol counts - generated page"
                " signature (report/viewer output)",
            }
        )

    # 4) 同 hash 副本（单一聚合 hint + 代表例）
    dup = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(c), 0) FROM"
        " (SELECT COUNT(*) AS c FROM file_hashes WHERE hash != '' GROUP BY hash HAVING COUNT(*) > 1)"
    ).fetchone()
    dup_clusters, dup_files = int(dup[0]), int(dup[1])
    if dup_clusters:
        examples = []
        for (h,) in con.execute(
            "SELECT hash FROM file_hashes WHERE hash != '' GROUP BY hash HAVING COUNT(*) > 1"
            " ORDER BY COUNT(*) DESC LIMIT 3"
        ):
            paths = [
                r[0]
                for r in con.execute(
                    "SELECT file_path FROM file_hashes WHERE hash = ? ORDER BY file_path LIMIT 2", (h,)
                )
            ]
            if len(paths) == 2:
                examples.append(paths)
        hints.append(
            {
                "kind": "same_hash_copies",
                "clusters": dup_clusters,
                "files": dup_files,
                "examples": examples,
                "note": "byte-identical file copies committed at multiple paths"
                " (double-tracked output)",
            }
        )

    return {"total_symbols": total, "indexed_files": len(rows), "hints": hints}


def _write_meta(dst: sqlite3.Connection, cbm_name: str, store_meta: dict, repo: Path, stats: dict):
    meta = {
        "vrp_version": __version__,
        "etl_at": _now(),
        "repo": str(repo),
        "cbm_project": cbm_name,
        "cbm_db_uid": store_meta.get("db_uid", ""),
        "cbm_mutation_gen": store_meta.get("mutation_gen", ""),
        "git_head": current_git_head(repo),
        "facts_schema": "1",
    }
    for key, value in stats.items():
        if not key.startswith("_"):
            meta[f"count_{key}"] = str(value)
    dst.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", list(meta.items()))
