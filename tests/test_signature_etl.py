# -*- coding: utf-8 -*-
"""#217 签名数据入库（schema v3）：CBM nodes.properties 的 signature/
return_type/param_count 提取为可查询列。

N19 锐化前置的 callee 面落库；caller 面（CALLS 边 properties_json 的
args）原样转存本就在库。#216 记档修正：原始签名 JSON 一直在
properties_json 里——真差距是可查询列与查询面，非数据缺席。
"""
import json
import sqlite3

import pytest

from vrp import etl
from vrp import nav


def _mk_cbm(db, nodes_rows):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO store_meta VALUES (?,?)", [("db_uid", "uid-1"), ("mutation_gen", "7")])
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, properties TEXT)"
    )
    con.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)", nodes_rows)
    con.execute("CREATE TABLE edges (source_id INT, target_id INT, type TEXT, properties TEXT, project TEXT)")
    con.execute("CREATE TABLE index_coverage (project TEXT, rel_path TEXT, kind TEXT, detail TEXT)")
    con.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT)")
    con.commit()
    con.close()


# ---------- _extract_signature 纯函数面 ----------


def test_extract_signature_full():
    props = json.dumps({
        "complexity": 1, "param_count": 2,
        "signature": "(store: Store, market: str)",
        "return_type": "str",
    })
    assert etl._extract_signature(props) == ("(store: Store, market: str)", "str", 2)


def test_extract_signature_absent_keys_degrade_to_none():
    assert etl._extract_signature(json.dumps({"complexity": 3})) == (None, None, None)


def test_extract_signature_none_and_empty():
    assert etl._extract_signature(None) == (None, None, None)
    assert etl._extract_signature("") == (None, None, None)


def test_extract_signature_non_json_non_dict():
    assert etl._extract_signature("not json {") == (None, None, None)
    assert etl._extract_signature("[1, 2, 3]") == (None, None, None)


def test_extract_signature_wrong_typed_values():
    """类型守卫：签名面必须是 str/str/int——错型降级 None 非抛异常。"""
    assert etl._extract_signature(json.dumps({"signature": 5, "return_type": None, "param_count": "2"})) == (
        None, None, None,
    )
    # bool 是 int 子类：param_count=true 不当 1 用
    assert etl._extract_signature(json.dumps({"param_count": True})) == (None, None, None)
    assert etl._extract_signature(json.dumps({"signature": "(a)", "param_count": 0})) == ("(a)", None, 0)


# ---------- E2E：run_etl 落列 ----------


@pytest.fixture
def sig_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    cbm = tmp_path / "proj.db"
    _mk_cbm(cbm, [
        (1, "proj", "Function", "styled", "p.styled", "a.py", 1, 3,
         json.dumps({"signature": "(rows, t_anchor, seed)", "return_type": "str", "param_count": 3})),
        (2, "proj", "Function", "bare", "p.bare", "a.py", 5, 6, "{}"),
        (3, "proj", "Variable", "X", "p.X", "a.py", 8, 8,
         json.dumps({"lines": 1})),
    ])
    monkeypatch.setattr(etl.s1_cbm, "cbm_db_path", lambda name: cbm)
    return repo


def test_run_etl_lands_signature_columns(sig_repo):
    out_db = sig_repo / ".vrp" / "facts.db"
    etl.run_etl(sig_repo, "proj", out_db=out_db)
    con = sqlite3.connect(out_db)
    try:
        rows = {
            qn: (sig, ret, pc)
            for qn, sig, ret, pc in con.execute(
                "SELECT qualified_name, signature, return_type, param_count FROM symbols"
            )
        }
        assert rows["p.styled"] == ("(rows, t_anchor, seed)", "str", 3)
        assert rows["p.bare"] == (None, None, None)  # 无键诚实 NULL
        assert rows["p.X"] == (None, None, None)  # Variable 无签名面
        # properties_json 原样保留（列是查询面非数据源切换）
        props = con.execute(
            "SELECT properties_json FROM symbols WHERE qualified_name = 'p.styled'"
        ).fetchone()[0]
        assert json.loads(props)["signature"] == "(rows, t_anchor, seed)"
    finally:
        con.close()


def test_run_etl_signature_deterministic(sig_repo):
    out_db = sig_repo / ".vrp" / "facts.db"
    etl.run_etl(sig_repo, "proj", out_db=out_db)
    con = sqlite3.connect(out_db)
    try:
        first = con.execute(
            "SELECT qualified_name, signature, return_type, param_count FROM symbols"
            " ORDER BY qualified_name"
        ).fetchall()
    finally:
        con.close()
    etl.run_etl(sig_repo, "proj", out_db=out_db)
    con = sqlite3.connect(out_db)
    try:
        second = con.execute(
            "SELECT qualified_name, signature, return_type, param_count FROM symbols"
            " ORDER BY qualified_name"
        ).fetchall()
    finally:
        con.close()
    assert first == second


# ---------- nav_symbol 查询面 ----------


def test_nav_symbol_exposes_signature(sig_repo):
    out_db = sig_repo / ".vrp" / "facts.db"
    etl.run_etl(sig_repo, "proj", out_db=out_db)
    con = sqlite3.connect(out_db)
    try:
        fid = con.execute(
            "SELECT fid FROM symbols WHERE qualified_name = 'p.styled'"
        ).fetchone()[0]
    finally:
        con.close()
    result = nav.nav_symbol(sig_repo, fid)
    assert result["symbol"]["signature"] == "(rows, t_anchor, seed)"
    assert result["symbol"]["return_type"] == "str"
    assert result["symbol"]["param_count"] == 3


def test_nav_symbol_pre_v3_library_degrades(tmp_path):
    """pre-v3 facts.db（symbols 无三列）上 nav_symbol 诚实降级——不炸、
    不迁移（nav 保持 ro 纯）、键缺席即「此库无此面」信号。"""
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(
        """
        CREATE TABLE symbols (
            fid TEXT PRIMARY KEY, project TEXT NOT NULL, qualified_name TEXT NOT NULL,
            label TEXT NOT NULL, name TEXT NOT NULL, file_path TEXT NOT NULL DEFAULT '',
            start_line INTEGER, end_line INTEGER, content_hash TEXT NOT NULL DEFAULT '',
            properties_json TEXT, UNIQUE (project, qualified_name)
        );
        CREATE TABLE edges (
            src_fid TEXT NOT NULL, dst_fid TEXT NOT NULL, type TEXT NOT NULL,
            evidence TEXT NOT NULL, strategy TEXT, confidence REAL, line INTEGER,
            properties_json TEXT, resolution_label TEXT
        );
        CREATE TABLE findings (
            id INTEGER PRIMARY KEY, pathology TEXT NOT NULL, kind TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'etl', anchor_fid TEXT, related_fid TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER,
            payload_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE slices (
            id INTEGER PRIMARY KEY, slice_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            summary TEXT NOT NULL, payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
        );
        INSERT INTO symbols VALUES ('f1','p','p.old','Function','old','a.py',1,2,'h',NULL);
        PRAGMA user_version = 2;
        """
    )
    con.commit()
    con.close()
    result = nav.nav_symbol(repo, "f1")
    assert result["symbol"]["qualified_name"] == "p.old"
    assert "signature" not in result["symbol"]
    assert "return_type" not in result["symbol"]
    assert "param_count" not in result["symbol"]
