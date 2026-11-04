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
    "query_facts",
    "tx_preview",
    "tx_submit",
    "tx_run",
    "tx_verdicts",
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
        return {"search": search, "mod": mod, "sym": sym, "facts": facts, "arch": arch}

    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert out["search"]["count"] == 1
    assert out["search"]["results"][0]["qualified_name"] == "pkg.calc.total"
    assert out["mod"]["symbols"][0]["fid"] == "f1"
    assert out["sym"]["symbol"]["qualified_name"] == "pkg.calc.total"
    assert out["facts"]["rows"] == [{"n": 1}]
    assert out["arch"]["fallback"] == "dir-aggregation"


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


def test_cli_mcp_wiring(tmp_path, monkeypatch):
    from vrp import cli

    got: dict = {}
    monkeypatch.setattr(mcp_server, "run_stdio", lambda repo: got.update(repo=repo))
    repo = _mk_repo(tmp_path)
    rc = cli.main(["mcp", str(repo)])
    assert rc == 0
    assert got["repo"] == repo
