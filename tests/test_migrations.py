"""V17 (#205) / #217：schema 迁移矩阵——旧版库 fixture × 迁移路径断言全覆盖。

学 Django migrations / alembic 线性迁移链思想（零依赖自建：MIGRATIONS
有序表 + PRAGMA user_version 原生机制）。V0 快照=pre-tracking 库形态
（edges 无 resolution_label 列、无 user_version、v_dead_code 单四型
NOT EXISTS）——即 BB1 前任何真实构建产物。链现状 [2, 3]：v2=edges
resolution_label（BB1），v3=symbols signature/return_type/param_count
（#217 签名入库）。
"""

import sqlite3
from pathlib import Path

from vrp import etl

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"
SCHEMA_TEXT = SCHEMA.read_text(encoding="utf-8")

V0_SCHEMA = r"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE symbols (
    fid TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    label TEXT NOT NULL,
    name TEXT NOT NULL,
    file_path TEXT NOT NULL DEFAULT '',
    start_line INTEGER,
    end_line INTEGER,
    content_hash TEXT NOT NULL DEFAULT '',
    properties_json TEXT,
    UNIQUE (project, qualified_name)
);
CREATE TABLE edges (
    src_fid TEXT NOT NULL,
    dst_fid TEXT NOT NULL,
    type TEXT NOT NULL,
    evidence TEXT NOT NULL,
    strategy TEXT,
    confidence REAL,
    line INTEGER,
    properties_json TEXT
);
CREATE TABLE usage_hints (
    src_fid TEXT NOT NULL,
    dst_fid TEXT NOT NULL,
    properties_json TEXT
);
CREATE TABLE test_linkage (
    test_fid TEXT NOT NULL,
    target_fid TEXT NOT NULL,
    source TEXT NOT NULL,
    properties_json TEXT
);
CREATE VIEW v_dead_code AS
WITH cand AS (
  SELECT s.*, replace(s.file_path, '\', '/') AS norm
  FROM symbols s
  WHERE s.label IN ('Function', 'Method')
    AND s.file_path != ''
    AND s.name != ''
)
SELECT c.fid, c.qualified_name, c.label, c.file_path, c.name, c.start_line, c.end_line
FROM cand c
WHERE NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst_fid = c.fid AND e.type IN ('CALLS', 'CALL_REFERENCE', 'MENTIONS', 'SPAWNS'))
  AND NOT EXISTS (SELECT 1 FROM usage_hints u WHERE u.dst_fid = c.fid)
  AND NOT EXISTS (SELECT 1 FROM test_linkage t WHERE t.target_fid = c.fid);
"""


def _build_v0(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executescript(V0_SCHEMA)
        con.execute("INSERT INTO meta (key, value) VALUES ('git_head', 'abc123')")
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
            " VALUES ('f1','p','p.alpha','Function','alpha','src/a.py','h1')"
        )
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
            " VALUES ('f2','p','p.beta','Function','beta','src/b.py','h2')"
        )
        con.execute(
            "INSERT INTO edges (src_fid, dst_fid, type, evidence, strategy, confidence)"
            " VALUES ('f2','f1','CALLS','hybrid-lsp','lsp_direct',0.95)"
        )
        con.commit()
    finally:
        con.close()


def _user_version(con) -> int:
    return con.execute("PRAGMA user_version").fetchone()[0]


def _edge_cols(con):
    return [row[1] for row in con.execute("PRAGMA table_info(edges)")]


def _sym_cols(con):
    return [row[1] for row in con.execute("PRAGMA table_info(symbols)")]


def test_new_db_reaches_v3(tmp_path):
    con = sqlite3.connect(tmp_path / "facts.db")
    try:
        etl.ensure_schema(con)
        assert _user_version(con) == 3
        assert "resolution_label" in _edge_cols(con)
        assert {"signature", "return_type", "param_count"} <= set(_sym_cols(con))
        # 视图可查（新定义引用新列）
        assert con.execute("SELECT COUNT(*) FROM v_dead_code").fetchone()[0] == 0
    finally:
        con.close()


def test_v0_library_upgraded_in_place(tmp_path):
    db = tmp_path / "facts.db"
    _build_v0(db)
    con = sqlite3.connect(db)
    try:
        assert _user_version(con) == 0
        etl.ensure_schema(con)
        assert "resolution_label" in _edge_cols(con)
        assert {"signature", "return_type", "param_count"} <= set(_sym_cols(con))
        assert _user_version(con) == 3
        # 数据保留：种子行原样
        assert con.execute("SELECT value FROM meta WHERE key = 'git_head'").fetchone()[0] == "abc123"
        assert con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 2
        row = con.execute(
            "SELECT src_fid, dst_fid, type, strategy, confidence, resolution_label FROM edges"
        ).fetchone()
        assert row == ("f2", "f1", "CALLS", "lsp_direct", 0.95, None)
    finally:
        con.close()


def test_migration_idempotent(tmp_path):
    db = tmp_path / "facts.db"
    _build_v0(db)
    for _ in range(2):
        con = sqlite3.connect(db)
        try:
            etl.ensure_schema(con)  # 第二轮绝不 'duplicate column name'
        finally:
            con.close()
    con = sqlite3.connect(db)
    try:
        assert _user_version(con) == 3
        assert con.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 2
    finally:
        con.close()


def test_v0_view_definition_replaced(tmp_path):
    db = tmp_path / "facts.db"
    _build_v0(db)
    con = sqlite3.connect(db)
    try:
        old_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='v_dead_code'"
        ).fetchone()[0]
        assert "resolution_label" not in old_sql
        etl.ensure_schema(con)
        new_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='v_dead_code'"
        ).fetchone()[0]
        assert "resolution_label" in new_sql
        # 迁移后库上新语义生效：f1 被 0.95 EXTRACTED 边救活，f2 只有
        # AMBIGUOUS 入边（f1→f2）→入视图
        con.execute(
            "INSERT INTO edges (src_fid, dst_fid, type, evidence, strategy, confidence, resolution_label)"
            " VALUES ('f1','f2','CALLS','heuristic','unique_name',0.38,'AMBIGUOUS')"
        )
        con.commit()
        fids = {r[0] for r in con.execute("SELECT fid FROM v_dead_code")}
        assert fids == {"f2"}
    finally:
        con.close()


def test_v2_rerun_is_noop(tmp_path):
    db = tmp_path / "facts.db"
    _build_v0(db)
    con = sqlite3.connect(db)
    try:
        etl.ensure_schema(con)
        before = (
            con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM findings").fetchone()[0],
        )
        etl.ensure_schema(con)  # v3 库重跑：无迁移步、executescript 幂等
        after = (
            con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM findings").fetchone()[0],
        )
        assert before == after
        assert _user_version(con) == 3
    finally:
        con.close()


def test_half_migrated_library_survives(tmp_path):
    """半迁移态（列已手工补但 user_version 仍 0）——幂等探测路径不炸。"""
    db = tmp_path / "facts.db"
    _build_v0(db)
    con = sqlite3.connect(db)
    try:
        con.execute("ALTER TABLE edges ADD COLUMN resolution_label TEXT")
        con.commit()
        etl.ensure_schema(con)
        assert _user_version(con) == 3
        assert _edge_cols(con).count("resolution_label") == 1  # 未重复加列
        assert con.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
    finally:
        con.close()


def test_half_migrated_v3_symbols_survives(tmp_path):
    """v3 半迁移态（symbols 已手工补 signature 列但 user_version 仍 0）——
    幂等探测不重复加列。"""
    db = tmp_path / "facts.db"
    _build_v0(db)
    con = sqlite3.connect(db)
    try:
        con.execute("ALTER TABLE symbols ADD COLUMN signature TEXT")
        con.commit()
        etl.ensure_schema(con)
        assert _user_version(con) == 3
        cols = _sym_cols(con)
        assert cols.count("signature") == 1
        assert "return_type" in cols
        assert "param_count" in cols
    finally:
        con.close()


def test_migrations_chain_shape():
    targets = [t for t, _fn in etl.MIGRATIONS]
    assert targets == sorted(targets), "MIGRATIONS 必须按目标版本升序"
    assert all(t2 > t1 for t1, t2 in zip(targets, targets[1:])), "版本严格递增无重复"
    assert targets[0] == 2, "首个迁移=v2（BB1 resolution_label，v1 为保留基线槽）"
    assert targets == [2, 3], "链现状：v2 resolution_label + v3 symbol signature"


def test_v0_migration_then_new_insert_shape(tmp_path):
    """迁移后的库接受新代码写入面（9 列 INSERT 形态）。"""
    db = tmp_path / "facts.db"
    _build_v0(db)
    con = sqlite3.connect(db)
    try:
        etl.ensure_schema(con)
        con.execute(
            "INSERT INTO edges (src_fid, dst_fid, type, evidence, strategy, confidence, line,"
            " properties_json, resolution_label) VALUES ('f1','f2','CALLS','heuristic',"
            " 'unique_name', 0.38, 7, '{}', 'AMBIGUOUS')"
        )
        con.commit()
        row = con.execute(
            "SELECT resolution_label FROM edges WHERE src_fid = 'f1'"
        ).fetchone()
        assert row == ("AMBIGUOUS",)
    finally:
        con.close()
