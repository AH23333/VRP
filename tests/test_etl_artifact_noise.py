"""P-1 产物噪音提示：四类启发式规则（hints 只提示，绝不自动排除）。

跨项目实测 10/15 项目命中：archify 生成 HTML 页（uniform）、docs/_build
（artifact dir）、*.min.js（minified/dense）、site/web 双轨入库（same hash）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from vrp import etl

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    return con


def _add_symbol(con, fid: str, path: str, start: int = 1, end: int = 2) -> None:
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
        " start_line, end_line, content_hash) VALUES (?, 'p', ?, 'Function', ?, ?, ?, ?, 'h')",
        (fid, "p." + fid, fid, path, start, end),
    )


def test_artifact_dir_name_hint_subtree_aggregation():
    con = _con()
    for i in range(3):
        _add_symbol(con, f"docs{i}", f"docs/_build/html/p{i}.html")
        _add_symbol(con, f"deep{i}", f"site/2020/01/post{i}.html")  # 深层页归并到 site/
    for i in range(2):
        _add_symbol(con, f"src{i}", f"src/mod{i}.py")
    con.commit()
    hints = etl.detect_artifact_noise(con)["hints"]
    by_dir = {h["dir"]: h for h in hints if h["kind"] == "artifact_dir_name"}
    assert set(by_dir) == {"docs/_build", "site"}
    assert by_dir["site"]["files"] == 3  # 深层子目录聚合
    assert by_dir["site"]["symbol_share_pct"] == 37.5  # 3/8


def test_minified_hint():
    con = _con()
    for i in range(250):
        _add_symbol(con, f"m{i}", "vendor/lib.min.js", start=(i % 3) + 1)
    con.commit()
    hints = etl.detect_artifact_noise(con)["hints"]
    hint = next(h for h in hints if h["kind"] == "minified_or_dense_files")
    assert hint["files"] == 1
    assert hint["symbols"] == 250


def test_dense_without_minified_name_hint():
    """digitaljs.js 案例：无 .min 命名但 3 行挤 5675 符号——密度签名命中。"""
    con = _con()
    for i in range(220):
        _add_symbol(con, f"d{i}", "generated/digitaljs.js", start=(i % 3) + 1)
    con.commit()
    hints = etl.detect_artifact_noise(con)["hints"]
    assert any(h["kind"] == "minified_or_dense_files" for h in hints)


def test_normal_source_not_flagged_dense():
    con = _con()
    for i in range(250):
        _add_symbol(con, f"s{i}", f"src/mod{i}.py", start=i + 1, end=i + 2)
    con.commit()
    hints = etl.detect_artifact_noise(con)["hints"]
    assert not any(h["kind"] == "minified_or_dense_files" for h in hints)


def test_uniform_html_pages_hint():
    """archify 案例：6 页 × 每页 358 符号全等——生成页签名。"""
    con = _con()
    for page in range(6):
        for i in range(358):
            _add_symbol(con, f"pg{page}_{i}", f"report/page{page}.html", start=i + 1)
    con.commit()
    hints = etl.detect_artifact_noise(con)["hints"]
    hint = next(h for h in hints if h["kind"] == "uniform_html_pages")
    assert hint["files"] == 6
    assert hint["symbols_per_file"] == 358
    assert hint["symbols"] == 6 * 358


def test_uniform_html_not_triggered_below_threshold():
    con = _con()
    for page in range(3):  # < UNIFORM_HTML_MIN_FILES (5)
        for i in range(358):
            _add_symbol(con, f"pg{page}_{i}", f"report/page{page}.html", start=i + 1)
    con.commit()
    hints = etl.detect_artifact_noise(con)["hints"]
    assert not any(h["kind"] == "uniform_html_pages" for h in hints)


def test_same_hash_copies_hint():
    con = _con()
    _add_symbol(con, "a", "web/page.html")
    _add_symbol(con, "b", "site/page.html")
    con.execute("INSERT INTO file_hashes (file_path, hash) VALUES ('web/page.html','h1')")
    con.execute("INSERT INTO file_hashes (file_path, hash) VALUES ('site/page.html','h1')")
    con.commit()
    hints = etl.detect_artifact_noise(con)["hints"]
    hint = next(h for h in hints if h["kind"] == "same_hash_copies")
    assert hint["clusters"] == 1
    assert hint["files"] == 2
    assert hint["examples"] == [["site/page.html", "web/page.html"]]


def test_clean_repo_no_hints():
    con = _con()
    _add_symbol(con, "a", "src/a.py", start=1, end=10)
    _add_symbol(con, "b", "src/b.py", start=1, end=10)
    con.execute("INSERT INTO file_hashes (file_path, hash) VALUES ('src/a.py','h1')")
    con.execute("INSERT INTO file_hashes (file_path, hash) VALUES ('src/b.py','h2')")
    con.commit()
    result = etl.detect_artifact_noise(con)
    assert result["hints"] == []
    assert result["total_symbols"] == 2
    assert result["indexed_files"] == 2


def test_empty_db_no_hints():
    con = _con()
    result = etl.detect_artifact_noise(con)
    assert result == {"total_symbols": 0, "indexed_files": 0, "hints": []}


def _mk_cbm(db) -> None:
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO store_meta VALUES (?,?)", [("db_uid", "uid-1"), ("mutation_gen", "7")])
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, properties TEXT)"
    )
    con.execute("INSERT INTO nodes VALUES (1,'proj','Function','old','p.a.old','a.py',1,2,'{}')")
    con.execute("CREATE TABLE edges (source_id INT, target_id INT, type TEXT, properties TEXT, project TEXT)")
    con.execute("CREATE TABLE index_coverage (project TEXT, rel_path TEXT, kind TEXT, detail TEXT)")
    con.execute("INSERT INTO index_coverage VALUES ('proj','a.py','ok','')")
    con.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT)")
    con.commit()
    con.close()


def test_run_etl_writes_artifact_hints_json(tmp_path, monkeypatch):
    """P-1 接线：run_etl 产出 .vrp/artifact-hints.json + stats["artifact_hints"]。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    cbm = tmp_path / "proj.db"
    _mk_cbm(cbm)
    monkeypatch.setattr(etl.s1_cbm, "cbm_db_path", lambda name: cbm)
    stats = etl.run_etl(repo, "proj")
    hints_path = repo / ".vrp" / "artifact-hints.json"
    assert hints_path.is_file()
    payload = json.loads(hints_path.read_text(encoding="utf-8"))
    assert payload["total_symbols"] == 1
    assert payload["hints"] == []
    assert stats["artifact_hints"] == 0
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
        assert meta["count_artifact_hints"] == "0"
    finally:
        con.close()
