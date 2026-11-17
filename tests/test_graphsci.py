"""R1 vrp graph：网络科学指标（构想v12）。

守卫重点：确定性红线（同输入同输出，无随机性）、dangling mass 守恒
（质量和为 1）、LPA 强团弱桥稳定 vs 对称互对吞并（诚实局限测试锁）、
桩过滤（file_path 空不进图）、type 过滤（只 CALLS/CALL_REFERENCE）、
对账口径（只 active structure）、facts.db 缺失诚实 SystemExit。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from vrp import graphsci as gs
from vrp.cli import build_parser, cmd_graph


def _seed_symbol(con, fid: str, name: str, path: str | None = None) -> None:
    if path is None:
        path = f"{name}.py"
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path)"
        " VALUES (?, 'p', ?, 'Function', ?, ?)",
        (fid, f"mod.{name}", name, path),
    )


def _seed_edge(con, src: str, dst: str, rows: int = 1, etype: str = "CALLS") -> None:
    for _ in range(rows):
        con.execute(
            "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?, ?, ?, 'test')",
            (src, dst, etype),
        )


def _seed_two_clusters(con) -> None:
    """双团（团内权重 5）+ 权重 1 弱桥——LPA 稳定双社区的标准格局。"""
    for fid in ("a1", "a2", "a3", "b1", "b2", "b3"):
        _seed_symbol(con, fid, fid)
    for src, dst in (("a1", "a2"), ("a1", "a3"), ("a2", "a3")):
        _seed_edge(con, src, dst, rows=5)
    for src, dst in (("b1", "b2"), ("b1", "b3"), ("b2", "b3")):
        _seed_edge(con, src, dst, rows=5)
    _seed_edge(con, "a1", "b1", rows=1)  # 弱桥


def test_pagerank_hub(facts_db):
    for fid in ("hub", "l1", "l2", "l3"):
        _seed_symbol(con := facts_db, fid, fid)
    _seed_edge(facts_db, "l1", "hub")
    _seed_edge(facts_db, "l2", "hub")
    _seed_edge(facts_db, "l3", "hub")
    _seed_edge(facts_db, "hub", "l1")  # hub 非纯 dangling
    graph = gs.load_call_graph(facts_db)
    rank, _ = gs.pagerank(graph)
    assert max(rank, key=lambda f: rank[f]) == "hub"
    assert rank["hub"] > rank["l1"]


def test_pagerank_deterministic(facts_db):
    _seed_two_clusters(facts_db)
    graph = gs.load_call_graph(facts_db)
    r1, it1 = gs.pagerank(graph)
    r2, it2 = gs.pagerank(graph)
    assert r1 == r2  # 复现性红线：同图同结果
    assert it1 == it2


def test_pagerank_dangling_mass(facts_db):
    """a→b、b 无出边：dangling mass 均匀回注——概率质量守恒（和为 1）。"""
    _seed_symbol(facts_db, "a", "a")
    _seed_symbol(facts_db, "b", "b")
    _seed_edge(facts_db, "a", "b")
    graph = gs.load_call_graph(facts_db)
    rank, _ = gs.pagerank(graph)
    assert rank["b"] > rank["a"]
    assert sum(rank.values()) == pytest.approx(1.0, abs=1e-6)


def test_lpa_two_weighted_clusters(facts_db):
    _seed_two_clusters(facts_db)
    graph = gs.load_call_graph(facts_db)
    communities, rounds = gs.lpa(graph)
    assert communities == {"a1": "a1", "a2": "a1", "a3": "a1",
                           "b1": "b1", "b2": "b1", "b3": "b1"}
    assert rounds < gs.LPA_MAX_ROUNDS  # 收敛而非跑满


def test_lpa_symmetric_pair_single_community(facts_db):
    """x↔y 互调（权 1）：异步更新收敛单社区——诚实局限（无模块度保护，
    弱桥吞并）测试锁：不假装 Louvain。
    """
    _seed_symbol(facts_db, "x", "x")
    _seed_symbol(facts_db, "y", "y")
    _seed_edge(facts_db, "x", "y")
    _seed_edge(facts_db, "y", "x")
    graph = gs.load_call_graph(facts_db)
    communities, rounds = gs.lpa(graph)
    assert set(communities.values()) == {"x"}  # canon = min(x, y)
    assert rounds < gs.LPA_MAX_ROUNDS


def test_structure_modules_latest_active(facts_db):
    con = facts_db
    payload = {"modules": [{"module_id": "core", "symbol_fids": ["a1", "a2"]}]}
    con.execute(
        "INSERT INTO structure (payload_json, status) VALUES (?, 'active')",
        (json.dumps(payload),),
    )
    mods = gs.structure_modules(con)
    assert mods == [{"module_id": "core", "fids": ["a1", "a2"]}]
    # 最新一版 rejected → 对账跳过（与 s7 同口径：只 active 有效）
    con.execute(
        "INSERT INTO structure (payload_json, status) VALUES (?, 'rejected')",
        (json.dumps(payload),),
    )
    assert gs.structure_modules(con) is None


def test_reconcile_jaccard():
    communities = {"a1": "a1", "a2": "a1", "a3": "a1",
                   "b1": "b1", "b2": "b1", "b3": "b1"}
    modules = [
        {"module_id": "core", "fids": ["a1", "a2", "a3"]},
        {"module_id": "mixed", "fids": ["a1", "a2", "b1"]},
        {"module_id": "ghost", "fids": ["zz"]},
    ]
    nodes = list(communities)
    recon = gs.reconcile(communities, modules, nodes)
    by_id = {m["module_id"]: m for m in recon["modules"]}
    assert by_id["core"]["best_community"] == "a1"
    assert by_id["core"]["jaccard"] == 1.0
    # mixed: |{a1,a2}∩{a1,a2,a3}|/|{a1,a2,a3,b1}| = 2/4
    assert by_id["mixed"]["best_community"] == "a1"
    assert by_id["mixed"]["jaccard"] == 0.5
    assert by_id["ghost"]["best_community"] is None
    s = recon["summary"]
    assert s["modules"] == 3
    assert s["modules_with_overlap"] == 2
    assert s["zero_overlap_modules"] == ["ghost"]
    assert s["communities"] == 2
    # 排序：jaccard 降序，模块按对账质量呈现
    assert [m["module_id"] for m in recon["modules"]] == ["core", "mixed", "ghost"]


def test_run_graph_end_to_end(tmp_path):
    from pathlib import Path

    import sqlite3 as sq

    schema_path = Path(gs.__file__).parent / "schema" / "facts_schema.sql"
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    src = sq.connect(":memory:")
    src.executescript(schema_path.read_text(encoding="utf-8"))
    for fid in ("a1", "a2", "a3", "b1", "b2", "b3"):
        _seed_symbol(src, fid, fid)
    for s, d in (("a1", "a2"), ("a1", "a3"), ("a2", "a3")):
        _seed_edge(src, s, d, rows=5)
    for s, d in (("b1", "b2"), ("b1", "b3"), ("b2", "b3")):
        _seed_edge(src, s, d, rows=5)
    _seed_edge(src, "a1", "b1", rows=1)
    payload = {"modules": [
        {"module_id": "core", "symbol_fids": ["a1", "a2", "a3"]},
        {"module_id": "util", "symbol_fids": ["b1"]},
    ]}
    src.execute(
        "INSERT INTO structure (payload_json, status) VALUES (?, 'active')",
        (json.dumps(payload),),
    )
    # sqlite3.backup 对未提交写事务的源连接死锁（SQLITE_BUSY 无限重试）——
    # 先 commit 再 backup（实测探针锁定）。
    src.commit()
    dst = sq.connect(repo / ".vrp" / "facts.db")
    src.backup(dst)
    dst.close()
    src.close()
    result = gs.run_graph(repo)
    assert result["graph"] == {"nodes": 6, "edge_pairs": 7}
    assert result["communities"]["count"] == 2
    top = result["pagerank"]["top"]
    assert top and top[0]["score"] >= top[-1]["score"]
    assert top[0]["qualified_name"] == "mod." + top[0]["name"]
    recon = result["structure_reconcile"]
    assert recon is not None
    assert recon["summary"]["modules"] == 2
    by_id = {m["module_id"]: m for m in recon["modules"]}
    assert by_id["core"]["jaccard"] == 1.0
    assert by_id["util"]["jaccard"] == pytest.approx(1 / 3, abs=1e-4)
    text = gs.render_text(result)
    assert text.startswith("vrp graph - repo")
    assert "communities : 2" in text
    assert "structure   : 2 modules vs 2 communities" in text


def test_run_graph_missing_facts_exit(tmp_path):
    with pytest.raises(SystemExit, match="facts.db missing"):
        gs.run_graph(tmp_path)


def test_run_graph_empty_graph(tmp_path):
    """type 过滤（MENTIONS 不进图）+桩过滤（file_path 空/`<...>` 合成桩
    家族不进图）→ 空图诚实呈现而非报错。
    """
    from pathlib import Path

    import sqlite3 as sq

    schema_path = Path(gs.__file__).parent / "schema" / "facts_schema.sql"
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    src = sq.connect(":memory:")
    src.executescript(schema_path.read_text(encoding="utf-8"))
    _seed_symbol(src, "stub", "stub", path="")  # 桩：file_path 空
    _seed_symbol(src, "builtin", "builtin", path="<python-builtins>")  # 合成桩家族
    _seed_symbol(src, "real", "real")
    _seed_edge(src, "stub", "real")  # CALLS 但桩端被滤
    _seed_edge(src, "builtin", "real")  # CALLS 但 <...> 桩端被滤（v_hotspots 口径）
    _seed_edge(src, "real", "stub", etype="MENTIONS")  # type 不在白名单
    src.commit()  # backup 前必须提交（未提交事务 backup 死锁）
    dst = sq.connect(repo / ".vrp" / "facts.db")
    src.backup(dst)
    dst.close()
    src.close()
    result = gs.run_graph(repo)
    assert result["graph"] == {"nodes": 0, "edge_pairs": 0}
    assert result["pagerank"]["top"] == []
    assert result["communities"]["count"] == 0
    assert result["structure_reconcile"] is None
    text = gs.render_text(result)
    assert "graph       : 0 nodes / 0 weighted pairs" in text
    assert "no active structure" in text


def test_cli_parser_accepts_graph():
    args = build_parser().parse_args(["graph", "somerepo", "--json"])
    assert args.func is cmd_graph
    assert args.repo == "somerepo"
    assert args.json is True
    args = build_parser().parse_args(["graph", "somerepo"])
    assert args.json is False
