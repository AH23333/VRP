"""BB2 (#197) 死代码置信度三级+reason——分层契约与整词匹配事故锁。

CGC 整词匹配事故（code_finder.py:20-25，#1332）：子串 `name CONTAINS 'main'`
误杀 domain_check/remainder/maintain_index——本文件钉死整词纪律回归。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s2_deadcode, s5_dossier
from vrp.s2_deadcode import CONFIDENCE_RANK, ENTRY_NAMES, classify, is_candidate

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_TEXT = (ROOT / "vrp" / "schema" / "facts_schema.sql").read_text(encoding="utf-8")


def _mk_con():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_TEXT)
    return con


def _sym(con, fid, name, path="src/a.py", label="Function", qn=None):
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
        " VALUES (?,?,?,?,?,?,?)",
        (fid, "p", qn or f"p.{fid}.{name}", label, name, path, "h"),
    )


def _edge(con, src, dst, etype="CALLS"):
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,?,'E')",
        (src, dst, etype),
    )


def test_classify_dunder_low():
    assert classify("__init__", set()) == ("low", "dunder - invoked implicitly by the runtime")
    assert classify("__deepcopy__", set())[0] == "low"


@pytest.mark.parametrize("name", ["test_helper", "helper_test", "test", "Test", "TEST"])
def test_classify_test_hook_names_low(name):
    confidence, reason = classify(name, set())
    assert confidence == "low"
    assert "test" in reason


@pytest.mark.parametrize("name", sorted(ENTRY_NAMES))
def test_classify_entry_names_low(name):
    confidence, reason = classify(name, set())
    assert confidence == "low"
    assert "entry-point" in reason


@pytest.mark.parametrize(
    "name",
    ["domain_check", "remainder", "maintain_index", "entry_count", "main_loop", "run_helper", "setup_wizard"],
)
def test_classify_whole_word_entry_regression(name):
    """整词纪律回归锁：入口名不得子串匹配（CGC CONTAINS 'main' 事故）。"""
    assert classify(name, set()) == ("high", "no callers, no usage hints, no excluding signal")


def test_classify_sibling_medium():
    confidence, reason = classify("helper", {"helper"})
    assert confidence == "medium"
    assert "dynamic dispatch" in reason


def test_classify_clean_high():
    assert classify("orphan_fn", set()) == ("high", "no callers, no usage hints, no excluding signal")


def test_classify_order_low_beats_sibling():
    """低置信类优先于 sibling：dunder/入口名即使同名有调用方也归 low。"""
    called = {"__init__", "main", "test_setup"}
    assert classify("__init__", called)[0] == "low"
    assert classify("main", called)[0] == "low"
    assert classify("test_setup", called)[0] == "low"


def test_is_candidate_structural_only():
    """BB2 后 is_candidate=结构性 universe：名字级类放行给 classify 分层。"""
    assert is_candidate("__init__", "src/a.py")
    assert is_candidate("main", "src/a.py")
    assert is_candidate("test_helper", "src/a.py")
    assert not is_candidate("helper", "tests/a.py")
    assert not is_candidate("helper", "test/a.py")
    assert not is_candidate("helper", "<synthetic>")
    assert not is_candidate("", "src/a.py")
    assert not is_candidate("helper", "")


def test_dead_code_candidates_universe_includes_low_classes():
    con = _mk_con()
    try:
        _sym(con, "f1", "__init__")
        _sym(con, "f2", "main")
        _sym(con, "f3", "orphan_fn")
        _sym(con, "f4", "used_fn")
        _edge(con, "f0", "f4")
        names = {row[2] for row in s2_deadcode.dead_code_candidates(con)}
        assert names == {"__init__", "main", "orphan_fn"}
    finally:
        con.close()


def test_scan_payload_confidence_reason_and_counts():
    con = _mk_con()
    try:
        _sym(con, "f1", "__init__")
        _sym(con, "f2", "main")
        _sym(con, "f3", "orphan_fn")
        result = s2_deadcode.scan(con)
        assert result["total"] == 3
        assert result["by_confidence"] == {"high": 1, "medium": 0, "low": 2}
        rows = con.execute(
            "SELECT anchor_fid, payload_json FROM findings WHERE source = 's2-deadcode' ORDER BY anchor_fid"
        ).fetchall()
        payloads = {fid: json.loads(p) for fid, p in rows}
        assert payloads["f1"]["confidence"] == "low"
        assert payloads["f1"]["reason"] == "dunder - invoked implicitly by the runtime"
        assert payloads["f2"]["confidence"] == "low"
        assert payloads["f2"]["reason"] == "entry-point name - typically called externally"
        assert payloads["f3"]["confidence"] == "high"
        assert set(payloads["f3"]) == {"qualified_name", "label", "confidence", "reason"}
    finally:
        con.close()


def test_scan_sibling_medium_flow():
    con = _mk_con()
    try:
        _sym(con, "a1", "helper", path="src/a.py")
        _sym(con, "b1", "helper", path="src/b.py")
        _sym(con, "c1", "caller")
        _edge(con, "c1", "b1")
        result = s2_deadcode.scan(con)
        assert result["by_confidence"]["medium"] == 1
        row = con.execute(
            "SELECT payload_json FROM findings WHERE source = 's2-deadcode' AND anchor_fid = 'a1'"
        ).fetchone()
        payload = json.loads(row[0])
        assert payload["confidence"] == "medium"
        assert "dynamic dispatch" in payload["reason"]
    finally:
        con.close()


def test_scan_deterministic_rerun():
    con = _mk_con()
    try:
        _sym(con, "f1", "__init__")
        _sym(con, "f2", "orphan_fn")
        first = s2_deadcode.scan(con)
        rows1 = con.execute(
            "SELECT anchor_fid, payload_json FROM findings WHERE source = 's2-deadcode' ORDER BY anchor_fid"
        ).fetchall()
        second = s2_deadcode.scan(con)
        rows2 = con.execute(
            "SELECT anchor_fid, payload_json FROM findings WHERE source = 's2-deadcode' ORDER BY anchor_fid"
        ).fetchall()
        assert first == second
        assert rows1 == rows2
    finally:
        con.close()


def test_view_matches_universe_python():
    con = _mk_con()
    try:
        _sym(con, "f1", "__init__")
        _sym(con, "f2", "main")
        _sym(con, "f3", "orphan_fn", path="tests/x.py")
        _sym(con, "f4", "used_fn")
        _edge(con, "f0", "f4")
        con.commit()
        view_fids = {row[0] for row in con.execute("SELECT fid FROM v_dead_code")}
        py_fids = {row[0] for row in s2_deadcode.dead_code_candidates(con)}
        assert view_fids == py_fids == {"f1", "f2"}
    finally:
        con.close()


def test_dossier_dead_code_tier_order_and_fields():
    con = _mk_con()
    try:
        _sym(con, "f1", "orphan_high")
        _sym(con, "f2", "helper", path="src/b.py")
        _sym(con, "f3", "helper", path="src/c.py")
        _sym(con, "f4", "caller")
        _edge(con, "f4", "f2")
        _sym(con, "f5", "__init__")
        con.commit()
        rows = s5_dossier.dead_code(con, limit=10)
        assert {r["qualified_name"] for r in rows} == {
            "p.f1.orphan_high",
            "p.f4.caller",
            "p.f3.helper",
            "p.f5.__init__",
        }
        ranks = [CONFIDENCE_RANK[r["confidence"]] for r in rows]
        assert ranks == sorted(ranks)
        assert [r["confidence"] for r in rows] == ["high", "high", "medium", "low"]
        assert all("reason" in r for r in rows)
    finally:
        con.close()


def test_dossier_dead_code_limit_prefers_high():
    con = _mk_con()
    try:
        _sym(con, "f1", "orphan_high")
        _sym(con, "f5", "__init__")
        con.commit()
        rows = s5_dossier.dead_code(con, limit=1)
        assert len(rows) == 1
        assert rows[0]["confidence"] == "high"
    finally:
        con.close()
