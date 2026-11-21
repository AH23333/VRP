"""BB1/V17 (#205): resolution_label 判据与 ETL 标注接线。

三仓真值探针（2026-11-21, docs/设计稿-BB1V17 §1）锚定的判据：
confidence 双阈值 0.85/0.70，None=未知不惩罚（CALL_REFERENCE 全族与
未迁移旧行保持原行为）。CGC calls.py 三值词汇对齐（EXTRACTED/
INFERRED/AMBIGUOUS），非逐级 tier 复刻——VRP 无解析器 tier 可透传。
"""

import json
import sqlite3
from pathlib import Path

from vrp import etl
from vrp.evidence import AMBIGUOUS, EXTRACTED, INFERRED, resolution_label

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"
SCHEMA_TEXT = SCHEMA.read_text(encoding="utf-8")


def test_threshold_boundaries():
    assert resolution_label(0.85) == EXTRACTED
    assert resolution_label(0.86) == EXTRACTED
    assert resolution_label(0.84) == INFERRED
    assert resolution_label(0.70) == INFERRED
    assert resolution_label(0.69) == AMBIGUOUS
    assert resolution_label(0.0) == AMBIGUOUS
    assert resolution_label(1.0) == EXTRACTED


def test_none_confidence_is_unknown():
    # None = unknown, not low - CALL_REFERENCE and un-migrated rows keep
    # their previous v_dead_code behavior (design doc D4).
    assert resolution_label(None) is None


def test_non_numeric_rejected():
    assert resolution_label("0.9") is None
    # bool is an int subclass - must not slip through as confidence 1/0.
    assert resolution_label(True) is None
    assert resolution_label(False) is None


def test_int_confidence_accepted():
    assert resolution_label(1) == EXTRACTED
    assert resolution_label(0) == AMBIGUOUS


def test_probe_distribution_snapshot():
    """三仓探针记档值抽查（不依赖真实仓——值来自 2026-11-21 probe_bb1_edges.py）：
    lsp_direct 0.95→EXTRACTED；structural import_map 0.95→EXTRACTED；
    php_function_global_fallback 0.70→INFERRED；lsp_kt_any 0.65→AMBIGUOUS；
    unique_name 0.38→AMBIGUOUS；suffix_match 0.28→AMBIGUOUS。"""
    assert resolution_label(0.95) == EXTRACTED
    assert resolution_label(0.70) == INFERRED
    assert resolution_label(0.65) == AMBIGUOUS
    assert resolution_label(0.38) == AMBIGUOUS
    assert resolution_label(0.28) == AMBIGUOUS
    # tsa 连续低带（suffix_match 评分函数连续输出）逐点单调：
    for conf in (0.01, 0.17, 0.33, 0.41, 0.55, 0.75):
        assert resolution_label(conf) in (INFERRED, AMBIGUOUS)


def _mk_dst_src(edges):
    dst = sqlite3.connect(":memory:")
    dst.executescript(SCHEMA_TEXT)
    src = sqlite3.connect(":memory:")
    src.execute(
        "CREATE TABLE edges (source_id INTEGER, target_id INTEGER,"
        " type TEXT, properties TEXT, project TEXT)"
    )
    src.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", edges)
    src.commit()
    return dst, src


def test_etl_labels_calls_edges():
    dst, src = _mk_dst_src(
        [
            (1, 2, "CALLS", json.dumps({"strategy": "lsp_direct", "confidence": 0.95, "line": 3}), "p"),
            (1, 2, "CALLS", json.dumps({"strategy": "unique_name", "confidence": 0.38}), "p"),
            (1, 2, "CALLS", json.dumps({"strategy": "php_function_global_fallback", "confidence": 0.70}), "p"),
        ]
    )
    try:
        etl._etl_edges(dst, src, "p", {1: "f1", 2: "f2"}, {1: "a.py", 2: "b.py"}, set())
        rows = dst.execute(
            "SELECT confidence, resolution_label FROM edges ORDER BY confidence"
        ).fetchall()
        assert rows == [(0.38, AMBIGUOUS), (0.7, INFERRED), (0.95, EXTRACTED)]
    finally:
        dst.close()
        src.close()


def test_etl_call_reference_stays_null():
    # CBM never annotates CALL_REFERENCE confidence - honest NULL label,
    # keeps the edge alive for v_dead_code (D4: unknown is not punished).
    dst, src = _mk_dst_src(
        [
            (1, 2, "CALL_REFERENCE", json.dumps({}), "p"),
            (1, 2, "CALL_REFERENCE", json.dumps({"confidence": None}), "p"),
        ]
    )
    try:
        etl._etl_edges(dst, src, "p", {1: "f1", 2: "f2"}, {1: "a.py", 2: "b.py"}, set())
        rows = dst.execute("SELECT type, resolution_label FROM edges").fetchall()
        assert rows == [("CALL_REFERENCE", None), ("CALL_REFERENCE", None)]
    finally:
        dst.close()
        src.close()


def test_etl_other_edge_types_stay_null():
    dst, src = _mk_dst_src(
        [
            (1, 2, "MENTIONS", json.dumps({}), "p"),
            (1, 2, "SPAWNS", json.dumps({}), "p"),
            (1, 2, "DEFINES", json.dumps({}), "p"),
        ]
    )
    try:
        etl._etl_edges(dst, src, "p", {1: "f1", 2: "f2"}, {1: "a.py", 2: "b.py"}, set())
        rows = dst.execute("SELECT type, resolution_label FROM edges ORDER BY type").fetchall()
        assert rows == [("DEFINES", None), ("MENTIONS", None), ("SPAWNS", None)]
    finally:
        dst.close()
        src.close()
