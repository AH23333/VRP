import asyncio
import json
import sqlite3
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from vrp import mcp_server

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

TOOL_NAMES = {
    "nav_search",
    "nav_architecture",
    "nav_module",
    "nav_symbol",
    "nav_callers",
    "nav_callees",
    "nav_slice",
    "nav_path",
    "query_facts",
    "curate_submit",
    "curate_slices",
    "curate_structure",
    "curate_reject",
    "curate_receipt",
    "tx_preview",
    "tx_submit",
    "tx_run",
    "tx_verdicts",
    "render_verdicts",
    "render_slice",
    "render_report",
    "session_begin",
    "session_end",
    "session_report",
    "enforce",
}


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'pkg.calc.total', 'Function', 'total', 'pkg/calc.py', 1, 3, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'pkg.calc.double', 'Function', 'double', 'pkg/calc.py', 5, 7, 'h')"
    )
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('f1', 'f2', 'CALLS', 'structural')")
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")  # M4: 非 git 仓 = ''
    con.commit()
    con.close()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "calc.py").write_text("def total(a, b):\n    return a + b\n", encoding="utf-8")
    return repo


def _tx_json(tid: str = "tx-mcp", ttype: str = "T2") -> str:
    return json.dumps(
        {
            "id": tid,
            "type": ttype,
            "summary": "mcp test tx",
            "declared_moves": [],
            "edits": [
                {
                    "file": "pkg/calc.py",
                    "op": "modify",
                    "content": "def total(a, b):\n    return a + b + 1\n",
                }
            ],
        }
    )


def _run(coro):
    return asyncio.run(coro)


async def _drive(repo: Path, actions: dict) -> dict:
    """in-process 对打：memory streams + ClientSession + lowlevel server task。"""
    server = mcp_server.build_server(repo)
    results: dict = {}
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with ClientSession(*client_streams) as session:
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    server._lowlevel_server.run,
                    server_streams[0],
                    server_streams[1],
                    server._lowlevel_server.create_initialization_options(),
                )
                await session.initialize()
                try:
                    for key, fn in actions.items():
                        results[key] = await fn(session)
                finally:
                    tg.cancel_scope.cancel()
    return results


async def _tools(session: ClientSession):
    listing = await session.list_tools()
    return {t.name: t for t in listing.tools}


async def _call(session: ClientSession, name: str, args: dict):
    result = await session.call_tool(name, args)
    text = result.content[0].text
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text, "_isError": result.is_error}


def test_list_tools_nine_and_no_confirm_param(tmp_path):
    repo = _mk_repo(tmp_path)
    tools = _run(_drive(repo, {"tools": _tools}))["tools"]
    assert set(tools) == TOOL_NAMES
    for name, tool in tools.items():
        assert tool.description, f"{name} missing description"
        props = (tool.input_schema or {}).get("properties", {})
        assert "confirm" not in props, f"{name} must not expose confirm"
        assert "repo" not in props, f"{name} must not expose repo (per-repo binding)"


def test_nav_chain_over_mcp(tmp_path):
    repo = _mk_repo(tmp_path)

    async def plan(session: ClientSession):
        search = await _call(session, "nav_search", {"query": "total"})
        fid = search["results"][0]["fid"]
        mod = await _call(session, "nav_module", {"module_id": "pkg/calc.py"})
        sym = await _call(session, "nav_symbol", {"fid": fid})
        facts = await _call(session, "query_facts", {"sql": "SELECT COUNT(*) AS n FROM symbols"})
        arch = await _call(session, "nav_architecture", {})
        path = await _call(session, "nav_path", {"from_fid": "f1", "to_fid": "f2"})
        rev = await _call(session, "nav_path", {"from_fid": "f2", "to_fid": "f1"})
        return {"search": search, "mod": mod, "sym": sym, "facts": facts, "arch": arch,
                "path": path, "rev": rev}

    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert out["search"]["count"] == 1
    assert out["search"]["results"][0]["qualified_name"] == "pkg.calc.total"
    assert out["mod"]["symbols"][0]["fid"] == "f1"
    assert out["sym"]["symbol"]["qualified_name"] == "pkg.calc.total"
    assert out["facts"]["rows"] == [{"n": 2}]
    assert out["arch"]["fallback"] == "dir-aggregation"
    assert out["path"]["found"] is True
    assert out["path"]["hops"] == 1
    assert [p["fid"] for p in out["path"]["path"]] == ["f1", "f2"]
    assert out["path"]["hops_detail"][0]["type"] == "CALLS"
    assert out["rev"]["found"] is False


def test_error_json_not_crash(tmp_path):
    repo = _mk_repo(tmp_path)

    async def plan(session: ClientSession):
        bad_sql = await _call(session, "query_facts", {"sql": "DROP TABLE symbols"})
        unknown_fid = await _call(session, "nav_symbol", {"fid": "nope"})
        return {"bad_sql": bad_sql, "unknown_fid": unknown_fid}

    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert "error" in out["bad_sql"]
    assert "only SELECT/WITH" in out["bad_sql"]["error"]
    assert "error" in out["unknown_fid"]
    assert "unknown fid" in out["unknown_fid"]["error"]


def test_tx_preview_submit_verdicts(tmp_path):
    repo = _mk_repo(tmp_path)

    async def plan(session: ClientSession):
        preview = await _call(session, "tx_preview", {"tx_json": _tx_json()})
        submit = await _call(session, "tx_submit", {"tx_json": _tx_json("tx-sub")})
        dup = await _call(session, "tx_submit", {"tx_json": _tx_json("tx-sub")})
        verdicts = await _call(session, "tx_verdicts", {})
        return {"preview": preview, "submit": submit, "dup": dup, "verdicts": verdicts}

    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert out["preview"]["ok"] is True
    assert out["preview"]["tid"] == "tx-mcp"
    assert "-    return a + b\n" in out["preview"]["edits"][0]["diff"]
    assert "+    return a + b + 1" in out["preview"]["edits"][0]["diff"]
    assert out["submit"]["submitted"] is True
    assert (repo / ".vrp" / "transactions" / "tx-sub.json").exists()
    assert "error" in out["dup"]
    assert out["verdicts"]["count"] == 0


def test_tx_run_t4_gate_never_confirms(tmp_path, monkeypatch):
    """T4 安全闸：tx_run 恒 confirm=False 调 s6 层（agent 不可绕过）。"""
    repo = _mk_repo(tmp_path)
    calls: list[dict] = []

    def fake_transact(repo_arg, path, name, confirm=False, timeout=900):
        calls.append({"confirm": confirm, "path": str(path)})
        return {
            "tid": "tx-t4",
            "type": "T4",
            "summary": "improve",
            "status": "needs_confirmation",
            "failed_gate": None,
            "gates": [],
            "verdict_at": "2026-11-04T00:00:00Z",
            "confidence": "low",
        }

    monkeypatch.setattr(mcp_server.s6_transact, "transact", fake_transact)

    async def plan(session: ClientSession):
        result = await _call(session, "tx_run", {"tx_json": _tx_json("tx-t4", "T4")})
        return result

    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert calls and calls[0]["confirm"] is False
    assert out["status"] == "needs_confirmation"
    assert out["type"] == "T4"
    assert (repo / ".vrp" / "transactions" / "tx-t4.json").exists()


def test_tx_run_stages_transaction_then_calls_transact(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    staged: list[Path] = []

    def fake_transact(repo_arg, path, name, confirm=False, timeout=900):
        staged.append(Path(path))
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        assert payload["id"] == "tx-stage"
        return {"tid": "tx-stage", "type": "T1", "status": "committed", "failed_gate": None,
                "gates": [], "verdict_at": "t", "commit": "abc"}

    monkeypatch.setattr(mcp_server.s6_transact, "transact", fake_transact)

    async def plan(session: ClientSession):
        return await _call(session, "tx_run", {"tx_json": _tx_json("tx-stage", "T1")})

    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert out["status"] == "committed"
    assert staged and staged[0].parent == repo / ".vrp" / "transactions"


def test_curate_flow_over_mcp(tmp_path):
    """curate 五件 + nav 三件 over MCP：submit 验收入库 -> 读回 -> reject 留痕 -> receipt。"""

    slices_ok = json.dumps([{
        "slice_id": "calc-flow",
        "name": "计算线",
        "summary": "total 调 double",
        "steps": [{"fid": "f1", "role": "entry"}, {"fid": "f2", "role": "process"}],
    }], ensure_ascii=False)
    slices_bad = json.dumps([{
        "slice_id": "bad-flow", "name": "n", "summary": "s",
        "steps": [{"fid": "ghost", "role": "entry"}],
    }])
    structure_ok = json.dumps({
        "modules": [{"module_id": "m1", "name": "计算", "responsibility": "算", "symbol_fids": ["f1", "f2"], "layer": "domain"}],
        "layers": [{"name": "domain", "may_depend_on": []}],
    }, ensure_ascii=False)

    async def plan(session: ClientSession):
        accept = await _call(session, "curate_submit", {"kind": "slices", "payload_json": slices_ok})
        reject = await _call(session, "curate_submit", {"kind": "slices", "payload_json": slices_bad})
        st = await _call(session, "curate_submit", {"kind": "structure", "payload_json": structure_ok})
        listing = await _call(session, "curate_slices", {})
        struct = await _call(session, "curate_structure", {})
        arch = await _call(session, "nav_architecture", {})
        sl = await _call(session, "nav_slice", {"slice_id": "calc-flow"})
        callers = await _call(session, "nav_callers", {"fid": "f2"})
        veto = await _call(session, "curate_reject", {"kind": "slice", "target_id": "calc-flow", "note": "n"})
        after = await _call(session, "curate_slices", {})
        return {"accept": accept, "reject": reject, "st": st, "listing": listing, "struct": struct,
                "arch": arch, "sl": sl, "callers": callers, "veto": veto, "after": after}

    repo = _mk_repo(tmp_path)
    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert out["accept"]["status"] == "accepted"
    assert out["accept"]["accepted"] == 1
    assert out["reject"]["status"] == "rejected"
    assert out["reject"]["errors"][0]["code"] == "SLICE_UNKNOWN_FID"
    assert out["st"]["status"] == "accepted"  # structure submit 收据（读回看 struct.status=active）
    assert out["listing"]["slices"][0]["slice_id"] == "calc-flow"
    assert out["listing"]["slices"][0]["steps"] == 2
    assert out["struct"]["status"] == "active"
    assert out["struct"]["payload"]["modules"][0]["module_id"] == "m1"
    assert out["arch"]["source"] == "structure"
    assert out["arch"]["module_edges"] == []  # f1->f2 同属 m1，模块间边不计自身
    assert out["sl"]["status"] == "active"
    assert out["sl"]["steps"][1]["qualified_name"] == "pkg.calc.double"
    assert out["callers"]["neighbors"][0]["fid"] == "f1"
    assert out["veto"]["status"] == "rejected"
    assert out["after"]["slices"][0]["status"] == "rejected"
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert con.execute("SELECT note FROM decisions").fetchone()[0] == "n"
    finally:
        con.close()


def test_render_tools_over_mcp(tmp_path, monkeypatch):
    """render 三件 over MCP：verdicts 快材料 + slice 快材料 + 全量报告（降级不炸）。"""
    from vrp import render as render_mod

    monkeypatch.delenv("VRP_ARCHIFY_MJS", raising=False)
    monkeypatch.setattr(render_mod, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO slices (slice_id, name, summary, payload_json, status) VALUES ('calc-flow', 'n', 's', ?, 'active')",
        (json.dumps({"steps": [{"fid": "f1", "role": "entry"}, {"fid": "f2", "role": "process"}]}),),
    )
    con.commit()
    con.close()
    (repo / ".vrp" / "verdicts").mkdir(parents=True)
    (repo / ".vrp" / "verdicts" / "tx-1.json").write_text(json.dumps({
        "tid": "tx-1", "type": "T2", "status": "needs_confirmation",
        "verdict_at": "2026-11-04T10:00:00+00:00",
    }), encoding="utf-8")

    async def plan(session: ClientSession):
        verdicts = await _call(session, "render_verdicts", {})
        sl = await _call(session, "render_slice", {"slice_id": "calc-flow"})
        report = await _call(session, "render_report", {})
        return {"verdicts": verdicts, "sl": sl, "report": report}

    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert out["verdicts"]["count"] == 1
    assert out["verdicts"]["t4_pending"][0]["tid"] == "tx-1"
    assert out["sl"]["slice_id"] == "calc-flow"
    assert out["sl"]["mermaid"].startswith("sequenceDiagram")
    assert out["report"]["archify"] is False
    assert any("archify not found" in d for d in out["report"]["degraded"])
    assert (repo / ".vrp" / "report" / "report.html").is_file()


def test_cli_mcp_wiring(tmp_path, monkeypatch):
    from vrp import cli

    got: dict = {}
    monkeypatch.setattr(mcp_server, "run_stdio", lambda repo: got.update(repo=repo))
    repo = _mk_repo(tmp_path)
    rc = cli.main(["mcp", str(repo)])
    assert rc == 0
    assert got["repo"] == repo


def test_session_flow_over_mcp(tmp_path, monkeypatch):
    """R3.5' session 三件 over MCP：begin（快照+元数据）-> end（diff）-> report（交付）。"""
    from vrp import render as render_mod

    monkeypatch.delenv("VRP_ARCHIFY_MJS", raising=False)
    monkeypatch.setattr(render_mod, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)

    async def plan(session: ClientSession):
        begin = await _call(session, "session_begin", {"note": "trust round"})
        mid = await _call(session, "session_begin", {"note": "should fail"})  # 活动会话互斥
        end = await _call(session, "session_end", {})
        report = await _call(session, "session_report", {})
        return {"begin": begin, "mid": mid, "end": end, "report": report}

    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert out["begin"]["session_id"]
    assert out["begin"]["begin"]["counts"]["symbols"] == 2
    assert "already active" in out["mid"]["error"]
    assert out["end"]["session_id"] == out["begin"]["session_id"]
    assert out["end"]["diff"]["symbols"] == {"begin": 2, "end": 2, "delta": 0}
    assert out["report"]["archify"] is False
    assert out["report"]["verdicts_in_window"] == 0
    sdir = repo / ".vrp" / "sessions" / out["begin"]["session_id"]
    assert (sdir / "report.html").is_file()
    assert (sdir / "begin.db").is_file() and (sdir / "end.db").is_file()


def test_enforce_over_mcp(tmp_path, monkeypatch):
    """S7 enforce over MCP：无 structure 诚实 skipped -> curate_submit -> 执法落 P9（mocked 执法器）。"""
    from vrp import s7_enforce

    structure_ok = json.dumps({
        "modules": [
            {"module_id": "m1", "name": "算", "responsibility": "算", "symbol_fids": ["f1"], "layer": "domain"},
            {"module_id": "m2", "name": "面", "responsibility": "面", "symbol_fids": ["f2"], "layer": "service"},
        ],
        "layers": [
            {"name": "domain", "may_depend_on": []},
            {"name": "service", "may_depend_on": ["domain"]},
        ],
    }, ensure_ascii=False)
    canned = (
        "Contracts: 0 kept, 1 broken.\n\n"
        "Broken contracts\n----------------\n\n"
        "layer: domain 不许 import service\n--\n\n"
        "domain is not allowed to import service:\n\n"
        "-   pkg.calc -> pkg.calc (l.1)\n"
    )
    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: "fake-lint-imports")
    monkeypatch.setattr(s7_enforce, "_execute", lambda *args: (1, canned))

    async def plan(session: ClientSession):
        skipped = await _call(session, "enforce", {})  # 尚无 structure
        st = await _call(session, "curate_submit", {"kind": "structure", "payload_json": structure_ok})
        out = await _call(session, "enforce", {})
        return {"skipped": skipped, "st": st, "out": out}

    repo = _mk_repo(tmp_path)
    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert out["skipped"]["status"] == "skipped"
    assert "no ingested structure" in out["skipped"]["reason"]
    assert out["st"]["status"] == "accepted"
    assert out["out"]["status"] == "ok"
    assert out["out"]["findings_written"] == 1
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        row = con.execute(
            "SELECT pathology, kind, source FROM findings WHERE source = 's7-enforce'"
        ).fetchone()
        assert row == ("P9", "layer-violation", "s7-enforce")
    finally:
        con.close()
