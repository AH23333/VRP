import sqlite3

import pytest

from vrp.etl import SCHEMA_PATH
from vrp.s5_repomap import (
    DEFINITION_LABELS,
    MAX_SYMBOLS_PER_FILE,
    _ranked_definitions,
    load_graph,
    render_repomap,
)

pytestmark = pytest.mark.deep


def _open_ro(repo) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)


def _make_con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return con


def _sym(con, fid, qn, label="Function", path="src/a.py", start_line=1, end_line=5):
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
        " start_line, end_line, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (fid, "p", qn, label, qn.split(".")[-1], path, start_line, end_line, "h"),
    )


def _edge(con, src, dst, etype="CALLS"):
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,?,?)",
        (src, dst, etype, "E"),
    )


def test_load_graph_excludes_synthetic_paths():
    con = _make_con()
    _sym(con, "b", "builtins.str", path="<python-builtins>")
    _sym(con, "k", "kotlin.Any", path="<kotlin-builtins>")
    _sym(con, "w", "p.weird", path=">synthetic<")
    _sym(con, "e", "p.empty", path="")
    _sym(con, "r", "p.real", path="src/real.py")
    _edge(con, "b", "r")
    _edge(con, "r", "k")
    _edge(con, "w", "r")
    adjacency, symbols = load_graph(con)
    assert set(symbols) == {"r"}
    assert adjacency == {"r": []}


def test_load_graph_keeps_all_real_symbols():
    con = _make_con()
    _sym(con, "a", "p.a", path="src/a.py")
    _sym(con, "b", "p.b", path="src/b.py")
    _edge(con, "a", "b")
    adjacency, symbols = load_graph(con)
    assert set(symbols) == {"a", "b"}
    assert adjacency == {"a": ["b"], "b": []}


def test_big_repo_load_graph_synthetic_free(big_repo):
    con = _open_ro(big_repo)
    try:
        adjacency, symbols = load_graph(con)
    finally:
        con.close()
    fake = [
        fid
        for fid, meta in symbols.items()
        if meta["file_path"] == ""
        or meta["file_path"].startswith("<")
        or meta["file_path"].startswith(">")
    ]
    assert fake == []
    assert len(symbols) >= 50000


def test_big_repo_repomap_render_synthetic_free(big_repo):
    con = _open_ro(big_repo)
    try:
        out = render_repomap(con, char_budget=4000)
    finally:
        con.close()
    assert out["files_included"] >= 10
    assert out["symbols_included"] >= 50
    for line in out["map"].splitlines():
        if line.endswith(":") and line != "":
            assert not line.startswith("<"), f"synthetic file header in repomap: {line}"
            assert not line.startswith(">")
    assert "<python-builtins>" not in out["map"]
    assert "<kotlin-builtins>" not in out["map"]
    assert "builtins." not in out["map"]
    assert "kotlin." not in out["map"]


def test_big_repo_top_ranked_definitions_all_real(big_repo):
    con = _open_ro(big_repo)
    try:
        ranked = _ranked_definitions(con)
    finally:
        con.close()
    assert len(ranked) >= 1000
    top = ranked[:20]
    for score, meta in top:
        assert meta["label"] in DEFINITION_LABELS
        assert not meta["file_path"].startswith("<"), meta
        assert not meta["file_path"].startswith(">"), meta
        assert meta["file_path"] != ""
        assert not meta["qualified_name"].startswith("builtins."), meta
        assert not meta["qualified_name"].startswith("kotlin."), meta
    assert top[0][0] > top[-1][0] * 2


def test_repomap_max_symbols_per_file_cap():
    con = _make_con()
    for i in range(MAX_SYMBOLS_PER_FILE + 5):
        _sym(con, f"f{i}", f"p.f{i}", path="src/one.py", start_line=i + 1, end_line=i + 2)
        _edge(con, f"f{i}", "f0")
    out = render_repomap(con, char_budget=10**6)
    body = [ln for ln in out["map"].splitlines() if not ln.endswith(":")]
    assert len(body) <= MAX_SYMBOLS_PER_FILE
    assert out["truncated"] is False
