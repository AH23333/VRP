import hashlib
import random
import sqlite3
from pathlib import Path

import pytest

from vrp.s5_repomap import DAMPING, load_graph, pagerank, render_repomap
from tests.deep.conftest import ROOT

pytestmark = pytest.mark.deep

SCHEMA = ROOT / "vrp" / "schema" / "facts_schema.sql"


def _make_con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    return con


def _seed_symbol(con, fid, qn, label="Function", path="src/a.py", start_line=1, end_line=5):
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
        " start_line, end_line, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (fid, "p", qn, label, qn.split(".")[-1], path, start_line, end_line, "h"),
    )


def _seed_edge(con, src, dst, etype="CALLS"):
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,?,?)",
        (src, dst, etype, "E"),
    )


def test_pagerank_conservation_random_graphs():
    rng = random.Random(20261102)
    for trial in range(10):
        n = rng.randint(5, 40)
        nodes = [f"n{i}" for i in range(n)]
        adjacency = {node: [] for node in nodes}
        for _ in range(rng.randint(0, n * 2)):
            src = rng.choice(nodes)
            dst = rng.choice(nodes)
            adjacency[src].append(dst)
        rank = pagerank(adjacency)
        assert set(rank) == set(nodes)
        assert abs(sum(rank.values()) - 1.0) < 1e-6, f"trial {trial}: sum={sum(rank.values())}"


def test_pagerank_dangling_mass_conserved():
    adjacency = {"a": ["b"], "b": [], "c": ["a"], "d": []}
    rank = pagerank(adjacency)
    assert abs(sum(rank.values()) - 1.0) < 1e-6


def test_pagerank_deterministic():
    rng = random.Random(7)
    adjacency = {f"n{i}": [f"n{rng.randint(0, 9)}"] for i in range(10)}
    r1 = pagerank(adjacency)
    r2 = pagerank(adjacency)
    assert r1 == r2


def test_pagerank_star_center_highest():
    adjacency = {f"leaf{i}": ["center"] for i in range(8)}
    adjacency["center"] = []
    rank = pagerank(adjacency)
    assert rank["center"] == max(rank.values())
    assert all(rank["center"] > rank[f"leaf{i}"] for i in range(8))


def test_pagerank_edge_cases():
    assert pagerank({}) == {}
    rank = pagerank({"only": []})
    assert rank == {"only": 1.0}
    rank = pagerank({"loop": ["loop"]})
    assert abs(sum(rank.values()) - 1.0) < 1e-6
    assert rank["loop"] == pytest.approx(1.0, abs=1e-6)


def test_pagerank_duplicate_edges_conserved():
    adjacency = {"a": ["b", "b", "b"], "b": ["a"], "c": ["a", "b"]}
    rank = pagerank(adjacency)
    assert abs(sum(rank.values()) - 1.0) < 1e-6


def test_pagerank_max_iterations_no_crash():
    ring = {f"n{i}": [f"n{(i + 1) % 12}"] for i in range(12)}
    rank = pagerank(ring, max_iterations=1)
    assert abs(sum(rank.values()) - 1.0) < 1e-6


def test_pagerank_damping_bounds():
    adjacency = {"a": ["b"], "b": ["a"], "c": ["a"]}
    for damping in (0.0, 0.5, 0.85, 1.0):
        rank = pagerank(adjacency, damping=damping)
        assert abs(sum(rank.values()) - 1.0) < 1e-6
        assert all(v >= 0.0 for v in rank.values())


def test_load_graph_drops_unpositioned_symbols():
    con = _make_con()
    _seed_symbol(con, "f1", "p.f1")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line)"
        " VALUES ('nopos','p','p.nopos','Function','nopos','src/a.py',NULL)"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line)"
        " VALUES ('nopath','p','p.nopath','Function','nopath','',1)"
    )
    _seed_edge(con, "f1", "nopos")
    _seed_edge(con, "nopath", "f1")
    adjacency, symbols = load_graph(con)
    assert "f1" in symbols
    assert "nopos" not in symbols
    assert "nopath" not in symbols
    assert adjacency["f1"] == []
    assert "nopos" not in adjacency


def test_render_repomap_budget_edges():
    con = _make_con()
    _seed_symbol(con, "f1", "p.f1", path="src/a.py")
    _seed_symbol(con, "f2", "p.f2", path="src/b.py")
    _seed_edge(con, "f2", "f1")
    out = render_repomap(con, char_budget=0)
    assert out["truncated"] is True
    assert out["files_included"] == 0
    out = render_repomap(con, char_budget=-5)
    assert out["files_included"] == 0
    out = render_repomap(con, char_budget=4000)
    assert out["files_included"] == 2
    assert "src/a.py" in out["map"]
    assert "src/b.py" in out["map"]
    assert out["symbols_included"] >= 1
    assert out["truncated"] is False


def test_render_repomap_orders_by_score():
    con = _make_con()
    _seed_symbol(con, "hub", "p.hub", path="src/hub.py")
    for i in range(5):
        _seed_symbol(con, f"leaf{i}", f"p.leaf{i}", path=f"src/leaf{i}.py")
        _seed_edge(con, f"leaf{i}", "hub")
    out = render_repomap(con, char_budget=4000)
    assert out["map"].index("src/hub.py") < out["map"].index("src/leaf0.py")


def test_pagerank_big_repo_conservation(big_repo):
    con = sqlite3.connect(f"file:{big_repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        adjacency, _ = load_graph(con)
    finally:
        con.close()
    rank = pagerank(adjacency)
    assert abs(sum(rank.values()) - 1.0) < 1e-4
    top = max(rank, key=rank.get)
    assert rank[top] > 1.0 / len(rank) * 5
