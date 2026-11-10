"""xcheck 第三方收编排除测试（R7' 诚实发现收口）。

病根：CBM 默认不索引 vendor/，code-atlas 全量索引——不对称使 agreement 失真
（vrp 自仓 27.7%，vrp/vendor/archify 贡献 68.5% 的 CA calls）。
双侧对称过滤（vendor/node_modules/site-packages 路径段）+ 排除计数公开入 summary/meta。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from vrp import xcheck

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _excluded_path_cases():
    return [
        ("vendor/v.py", True),
        ("a/vendor/b.py", True),
        ("node_modules/x.js", True),
        ("site-packages/pkg/__init__.py", True),
        ("./vendor\\v.py", True),
        ("app/vendorx/v.py", False),
        ("src/main.py", False),
        ("vendor_file.py", False),
    ]


def test_excluded_path_segment_match():
    for path, expected in _excluded_path_cases():
        assert xcheck._excluded_path(path) is expected, path


def _mk_ca_db(repo: Path, with_vendor: bool = True, with_minified: bool = False) -> None:
    ca_dir = repo / ".code-atlas"
    ca_dir.mkdir(parents=True, exist_ok=True)
    ca = sqlite3.connect(ca_dir / "index.db")
    ca.executescript(
        """
        CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT);
        CREATE TABLE symbols (id INTEGER PRIMARY KEY, name TEXT, file_id INTEGER);
        CREATE TABLE edges (src_symbol_id INTEGER, dst_symbol_id INTEGER, kind TEXT,
                            confidence REAL, provenance TEXT);
        """
    )
    ca.execute("INSERT INTO files (id, path) VALUES (1, 'x.py')")
    ca.execute("INSERT INTO symbols (id, name, file_id) VALUES (1, 'a', 1)")
    ca.execute("INSERT INTO symbols (id, name, file_id) VALUES (2, 'b', 1)")
    ca.execute("INSERT INTO edges VALUES (1, 2, 'calls', 0.9, 'heuristic')")
    if with_vendor:
        ca.execute("INSERT INTO files (id, path) VALUES (2, 'vendor/v.py')")
        ca.execute("INSERT INTO symbols (id, name, file_id) VALUES (3, 'v1', 2)")
        ca.execute("INSERT INTO symbols (id, name, file_id) VALUES (4, 'v2', 2)")
        ca.execute("INSERT INTO edges VALUES (3, 4, 'calls', 0.9, 'heuristic')")
    if with_minified:
        ca.execute("INSERT INTO files (id, path) VALUES (3, 'lib/x.min.js')")
        ca.execute("INSERT INTO symbols (id, name, file_id) VALUES (5, 'm1', 3)")
        ca.execute("INSERT INTO symbols (id, name, file_id) VALUES (6, 'm2', 3)")
        ca.execute("INSERT INTO edges VALUES (5, 6, 'calls', 0.9, 'heuristic')")
    ca.commit()
    ca.close()


def _mk_facts_db(repo: Path, with_vendor: bool = True, with_minified: bool = False) -> None:
    (repo / ".vrp").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    rows = [("f-a", "a", "x.py"), ("f-b", "b", "x.py")]
    if with_vendor:
        rows += [("f-v1", "v1", "vendor/v.py"), ("f-v2", "v2", "vendor/v.py")]
    if with_minified:
        rows += [("f-m1", "m1", "lib/x.min.js"), ("f-m2", "m2", "lib/x.min.js")]
    for fid, name, path in rows:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
            " start_line, end_line, content_hash) VALUES (?, 'p', ?, 'Function', ?, ?, 1, 3, 'h')",
            (fid, "pkg." + name, name, path),
        )
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('f-a', 'f-b', 'CALLS', 'structural')")
    if with_vendor:
        con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('f-v1', 'f-v2', 'CALLS', 'structural')")
    if with_minified:
        con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('f-m1', 'f-m2', 'CALLS', 'structural')")
    con.commit()
    con.close()


def test_xcheck_vendor_excluded_both_sides(tmp_path, monkeypatch):
    """双仓对称排除：vendor 调用两侧剔除，agreement 按一方代码计算，计数公开。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _mk_ca_db(repo, with_vendor=True)
    _mk_facts_db(repo, with_vendor=True)
    monkeypatch.setattr(xcheck, "index_with_code_atlas",
                        lambda repo, reindex=False, timeout=900: {"status": "indexed"})
    summary = xcheck.run_xcheck(repo, repo / ".vrp" / "facts.db")
    assert summary["ca_calls"] == 1
    assert summary["cbm_calls"] == 1
    assert summary["agreement"] == 1
    assert summary["agreement_pct"] == 100.0
    assert summary["ca_excluded_files"] == 1
    assert summary["ca_excluded_symbols"] == 2
    assert summary["ca_excluded_calls"] == 1
    assert summary["cbm_excluded_calls"] == 1
    assert summary["excluded_segments"] == ["node_modules", "site-packages", "vendor"]
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        cats = {r[0] for r in con.execute("SELECT DISTINCT category FROM discrepancy_ledger")}
        assert cats == set()
        meta = dict(con.execute("SELECT key, value FROM meta"))
        assert meta["xcheck_ca_excluded_calls"] == "1"
        assert meta["xcheck_cbm_excluded_calls"] == "1"
    finally:
        con.close()


def test_xcheck_asymmetric_vendor_no_longer_sinks_agreement(tmp_path, monkeypatch):
    """vrp 自仓病根复刻：CA 索引 vendor 而 facts 无 vendor——排除后 agreement 不再被单侧稀释。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _mk_ca_db(repo, with_vendor=True)
    _mk_facts_db(repo, with_vendor=False)
    monkeypatch.setattr(xcheck, "index_with_code_atlas",
                        lambda repo, reindex=False, timeout=900: {"status": "indexed"})
    summary = xcheck.run_xcheck(repo, repo / ".vrp" / "facts.db")
    assert summary["ca_calls"] == 1
    assert summary["ca_excluded_calls"] == 1
    assert summary["agreement"] == 1
    assert summary["agreement_pct"] == 100.0
    assert summary["ca_only"] == 0


def test_xcheck_no_vendor_unchanged(tmp_path, monkeypatch):
    """无 vendor 仓：排除计数全零，行为与旧版一致。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _mk_ca_db(repo, with_vendor=False)
    _mk_facts_db(repo, with_vendor=False)
    monkeypatch.setattr(xcheck, "index_with_code_atlas",
                        lambda repo, reindex=False, timeout=900: {"status": "indexed"})
    summary = xcheck.run_xcheck(repo, repo / ".vrp" / "facts.db")
    assert summary["ca_calls"] == 1
    assert summary["cbm_calls"] == 1
    assert summary["agreement_pct"] == 100.0
    assert summary["ca_excluded_files"] == 0
    assert summary["ca_excluded_symbols"] == 0
    assert summary["ca_excluded_calls"] == 0
    assert summary["cbm_excluded_calls"] == 0


def test_xcheck_stale_ca_db_not_compared_when_index_not_fresh(tmp_path, monkeypatch):
    """R-D X1（病根五残留收口）：index skipped/failed 时陈旧 ca.db 不比对。

    旧版 index 非 indexed 仍用遗留 .code-atlas/index.db 比对——agreement%
    失真（陈旧交叉索引），且 ledger 照写。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _mk_ca_db(repo, with_vendor=False)
    _mk_facts_db(repo, with_vendor=False)
    monkeypatch.setattr(xcheck, "index_with_code_atlas",
                        lambda repo, reindex=False, timeout=900: {
                            "status": "skipped", "reason": "node not found"})
    summary = xcheck.run_xcheck(repo, repo / ".vrp" / "facts.db")
    assert summary["ledger_rows"] == 0
    assert "reason" in summary and "not fresh" in summary["reason"]
    assert "agreement_pct" not in summary  # 不产出失真 agreement
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM discrepancy_ledger").fetchone()[0] == 0
    finally:
        con.close()


def test_xcheck_minified_excluded_both_sides(tmp_path, monkeypatch):
    """P-1b：.min.js 两侧对称排除，agreement 不被压缩代码提取分歧稀释，计数公开。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _mk_ca_db(repo, with_vendor=True, with_minified=True)
    _mk_facts_db(repo, with_vendor=True, with_minified=True)
    monkeypatch.setattr(xcheck, "index_with_code_atlas",
                        lambda repo, reindex=False, timeout=900: {"status": "indexed"})
    summary = xcheck.run_xcheck(repo, repo / ".vrp" / "facts.db")
    assert summary["ca_calls"] == 1
    assert summary["cbm_calls"] == 1
    assert summary["ca_minified_symbols"] == 2
    assert summary["ca_minified_calls"] == 1
    assert summary["cbm_minified_calls"] == 1
    assert summary["agreement"] == 1
    assert summary["agreement_pct"] == 100.0
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
        assert meta["xcheck_ca_minified_symbols"] == "2"
        assert meta["xcheck_cbm_minified_calls"] == "1"
    finally:
        con.close()


def test_xcheck_minified_only_in_facts_counted_not_compared(tmp_path, monkeypatch):
    """单侧 minified（facts 有、CA 无）：cbm 侧照计排除，不产 cbm_only 噪音。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _mk_ca_db(repo, with_vendor=False, with_minified=False)
    _mk_facts_db(repo, with_vendor=False, with_minified=True)
    monkeypatch.setattr(xcheck, "index_with_code_atlas",
                        lambda repo, reindex=False, timeout=900: {"status": "indexed"})
    summary = xcheck.run_xcheck(repo, repo / ".vrp" / "facts.db")
    assert summary["cbm_calls"] == 1
    assert summary["cbm_minified_calls"] == 1
    assert summary["cbm_only"] == 0
    assert summary["ledger_rows"] == 0


def test_xcheck_no_minified_counters_zero(tmp_path, monkeypatch):
    """无 minified 仓：新计数全零，行为与旧版一致。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _mk_ca_db(repo, with_vendor=False)
    _mk_facts_db(repo, with_vendor=False)
    monkeypatch.setattr(xcheck, "index_with_code_atlas",
                        lambda repo, reindex=False, timeout=900: {"status": "indexed"})
    summary = xcheck.run_xcheck(repo, repo / ".vrp" / "facts.db")
    assert summary["ca_minified_symbols"] == 0
    assert summary["ca_minified_calls"] == 0
    assert summary["cbm_minified_calls"] == 0
    assert summary["cbm_minified_usage"] == 0
