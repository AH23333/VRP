"""#145 测试：完备性标注查询面（学 CBM check_index_coverage——只读消费 coverage_gaps）。

覆盖：盘点汇总/--kind 过滤/exact 命中/祖先目录命中/exact 降权/status 六态
pass 序/recommended_action 全表/无 gap no_recorded_issue/路径 cap/CLI 接线
/MCP over the wire。
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from vrp import cli, coverage
from vrp import mcp_server

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")  # M4: 非 git 仓 = ''
    con.commit()
    con.close()
    return repo


def _add_gap(repo: Path, file_path: str, kind: str, detail: str = "") -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO coverage_gaps (file_path, kind, detail) VALUES (?, ?, ?)",
        (file_path, kind, detail),
    )
    con.commit()
    con.close()


# ---------- 盘点面（paths 空） ----------


def test_summarize_inventory_by_kind(tmp_path):
    """盘点面：kind 分组计数+样本（样本帽 KIND_SAMPLE_CAP=5）。"""
    repo = _mk_repo(tmp_path)
    for i in range(7):
        _add_gap(repo, f"big/f{i}.py", "parse_partial", "1-1")
    _add_gap(repo, "cfg.ini", "parse_unusable", "1-105")
    out = coverage.check_coverage(repo)
    assert out["mode"] == "summary" and out["total_rows"] == 8
    assert out["kinds"]["parse_partial"]["count"] == 7
    assert len(out["kinds"]["parse_partial"]["samples"]) == 5  # 样本帽
    assert out["kinds"]["parse_unusable"]["count"] == 1
    assert out["kinds"]["parse_unusable"]["samples"] == ["cfg.ini"]


def test_summarize_kind_filter(tmp_path):
    """--kind 过滤：盘点面只数目标 kind（total_rows 仍是全量）。"""
    repo = _mk_repo(tmp_path)
    _add_gap(repo, "a.py", "parse_partial")
    _add_gap(repo, "b.py", "parse_unusable", "1-2")
    out = coverage.check_coverage(repo, kinds=["parse_unusable"])
    assert out["total_rows"] == 2 and out["filtered"] is True
    assert set(out["kinds"]) == {"parse_unusable"}
    assert out["kinds"]["parse_unusable"]["count"] == 1


# ---------- 核验面（paths 非空） ----------


def test_check_exact_hit_unusable(tmp_path):
    """exact 命中 parse_unusable → unusable → read_source_directly。"""
    repo = _mk_repo(tmp_path)
    _add_gap(repo, "pytest.ini", "parse_unusable", "1-105")
    out = coverage.check_coverage(repo, paths=["pytest.ini"])
    entry = out["paths"][0]
    assert entry["path"] == "pytest.ini" and entry["status"] == "unusable"
    assert entry["recommended_action"] == "read_source_directly"
    assert entry["matched"] == [
        {"file_path": "pytest.ini", "kind": "parse_unusable", "detail": "1-105", "match": "exact"}
    ]


def test_check_partial_line_ranges(tmp_path):
    """parse_partial（行区间 detail）→ partial → read_ranges_and_verify_scope。"""
    repo = _mk_repo(tmp_path)
    _add_gap(repo, "examples/BigService.json", "parse_partial", "1-1,2-2,3-3,3-5")
    out = coverage.check_coverage(repo, paths=["examples/BigService.json"])
    entry = out["paths"][0]
    assert entry["status"] == "partial"
    assert entry["recommended_action"] == "read_ranges_and_verify_scope"


def test_check_ancestor_dir_match(tmp_path):
    """祖先目录命中：gap 行是请求路径的祖先 → ancestor 标记（not_indexed_dir 语义）。"""
    repo = _mk_repo(tmp_path)
    _add_gap(repo, "docs", "not_indexed_dir", "excluded subtree")
    out = coverage.check_coverage(repo, paths=["docs/guide.md"])
    entry = out["paths"][0]
    assert entry["status"] == "excluded"
    assert entry["recommended_action"] == "read_source_or_change_ignore_rules"
    assert entry["matched"][0]["match"] == "ancestor"
    assert entry["matched"][0]["file_path"] == "docs"


def test_check_exact_downweights_ancestor(tmp_path):
    """exact 行存在只判 exact（CBM 精确命中降权祖先同款）。"""
    repo = _mk_repo(tmp_path)
    _add_gap(repo, "docs", "not_indexed_dir", "excluded subtree")
    _add_gap(repo, "docs/guide.md", "parse_partial", "3-5")
    out = coverage.check_coverage(repo, paths=["docs/guide.md"])
    entry = out["paths"][0]
    assert entry["status"] == "partial"  # 祖先 excluded 不参与判定
    assert all(m["match"] == "exact" for m in entry["matched"])


def test_check_status_pass_order(tmp_path):
    """多行 status 按 CBM pass 序取最先：unusable > partial > excluded > skipped。"""
    repo = _mk_repo(tmp_path)
    _add_gap(repo, "a.py", "parse_partial", "1-1")
    _add_gap(repo, "a.py", "parse_unusable", "1-99")
    out = coverage.check_coverage(repo, paths=["a.py"])
    assert out["paths"][0]["status"] == "unusable"
    # 祖先 excluded 与 exact 并存时 exact 池内判定（此处池=exact 两行）
    _add_gap(repo, "sub", "not_indexed_dir")
    out2 = coverage.check_coverage(repo, paths=["sub/a.py"])
    assert out2["paths"][0]["status"] == "excluded"


def test_check_unresolved_calls_and_skipped(tmp_path):
    """unresolved_calls → read_source_and_verify_calls；其余非空 kind → skipped。"""
    repo = _mk_repo(tmp_path)
    _add_gap(repo, "dyn.js", "unresolved_calls", "5 dynamic calls")
    _add_gap(repo, "gen.rs", "custom_kind", "")
    out = coverage.check_coverage(repo, paths=["dyn.js", "gen.rs"])
    assert out["paths"][0]["status"] == "unresolved_calls"
    assert out["paths"][0]["recommended_action"] == "read_source_and_verify_calls"
    assert out["paths"][1]["status"] == "skipped"
    assert out["paths"][1]["recommended_action"] == "read_source_directly"


def test_check_no_recorded_issue_and_clean_note(tmp_path):
    """无 gap 命中 → no_recorded_issue（不证明完备——clean ≠ proof）。"""
    repo = _mk_repo(tmp_path)
    _add_gap(repo, "other.py", "parse_partial", "1-1")
    out = coverage.check_coverage(repo, paths=["clean.py"])
    entry = out["paths"][0]
    assert entry["status"] == "no_recorded_issue"
    assert entry["matched"] == []
    assert entry["recommended_action"] == "use_graph_with_best_effort_caveat"
    assert "not proof of completeness" in out["note"]


def test_check_kind_filter_on_check(tmp_path):
    """kinds 过滤也适用于核验面：过滤后无命中 → no_recorded_issue。"""
    repo = _mk_repo(tmp_path)
    _add_gap(repo, "a.py", "parse_unusable", "1-9")
    out = coverage.check_coverage(repo, paths=["a.py"], kinds=["parse_partial"])
    assert out["paths"][0]["status"] == "no_recorded_issue"
    assert out["paths"][0]["matched"] == []


def test_check_path_cap(tmp_path):
    """paths 超 COVERAGE_PATH_CAP=128 → SystemExit（CBM maxItems 同款）。"""
    repo = _mk_repo(tmp_path)
    with pytest.raises(SystemExit, match="cap 128"):
        coverage.check_coverage(repo, paths=[f"f{i}.py" for i in range(129)])


def test_check_missing_facts_db(tmp_path):
    """facts.db 缺 → SystemExit 诚实（rc=1 语义）。"""
    repo = tmp_path / "empty"
    (repo / ".vrp").mkdir(parents=True)
    with pytest.raises(SystemExit, match="facts.db missing"):
        coverage.check_coverage(repo, paths=["a.py"])


def test_recommended_actions_full_table():
    """recommended_action 全表锁（CBM coverage_recommended_action 照抄）。"""
    assert coverage.RECOMMENDED_ACTIONS == {
        "unusable": "read_source_directly",
        "partial": "read_ranges_and_verify_scope",
        "unresolved_calls": "read_source_and_verify_calls",
        "skipped": "read_source_directly",
        "excluded": "read_source_or_change_ignore_rules",
        "no_recorded_issue": "use_graph_with_best_effort_caveat",
    }


# ---------- CLI 接线 ----------


def test_cli_coverage_wiring(tmp_path, capsys):
    """CLI vrp coverage 接线：盘点/核验 + --path 可重复。"""
    repo = _mk_repo(tmp_path)
    _add_gap(repo, "pytest.ini", "parse_unusable", "1-105")
    _add_gap(repo, "docs", "not_indexed_dir", "excluded subtree")
    parser = cli.build_parser()
    args = parser.parse_args(["coverage", str(repo), "--json"])
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["mode"] == "summary" and out["total_rows"] == 2
    args = parser.parse_args(
        ["coverage", str(repo), "--path", "pytest.ini", "--path", "docs/x.md", "--kind", "parse_unusable", "--json"]
    )
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["mode"] == "check"
    assert [p["status"] for p in out["paths"]] == ["unusable", "no_recorded_issue"]


def test_cli_coverage_missing_facts_rejected(tmp_path):
    repo = tmp_path / "empty"
    (repo / ".vrp").mkdir(parents=True)
    parser = cli.build_parser()
    args = parser.parse_args(["coverage", str(repo)])
    with pytest.raises(SystemExit, match="facts.db missing"):
        args.func(args)


# ---------- MCP over the wire ----------


def test_coverage_over_mcp(tmp_path):
    """MCP vrp_query(type=coverage) over the wire：in-process 对打（seams 同款 _drive）。"""
    repo = _mk_repo(tmp_path)
    _add_gap(repo, "a.py", "parse_partial", "1-1")

    async def drive() -> dict:
        results: dict = {}
        server = mcp_server.build_server(repo)
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
                        res = await session.call_tool(
                            "vrp_query",
                            {"query_type": "coverage", "options_json": json.dumps({"paths": ["a.py"]})},
                        )
                        results["check"] = json.loads(res.content[0].text)
                        res = await session.call_tool("vrp_query", {"query_type": "coverage", "options_json": "{}"})
                        results["summary"] = json.loads(res.content[0].text)
                    finally:
                        tg.cancel_scope.cancel()
        return results

    out = asyncio.run(drive())
    assert out["check"]["paths"][0]["status"] == "partial"
    assert out["summary"]["mode"] == "summary" and out["summary"]["total_rows"] == 1
