"""#146 测试：测试影响查询面（学 CBM detect_changes/test_impact——只读消费 facts）。

覆盖：变更面（worktree hunk 播种/--base 并集/untracked/非 git 仓诚实错/
空变更）/播种（hunk 重叠/零重叠回退整文件/hunk cap 退整文件/--paths 显式/
结构 label 剔除）/BFS（hop+depth clamp/MIN hop 跨种子/direction 三态+边类型
过滤/saturation）/建议测试（种子命中/impacted 命中/关闭）/模块聚合/
CLI 接线/MCP over the wire。
"""

import asyncio
import json
import sqlite3
import subprocess
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from vrp import cli, impact
from vrp import mcp_server

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path, git: bool = False) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.commit()
    con.close()
    if git:
        (repo / ".gitignore").write_text(".vrp/\n", encoding="utf-8")
        _git(repo, "init")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
    return repo


def _commit_all(repo: Path, msg: str = "c") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def _add_symbol(repo: Path, fid: str, qn: str, file_path: str,
                start: int, end: int, label: str = "Function") -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
        " start_line, end_line, content_hash) VALUES (?, 'p', ?, ?, ?, ?, ?, ?, 'h')",
        (fid, qn, label, qn.split(".")[-1], file_path, start, end),
    )
    con.commit()
    con.close()


def _add_edge(repo: Path, src: str, dst: str, etype: str = "CALLS") -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?, ?, ?, 'static')",
        (src, dst, etype),
    )
    con.commit()
    con.close()


def _add_link(repo: Path, test_fid: str, target_fid: str) -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO test_linkage (test_fid, target_fid, source) VALUES (?, ?, 'cbm')",
        (test_fid, target_fid),
    )
    con.commit()
    con.close()


# ---------- 变更面 ----------


def test_not_git_repo_honest_error(tmp_path):
    """非 git 仓且无 --paths → SystemExit 诚实（无 HEAD 可 diff）。"""
    repo = _mk_repo(tmp_path)
    with pytest.raises(SystemExit, match="not a git repo"):
        impact.impact_report(repo)


def test_missing_facts_db(tmp_path):
    """facts.db 缺 → SystemExit 诚实。"""
    repo = tmp_path / "empty"
    (repo / ".vrp").mkdir(parents=True)
    with pytest.raises(SystemExit, match="facts.db missing"):
        impact.impact_report(repo, paths=["a.py"])


def test_worktree_hunk_seeding(tmp_path):
    """默认变更面：worktree 相对 HEAD 的 diff hunk 与符号区间重叠 → 播种。"""
    repo = _mk_repo(tmp_path, git=True)
    a = repo / "a.py"
    a.write_text("def foo():\n    return 1\n", encoding="utf-8")
    _commit_all(repo, "init")
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 2)
    a.write_text("def foo():\n    return 2\n", encoding="utf-8")
    out = impact.impact_report(repo)
    assert out["base"] == "HEAD (worktree+index)"
    assert out["changed_files"] == ["a.py"]
    assert [s["qn"] for s in out["seeds"]] == ["a.foo"]
    assert out["saturated"] is False
    assert "suggested is NOT sufficient" in out["note"]


def test_zero_overlap_whole_file_fallback(tmp_path):
    """hunk 与全部符号零重叠 → 回退整文件播种（CBM 绝不欠播种同款）。"""
    repo = _mk_repo(tmp_path, git=True)
    a = repo / "a.py"
    a.write_text("\n" * 9 + "x = 1\n", encoding="utf-8")
    _commit_all(repo, "init")
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 2)
    _add_symbol(repo, "f2", "a.bar", "a.py", 4, 5)
    a.write_text("\n" * 9 + "x = 2\n", encoding="utf-8")  # hunk 行 10，零重叠
    out = impact.impact_report(repo)
    assert {s["qn"] for s in out["seeds"]} == {"a.foo", "a.bar"}


def test_untracked_file_listed_no_seeds(tmp_path):
    """untracked 新文件：changed 列出但 facts 无符号 → seeds 空（诚实）。"""
    repo = _mk_repo(tmp_path, git=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "init")
    (repo / "new.py").write_text("y = 2\n", encoding="utf-8")
    out = impact.impact_report(repo)
    assert out["changed_files"] == ["new.py"]
    assert out["seeds"] == [] and out["impacted"] == [] and out["suggested_tests"] == []


def test_empty_change_face(tmp_path):
    """干净 worktree 无 --paths → 空变更面（changed/seeds/impacted 全空）。"""
    repo = _mk_repo(tmp_path, git=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "init")
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 1)
    out = impact.impact_report(repo)
    assert out["changed_files"] == [] and out["seeds"] == []
    assert out["impacted_total"] == 0 and out["suggested_tests"] == []


def test_base_ref_union_committed_diff(tmp_path):
    """--base：merge-base..HEAD 已提交 diff ∪ worktree 并集（CBM 三路同款）。"""
    repo = _mk_repo(tmp_path, git=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "c1")
    (repo / "b.py").write_text("def bar():\n    pass\n", encoding="utf-8")
    _commit_all(repo, "c2")
    _add_symbol(repo, "f2", "b.bar", "b.py", 1, 2)
    out = impact.impact_report(repo, base="HEAD~1")
    assert out["base"] == "HEAD~1"
    assert out["changed_files"] == ["b.py"]
    assert [s["qn"] for s in out["seeds"]] == ["b.bar"]


# ---------- 播种 ----------


def test_explicit_paths_skip_git(tmp_path):
    """--paths 显式播种：跳过 git（整文件），非 git 仓可用。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 2)
    _add_symbol(repo, "f2", "b.bar", "b.py", 1, 2)
    out = impact.impact_report(repo, paths=["a.py"])
    assert out["changed_files"] == ["a.py"]
    assert [s["qn"] for s in out["seeds"]] == ["a.foo"]


def test_structure_labels_excluded_from_seeds(tmp_path):
    """结构容器 label（File/Module/...）不播种（CBM 剔结构容器同款）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 2)
    _add_symbol(repo, "f0", "a", "a.py", 0, 99, label="File")
    _add_symbol(repo, "f9", "mod", "a.py", 0, 99, label="Module")
    out = impact.impact_report(repo, paths=["a.py"])
    assert [s["qn"] for s in out["seeds"]] == ["a.foo"]


def test_hunk_cap_whole_file_seeding(tmp_path, monkeypatch):
    """hunk 超 HUNK_CAP → 整体退整文件播种（绝不静默欠播种）。"""
    repo = _mk_repo(tmp_path, git=True)
    a = repo / "a.py"
    a.write_text("def foo():\n    pass\n\n\ndef bar():\n    pass\n", encoding="utf-8")
    _commit_all(repo, "init")
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 2)
    _add_symbol(repo, "f2", "a.bar", "a.py", 5, 6)
    a.write_text("def foo():\n    return 1\n\n\ndef bar():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(impact, "HUNK_CAP", 0)  # 任意 hunk 即超限
    out = impact.impact_report(repo)
    assert {s["qn"] for s in out["seeds"]} == {"a.foo", "a.bar"}


# ---------- BFS ----------


def test_bfs_inbound_hops_and_depth_clamp(tmp_path):
    """inbound hop 链：depth 截断生效；depth clamp [1, 15]。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 2)
    _add_symbol(repo, "c1", "b.bar", "b.py", 1, 2)
    _add_symbol(repo, "c2", "c.baz", "c.py", 1, 2)
    _add_edge(repo, "c1", "f1")   # bar -> foo
    _add_edge(repo, "c2", "c1")   # baz -> bar
    out = impact.impact_report(repo, paths=["a.py"], depth=2)
    assert {i["qn"]: i["hop"] for i in out["impacted"]} == {"b.bar": 1, "c.baz": 2}
    out1 = impact.impact_report(repo, paths=["a.py"], depth=1)
    assert [i["qn"] for i in out1["impacted"]] == ["b.bar"]
    assert impact.impact_report(repo, paths=["a.py"], depth=99)["depth"] == 15
    assert impact.impact_report(repo, paths=["a.py"], depth=0)["depth"] == 1


def test_bfs_min_hop_across_seeds(tmp_path):
    """多源 BFS 跨种子 MIN(hop)：同目标取最短（CBM bfs_multi 同款）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "s1", "a.s1", "a.py", 1, 2)
    _add_symbol(repo, "s2", "b.s2", "b.py", 1, 2)
    _add_symbol(repo, "m", "m.mid", "m.py", 1, 2)
    _add_symbol(repo, "t", "t.target", "t.py", 1, 2)
    _add_edge(repo, "t", "s1")    # target 距 s1 一跳
    _add_edge(repo, "t", "m")     # 距 s2 两跳（经 mid）
    _add_edge(repo, "m", "s2")
    out = impact.impact_report(repo, paths=["a.py", "b.py"], depth=5)
    hops = {i["qn"]: i["hop"] for i in out["impacted"]}
    assert hops["t.target"] == 1 and hops["m.mid"] == 1


def test_direction_outbound_both_and_edge_filter(tmp_path):
    """direction 三态 + 边类型过滤：只走 CALLS/CALL_REFERENCE，IMPORTS 不走。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 2)
    _add_symbol(repo, "q", "q.qux", "q.py", 1, 2)
    _add_symbol(repo, "z", "z.zzz", "z.py", 1, 2)
    _add_edge(repo, "f1", "q")            # foo 调 qux（outbound 邻居）
    _add_edge(repo, "f1", "z", "IMPORTS") # 非影响边类型
    out = impact.impact_report(repo, paths=["a.py"], direction="outbound")
    assert [i["qn"] for i in out["impacted"]] == ["q.qux"]
    out = impact.impact_report(repo, paths=["a.py"], direction="both")
    assert [i["qn"] for i in out["impacted"]] == ["q.qux"]
    out = impact.impact_report(repo, paths=["a.py"], direction="inbound")
    assert out["impacted"] == []
    with pytest.raises(SystemExit, match="direction must be"):
        impact.impact_report(repo, paths=["a.py"], direction="sideways")


def test_row_cap_saturation(tmp_path, monkeypatch):
    """行上限触顶 → saturated 诚实（总数=下界，CBM engine_saturated 同款）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 2)
    _add_symbol(repo, "c1", "b.c1", "b.py", 1, 2)
    _add_symbol(repo, "c2", "c.c2", "c.py", 1, 2)
    _add_edge(repo, "c1", "f1")
    _add_edge(repo, "c2", "f1")
    monkeypatch.setattr(impact, "IMPACT_ROW_CAP", 1)
    out = impact.impact_report(repo, paths=["a.py"])
    assert out["saturated"] is True
    assert out["impacted_total"] == 1 and len(out["impacted"]) == 1


# ---------- 建议测试 / 模块 ----------


def test_suggested_tests_seed_and_impacted(tmp_path):
    """test_linkage 消费：种子命中+受影响命中都建议；--no-tests 关闭。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 2)
    _add_symbol(repo, "c1", "b.bar", "b.py", 1, 2)
    _add_edge(repo, "c1", "f1")
    _add_symbol(repo, "t1", "tests.unit.test_a::test_foo", "tests/unit/test_a.py", 1, 5)
    _add_link(repo, "t1", "f1")
    _add_symbol(repo, "t2", "tests.unit.test_b::test_bar", "tests/unit/test_b.py", 1, 5)
    _add_link(repo, "t2", "c1")
    out = impact.impact_report(repo, paths=["a.py"])
    assert out["suggested_tests"] == [
        {"test_file": "tests/unit/test_a.py", "tests": 1, "sample_qns": ["tests.unit.test_a::test_foo"]},
        {"test_file": "tests/unit/test_b.py", "tests": 1, "sample_qns": ["tests.unit.test_b::test_bar"]},
    ]
    out = impact.impact_report(repo, paths=["a.py"], tests=False)
    assert out["suggested_tests"] == []


def test_modules_top2_and_overflow(tmp_path, monkeypatch):
    """模块 top-2 路径段聚合；超 IMPACT_MODULE_CAP 溢出 '(other)'。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a.foo", "src/mod1/a.py", 1, 2)
    _add_symbol(repo, "f2", "b.bar", "src/mod2/b.py", 1, 2)
    out = impact.impact_report(repo, paths=["src/mod1/a.py", "src/mod2/b.py"], tests=False)
    assert {m["module"] for m in out["modules"]} == {"src/mod1", "src/mod2"}
    monkeypatch.setattr(impact, "IMPACT_MODULE_CAP", 1)
    out = impact.impact_report(repo, paths=["src/mod1/a.py", "src/mod2/b.py"], tests=False)
    assert [m["module"] for m in out["modules"]] == ["src/mod1", "(other)"]
    assert out["modules"][1]["count"] == 1


# ---------- CLI / MCP ----------


def test_cli_impact_wiring(tmp_path, capsys):
    """CLI vrp impact 接线：--path 可重复/--depth/--direction/--no-tests。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 2)
    parser = cli.build_parser()
    args = parser.parse_args(["impact", str(repo), "--path", "a.py"])
    assert args.func(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["level"] == "impact" and out["depth"] == 2
    assert [s["qn"] for s in out["seeds"]] == ["a.foo"]
    args = parser.parse_args(
        ["impact", str(repo), "--path", "a.py", "--depth", "3",
         "--direction", "both", "--no-tests"]
    )
    assert args.func(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["depth"] == 3 and out["direction"] == "both"
    assert out["suggested_tests"] == []


def test_impact_over_mcp(tmp_path):
    """MCP impact 工具 over the wire：in-process 对打（coverage 同款 _drive）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a.foo", "a.py", 1, 2)
    _add_symbol(repo, "c1", "b.bar", "b.py", 1, 2)
    _add_edge(repo, "c1", "f1")
    _add_symbol(repo, "t1", "tests.unit.test_a::test_foo", "tests/unit/test_a.py", 1, 5)
    _add_link(repo, "t1", "f1")

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
                            {"query_type": "impact", "options_json": json.dumps({"paths": ["a.py"]})},
                        )
                        results["paths"] = json.loads(res.content[0].text)
                        res = await session.call_tool("vrp_query", {"query_type": "impact", "options_json": "{}"})
                        results["auto"] = json.loads(res.content[0].text)
                    finally:
                        tg.cancel_scope.cancel()
        return results

    out = asyncio.run(drive())
    assert out["paths"]["level"] == "impact"
    assert [i["qn"] for i in out["paths"]["impacted"]] == ["b.bar"]
    assert out["paths"]["suggested_tests"][0]["test_file"] == "tests/unit/test_a.py"
    # 空 {}：非 git 仓自动变更面 → _guard 转诚实 error JSON（不炸 server）
    assert "not a git repo" in out["auto"]["error"]
