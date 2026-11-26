"""J8：ETL 增量化——等价对拍（D6 本件最重要）+ 四分类 + 闸门 + 崩溃安全。

等价判据：同输入下增量 vs 全量逐表行集相等。时点性元数据列不参与对拍
（findings.created_at / fid_ledger.first_seen_at / meta.etl_at——全量路径
自身也不满足字节自复现，红线绑定下游产物）。
"""

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from vrp import cli, etl
from vrp import etl_incremental as inc

# ---- fixture 语料：3 真文件 + 1 合成节点；边覆盖全路由面 ----
A_CONTENT = "def f():\n    return 1\n\ndef helper():\n    return 2\n"
B_CONTENT = "def g():\n    return 3\n"
C_CONTENT = "def h():\n    return 4\n"

V1_NODES = [
    (1, "proj", "Function", "f", "p.a.f", "a.py", 1, 2, "{}"),
    (2, "proj", "Function", "g", "p.b.g", "b.py", 1, 2, "{}"),
    (3, "proj", "Function", "h", "p.c.h", "c.py", 1, 2, "{}"),
    (4, "proj", "Function", "helper", "p.a.helper", "a.py", 4, 5, "{}"),
    (5, "proj", "Module", "mod", "<pkg>.mod", "<pkg::js>", 1, 1, "{}"),
]
V1_EDGES = [
    (1, 2, "CALLS", "{}", "proj"),
    (1, 3, "USAGE", "{}", "proj"),
    (1, 4, "SIMILAR_TO", '{"jaccard": 0.5, "same_file": false}', "proj"),
    (2, 3, "FILE_CHANGES_WITH", "{}", "proj"),
    (4, 1, "TESTS", "{}", "proj"),
    (2, 1, "USAGE", "{}", "proj"),
]
V1_HASHES = [("proj", "a.py", "ha"), ("proj", "b.py", "hb"), ("proj", "c.py", "hc")]
V1_COV = [("proj", "a.py", "ok", ""), ("proj", "b.py", "ok", ""), ("proj", "c.py", "gap", "no symbol for X")]


def _mk_cbm(db, nodes, edges, hashes, coverage, uid="uid-1", gen="7"):
    db = Path(db)
    db.unlink(missing_ok=True)  # 测试内重建：先清旧库
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO store_meta VALUES (?,?)", [("db_uid", uid), ("mutation_gen", gen)])
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, properties TEXT)"
    )
    con.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)", nodes)
    con.execute("CREATE TABLE edges (source_id INT, target_id INT, type TEXT, properties TEXT, project TEXT)")
    con.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", edges)
    con.execute("CREATE TABLE index_coverage (project TEXT, rel_path TEXT, kind TEXT, detail TEXT)")
    con.executemany("INSERT INTO index_coverage VALUES (?,?,?,?)", coverage)
    con.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT)")
    con.executemany("INSERT INTO file_hashes VALUES (?,?,?)", hashes)
    con.commit()
    con.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(A_CONTENT, encoding="utf-8", newline="\n")
    (repo / "b.py").write_text(B_CONTENT, encoding="utf-8", newline="\n")
    (repo / "c.py").write_text(C_CONTENT, encoding="utf-8", newline="\n")
    cbm = tmp_path / "proj.db"
    _mk_cbm(cbm, V1_NODES, V1_EDGES, V1_HASHES, V1_COV)
    monkeypatch.setattr(etl.s1_cbm, "cbm_db_path", lambda name, repo=None: cbm)
    monkeypatch.setattr(etl.s1_cbm, "cbm_version", lambda timeout=30: "t-1")
    return repo


def _row_set(db_path, sql):
    con = sqlite3.connect(db_path)
    try:
        return sorted((tuple(r) for r in con.execute(sql)), key=repr)
    finally:
        con.close()


TABLE_SQLS = {
    "symbols": "SELECT fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash, properties_json, signature, return_type, param_count FROM symbols",
    "edges": "SELECT * FROM edges",
    "usage_hints": "SELECT * FROM usage_hints",
    "history_coupling": "SELECT * FROM history_coupling",
    "test_linkage": "SELECT * FROM test_linkage",
    "findings": "SELECT pathology, kind, source, anchor_fid, related_fid, file_path, start_line, end_line, payload_json FROM findings WHERE source = 'etl'",
    "coverage_gaps": "SELECT * FROM coverage_gaps",
    "file_hashes": "SELECT * FROM file_hashes",
    "etl_file_state": "SELECT * FROM etl_file_state",
}


def _assert_equiv(db_a, db_b):
    for table, sql in TABLE_SQLS.items():
        ra, rb = _row_set(db_a, sql), _row_set(db_b, sql)
        assert ra == rb, f"table {table} diverged:\nfull={ra}\ninc ={rb}"


def _fids_by_qn(db_path):
    con = sqlite3.connect(db_path)
    try:
        return {qn: fid for fid, qn in con.execute("SELECT fid, qualified_name FROM symbols")}
    finally:
        con.close()


def _fids_by_file(db_path, fpath):
    con = sqlite3.connect(db_path)
    try:
        return [fid for (fid,) in con.execute("SELECT fid FROM symbols WHERE file_path = ?", (fpath,))]
    finally:
        con.close()


# ---- 基线：首跑全量建档，次跑增量 no-op ----

def test_first_run_full_then_incremental_noop(world):
    s1 = etl.run_etl(world, "repo")
    assert s1["mode"] == "full"
    db = world / ".vrp" / "facts.db"
    v1_symbols = _row_set(db, TABLE_SQLS["symbols"])
    v1_fids = _fids_by_qn(db)
    s2 = etl.run_etl(world, "repo")
    assert s2["mode"] == "incremental"
    assert s2["files_unchanged"] == 3
    assert (s2["files_changed"], s2["files_new"], s2["files_deleted"]) == (0, 0, 0)
    assert _row_set(db, TABLE_SQLS["symbols"]) == v1_symbols
    assert _fids_by_qn(db) == v1_fids  # no-op 零 fid 轮换


# ---- #218 守面：增量重提取集的签名三列不回退 NULL ----
# #217 增量路径曾漏并三列（10 列 INSERT）——重提取文件的 param_count 被抹
# NULL，仅深测 test_etl_idempotent 抓到（<python-builtins> 每轮重提取）。

def test_reextracted_symbols_keep_signature_columns(world):
    db = world / ".vrp" / "facts.db"
    cbm = world.parent / "proj.db"
    sig_props = '{"signature": "(x)", "return_type": "int", "param_count": 1}'
    nodes = [n for n in V1_NODES if n[0] != 1] + [
        (1, "proj", "Function", "f", "p.a.f", "a.py", 1, 2, sig_props),
    ]
    _mk_cbm(cbm, nodes, V1_EDGES, V1_HASHES, V1_COV)

    def sig_row(qn):
        con = sqlite3.connect(db)
        try:
            return con.execute(
                "SELECT signature, return_type, param_count FROM symbols WHERE qualified_name = ?",
                (qn,),
            ).fetchone()
        finally:
            con.close()

    etl.run_etl(world, "repo")
    assert sig_row("p.a.f") == ("(x)", "int", 1)

    # a.py 变更 + CBM gen 轮换 → 增量重提取 a.py 节点（修复前三列回退 NULL）
    (world / "a.py").write_text("def f():\n    return 9\n\ndef helper():\n    return 2\n", encoding="utf-8", newline="\n")
    _mk_cbm(cbm, nodes, V1_EDGES, V1_HASHES, V1_COV, gen="8")
    out = etl.run_etl(world, "repo")
    assert out["mode"] == "incremental"
    assert sig_row("p.a.f") == ("(x)", "int", 1)


# ---- 等价对拍（D6 本件最重要）----

def test_equivalence_after_single_file_change(world, tmp_path):
    db1 = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    v1_backup = tmp_path / "v1.db"
    shutil.copy2(db1, v1_backup)

    # v2：b.py 内容变更（g 内容变）+ b.py 新增符号 g2 + 三条新边
    (world / "b.py").write_text("def g():\n    return 30\n\ndef g2():\n    return 31\n", encoding="utf-8", newline="\n")
    nodes = [n for n in V1_NODES if n[0] != 2] + [
        (2, "proj", "Function", "g", "p.b.g", "b.py", 1, 2, "{}"),
        (6, "proj", "Function", "g2", "p.b.g2", "b.py", 4, 5, "{}"),
    ]
    edges = [e for e in V1_EDGES if e[0] != 2 and e[1] != 2] + [
        (2, 1, "USAGE", "{}", "proj"),
        (6, 1, "CALLS", "{}", "proj"),
        (2, 6, "CALLS", "{}", "proj"),
    ]
    cbm = world.parent / "proj.db"
    _mk_cbm(cbm, nodes, edges, V1_HASHES, V1_COV, gen="8")

    out_full = tmp_path / "full.db"
    etl.run_etl(world, "repo", out_db=out_full, full=True)
    out_inc = tmp_path / "inc.db"
    shutil.copy2(v1_backup, out_inc)
    s_inc = etl.run_etl(world, "repo", out_db=out_inc)
    assert s_inc["mode"] == "incremental"
    assert s_inc["files_changed"] == 1 and s_inc["files_unchanged"] == 2
    _assert_equiv(out_full, out_inc)


def test_equivalence_with_new_and_deleted_files(world, tmp_path):
    db1 = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    v1_backup = tmp_path / "v1.db"
    shutil.copy2(db1, v1_backup)

    # v2：删 c.py（节点/边/哈希/覆盖全撤）；新增 d.py + 交叉边
    (world / "c.py").unlink()
    (world / "d.py").write_text("def d():\n    return 9\n", encoding="utf-8", newline="\n")
    nodes = [n for n in V1_NODES if n[0] != 3] + [(7, "proj", "Function", "d", "p.d.d", "d.py", 1, 2, "{}")]
    edges = [e for e in V1_EDGES if e[0] != 3 and e[1] != 3] + [(7, 1, "CALLS", "{}", "proj")]
    hashes = [h for h in V1_HASHES if h[1] != "c.py"] + [("proj", "d.py", "hd")]
    cov = [c for c in V1_COV if c[1] != "c.py"] + [("proj", "d.py", "ok", "")]
    cbm = world.parent / "proj.db"
    _mk_cbm(cbm, nodes, edges, hashes, cov, gen="8")

    out_full = tmp_path / "full.db"
    etl.run_etl(world, "repo", out_db=out_full, full=True)
    out_inc = tmp_path / "inc.db"
    shutil.copy2(v1_backup, out_inc)
    s_inc = etl.run_etl(world, "repo", out_db=out_inc)
    assert s_inc["mode"] == "incremental"
    assert s_inc["files_new"] == 1 and s_inc["files_deleted"] == 1
    _assert_equiv(out_full, out_inc)


# ---- 四分类各路径 ----

def test_changed_file_rotates_fid_and_ledger_grows(world):
    db = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    old_fid = _fids_by_qn(db)["p.b.g"]
    (world / "b.py").write_text("def g():\n    return 30\n", encoding="utf-8", newline="\n")
    cbm = world.parent / "proj.db"
    _mk_cbm(cbm, V1_NODES, V1_EDGES, V1_HASHES, V1_COV, gen="8")
    s = etl.run_etl(world, "repo")
    assert s["mode"] == "incremental" and s["files_changed"] == 1
    new_fid = _fids_by_qn(db)["p.b.g"]
    assert new_fid != old_fid
    con = sqlite3.connect(db)
    ledger = con.execute(
        "SELECT COUNT(*) FROM fid_ledger WHERE fid IN (?, ?)", (old_fid, new_fid)
    ).fetchone()[0]
    con.close()
    assert ledger == 2  # F1：旧 fid 记忆保留，新 fid 入账


def test_unchanged_file_fids_stable(world):
    db = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    a_fids_before = set(_fids_by_file(db, "a.py"))
    (world / "b.py").write_text("def g():\n    return 30\n", encoding="utf-8", newline="\n")
    cbm = world.parent / "proj.db"
    _mk_cbm(cbm, V1_NODES, V1_EDGES, V1_HASHES, V1_COV, gen="8")
    etl.run_etl(world, "repo")
    assert set(_fids_by_file(db, "a.py")) == a_fids_before


def test_deleted_file_cascades_rows_and_edges(world):
    db = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    c_fids = _fids_by_file(db, "c.py")
    assert c_fids
    (world / "c.py").unlink()
    nodes = [n for n in V1_NODES if n[0] != 3]
    edges = [e for e in V1_EDGES if e[0] != 3 and e[1] != 3]
    hashes = [h for h in V1_HASHES if h[1] != "c.py"]
    cov = [c for c in V1_COV if c[1] != "c.py"]
    cbm = world.parent / "proj.db"
    _mk_cbm(cbm, nodes, edges, hashes, cov, gen="8")
    s = etl.run_etl(world, "repo")
    assert s["mode"] == "incremental" and s["files_deleted"] == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM symbols WHERE file_path = 'c.py'").fetchone()[0] == 0
    for table, cols in (
        ("edges", ("src_fid", "dst_fid")),
        ("usage_hints", ("src_fid", "dst_fid")),
        ("history_coupling", ("src_fid", "dst_fid")),
    ):
        for fid in c_fids:
            n = con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {cols[0]} = ? OR {cols[1]} = ?", (fid, fid)
            ).fetchone()[0]
            assert n == 0, f"{table} still references deleted fid"
    assert con.execute(
        "SELECT COUNT(*) FROM findings WHERE source = 'etl' AND (anchor_fid = ? OR related_fid = ?)",
        (c_fids[0], c_fids[0]),
    ).fetchone()[0] == 0
    con.close()


def test_new_file_extracted_with_cross_edges(world):
    db = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    (world / "d.py").write_text("def d():\n    return 9\n", encoding="utf-8", newline="\n")
    nodes = list(V1_NODES) + [(7, "proj", "Function", "d", "p.d.d", "d.py", 1, 2, "{}")]
    edges = list(V1_EDGES) + [(7, 1, "CALLS", "{}", "proj")]
    hashes = list(V1_HASHES) + [("proj", "d.py", "hd")]
    cov = list(V1_COV) + [("proj", "d.py", "ok", "")]
    cbm = world.parent / "proj.db"
    _mk_cbm(cbm, nodes, edges, hashes, cov, gen="8")
    s = etl.run_etl(world, "repo")
    assert s["mode"] == "incremental" and s["files_new"] == 1
    con = sqlite3.connect(db)
    d_fid = con.execute("SELECT fid FROM symbols WHERE qualified_name = 'p.d.d'").fetchone()[0]
    a_fid = con.execute("SELECT fid FROM symbols WHERE qualified_name = 'p.a.f'").fetchone()[0]
    assert con.execute(
        "SELECT COUNT(*) FROM edges WHERE src_fid = ? AND dst_fid = ?", (d_fid, a_fid)
    ).fetchone()[0] == 1  # 交叉边：新文件 → 未触碰文件旧 fid 锚定
    con.close()


def test_cross_edge_reanchors_to_unchanged_fid(world):
    db = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    old_a_fid = _fids_by_qn(db)["p.a.f"]
    (world / "b.py").write_text("def g():\n    return 30\n", encoding="utf-8", newline="\n")
    cbm = world.parent / "proj.db"
    _mk_cbm(cbm, V1_NODES, V1_EDGES, V1_HASHES, V1_COV, gen="8")
    etl.run_etl(world, "repo")
    con = sqlite3.connect(db)
    new_b_fid = con.execute("SELECT fid FROM symbols WHERE qualified_name = 'p.b.g'").fetchone()[0]
    # v1 的 USAGE 边 b→a：b 侧 fid 轮换，a 侧锚定未触碰旧 fid
    assert con.execute(
        "SELECT COUNT(*) FROM usage_hints WHERE src_fid = ? AND dst_fid = ?", (new_b_fid, old_a_fid)
    ).fetchone()[0] == 1
    con.close()


def test_carried_rows_survive_noop(world):
    db = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    edges_before = _row_set(db, TABLE_SQLS["edges"])
    findings_before = _row_set(db, TABLE_SQLS["findings"])
    s = etl.run_etl(world, "repo")
    assert s["mode"] == "incremental"
    assert _row_set(db, TABLE_SQLS["edges"]) == edges_before
    assert _row_set(db, TABLE_SQLS["findings"]) == findings_before


def test_synthetic_node_reextracted_every_run(world):
    db = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM symbols WHERE file_path = '<pkg::js>'").fetchone()[0] == 1
    con.close()
    s = etl.run_etl(world, "repo")
    assert s["mode"] == "incremental" and s["files_synthetic"] == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM symbols WHERE file_path = '<pkg::js>'").fetchone()[0] == 1
    con.close()


# ---- OWNED 存续 ----

def test_owned_tables_untouched_by_incremental(world):
    db = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO annotations (qualified_name, file_path, start_line, end_line, symbol_hash,"
        " window_hash, author, kind, body, status) VALUES ('p.a.f','a.py',1,2,'h','w','ai','purpose','x','active')"
    )
    con.execute("INSERT INTO curate_base (key, payload_json) VALUES ('slice:sl-1','{}')")
    con.execute("INSERT INTO decisions (kind, target_id, decision, note) VALUES ('curate_merge','x','keep','n')")
    con.commit()
    con.close()
    etl.run_etl(world, "repo")  # no-op 增量
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM curate_base").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    con.close()


# ---- 闸门（D4/D5）与回落 ----

def test_cbm_version_drift_forces_full(world, monkeypatch):
    etl.run_etl(world, "repo")
    monkeypatch.setattr(etl.s1_cbm, "cbm_version", lambda timeout=30: "t-2")
    s = etl.run_etl(world, "repo")
    assert s["mode"] == "full"
    assert "cbm_version drift" in s["incremental_fallback"]


def test_db_uid_drift_forces_full(world):
    etl.run_etl(world, "repo")
    cbm = world.parent / "proj.db"
    _mk_cbm(cbm, V1_NODES, V1_EDGES, V1_HASHES, V1_COV, uid="uid-2", gen="8")
    s = etl.run_etl(world, "repo")
    assert s["mode"] == "full"
    assert "cbm_db_uid drift" in s["incremental_fallback"]


def test_mutation_gen_bump_stays_incremental(world):
    etl.run_etl(world, "repo")
    cbm = world.parent / "proj.db"
    _mk_cbm(cbm, V1_NODES, V1_EDGES, V1_HASHES, V1_COV, gen="99")  # 仅代数 bump：不设闸（D4）
    s = etl.run_etl(world, "repo")
    assert s["mode"] == "incremental"


def test_full_flag_forces_full(world):
    etl.run_etl(world, "repo")
    s = etl.run_etl(world, "repo", full=True)
    assert s["mode"] == "full"
    assert "incremental_fallback" not in s


def test_internal_error_falls_back_to_full(world, monkeypatch):
    etl.run_etl(world, "repo")

    def _boom(*args, **kwargs):
        raise RuntimeError("patch exploded")

    monkeypatch.setattr(inc, "_patch", _boom)
    s = etl.run_etl(world, "repo")
    assert s["mode"] == "full"
    assert "internal error" in s["incremental_fallback"]
    con = sqlite3.connect(world / ".vrp" / "facts.db")
    assert con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 5  # 全量兜底重建完整
    con.close()


def test_pre_j8_db_without_file_state_falls_back(world):
    db = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    con = sqlite3.connect(db)
    con.execute("DROP TABLE etl_file_state")
    con.commit()
    con.close()
    s = etl.run_etl(world, "repo")
    assert s["mode"] == "full"
    assert "etl_file_state" in s["incremental_fallback"]
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM etl_file_state").fetchone()[0] == 3  # 全量路径重建档案
    con.close()


def test_crash_keeps_old_db_and_cleans_tmp(world, monkeypatch):
    db = world / ".vrp" / "facts.db"
    etl.run_etl(world, "repo")
    good = db.read_bytes()

    def _boom(*args, **kwargs):
        raise RuntimeError("meta exploded")

    monkeypatch.setattr(etl, "_write_meta", _boom)  # 增量与全量两路都炸：老库必须完好
    with pytest.raises(RuntimeError, match="meta exploded"):
        etl.run_etl(world, "repo")
    assert db.read_bytes() == good
    leftovers = [p.name for p in db.parent.iterdir() if p.name not in ("facts.db", "artifact-hints.json")]
    assert leftovers == [], f"temp files left behind: {leftovers}"


# ---- 确定性与 stats/meta 面 ----

def test_incremental_deterministic_double_run(world):
    etl.run_etl(world, "repo")
    (world / "b.py").write_text("def g():\n    return 30\n", encoding="utf-8", newline="\n")
    cbm = world.parent / "proj.db"
    _mk_cbm(cbm, V1_NODES, V1_EDGES, V1_HASHES, V1_COV, gen="8")
    etl.run_etl(world, "repo")
    db = world / ".vrp" / "facts.db"
    snap = {t: _row_set(db, sql) for t, sql in TABLE_SQLS.items()}
    etl.run_etl(world, "repo")  # 第二轮增量 no-op
    for t, sql in TABLE_SQLS.items():
        assert _row_set(db, sql) == snap[t], f"second incremental run drifted: {t}"


def test_stats_and_meta_fields(world):
    etl.run_etl(world, "repo")
    s = etl.run_etl(world, "repo")
    for key in ("mode", "files_unchanged", "files_changed", "files_new", "files_deleted",
                "t_classify_s", "t_patch_s", "t_total_s", "symbols", "edges"):
        assert key in s, f"stats missing {key}"
    assert s["symbols"] == 5 and s["edges"] == 1
    con = sqlite3.connect(world / ".vrp" / "facts.db")
    meta = dict(con.execute("SELECT key, value FROM meta"))
    con.close()
    assert meta["count_mode"] == "incremental"
    assert meta["count_files_unchanged"] == "3"


def test_wt_hash_matches_file_bytes(world):
    etl.run_etl(world, "repo")
    con = sqlite3.connect(world / ".vrp" / "facts.db")
    got = dict(con.execute("SELECT file_path, wt_hash FROM etl_file_state"))
    con.close()
    assert got["a.py"] == hashlib.sha256(A_CONTENT.encode("utf-8")).hexdigest()
    assert got["b.py"] == hashlib.sha256(B_CONTENT.encode("utf-8")).hexdigest()
    assert set(got) == {"a.py", "b.py", "c.py"}  # 检测范围对齐 CBM 文件集，合成路径不入档


def test_cli_etl_full_flag(tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args(["etl", str(tmp_path), "--full"])
    assert args.full is True
    args = parser.parse_args(["etl", str(tmp_path)])
    assert args.full is False
