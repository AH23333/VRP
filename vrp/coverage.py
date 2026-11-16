"""#145 完备性标注：coverage_gaps 核验查询面（学 CBM check_index_coverage）。

给 AI 消费者的引用前核验面——引用 graph 里某文件前先问"它被完整索引了吗"
（parse_partial 行区间缺符号/parse_unusable 几乎全文件/not_indexed 排除）。
数据源 facts.db coverage_gaps（etl _etl_coverage 从 CBM index_coverage 落库，
零新增采集）；"Clean is not proof of completeness"（CBM best-effort 哲学——
no_recorded_issue 不证明完备，只证明未记录问题）。
"""

import sqlite3
from pathlib import Path

from . import dbpath

COVERAGE_PATH_CAP = 128        # CBM check_index_coverage paths maxItems 同款
KIND_SAMPLE_CAP = 5            # 盘点面每 kind 样本帽
NOT_INDEXED_PREFIX = "not_indexed"  # CBM COVERAGE_EXCLUDED_PREFIX_LENGTH=11 同款

# status → recommended_action（CBM coverage_recommended_action 全表照抄）
RECOMMENDED_ACTIONS = {
    "unusable": "read_source_directly",
    "partial": "read_ranges_and_verify_scope",
    "unresolved_calls": "read_source_and_verify_calls",
    "skipped": "read_source_directly",
    "excluded": "read_source_or_change_ignore_rules",
    "no_recorded_issue": "use_graph_with_best_effort_caveat",
}

# 多行命中时的判定优先序（CBM coverage_status pass 序照抄：
# PARSE(unusable>partial) > EXCLUDED > SKIPPED > UNRESOLVED）
_STATUS_PASSES = ("unusable", "partial", "excluded", "skipped", "unresolved_calls")


def _connect_ro(repo: Path) -> sqlite3.Connection:
    db = Path(repo) / ".vrp" / "facts.db"
    if not db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {db}")
    return dbpath.ro_connect(db)


def _row_status(kind: str) -> str | None:
    """kind → status 单行判定（CBM coverage_row_status pass 序展开）。"""
    if kind == "parse_unusable":
        return "unusable"
    if kind == "parse_partial":
        return "partial"
    if kind.startswith(NOT_INDEXED_PREFIX):
        return "excluded"
    if kind and kind != "unresolved_calls":
        return "skipped"
    if kind == "unresolved_calls":
        return "unresolved_calls"
    return None


def _rows_status(statuses: list[str]) -> str | None:
    """多行 status 归并：按 CBM pass 优先序取最先命中。"""
    present = set(statuses)
    for target in _STATUS_PASSES:
        if target in present:
            return target
    return None


def _norm_rel(path: str) -> str:
    return (path or "").replace("\\", "/")


def check_coverage(repo: Path, paths: list[str] | None = None, kinds: list[str] | None = None) -> dict:
    """完备性核验/盘点查询面（只读）。

    - paths 非空：**核验面**——逐 path exact+祖先匹配（gap 行是请求路径的
      祖先目录时命中，not_indexed_dir 语义）；exact 行存在只判 exact
      （CBM 精确命中降权祖先同款）；status 六态 + recommended_action。
    - paths 空：**盘点面**——kind 汇总分布 + 每 kind 样本（KIND_SAMPLE_CAP 帽）；
      kinds 过滤细分（过滤也适用于核验面）。
    """
    if paths is not None and len(paths) > COVERAGE_PATH_CAP:
        raise SystemExit(
            f"check_coverage: too many paths ({len(paths)}) - cap {COVERAGE_PATH_CAP} (CBM same)"
        )
    con = _connect_ro(repo)
    try:
        if paths:
            return _check_paths(con, paths, kinds)
        return _summarize(con, kinds)
    finally:
        con.close()


def _check_paths(con: sqlite3.Connection, paths: list[str], kinds: list[str] | None) -> dict:
    kind_filter = set(kinds) if kinds else None
    results = []
    for raw in paths:
        path = _norm_rel(raw)
        rows = con.execute(
            "SELECT file_path, kind, detail FROM coverage_gaps"
            " WHERE file_path = ? OR ? LIKE file_path || '/%'"
            " ORDER BY file_path",
            (path, path),
        ).fetchall()
        if kind_filter is not None:
            rows = [r for r in rows if r[1] in kind_filter]
        exact = [r for r in rows if _norm_rel(r[0]) == path]
        pool = exact if exact else rows  # exact 命中时降权祖先行（CBM 同款）
        matched = []
        statuses: list[str] = []
        for file_path, kind, detail in pool:
            status = _row_status(kind)
            if status:
                statuses.append(status)
            matched.append({
                "file_path": _norm_rel(file_path),
                "kind": kind,
                "detail": detail or "",
                "match": "exact" if _norm_rel(file_path) == path else "ancestor",
            })
        status = _rows_status(statuses) or "no_recorded_issue"
        results.append({
            "path": path,
            "status": status,
            "matched": matched,
            "recommended_action": RECOMMENDED_ACTIONS[status],
        })
    return {
        "level": "coverage",
        "mode": "check",
        "paths": results,
        "note": "best-effort index completeness check (CBM check_index_coverage); "
        "no_recorded_issue is not proof of completeness - clean means no recorded gap",
    }


def _summarize(con: sqlite3.Connection, kinds: list[str] | None) -> dict:
    kind_filter = set(kinds) if kinds else None
    rows = con.execute(
        "SELECT file_path, kind, detail FROM coverage_gaps ORDER BY file_path"
    ).fetchall()
    total = 0
    kind_counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for file_path, kind, _detail in rows:
        total += 1
        if kind_filter is not None and kind not in kind_filter:
            continue
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        bucket = samples.setdefault(kind, [])
        if len(bucket) < KIND_SAMPLE_CAP:
            bucket.append(_norm_rel(file_path))
    kinds_out = {
        kind: {"count": kind_counts[kind], "samples": samples[kind]}
        for kind in sorted(kind_counts)
    }
    return {
        "level": "coverage",
        "mode": "summary",
        "total_rows": total,
        "filtered": bool(kind_filter),
        "kinds": kinds_out,
        "note": "coverage gap inventory by kind (etl ingests CBM index_coverage); "
        "check specific paths with --path for per-path status and recommended action",
    }
