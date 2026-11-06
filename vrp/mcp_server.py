"""vrp MCP server（AI 接口层，构想v7 §1.4.1）——官方 MCP Python SDK，optional extra。

- 启动：`vrp mcp <repo>`（stdio transport，per-repo 绑定，工具不带 repo 参数）
- 依赖：`pip install vrp[mcp]`（mcp>=2.0）；核心管道零第三方依赖红线不动
- 安全闸：tx_run 不暴露 confirm 参数且恒以 confirm=False 调用 s6 层——
  T4 放行只能 CLI 人工 `vrp transact --confirm`，agent 不可绕过（构想v7 §3 决策点矩阵）
- 错误语义：nav/transact 层的 SystemExit/ValueError 统一转 {"error": ...} JSON
  （SystemExit 是 BaseException，裸抛会炸穿 server task）
- 双 server 组合：本 server 之外推荐 bytebase/dbhub（SQLite 只读）自由浏览 facts.db
"""

from __future__ import annotations

import functools
import json
import sqlite3
import tempfile
from pathlib import Path

from . import nav, s6_transact
from . import curate as curate_mod
from . import etl as etl_mod
from . import render as render_mod
from . import s5_dossier
from . import s7_enforce
from . import session as session_mod

try:
    from mcp.server.mcpserver import MCPServer
except ModuleNotFoundError:  # optional extra guard
    MCPServer = None  # type: ignore[assignment]

_VERSION = "0.1.1"
_INSTRUCTIONS = (
    "vrp restructuring loop: nav_search to locate -> nav_module/nav_symbol to drill down and verify -> "
    "tx_preview to self-review the diff -> tx_run to execute -> tx_verdicts to read receipts; "
    "on failure read failed_codes, fix and retry (<=3 attempts per transaction). "
    "Curation: dossier (task-typed intake briefing) then curate_submit to submit slices/structure (deterministic validation; on rejection read the receipt, fix, resubmit), "
    "nav_slice to drill down by business line; "
    "Enforcement: once curate_structure is ingested, enforce runs architecture enforcement (P9 findings, idempotent). "
    "Reporting: render_verdicts/render_slice produce decision-point briefing material, render_report produces the full static report; "
    "Trust mode (design doc v7 §3.3): session_begin -> multi-round loop -> session_end -> "
    "session_report delivers initial-vs-final comparison (count diff + side-by-side L0 graphs + verdict timeline). "
    "Production rules: docs/契约/事务契约.md and docs/契约/梳理工件契约.md."
)


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _guard(fn):
    """nav/s6/curate 层 SystemExit/ValueError/RuntimeError -> {"error": ...}，不炸 server。

    functools.wraps 保住原函数签名（inspect.signature 跟 __wrapped__），
    否则 schema 提取器看到 (*args, **kwargs) 会生成 args/kwargs 两个参数。
    RuntimeError 含 M4 freshness 硬拒（etl.check_facts_freshness）。
    """

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (SystemExit, ValueError, RuntimeError) as exc:
            return _dumps({"error": str(exc) or exc.__class__.__name__})

    return wrapped


def _write_temp(tx_json: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=".json", text=True)
    tmp = Path(name)
    with open(fd, "w", encoding="utf-8") as f:
        f.write(tx_json)
    return tmp


def _open_facts_rw(repo: Path) -> sqlite3.Connection:
    """curate 族写 facts.db：ensure_schema 自动迁移新表 + M4 freshness 硬拒。"""
    facts = repo / ".vrp" / "facts.db"
    if not facts.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts}")
    con = sqlite3.connect(facts)
    etl_mod.ensure_schema(con)
    etl_mod.check_facts_freshness(con, repo)
    return con


def build_server(repo: Path) -> "MCPServer":
    """构造绑定单个 repo 的 MCP server（in-process 可测）。"""
    if MCPServer is None:
        raise SystemExit("mcp package missing - install with: pip install vrp[mcp]")
    repo = Path(repo)
    server = MCPServer(name="vrp", title="vrp", version=_VERSION, instructions=_INSTRUCTIONS)

    def nav_search(query: str, limit: int = 20) -> str:
        return _dumps(nav.nav_search(repo, query, limit))

    def nav_architecture() -> str:
        return _dumps(nav.nav_architecture(repo))

    def nav_module(module_id: str, limit: int = 200) -> str:
        return _dumps(nav.nav_module(repo, module_id, limit))

    def nav_symbol(fid: str) -> str:
        return _dumps(nav.nav_symbol(repo, fid))

    def query_facts(sql: str, params: str = "", max_rows: int = 100) -> str:
        return _dumps(nav.query_facts(repo, sql, params, max_rows))

    def nav_callers(fid: str, limit: int = 50) -> str:
        return _dumps(nav.nav_callers(repo, fid, limit))

    def nav_callees(fid: str, limit: int = 50) -> str:
        return _dumps(nav.nav_callees(repo, fid, limit))

    def nav_slice(slice_id: str) -> str:
        return _dumps(nav.nav_slice(repo, slice_id))

    def nav_path(from_fid: str, to_fid: str, max_hops: int = 5) -> str:
        return _dumps(nav.nav_path(repo, from_fid, to_fid, max_hops))

    def curate_submit(kind: str, payload_json: str) -> str:
        con = _open_facts_rw(repo)
        try:
            return _dumps(curate_mod.curate_payload(con, kind, payload_json))
        finally:
            con.close()

    def curate_slices() -> str:
        con = _open_facts_rw(repo)
        try:
            return _dumps({"slices": curate_mod.list_slices(con)})
        finally:
            con.close()

    def curate_structure() -> str:
        con = _open_facts_rw(repo)
        try:
            return _dumps(curate_mod.get_structure(con) or {"structure": None})
        finally:
            con.close()

    def curate_reject(kind: str, target_id: str, note: str = "") -> str:
        con = _open_facts_rw(repo)
        try:
            return _dumps(curate_mod.reject(con, kind, target_id, note))
        finally:
            con.close()

    def curate_receipt() -> str:
        return _dumps(curate_mod.read_receipt(repo) or {"receipt": None})

    def drift_report() -> str:
        facts = repo / ".vrp" / "facts.db"
        if not facts.exists():
            raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts}")
        con = sqlite3.connect(f"file:{facts}?mode=ro", uri=True)
        try:
            return _dumps(curate_mod.drift_report(con))
        finally:
            con.close()

    def dossier(task: str = "T4", budget: int = 32000, skip_external: bool = True) -> str:
        facts = repo / ".vrp" / "facts.db"
        if not facts.exists():
            raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts}")
        con = _open_facts_rw(repo)  # M4 freshness：build_dossier 写 dossier_views
        con.close()
        return _dumps(
            s5_dossier.build_dossier(
                repo, facts, repo.name, budget=budget, task=task, skip_external=skip_external
            )
        )

    def tx_preview(tx_json: str) -> str:
        tmp = _write_temp(tx_json)
        try:
            return _dumps(s6_transact.preview_transaction(repo, tmp))
        finally:
            tmp.unlink(missing_ok=True)

    def tx_submit(tx_json: str, overwrite: bool = False) -> str:
        tmp = _write_temp(tx_json)
        try:
            payload = s6_transact.load_transaction(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        tx_dir = repo / ".vrp" / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        path = tx_dir / f"{payload['id']}.json"
        if path.exists() and not overwrite:
            raise SystemExit(
                f"tx_submit: transaction exists (overwrite=True to replace): {path.name}"
            )
        path.write_text(tx_json, encoding="utf-8")
        return _dumps(
            {"submitted": True, "path": str(path), "id": payload["id"], "type": payload["type"]}
        )

    def tx_run(tx_json: str, name: str = "") -> str:
        tmp = _write_temp(tx_json)
        try:
            payload = s6_transact.load_transaction(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        tx_dir = repo / ".vrp" / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        path = tx_dir / f"{payload['id']}.json"
        path.write_text(tx_json, encoding="utf-8")
        verdict = s6_transact.transact(repo, path, name or repo.name, confirm=False)
        return _dumps(verdict)

    def tx_verdicts(tid: str = "") -> str:
        verdicts = s6_transact.load_verdicts(repo)
        if tid:
            match = [v for v in verdicts if v.get("tid") == tid]
            if not match:
                raise SystemExit(f"tx_verdicts: no verdict for tid: {tid}")
            return _dumps(match)
        return _dumps({"count": len(verdicts), "verdicts": verdicts})

    def render_verdicts() -> str:
        return _dumps(render_mod.render_verdicts(repo))

    def render_slice(slice_id: str) -> str:
        return _dumps(render_mod.render_slice(repo, slice_id))

    def render_report() -> str:
        return _dumps(render_mod.render_report(repo))

    def session_begin(note: str = "") -> str:
        return _dumps(session_mod.session_begin(repo, note))

    def session_end(note: str = "") -> str:
        return _dumps(session_mod.session_end(repo, note))

    def session_report(session_id: str = "") -> str:
        return _dumps(session_mod.session_report(repo, session_id))

    def enforce(timeout: int = 600) -> str:
        con = _open_facts_rw(repo)
        try:
            return _dumps(s7_enforce.run(repo, con, timeout))
        finally:
            con.close()

    server.add_tool(
        _guard(nav_search),
        name="nav_search",
        description="Symbol search (AI entry point): name/qualified_name matching, exact matches first; returns fid handles for drill-down",
    )
    server.add_tool(
        _guard(nav_architecture),
        name="nav_architecture",
        description="L0 global map: directory-level aggregation (files/symbols/pathology counts), drill-down handle = module_id (file_path); "
        "prefers the structure table (R2' curate ingest) when present",
    )
    server.add_tool(
        _guard(nav_module),
        name="nav_module",
        description="L1 file-level drill-down: module_id = file_path; returns symbol list + pathology summary + fid handles; "
        "get paths first via nav_search / nav_architecture",
    )
    server.add_tool(
        _guard(nav_symbol),
        name="nav_symbol",
        description="L2 symbol-level drill-down: signature/lines/callers/callees/related findings; "
        "caller/callee fids keep drilling (full chain reachable, no shallow stops)",
    )
    server.add_tool(
        _guard(query_facts),
        name="query_facts",
        description="Lightweight SQL over facts.db (read-only SELECT/WITH, single statement); params is a JSON array string bound positionally; "
        "schema in vrp/schema/facts_schema.sql (five views: v_hotspots/v_cycles/v_dead_code/v_dup_clusters/v_coupling)",
    )
    server.add_tool(
        _guard(nav_callers),
        name="nav_callers",
        description="Full caller list (paginated limit; complements nav_symbol's built-in cap of 20); "
        "truncated=true means raise limit; fid handles drill down via nav_symbol",
    )
    server.add_tool(
        _guard(nav_callees),
        name="nav_callees",
        description="Full callee list (paginated limit; complements nav_symbol's built-in cap of 20); "
        "truncated=true means raise limit; fid handles drill down via nav_symbol",
    )
    server.add_tool(
        _guard(nav_slice),
        name="nav_slice",
        description="Business-line view: steps (with symbol summaries + fid handles) + acts; "
        "slices are ingested via curate_submit; status=stale means symbols/edges drifted",
    )
    server.add_tool(
        _guard(nav_path),
        name="nav_path",
        description="Coupling path lookup: shortest from→to path along edges (src→dst) (WITH RECURSIVE BFS); "
        "hops_detail carries per-hop type/evidence/confidence — verify low-confidence edges by drilling down; "
        "found=false when unreachable (raise max_hops up to 10 or query the reverse direction)",
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
        _guard(curate_slices),
        name="curate_slices",
        description="List ingested business lines (slice_id/name/status/step counts); "
        "status=active is usable, stale=drifted and awaiting re-curation, rejected=user veto",
    )
    server.add_tool(
        _guard(curate_structure),
        name="curate_structure",
        description="Read the ingested overall structure (modules/layers/rules verbatim + status); "
        "structure feeds S7 architecture enforcement and the L0 map",
    )
    server.add_tool(
        _guard(curate_reject),
        name="curate_reject",
        description="User vetoes an ingested artifact (kind=slice|structure, target_id=slice_id or 'structure'): "
        "status→rejected + decision logged; veto is the conservative direction, the agent may relay the user's decision",
    )
    server.add_tool(
        _guard(curate_receipt),
        name="curate_receipt",
        description="Read the latest curate receipt (.vrp/curate-receipt.json): "
        "when rejected, fix the artifact per error codes and resubmit",
    )
    server.add_tool(
        _guard(drift_report),
        name="drift_report",
        description="Unified drift query surface (read-only): per-artifact fresh/missing_fids/missing_edges for active/stale artifacts "
        "+ relocation candidates — fid_ledger identity history maps old fid to qualified_name, then re-resolves to the current fid; "
        "a unique candidate yields a direct remap (F1 read-out aid); when the qualified_name is gone, same-file candidates fill in; "
        "re-curate with the remap and curate_submit again (full re-validation, no bypass); "
        "non-empty summary.active_but_drifted = a hole in the revalidation chain, must be reported",
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
        _guard(tx_verdicts),
        name="tx_verdicts",
        description="Read transaction receipts: tid returns one full record, no listing; "
        "open (non-committed) items feed back into the dossier evidence section automatically",
    )
    server.add_tool(
        _guard(render_verdicts),
        name="render_verdicts",
        description="T4 decision-point briefing: verdict timeline + open/T4 list; "
        "T4 release only via manual CLI vrp transact --confirm",
    )
    server.add_tool(
        _guard(render_slice),
        name="render_slice",
        description="Single business-line briefing: Mermaid sequence source (steps/acts), paste into mermaid.live to render; "
        "get slice_id from curate_slices",
    )
    server.add_tool(
        _guard(render_report),
        name="render_report",
        description="S8 full static report -> .vrp/report/ (report.html + IR + SARIF + Mermaid; "
        "auto-degrades to Mermaid-only when archify is missing); no timestamps, traceable via facts.git_head",
    )
    server.add_tool(
        _guard(session_begin),
        name="session_begin",
        description="Trust-mode opening (design doc v7 §3.3): facts.db snapshot + session metadata; "
        "only one active session per repo at a time; subsequent refactor rounds run undisturbed until session_end",
    )
    server.add_tool(
        _guard(session_end),
        name="session_end",
        description="Trust-mode closing: final snapshot + ended_at + initial-vs-final count diff "
        "(nodes/edges/pathology/xcheck); session_report then delivers the output",
    )
    server.add_tool(
        _guard(session_report),
        name="session_report",
        description="Trust-mode delivery: initial-vs-final count diff + side-by-side L0 architecture graphs (archify, Mermaid fallback) "
        "+ verdict timeline within the session window -> report.html in the session directory; session_id defaults to the latest ended session",
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
