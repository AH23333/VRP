"""BB1 (#205): v_dead_code AMBIGUOUS 过滤与 s2/s5 提示面行为。

唯一动行为点：AMBIGUOUS CALLS 边不再证明"活"（构想v13 BB1 原文
"AMBIGUOUS 边不参与必死判定只参与提示"）；NULL label（未知：CALL_REFERENCE
全族、未迁移旧行）维持救活——tsa 1268 条 CALL_REFERENCE 行为零变化。
视图与 s2_deadcode.dead_code_candidates 两处定义同批拆分，必须 lockstep
（deep 侧 test_deep_view_alignment 对照 schema 文本面）。
"""

import sqlite3
from pathlib import Path

from vrp import s2_deadcode, s5_dossier
from vrp.evidence import AMBIGUOUS, EXTRACTED, INFERRED

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"
SCHEMA_TEXT = SCHEMA.read_text(encoding="utf-8")


def _mk():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_TEXT)
    return con


def _sym(con, fid, qn, name="fn", path="src/a.py", label="Function"):
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
        " VALUES (?,?,?,?,?,?,?)",
        (fid, "p", qn, label, name, path, "h"),
    )


def _edge(con, src, dst, etype="CALLS", rlabel=None):
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence, resolution_label)"
        " VALUES (?,?,?,?,'')",
        (src, dst, etype, "hybrid-lsp"),
    )
    con.execute("UPDATE edges SET resolution_label = ? WHERE rowid = last_insert_rowid()", (rlabel,))


def _view_fids(con):
    return {r[0] for r in con.execute("SELECT fid FROM v_dead_code")}


def _cand_fids(con):
    return {r[0] for r in s2_deadcode.dead_code_candidates(con)}


def test_ambiguous_only_calls_yield_candidate():
    con = _mk()
    try:
        _sym(con, "f1", "p.alpha", "alpha")
        _sym(con, "f2", "p.caller", "caller", label="Class")  # caller 非 Function/Method：不入候选
        _edge(con, "f2", "f1", "CALLS", AMBIGUOUS)
        con.commit()
        # 视图收留：AMBIGUOUS 边不救活
        assert _view_fids(con) == {"f1"}
        # candidates 带提示信号
        rows = s2_deadcode.dead_code_candidates(con)
        assert len(rows) == 1
        assert bool(rows[0][7]) is True  # has_ambiguous 尾列
    finally:
        con.close()


def test_null_label_edge_keeps_symbol_alive():
    con = _mk()
    try:
        _sym(con, "f1", "p.alpha", "alpha")
        _sym(con, "f2", "p.caller", "caller", label="Class")  # caller 非 Function/Method：不入候选
        _edge(con, "f2", "f1", "CALL_REFERENCE", None)
        con.commit()
        assert _view_fids(con) == set()
        rows = s2_deadcode.dead_code_candidates(con)
        assert rows == []
    finally:
        con.close()


def test_extracted_and_inferred_keep_alive():
    con = _mk()
    try:
        _sym(con, "f1", "p.alpha", "alpha")
        _sym(con, "f2", "p.caller", "caller", label="Class")
        _sym(con, "f3", "p.other", "other", label="Class")
        _edge(con, "f2", "f1", "CALLS", EXTRACTED)
        _edge(con, "f3", "f2", "CALLS", INFERRED)
        con.commit()
        assert _view_fids(con) == set()
    finally:
        con.close()


def test_mentions_spawns_unfiltered():
    con = _mk()
    try:
        _sym(con, "f1", "p.alpha", "alpha")
        _sym(con, "f2", "p.caller", "caller", label="Class")  # caller 非 Function/Method：不入候选
        _edge(con, "f2", "f1", "MENTIONS", AMBIGUOUS)  # 非 CALLS 族：label 不适用
        con.commit()
        assert _view_fids(con) == set()
        _edge(con, "f2", "f1", "SPAWNS", None)
        con.commit()
        assert _view_fids(con) == set()
    finally:
        con.close()


def test_view_and_candidates_lockstep():
    """混合边灌库：视图成员集 == candidates 成员集（两处定义同批拆分，
    漂移即红——BB1 第二实现事故防线）。"""
    con = _mk()
    try:
        _sym(con, "fa", "p.a", "a")
        _sym(con, "fb", "p.b", "b")
        _sym(con, "fc", "p.c", "c")
        _sym(con, "fd", "p.d", "d")
        _sym(con, "fx", "p.x", "x", label="Class")  # caller 非 Function/Method：不入候选
        _edge(con, "fx", "fa", "CALLS", AMBIGUOUS)      # 入候选（has_ambiguous）
        _edge(con, "fx", "fb", "CALL_REFERENCE", None)  # NULL 救活
        _edge(con, "fx", "fc", "CALLS", EXTRACTED)      # 高置信救活
        _edge(con, "fx", "fd", "CALLS", AMBIGUOUS)
        _edge(con, "fd", "fa", "CALLS", INFERRED)       # fa 双边：一条 INFERRED 救活
        con.commit()
        assert _view_fids(con) == {"fd"}
        assert _cand_fids(con) == {"fd"}
        rows = {r[0]: bool(r[7]) for r in s2_deadcode.dead_code_candidates(con)}
        assert rows == {"fd": True}
    finally:
        con.close()


def test_scan_emits_medium_for_ambiguous():
    con = _mk()
    try:
        _sym(con, "f1", "p.alpha", "alpha")
        _sym(con, "f2", "p.caller", "caller", label="Class")  # caller 非 Function/Method：不入候选
        _edge(con, "f2", "f1", "CALLS", AMBIGUOUS)
        con.commit()
        result = s2_deadcode.scan(con)
        assert result["total"] == 1
        assert result["by_confidence"]["medium"] == 1
        assert result["by_confidence"]["high"] == 0
        payload = con.execute(
            "SELECT payload_json FROM findings WHERE source = ?", (s2_deadcode.FINDINGS_SOURCE,)
        ).fetchone()[0]
        assert "low-confidence" in payload
    finally:
        con.close()


def test_dossier_dead_code_no_key_leak():
    """dossier 输出形状零变化（fid/has_ambiguous 不泄漏——pre-BB1 键集恒等）。"""
    con = _mk()
    try:
        _sym(con, "f1", "p.alpha", "alpha")
        _sym(con, "f2", "p.caller", "caller", label="Class")  # caller 非 Function/Method：不入候选
        _edge(con, "f2", "f1", "CALLS", AMBIGUOUS)
        con.commit()
        rows = s5_dossier.dead_code(con, limit=10)
        assert len(rows) == 1
        assert set(rows[0].keys()) == {
            "qualified_name",
            "label",
            "file_path",
            "name",
            "confidence",
            "reason",
        }
    finally:
        con.close()


def test_dossier_dead_code_medium_signal():
    con = _mk()
    try:
        _sym(con, "f1", "p.alpha", "alpha")
        _sym(con, "f2", "p.caller", "caller", label="Class")  # caller 非 Function/Method：不入候选
        _edge(con, "f2", "f1", "CALLS", AMBIGUOUS)
        con.commit()
        rows = s5_dossier.dead_code(con, limit=10)
        assert rows[0]["qualified_name"] == "p.alpha"
        assert rows[0]["confidence"] == "medium"
        assert "low-confidence" in rows[0]["reason"]
    finally:
        con.close()
