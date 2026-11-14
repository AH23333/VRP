"""导航接口族 nav_*（构想v7 §1.4.2）——不同深度/颗粒度查询，全链可查防浅层。

分层：
- nav_search       符号搜索（AI 入口），返回 fid 句柄
- nav_architecture L0 全局地图（structure 表优先；缺失时目录聚合兜底）
- nav_module       L1 文件级：符号清单 + 病理摘要，返回 fid 句柄
- nav_symbol       L2 符号级：签名/行号/调用关系/病理/所属业务线，返回 edge 句柄
- nav_callers/callees L2 调用关系完整列表（分页，nav_symbol 内 cap 20 的补充）
- nav_slice        业务线视角（slices 表，curate 验收入库后可用）
- nav_path         耦合路径查找（WITH RECURSIVE BFS 最短路径，R4'）
- query_facts      轻量直查（mode=ro + SELECT 白名单校验）

句柄语义：fid = sha256(project|qualified_name|content_hash)——同一 facts.db 快照内稳定，
事务提交（promote-etl）后 content 变则 fid 变，跨事务需重新 nav_search。
只读连接不受 M4 freshness 硬拒限制（同 status 直连）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from . import dbpath

MODULE_SYMBOL_CAP = 200
MODULE_FINDINGS_CAP = 20
SYMBOL_NEIGHBOR_CAP = 20
SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 100
ARCH_MODULES_CAP = 50
ARCH_MOD_EDGES_CAP = 100
QUERY_MAX_ROWS = 100
NEIGHBOR_LIST_MAX_LIMIT = 200
NEIGHBOR_LIST_DEFAULT_LIMIT = 50
SYMBOL_SLICES_CAP = 10
FID_CHUNK = 500
PATH_DEFAULT_MAX_HOPS = 5
PATH_MAX_HOPS = 10

_SELECT_HEADS = ("select", "with")


def _connect_ro(repo: Path) -> sqlite3.Connection:
    db = Path(repo) / ".vrp" / "facts.db"
    if not db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {db}")
    con = dbpath.ro_connect(db)
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
        "note": "fid handles drill down via nav_symbol; fids go stale across transactions (promote-etl), re-query then",
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
        "note": "no active structure row (filled by curate ingest); dir-aggregation fallback; "
        "module drill-down handle = file_path (nav_module)",
    }


def _architecture_from_structure(con: sqlite3.Connection) -> dict:
    """structure 存在且 active：组件=modules、分层=layers、依赖=实测模块间边（v7 §5.3 L0）。"""
    row = con.execute(
        "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return _architecture_dir_fallback(con)
    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError:
        return _architecture_dir_fallback(con)
    if not isinstance(payload, dict):
        # 合法 JSON 非 dict（R-X 族）：与不可解析同降级目录聚合
        return _architecture_dir_fallback(con)
    modules_raw = payload.get("modules", [])
    if not isinstance(modules_raw, list) or any(
        not isinstance(mod, dict) or not isinstance(mod.get("symbol_fids", []), list)
        for mod in modules_raw
    ):
        # 嵌套形状违背 curate 入库契约（modules 为对象数组、symbol_fids 为数组，
        # 手改库/异源写入）：与不可解析/顶层非 dict 同降级目录聚合——
        # 整份结构不可信不部分采信（R-X 族嵌套盲区收口）。
        return _architecture_dir_fallback(con)
    layers = payload.get("layers", [])
    fid_to_mod: dict[str, str] = {}
    modules: list[dict] = []
    for mod in modules_raw:
        mid = mod.get("module_id", "")
        fids = mod.get("symbol_fids", [])
        for fid in fids:
            fid_to_mod[fid] = mid
        modules.append({
            "module_id": mid,
            "name": mod.get("name", ""),
            "responsibility": mod.get("responsibility", ""),
            "layer": mod.get("layer", ""),
            "symbols": len(fids),
            "sample_fids": fids[:5],
        })
    modules = sorted(modules, key=lambda m: m["module_id"])[:ARCH_MODULES_CAP]
    mod_edges: dict[tuple[str, str], int] = {}
    fids = list(fid_to_mod)
    for i in range(0, len(fids), FID_CHUNK):
        chunk = fids[i:i + FID_CHUNK]
        marks = ",".join("?" * len(chunk))
        for src, dst in con.execute(
            f"SELECT src_fid, dst_fid FROM edges WHERE src_fid IN ({marks}) AND dst_fid IN ({marks})",
            (*chunk, *chunk),
        ):
            a = fid_to_mod.get(src)
            b = fid_to_mod.get(dst)
            if a and b and a != b:
                mod_edges[(a, b)] = mod_edges.get((a, b), 0) + 1
    edges = [
        {"from": a, "to": b, "count": n}
        for (a, b), n in sorted(mod_edges.items(), key=lambda kv: (-kv[1], kv[0]))[:ARCH_MOD_EDGES_CAP]
    ]
    return {
        "level": "L0",
        "source": "structure",
        "layers": layers,
        "modules": modules,
        "module_edges": edges,
        "unassigned_symbols": con.execute(
            "SELECT COUNT(*) FROM symbols WHERE file_path != '' AND file_path NOT GLOB '<*' AND file_path NOT GLOB '>*'"
        ).fetchone()[0] - len(fid_to_mod),
        "note": "structure is AI-curated and ingested via curate; module_edges are measured inter-module edges (fid-ownership aggregation); "
        "sample_fids/nav_search drill down via nav_symbol",
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
        "note": "fid handles drill down via nav_symbol",
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
        slices = _slices_of(con, fid)
    finally:
        con.close()
    return {
        "level": "L2",
        "symbol": symbol,
        "callers": callers,
        "callees": callees,
        "findings": findings,
        "slices": slices,
        "note": "caller/callee fid handles keep drilling via nav_symbol (full chain reachable); "
        "slices = business lines containing this symbol (nav_slice)",
    }


def _slices_of(con: sqlite3.Connection, fid: str) -> list[dict]:
    """含指定符号的 active 业务线（LIKE 粗筛 + python 精确核对）。"""
    out: list[dict] = []
    like = f'%{fid}%'
    for slice_id, name, payload_json in con.execute(
        "SELECT slice_id, name, payload_json FROM slices WHERE status = 'active' AND payload_json LIKE ?",
        (like,),
    ).fetchall():
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        # 合法 JSON 但非 dict 的 payload（手改库/异源写入）同列容错跳过，
        # 不让 AttributeError 击穿 membership 检索（R-X：与 render/render_chain
        # 消费点 isinstance 守卫同口径）。
        steps = payload.get("steps", []) if isinstance(payload, dict) else []
        for i, step in enumerate(steps):
            if isinstance(step, dict) and step.get("fid") == fid:
                out.append({"slice_id": slice_id, "name": name, "step": i})
                break
        if len(out) >= SYMBOL_SLICES_CAP:
            break
    return out


def nav_callers(repo: Path, fid: str, limit: int = NEIGHBOR_LIST_DEFAULT_LIMIT) -> dict:
    """完整调用者列表（nav_symbol 内 cap 20，本件带 limit 分页）。"""
    return _neighbors(repo, fid, limit, direction="callers")


def nav_callees(repo: Path, fid: str, limit: int = NEIGHBOR_LIST_DEFAULT_LIMIT) -> dict:
    """完整被调用者列表（nav_symbol 内 cap 20，本件带 limit 分页）。"""
    return _neighbors(repo, fid, limit, direction="callees")


def _neighbors(repo: Path, fid: str, limit: int, direction: str) -> dict:
    fid = fid.strip()
    if not fid:
        raise SystemExit(f"nav_{direction}: fid must be non-empty")
    limit = max(1, min(int(limit), NEIGHBOR_LIST_MAX_LIMIT))
    con = _connect_ro(repo)
    try:
        if direction == "callers":
            join_on, where = "s.fid = e.src_fid", "e.dst_fid = ?"
        else:
            join_on, where = "s.fid = e.dst_fid", "e.src_fid = ?"
        rows = con.execute(
            f"""
            SELECT s.fid, s.qualified_name, s.name, s.file_path, s.start_line,
                   e.type, e.evidence, e.confidence, e.line
            FROM edges e JOIN symbols s ON {join_on}
            WHERE {where} ORDER BY e.type, s.qualified_name LIMIT ?
            """,
            (fid, limit + 1),
        ).fetchall()
    finally:
        con.close()
    truncated = len(rows) > limit
    return {
        "level": "L2",
        "fid": fid,
        "direction": direction,
        "neighbors": [dict(r) for r in rows[:limit]],
        "count": min(len(rows), limit),
        "truncated": truncated,
        "note": "truncated=true means cut at limit, raise limit for more; fid handles drill down via nav_symbol",
    }


def nav_slice(repo: Path, slice_id: str) -> dict:
    """业务线视角：steps（附符号摘要句柄）+ acts（分幕）。"""
    slice_id = slice_id.strip()
    if not slice_id:
        raise SystemExit("nav_slice: slice_id must be non-empty")
    con = _connect_ro(repo)
    try:
        row = con.execute(
            "SELECT name, summary, status, payload_json, created_at FROM slices WHERE slice_id = ?",
            (slice_id,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"nav_slice: unknown slice_id: {slice_id}")
        name, summary, status, payload_json, created_at = row
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            raise SystemExit(
                f"nav_slice: corrupt slice payload for {slice_id} - re-run curation to rebuild it"
            )
        # 合法 JSON 非 dict：同 corrupt 语义给干净错误（R-X），非裸 AttributeError。
        if not isinstance(payload, dict):
            raise SystemExit(
                f"nav_slice: corrupt slice payload for {slice_id} - re-run curation to rebuild it"
            )
        steps_raw = payload.get("steps", [])
        steps_iter = steps_raw if isinstance(steps_raw, list) else []
        steps: list[dict] = []
        for i, step in enumerate(steps_iter):
            if not isinstance(step, dict):
                # 合法 JSON 但非 dict 的 step（手改库/异源写入）同列容错跳过，
                # 与 _slices_of 的 isinstance(step, dict) 守卫同口径（R-X 族）。
                continue
            fid = step.get("fid", "")
            sym = con.execute(
                "SELECT qualified_name, name, file_path, start_line FROM symbols WHERE fid = ?",
                (fid,),
            ).fetchone()
            entry = {"step": i, "fid": fid, "role": step.get("role", "process"), "note": step.get("note", "")}
            if sym is not None:
                entry["qualified_name"] = sym["qualified_name"]
                entry["file_path"] = sym["file_path"]
                entry["start_line"] = sym["start_line"]
            else:
                entry["missing"] = True
            steps.append(entry)
        acts = payload.get("acts", [])
    finally:
        con.close()
    return {
        "level": "slice",
        "slice_id": slice_id,
        "name": name,
        "summary": summary,
        "status": status,
        "created_at": created_at,
        "steps": steps,
        "acts": acts,
        "note": "status=stale means symbols/edges drifted (re-validation failed); step.fid handles drill down via nav_symbol",
    }


def nav_path(
    repo: Path,
    from_fid: str,
    to_fid: str,
    max_hops: int = PATH_DEFAULT_MAX_HOPS,
) -> dict:
    """耦合路径查找：from→to 沿 edges（src→dst）最短路径（WITH RECURSIVE BFS，R4'）。

    BFS 层序保证首达即最短；同长最短路径可能不止一条，确定性选一（fid 字典序）。
    hops_detail 透传 edge 的 type/evidence/confidence——地基质量透传（构想v7 §1.4.2）。
    """
    from_fid = from_fid.strip()
    to_fid = to_fid.strip()
    if not from_fid or not to_fid:
        raise SystemExit("nav_path: from_fid/to_fid must be non-empty")
    if from_fid == to_fid:
        raise SystemExit("nav_path: from_fid and to_fid must differ")
    max_hops = max(1, min(int(max_hops), PATH_MAX_HOPS))
    con = _connect_ro(repo)
    try:
        for fid in (from_fid, to_fid):
            if con.execute("SELECT 1 FROM symbols WHERE fid = ?", (fid,)).fetchone() is None:
                raise SystemExit(f"nav_path: unknown fid: {fid[:16]}...")
        con.execute("CREATE TEMP TABLE bfs_path_map (fid TEXT PRIMARY KEY, depth INTEGER NOT NULL)")
        # 递归 CTE 的 UNION 按整行 (fid, depth) 去重——同 fid 可带多个深度；
        # MIN(depth) 聚合归一为"每节点最短到达深度"，重建用 predecessor 性质成立
        con.execute(
            """
            WITH RECURSIVE walk(fid, depth) AS (
                SELECT ?, 0
                UNION
                SELECT e.dst_fid, walk.depth + 1
                FROM edges e JOIN walk ON e.src_fid = walk.fid
                WHERE walk.depth < ?
            )
            INSERT INTO bfs_path_map (fid, depth)
            SELECT fid, MIN(depth) FROM walk GROUP BY fid
            """,
            (from_fid, max_hops),
        )
        target = con.execute(
            "SELECT depth FROM bfs_path_map WHERE fid = ?", (to_fid,)
        ).fetchone()
        nodes_searched = con.execute("SELECT COUNT(*) FROM bfs_path_map").fetchone()[0]
        if target is None:
            return {
                "level": "path",
                "from_fid": from_fid,
                "to_fid": to_fid,
                "found": False,
                "hops": None,
                "max_hops": max_hops,
                "nodes_searched": nodes_searched,
                "note": f"unreachable within max_hops={max_hops} (edges src→dst direction); "
                f"raise max_hops (cap {PATH_MAX_HOPS}) or run nav_path(to, from) for reverse coupling",
            }
        depth = target["depth"]
        chain = [to_fid]
        hops_detail: list[dict] = []
        current = to_fid
        for d in range(depth, 0, -1):
            pred = con.execute(
                """
                SELECT b.fid FROM bfs_path_map b JOIN edges e ON e.src_fid = b.fid
                WHERE e.dst_fid = ? AND b.depth = ? ORDER BY b.fid LIMIT 1
                """,
                (current, d - 1),
            ).fetchone()
            edge = con.execute(
                """
                SELECT type, evidence, strategy, confidence, line FROM edges
                WHERE src_fid = ? AND dst_fid = ?
                ORDER BY type, evidence LIMIT 1
                """,
                (pred["fid"], current),
            ).fetchone()
            hops_detail.append({
                "from_fid": pred["fid"],
                "to_fid": current,
                "type": edge["type"],
                "evidence": edge["evidence"],
                "strategy": edge["strategy"],
                "confidence": edge["confidence"],
                "line": edge["line"],
            })
            chain.append(pred["fid"])
            current = pred["fid"]
        chain.reverse()
        hops_detail.reverse()
        path: list[dict] = []
        for fid in chain:
            row = con.execute(
                "SELECT fid, qualified_name, name, file_path, start_line FROM symbols WHERE fid = ?",
                (fid,),
            ).fetchone()
            path.append(dict(row))
    finally:
        con.close()
    return {
        "level": "path",
        "from_fid": from_fid,
        "to_fid": to_fid,
        "found": True,
        "hops": depth,
        "max_hops": max_hops,
        "nodes_searched": nodes_searched,
        "path": path,
        "hops_detail": hops_detail,
        "note": "shortest coupling path (edges src→dst); hops_detail carries confidence per hop — "
        "verify low-confidence edges via nav_symbol or source; ties broken deterministically "
        "(lexicographically smallest fid)",
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
        "note": "read-only mode; truncated=true means cut at max_rows, narrow with WHERE/LIMIT",
    }
