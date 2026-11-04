"""导航接口族 nav_*（构想v7 §1.4.2）——不同深度/颗粒度查询，全链可查防浅层。

分层：
- nav_search       符号搜索（AI 入口），返回 fid 句柄
- nav_architecture L0 全局地图（structure 表优先；缺失时目录聚合兜底）
- nav_module       L1 文件级：符号清单 + 病理摘要，返回 fid 句柄
- nav_symbol       L2 符号级：签名/行号/调用关系/病理，返回 edge 句柄
- query_facts      轻量直查（mode=ro + SELECT 白名单校验）

句柄语义：fid = sha256(project|qualified_name|content_hash)——同一 facts.db 快照内稳定，
事务提交（promote-etl）后 content 变则 fid 变，跨事务需重新 nav_search。
只读连接不受 M4 freshness 硬拒限制（同 status 直连）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

MODULE_SYMBOL_CAP = 200
MODULE_FINDINGS_CAP = 20
SYMBOL_NEIGHBOR_CAP = 20
SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 100
ARCH_MODULES_CAP = 50
QUERY_MAX_ROWS = 100

_SELECT_HEADS = ("select", "with")


def _connect_ro(repo: Path) -> sqlite3.Connection:
    db = Path(repo) / ".vrp" / "facts.db"
    if not db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {db}")
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def nav_search(repo: Path, query: str, limit: int = SEARCH_DEFAULT_LIMIT) -> dict:
    """符号搜索：name 精确命中 > name 前缀 > 其余，按 qualified_name 稳定排序。"""
    if not query.strip():
        raise SystemExit("nav_search: query must be non-empty")
    limit = max(1, min(int(limit), SEARCH_MAX_LIMIT))
    like = f"%{query.strip()}%"
    exact = query.strip()
    con = _connect_ro(repo)
    try:
        rows = con.execute(
            """
            SELECT fid, qualified_name, label, name, file_path, start_line
            FROM symbols
            WHERE (name LIKE ? OR qualified_name LIKE ?)
              AND file_path != ''
              AND file_path NOT GLOB '<*'
              AND file_path NOT GLOB '>*'
            ORDER BY CASE
                WHEN name = ? THEN 0
                WHEN name LIKE ? THEN 1
                WHEN qualified_name = ? THEN 2
                ELSE 3 END, qualified_name
            LIMIT ?
            """,
            (like, like, exact, f"{exact}%", exact, limit),
        ).fetchall()
        results = [dict(r) for r in rows]
    finally:
        con.close()
    return {
        "level": "search",
        "query": query.strip(),
        "count": len(results),
        "results": results,
        "note": "fid 句柄供 nav_symbol 下钻；跨事务（promote-etl）后 fid 失效需重查",
    }


def nav_architecture(repo: Path) -> dict:
    """L0 全局地图。structure 表（R2'）缺失时按目录聚合兜底。"""
    con = _connect_ro(repo)
    try:
        if _has_table(con, "structure"):
            return _architecture_from_structure(con)
        return _architecture_dir_fallback(con)
    finally:
        con.close()


def _architecture_dir_fallback(con: sqlite3.Connection) -> dict:
    sym_by_file: dict[str, int] = {}
    for row in con.execute(
        """
        SELECT file_path, COUNT(*) AS n FROM symbols
        WHERE file_path != '' AND file_path NOT GLOB '<*' AND file_path NOT GLOB '>*'
        GROUP BY file_path
        """
    ):
        sym_by_file[row["file_path"].replace("\\", "/")] = row["n"]
    find_by_file: dict[str, int] = {}
    for row in con.execute(
        "SELECT file_path, COUNT(*) AS n FROM findings WHERE file_path IS NOT NULL GROUP BY file_path"
    ):
        find_by_file[row["file_path"].replace("\\", "/")] = row["n"]
    dirs: dict[str, dict] = {}
    for fpath, n in sym_by_file.items():
        d = fpath.rsplit("/", 1)[0] if "/" in fpath else "."
        slot = dirs.setdefault(d, {"path": d, "files": 0, "symbols": 0, "findings": 0})
        slot["files"] += 1
        slot["symbols"] += n
    for fpath, n in find_by_file.items():
        d = fpath.rsplit("/", 1)[0] if "/" in fpath else "."
        if d in dirs:
            dirs[d]["findings"] += n
    modules = sorted(
        dirs.values(), key=lambda m: (-m["symbols"], m["path"])
    )[:ARCH_MODULES_CAP]
    return {
        "level": "L0",
        "fallback": "dir-aggregation",
        "total_symbols": sum(sym_by_file.values()),
        "total_findings": sum(find_by_file.values()),
        "modules": modules,
        "note": "structure 表缺失（R2' curate 后填充），目录聚合兜底；"
        "module 下钻句柄 = file_path（nav_module）",
    }


def _architecture_from_structure(con: sqlite3.Connection) -> dict:
    rows = [dict(r) for r in con.execute("SELECT * FROM structure ORDER BY path")]
    return {
        "level": "L0",
        "fallback": None,
        "structures": rows,
        "note": "structure 表为 AI 梳理固化产物（R2' curate 验收入库）",
    }


def nav_module(repo: Path, module_id: str, limit: int = MODULE_SYMBOL_CAP) -> dict:
    """L1 文件级：module_id = file_path（repo 相对，正反斜杠均可）。"""
    module_id = module_id.strip().replace("\\", "/")
    if not module_id:
        raise SystemExit("nav_module: module_id must be non-empty")
    con = _connect_ro(repo)
    try:
        rows = con.execute(
            """
            SELECT fid, qualified_name, label, name, start_line, end_line
            FROM symbols WHERE file_path = ? ORDER BY start_line, qualified_name LIMIT ?
            """,
            (module_id, max(1, int(limit))),
        ).fetchall()
        if not rows:
            raise SystemExit(f"nav_module: no symbols for module_id: {module_id}")
        symbols = [dict(r) for r in rows]
        frows = con.execute(
            """
            SELECT pathology, kind, source, start_line, end_line
            FROM findings WHERE file_path = ? ORDER BY pathology, start_line LIMIT ?
            """,
            (module_id, MODULE_FINDINGS_CAP),
        ).fetchall()
        findings = [dict(r) for r in frows]
        by_path: dict[str, int] = {}
        for f in findings:
            by_path[f["pathology"]] = by_path.get(f["pathology"], 0) + 1
        counts = con.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM edges e JOIN symbols s ON s.fid = e.src_fid WHERE s.file_path = ?) AS edges_out,
              (SELECT COUNT(*) FROM edges e JOIN symbols s ON s.fid = e.dst_fid WHERE s.file_path = ?) AS edges_in
            """,
            (module_id, module_id),
        ).fetchone()
    finally:
        con.close()
    return {
        "level": "L1",
        "module_id": module_id,
        "symbols": symbols,
        "findings": findings,
        "findings_by_pathology": by_path,
        "edges_out": counts["edges_out"],
        "edges_in": counts["edges_in"],
        "note": "fid 句柄供 nav_symbol 下钻",
    }


def nav_symbol(repo: Path, fid: str) -> dict:
    """L2 符号级：签名/行号/调用关系/关联病理。"""
    fid = fid.strip()
    con = _connect_ro(repo)
    try:
        row = con.execute(
            """
            SELECT fid, qualified_name, label, name, file_path, start_line, end_line, content_hash
            FROM symbols WHERE fid = ?
            """,
            (fid,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"nav_symbol: unknown fid: {fid[:16]}...")
        symbol = dict(row)
        callers = [
            dict(r)
            for r in con.execute(
                """
                SELECT s.fid, s.qualified_name, e.type, e.confidence
                FROM edges e JOIN symbols s ON s.fid = e.src_fid
                WHERE e.dst_fid = ? ORDER BY e.type, s.qualified_name LIMIT ?
                """,
                (fid, SYMBOL_NEIGHBOR_CAP),
            )
        ]
        callees = [
            dict(r)
            for r in con.execute(
                """
                SELECT s.fid, s.qualified_name, e.type, e.confidence
                FROM edges e JOIN symbols s ON s.fid = e.dst_fid
                WHERE e.src_fid = ? ORDER BY e.type, s.qualified_name LIMIT ?
                """,
                (fid, SYMBOL_NEIGHBOR_CAP),
            )
        ]
        findings = [
            dict(r)
            for r in con.execute(
                """
                SELECT pathology, kind, source, start_line, end_line
                FROM findings WHERE anchor_fid = ? ORDER BY pathology LIMIT ?
                """,
                (fid, MODULE_FINDINGS_CAP),
            )
        ]
    finally:
        con.close()
    return {
        "level": "L2",
        "symbol": symbol,
        "callers": callers,
        "callees": callees,
        "findings": findings,
        "slices": None,
        "note": "callers/callees 的 fid 句柄可继续 nav_symbol 下钻（全链可查）；"
        "slices 待 R2' curate 入库后填充",
    }


def query_facts(repo: Path, sql: str, params: str = "", max_rows: int = QUERY_MAX_ROWS) -> dict:
    """轻量直查 facts.db：mode=ro + 首关键词白名单（SELECT/WITH）+ 单语句校验。

    params 为可选 JSON 数组字符串（位置参数绑定）；通用工具不教视图语义，
    schema 见 vrp/schema/facts_schema.sql。
    """
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise SystemExit("query_facts: sql must be non-empty")
    if ";" in stripped:
        raise SystemExit("query_facts: multiple statements rejected (single SELECT/WITH only)")
    head = stripped.split(None, 1)[0].lower()
    if head not in _SELECT_HEADS:
        raise SystemExit(
            f"query_facts: only SELECT/WITH allowed, got: {head.upper()}"
        )
    bind: list = []
    if params.strip():
        try:
            parsed = json.loads(params)
        except ValueError as exc:
            raise SystemExit(f"query_facts: params must be a JSON array: {exc}") from exc
        if not isinstance(parsed, list):
            raise SystemExit("query_facts: params must be a JSON array")
        bind = parsed
    max_rows = max(1, min(int(max_rows), 1000))
    con = _connect_ro(repo)
    try:
        cur = con.execute(stripped, bind)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows)
        more = cur.fetchone() is not None
        truncated = bool(more)
    finally:
        con.close()
    return {
        "columns": cols,
        "rows": [dict(zip(cols, r)) for r in rows],
        "rowcount": len(rows),
        "truncated": truncated,
        "note": "mode=ro 只读；超 max_rows 截断（truncated=true 时加 WHERE/LIMIT 收窄）",
    }
