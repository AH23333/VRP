"""vrp MCP server（AI 接口层，构想v7 §1.4.1）——官方 MCP Python SDK，optional extra。

- 启动：`vrp mcp <repo>`（stdio transport，per-repo 绑定，工具不带 repo 参数）
- 依赖：`pip install vrp[mcp]`（mcp>=2.0）；核心管道零第三方依赖红线不动
- 安全闸：tx_run 不暴露 confirm 参数且恒以 confirm=False 调用 s6 层——
  T4 放行只能 CLI 人工 `vrp transact --confirm`，agent 不可绕过（构想v7 §3 决策点矩阵）
- 错误语义：工具边界捕 (SystemExit, Exception) 统一转 {"error": ...} 成功形状
  （SystemExit 是 nav 层错误约定族；KeyboardInterrupt 属 BaseException 照传不捕——
  isError 面归零：codegraph 实测一次 isError 早出现即让 agent 整个抛弃工具集，
  设计稿-AA1AA2 D4）
- 输出预算（AA2）：返回值经 mcp_budget.emit——24K 硬顶+JSON 感知确定性收缩
  （设计稿 D5）；输入上界 mcp_budget.check_input（设计稿 L5）
- 工具面（AA1，设计稿 D1-D3，0.2.0 破坏性收敛 30→10）：
  vrp_query（17 只读类型单强查询工具，CGC analyze_code_relationships 律）
  + render/session（kind/action 聚合）+ dossier/curate_submit/curate_reject
  + tx_preview/tx_submit/tx_run + enforce（写路径/执行动词独立=安全闸）
- 双 server 组合：本 server 之外推荐 bytebase/dbhub（SQLite 只读）自由浏览 facts.db
"""

from __future__ import annotations

import functools
import json
import sqlite3
import tempfile
from pathlib import Path

from . import nav, s6_transact
from . import coverage as coverage_mod
from . import impact as impact_mod
from . import dbpath
from . import curate as curate_mod
from . import etl as etl_mod
from . import mcp_budget
from . import render as render_mod
from . import s5_dossier
from . import s7_enforce
from . import session as session_mod

try:
    from mcp.server.mcpserver import MCPServer
except ModuleNotFoundError:  # optional extra guard
    MCPServer = None  # type: ignore[assignment]

_VERSION = "0.2.0"
_INSTRUCTIONS = (
    "vrp restructuring loop: dossier (task-typed intake briefing) then curate_submit to submit "
    "slices/structure (deterministic validation; on rejection the returned receipt carries stable "
    "error codes - fix accordingly and resubmit; curate_receipt reads CLI-run receipts only); "
    "vrp_query is the single read tool (query_type: search/architecture/module/symbol/facts/"
    "callers/callees/slice/path/seams/coverage/impact/drift/curate_slices/curate_structure/"
    "curate_receipt/tx_verdicts; target carries the primary key - query text, fid, module_id, "
    "slice_id, sql, tid, or 'from_fid->to_fid' for path; options_json carries secondary params "
    "as a JSON object, e.g. {\"limit\":50}); "
    "transactions: tx_preview the diff first, then tx_run to execute (T4 always needs_confirmation "
    "- release only via manual CLI `vrp transact --confirm`, never by the agent), "
    "then vrp_query tx_verdicts to read receipts; on failure read failed_codes, fix and retry "
    "(<=3 attempts per transaction); "
    "enforce runs architecture enforcement once curate_structure is ingested (P9 findings, idempotent); "
    "render (kind=verdicts|slice|report) produces decision-point briefing material; "
    "trust mode: session action=begin -> multi-round loop -> action=end -> action=report "
    "(initial-vs-final comparison); "
    "production rules: docs/契约/事务契约.md and docs/契约/梳理工件契约.md."
)


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _guard(fn):
    """工具边界（AA1 D4 + AA2 D5）：except (SystemExit, Exception) → {"error"} 成功形状。

    isError 面归零：KeyError/TypeError/IndexError 族此前逃逸到 SDK 变 isError:true
    （codegraph 实测 isError 一次即弃用工具集）；KeyboardInterrupt 属 BaseException
    照传不捕（与 _open_facts_rw 同理）。functools.wraps 保住原函数签名
    （inspect.signature 跟 __wrapped__），否则 schema 提取器看到 (*args, **kwargs)。
    返回值经 mcp_budget.emit：24K 硬顶+JSON 感知确定性收缩（截断永远可见）。
    """

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return mcp_budget.emit(fn(*args, **kwargs))
        except (SystemExit, Exception) as exc:
            return _dumps({"error": str(exc) or exc.__class__.__name__})

    return wrapped


def _write_temp(tx_json: str) -> Path:
    # text=False：fd 不带 CRT O_TEXT 标志（Python io 绕过 CRT，无双重翻译，
    # 但标志本身是平台尾巴）；换行翻译统一由 TextIOWrapper 的 newline 钉住。
    fd, name = tempfile.mkstemp(suffix=".json")
    tmp = Path(name)
    with open(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(tx_json)
    return tmp


def _open_facts_rw(repo: Path) -> sqlite3.Connection:
    """curate 族写 facts.db：ensure_schema 自动迁移新表 + M4 freshness 硬拒。

    异常路径必须 con.close()——长驻 MCP 会话里句柄泄漏会在 Windows 上
    累积文件锁（facts.db 无法删除/被后续访问阻塞）。
    """
    facts = repo / ".vrp" / "facts.db"
    if not facts.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts}")
    con = sqlite3.connect(facts)
    try:
        etl_mod.ensure_schema(con)
        etl_mod.check_facts_freshness(con, repo)
    except (Exception, KeyboardInterrupt):
        # Ctrl-C 同样走 con.close()（KI 非 Exception 子类，只捕 Exception
        # 会绕过清理泄漏句柄——长驻 MCP 会话里 Windows 文件锁累积）。
        con.close()
        raise
    return con


def _parse_options(options_json: str) -> dict:
    """vrp_query options_json 解析：JSON object，非法/非 object 一律 ValueError
    （工具层转成功形状 {"error"}）。"""
    if not options_json or not options_json.strip():
        return {}
    try:
        opts = json.loads(options_json)
    except ValueError as exc:
        raise ValueError(f"vrp_query: options_json must be a JSON object: {exc}") from exc
    if not isinstance(opts, dict):
        raise ValueError("vrp_query: options_json must be a JSON object")
    return opts


def _opt_int(opts: dict, key: str, default) -> int:
    raw = opts.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"vrp_query: option {key!r} must be an integer, got: {raw!r}")


def _opt_str(opts: dict, key: str, default: str) -> str:
    raw = opts.get(key, default)
    if not isinstance(raw, str):
        raise ValueError(f"vrp_query: option {key!r} must be a string, got: {raw!r}")
    return raw


def _opt_str_list(opts: dict, key: str):
    raw = opts.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"vrp_query: option {key!r} must be an array of strings")
    return raw


def _opt_bool(opts: dict, key: str, default: bool) -> bool:
    raw = opts.get(key, default)
    if not isinstance(raw, bool):
        raise ValueError(f"vrp_query: option {key!r} must be a boolean, got: {raw!r}")
    return raw


_VRP_QUERY_TYPES = (
    "search", "architecture", "module", "symbol", "facts", "callers", "callees",
    "slice", "path", "seams", "coverage", "impact", "drift", "curate_slices",
    "curate_structure", "curate_receipt", "tx_verdicts",
)


def build_server(repo: Path) -> "MCPServer":
    """构造绑定单个 repo 的 MCP server（in-process 可测）。"""
    if MCPServer is None:
        raise SystemExit("mcp package missing - install with: pip install vrp[mcp]")
    repo = Path(repo)
    server = MCPServer(name="vrp", title="vrp", version=_VERSION, instructions=_INSTRUCTIONS)

    def vrp_query(query_type: str, target: str = "", options_json: str = "{}") -> dict:
        """17 只读类型单强查询工具（AA1 D1/D2；内层 nav/curate/tx 函数零改动纯转发）。"""
        mcp_budget.check_input("query_type", query_type, mcp_budget.MAX_QUERY_INPUT_CHARS)
        mcp_budget.check_input("target", target, mcp_budget.MAX_JSON_INPUT_CHARS)
        mcp_budget.check_input("options_json", options_json, mcp_budget.MAX_JSON_INPUT_CHARS)
        opts = _parse_options(options_json)
        qt = (query_type or "").strip()
        if qt == "search":
            mcp_budget.check_input("target", target, mcp_budget.MAX_QUERY_INPUT_CHARS)
            return nav.nav_search(
                repo, target, _opt_int(opts, "limit", mcp_budget.get_tool_result_limit("nav_search", nav.SEARCH_DEFAULT_LIMIT))
            )
        if qt == "architecture":
            return nav.nav_architecture(repo)
        if qt == "module":
            return nav.nav_module(
                repo, target, _opt_int(opts, "limit", mcp_budget.get_tool_result_limit("nav_module", nav.MODULE_SYMBOL_CAP))
            )
        if qt == "symbol":
            return nav.nav_symbol(repo, target)
        if qt == "facts":
            return nav.query_facts(
                repo, target,
                _opt_str(opts, "params", ""),
                _opt_int(opts, "max_rows", mcp_budget.get_tool_result_limit("query_facts", nav.QUERY_MAX_ROWS)),
            )
        if qt == "callers":
            return nav.nav_callers(
                repo, target, _opt_int(opts, "limit", mcp_budget.get_tool_result_limit("nav_callers", nav.NEIGHBOR_LIST_DEFAULT_LIMIT))
            )
        if qt == "callees":
            return nav.nav_callees(
                repo, target, _opt_int(opts, "limit", mcp_budget.get_tool_result_limit("nav_callees", nav.NEIGHBOR_LIST_DEFAULT_LIMIT))
            )
        if qt == "slice":
            return nav.nav_slice(repo, target)
        if qt == "path":
            if "->" not in target:
                raise ValueError("vrp_query: path target must be 'from_fid->to_fid'")
            from_fid, to_fid = target.split("->", 1)
            return nav.nav_path(repo, from_fid.strip(), to_fid.strip(), _opt_int(opts, "max_hops", nav.PATH_DEFAULT_MAX_HOPS))
        if qt == "seams":
            return nav.nav_seams(repo)
        if qt == "coverage":
            return coverage_mod.check_coverage(
                repo, paths=_opt_str_list(opts, "paths"), kinds=_opt_str_list(opts, "kinds")
            )
        if qt == "impact":
            base_raw = opts.get("base")
            if base_raw is not None and not isinstance(base_raw, str):
                raise ValueError("vrp_query: option 'base' must be a string")
            return impact_mod.impact_report(
                repo,
                base=base_raw or None,
                paths=_opt_str_list(opts, "paths"),
                depth=_opt_int(opts, "depth", 2),
                direction=_opt_str(opts, "direction", "inbound"),
                tests=_opt_bool(opts, "tests", True),
            )
        if qt == "drift":
            facts = repo / ".vrp" / "facts.db"
            if not facts.exists():
                raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts}")
            con = dbpath.ro_connect(facts)
            try:
                return curate_mod.drift_report(con)
            finally:
                con.close()
        if qt == "curate_slices":
            con = _open_facts_rw(repo)
            try:
                return {"slices": curate_mod.list_slices(con)}
            finally:
                con.close()
        if qt == "curate_structure":
            con = _open_facts_rw(repo)
            try:
                return curate_mod.get_structure(con) or {"structure": None}
            finally:
                con.close()
        if qt == "curate_receipt":
            return curate_mod.read_receipt(repo) or {"receipt": None}
        if qt == "tx_verdicts":
            verdicts = s6_transact.load_verdicts(repo)
            tid = target.strip()
            if tid:
                match = [v for v in verdicts if v.get("tid") == tid]
                if not match:
                    raise SystemExit(f"vrp_query: no verdict for tid: {tid}")
                return match
            return {"count": len(verdicts), "verdicts": verdicts}
        raise ValueError(
            f"vrp_query: unknown query_type: {query_type!r} (valid: {', '.join(_VRP_QUERY_TYPES)})"
        )

    def dossier(task: str = "T4", budget: int = 32000, skip_external: bool = True) -> dict:
        facts = repo / ".vrp" / "facts.db"
        if not facts.exists():
            raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts}")
        con = _open_facts_rw(repo)  # M4 freshness：build_dossier 写 dossier_views
        con.close()
        return s5_dossier.build_dossier(
            repo, facts, repo.name, budget=budget, task=task, skip_external=skip_external
        )

    def curate_submit(kind: str, payload_json: str) -> dict:
        mcp_budget.check_input("payload_json", payload_json, mcp_budget.MAX_JSON_INPUT_CHARS)
        con = _open_facts_rw(repo)
        try:
            return curate_mod.curate_payload(con, kind, payload_json)
        finally:
            con.close()

    def curate_reject(kind: str, target_id: str, note: str = "") -> dict:
        con = _open_facts_rw(repo)
        try:
            return curate_mod.reject(con, kind, target_id, note)
        finally:
            con.close()

    def curate_refresh() -> dict:
        con = _open_facts_rw(repo)
        try:
            return curate_mod.curate_refresh(repo, con)
        finally:
            con.close()

    def tx_preview(tx_json: str) -> dict:
        mcp_budget.check_input("tx_json", tx_json, mcp_budget.MAX_JSON_INPUT_CHARS)
        tmp = _write_temp(tx_json)
        try:
            return s6_transact.preview_transaction(repo, tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def tx_submit(tx_json: str, overwrite: bool = False) -> dict:
        mcp_budget.check_input("tx_json", tx_json, mcp_budget.MAX_JSON_INPUT_CHARS)
        tmp = _write_temp(tx_json)
        try:
            payload = s6_transact.load_transaction(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        tx_dir = repo / ".vrp" / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        tid = s6_transact.TID_SAFE.sub("_", payload["id"])
        path = tx_dir / f"{tid}.json"
        if path.exists() and not overwrite:
            raise SystemExit(
                f"tx_submit: transaction exists (overwrite=True to replace): {path.name}"
            )
        path.write_text(tx_json, encoding="utf-8", newline="\n")
        return {"submitted": True, "path": str(path), "id": payload["id"], "type": payload["type"]}

    def tx_run(tx_json: str, name: str = "") -> dict:
        mcp_budget.check_input("tx_json", tx_json, mcp_budget.MAX_JSON_INPUT_CHARS)
        tmp = _write_temp(tx_json)
        try:
            payload = s6_transact.load_transaction(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        tx_dir = repo / ".vrp" / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        tid = s6_transact.TID_SAFE.sub("_", payload["id"])
        path = tx_dir / f"{tid}.json"
        path.write_text(tx_json, encoding="utf-8", newline="\n")
        verdict = s6_transact.transact(repo, path, name or repo.name, confirm=False)
        return verdict

    def render(kind: str, target: str = "") -> dict:
        mcp_budget.check_input("kind", kind, mcp_budget.MAX_QUERY_INPUT_CHARS)
        mcp_budget.check_input("target", target, mcp_budget.MAX_QUERY_INPUT_CHARS)
        k = kind.strip()
        if k == "verdicts":
            return render_mod.render_verdicts(repo)
        if k == "slice":
            return render_mod.render_slice(repo, target)
        if k == "report":
            return render_mod.render_report(repo)
        raise ValueError(f"render: unknown kind: {kind!r} (valid: verdicts, slice, report)")

    def session(action: str, target: str = "") -> dict:
        mcp_budget.check_input("action", action, mcp_budget.MAX_QUERY_INPUT_CHARS)
        mcp_budget.check_input("target", target, mcp_budget.MAX_QUERY_INPUT_CHARS)
        a = action.strip()
        if a == "begin":
            return session_mod.session_begin(repo, target)
        if a == "end":
            return session_mod.session_end(repo, target)
        if a == "report":
            return session_mod.session_report(repo, target)
        raise ValueError(f"session: unknown action: {action!r} (valid: begin, end, report)")

    def enforce(timeout: int = 600) -> dict:
        con = _open_facts_rw(repo)
        try:
            return s7_enforce.run(repo, con, timeout)
        finally:
            con.close()

    server.add_tool(
        _guard(vrp_query),
        name="vrp_query",
        description="Single read tool over facts.db (17 query types; AA1 single-strong-tool, CGC analyze_code_relationships precedent): "
        "search (AI entry point - name/qualified_name matching, exact matches first, returns fid handles for drill-down; option limit), "
        "architecture (L0 global map, prefers ingested structure), "
        "module (L1 file drill-down, target=module_id i.e. file_path; option limit), "
        "symbol (L2 symbol drill-down by fid: signature/lines/callers/callees/findings), "
        "facts (lightweight read-only SQL, single SELECT/WITH, target=sql; options params (JSON array string, positional), max_rows; "
        "schema in vrp/schema/facts_schema.sql), "
        "callers/callees (full neighbor lists by fid; option limit; truncated=true means raise limit), "
        "slice (business-line view by slice_id), "
        "path (shortest coupling path along edges src->dst, target='from_fid->to_fid'; option max_hops cap 10; "
        "hops_detail carries per-hop type/evidence/confidence), "
        "seams (cross-module edge aggregation over the active curated structure; honestly reports structure=missing), "
        "coverage (index completeness check per path; options paths (array), kinds (array); no paths = gap inventory), "
        "impact (change blast radius + suggested tests, read-only; options base, paths (array), depth, direction, tests), "
        "drift (unified artifact drift query: fresh/missing_fids/missing_edges + fid_ledger remap candidates; "
        "non-empty summary.active_but_drifted must be reported), "
        "curate_slices (list ingested business lines with status), "
        "curate_structure (read ingested overall structure), "
        "curate_receipt (read the latest CLI-run curate receipt; curate_submit returns its own receipt inline), "
        "tx_verdicts (transaction receipts; target=tid returns one full record, empty lists all). "
        "target carries the primary key for the type; options_json carries secondary params as a JSON object",
    )
    server.add_tool(
        _guard(dossier),
        name="dossier",
        description="S5 intake dossier: task-typed briefing over facts.db (task=T1|T2|T3|T4 budget profiles; "
        "sections: architecture/repomap/sql_views/evidence/summary) — the intake material for curation and refactor planning; "
        "default skip_external=True = fast and deterministic (external sections honestly marked skipped); "
        "skip_external=False opts into external tooling (repo_map_brief/raw_pack, slow, up to 180s); "
        "writes .vrp/dossier.json and dossier_views; facts.db must be fresh (M4)",
    )
    server.add_tool(
        _guard(curate_submit),
        name="curate_submit",
        description="Submit AI-curated artifacts for deterministic validation and ingest: kind=slices (JSON array) | structure (JSON object), "
        "payload_json is the artifact verbatim; ingested only if all validation passes, otherwise rejected with a receipt (errors each carry a stable code); "
        "default output language for curated content: English; "
        "schema in docs/契约/梳理工件契约.md",
    )
    server.add_tool(
        _guard(curate_reject),
        name="curate_reject",
        description="User vetoes an ingested artifact (kind=slice|structure, target_id=slice_id or 'structure'): "
        "for structure a numeric target_id vetoes that exact row id (unknown id errors), "
        "non-numeric vetoes the latest version; "
        "status→rejected + decision logged; veto is the conservative direction, the agent may relay the user's decision",
    )
    server.add_tool(
        _guard(curate_refresh),
        name="curate_refresh",
        description="Deterministically remap dead fids in ingested structure/slices to live fids "
        "(content churn and project rename both bridged via the fid_ledger stable qualified_name key); "
        "semantics (module names/responsibilities/layers, slice step roles/notes/acts) preserved verbatim, "
        "only fid pointers refreshed; unremappable fids honestly dropped and counted, empty modules dropped, "
        "all-dead structures refused (action=failed); refreshed payloads fully re-validated before ingest; "
        "receipt at .vrp/curate-refresh-receipt.json",
    )
    server.add_tool(
        _guard(tx_preview),
        name="tx_preview",
        description="Pre-submit self-review: per-edit unified diff + difftastic structural signals; "
        "runs no gates, touches no tree, writes no verdict; precondition failure = target missing/already exists (a G0 pre-check)",
    )
    server.add_tool(
        _guard(tx_submit),
        name="tx_submit",
        description="Persist the transaction to .vrp/transactions/<id>.json (strict schema validation, no execution); "
        "split large refactors into transaction sequences for CLI batching (vrp transact-batch)",
    )
    server.add_tool(
        _guard(tx_run),
        name="tx_run",
        description="Execute a transaction: T1-T3 run the gates directly (G0-G5) and emit a verdict; "
        "T4 always returns needs_confirmation — release only via manual CLI `vrp transact --confirm`, never by the agent; "
        "tx_preview the diff first",
    )
    server.add_tool(
        _guard(render),
        name="render",
        description="Briefing material (kind=verdicts|slice|report): "
        "verdicts = T4 decision-point briefing (verdict timeline + open/T4 list; T4 release only via manual CLI vrp transact --confirm); "
        "slice = single business-line briefing as Mermaid sequence source (target=slice_id, get ids via vrp_query curate_slices; "
        "paste into mermaid.live to render); "
        "report = S8 full static report family -> .vrp/report/ (report.html + tour.html newcomer walkthrough "
        "(skipped, honestly noted, when no curated structure) + IR + SARIF + Mermaid; "
        "auto-degrades to Mermaid-only when archify is missing); no timestamps, traceable via facts.git_head",
    )
    server.add_tool(
        _guard(session),
        name="session",
        description="Trust-mode lifecycle (action=begin|end|report; target semantics per action): "
        "begin = facts.db snapshot + session metadata (target=note; only one active session per repo); "
        "end = final snapshot + initial-vs-final count diff (target=note); "
        "report = initial-vs-final comparison + side-by-side L0 graphs + verdict timeline -> report.html "
        "(target=session_id, defaults to the latest ended session)",
    )
    server.add_tool(
        _guard(enforce),
        name="enforce",
        description="S7 architecture enforcement (design doc v7 §4): runs the curate-ingested structure through import-linter; "
        "broken contracts = P9 findings (source='s7-enforce', idempotent replace-all); "
        "honestly skipped when no structure or not active; violations enter refactor targets via the dossier — AI understands, tools enforce",
    )
    return server


def run_stdio(repo: Path) -> None:
    server = build_server(repo)
    server.run("stdio")


def main(argv: list[str] | None = None) -> int:
    import sys

    repo = Path(argv[0]) if argv else Path.cwd()
    run_stdio(repo)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main(sys.argv[1:]))
