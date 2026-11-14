import sqlite3
from pathlib import Path

import pytest

from vrp import s5_repomap

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _seed(con, fid, qn, name, label, file_path, start, end):
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES (?, 'p', ?, ?, ?, ?, ?, ?, 'h')",
        (fid, qn, label, name, file_path, start, end),
    )


@pytest.fixture
def con():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    _seed(con, "fa", "mod.a", "a", "Function", "src/one.ts", 1, 2)
    _seed(con, "fb", "mod.b", "b", "Function", "src/one.ts", 4, 6)
    _seed(con, "fc", "mod.c", "c", "Function", "src/two.ts", 1, 3)
    _seed(con, "fm", "mod.m", "m", "Module", "src/mod.ts", 1, 9)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fa','fb','CALLS','E2')")
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fc','fb','CALLS','E2')")
    con.commit()
    yield con
    con.close()


def test_pagerank_empty():
    assert s5_repomap.pagerank({}) == {}


def test_pagerank_hub_ranks_highest(con):
    adjacency, symbols = s5_repomap.load_graph(con)
    rank = s5_repomap.pagerank(adjacency)
    assert rank["fb"] > rank["fa"]
    assert rank["fb"] > rank["fc"]
    total = sum(rank.values())
    assert abs(total - 1.0) < 0.02


def test_module_label_excluded_from_definitions(con):
    ranked = s5_repomap._ranked_definitions(con)
    names = {meta["name"] for _, meta in ranked}
    assert names == {"a", "b", "c"}
    assert "m" not in names


def test_render_repomap_groups_and_orders(con):
    result = s5_repomap.render_repomap(con, char_budget=4000)
    lines = result["map"].splitlines()
    assert lines[0] == "src/one.ts:"
    assert lines[1] == "  4-6 b"
    assert lines[2] == "  1-2 a"
    assert lines[3] == "src/two.ts:"
    assert lines[4] == "  1-3 c"
    assert result["truncated"] is False
    assert result["files_included"] == 2
    assert result["symbols_included"] == 3
    assert result["symbols_total"] == 3


def test_render_repomap_budget_truncates(con):
    result = s5_repomap.render_repomap(con, char_budget=15)
    assert result["truncated"] is True
    assert len(result["map"]) <= 15


def test_render_repomap_budget_rollback_bare_header(con):
    """R-AJ：预算恰在文件头后、首条目前耗尽 → 回滚裸 "path:" 行
    （零符号文件头在 aider 树形图语义里读作"该文件无定义"，实为预算耗尽），
    files_included 收据不计零符号文件（收据即内容红线）。"""
    result = s5_repomap.render_repomap(con, char_budget=15)
    assert result["truncated"] is True
    assert result["map"] == ""
    assert result["files_included"] == 0
    assert result["symbols_included"] == 0


def test_render_repomap_partial_file_keeps_header(con):
    """R-AJ 防倒退 pin：文件部分渲染（≥1 条目已入列后预算耗尽）保留头——
    部分文件是诚实截断，与裸头回滚语义分界清晰。"""
    result = s5_repomap.render_repomap(con, char_budget=21)
    assert result["truncated"] is True
    assert result["map"] == "src/one.ts:\n  4-6 b"
    assert result["files_included"] == 1
    assert result["symbols_included"] == 1
