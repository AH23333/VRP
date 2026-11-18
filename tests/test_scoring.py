"""S6（构想v12）：finding 优先级评分测试——公式开放可解释（分量明细人可手算复验）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import cli, scoring

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY, pathology TEXT NOT NULL, kind TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'etl', anchor_fid TEXT, related_fid TEXT,
    file_path TEXT, start_line INTEGER, end_line INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS edges (
    src_fid TEXT NOT NULL, dst_fid TEXT NOT NULL, type TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '', strategy TEXT, confidence REAL,
    line INTEGER, properties_json TEXT);
"""


def _mk_con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA)
    return con


def _add_finding(con, *, id=None, kind="debug-print", anchor=None, path="a.py", start=None, end=None):
    con.execute(
        "INSERT INTO findings (id, pathology, kind, anchor_fid, file_path, start_line, end_line)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (id, "P9", kind, anchor, path, start, end),
    )


def _add_edge(con, dst, n=1, src=None):
    for i in range(n):
        con.execute(
            "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?, ?, 'CALLS', 'x')",
            (src or f"s{i}", dst),
        )


# ---- 分量数学（手算锚定） ----


def test_score_components_hand_computed():
    """impact=log1p(3 入度)、evidence=log1p(1)（2 独立 kind 减 1——第二类起
    才是佐证）、span=log1p(11 行)——手算复验：公式开放可解释红线。"""
    import math

    weights = {"impact": 1.0, "evidence": 0.5, "span": 0.25}
    score, comps = scoring.score_components(
        (1, "debug-print", "mod.fn", 100, 110), {"mod.fn": 3}, {"mod.fn": 2}, weights
    )
    assert comps["impact"] == pytest.approx(math.log1p(3), abs=1e-4)
    assert comps["evidence"] == pytest.approx(math.log1p(1), abs=1e-4)
    assert comps["span"] == pytest.approx(math.log1p(11), abs=1e-4)
    expected = math.log1p(3) + 0.5 * math.log1p(1) - 0.25 * math.log1p(11)
    assert score == pytest.approx(expected, abs=1e-9)


def test_score_components_no_anchor_zero_impact_evidence():
    """无锚 finding（unused-file 等）：impact/evidence 诚实 0，span 仍计。"""
    score, comps = scoring.score_components(
        (2, "unused-file", None, 1, 4), {}, {}, dict(scoring.DEFAULT_WEIGHTS)
    )
    assert comps["impact"] == 0.0 and comps["evidence"] == 0.0
    assert comps["span"] > 0.0  # 行面照计（修复成本与有无锚无关）


def test_score_components_span_lower_bound():
    """行序异常（end<start）/行号缺失：span 诚实 0 不产生负成本。"""
    for start, end in ((10, 5), (None, 4), (3, None), (None, None)):
        _, comps = scoring.score_components(
            (3, "k", None, start, end), {}, {}, dict(scoring.DEFAULT_WEIGHTS)
        )
        assert comps["span"] == 0.0


# ---- 权重解析面 ----


def test_parse_weights_none_uses_defaults():
    weights = scoring.parse_weights(None)
    assert weights == {"impact": 1.0, "evidence": 0.5, "span": 0.25}


def test_parse_weights_partial_override_and_unknown_key():
    weights = scoring.parse_weights('{"impact": 2.0}')
    assert weights["impact"] == 2.0
    assert weights["evidence"] == 0.5  # 未覆盖键走默认
    with pytest.raises(SystemExit, match="unknown weight key 'bogus'"):
        scoring.parse_weights('{"bogus": 1.0}')
    with pytest.raises(SystemExit, match="not valid JSON"):
        scoring.parse_weights("{oops")
    with pytest.raises(SystemExit, match="must be a JSON object"):
        scoring.parse_weights("[1, 2]")
    with pytest.raises(SystemExit, match="must be a number"):
        scoring.parse_weights('{"impact": "high"}')


# ---- 排序语义 ----


def test_score_findings_order_impact_first():
    """高入度锚排前（score 降序）；零分量平局退化为 id 升序。"""
    con = _mk_con()
    _add_finding(con, id=1, anchor="mod.quiet")   # 0 入度 -> score 0
    _add_finding(con, id=2, anchor="mod.hub")     # 5 入度 -> score > 0
    _add_finding(con, id=3, anchor=None)          # 无锚 -> score 0
    _add_edge(con, "mod.hub", n=5)
    result = scoring.score_findings(con, dict(scoring.DEFAULT_WEIGHTS))
    order = [e["id"] for e in result["findings"]]
    assert order == [2, 1, 3]  # hub 首位；平局 id 升序


def test_score_findings_span_penalizes_big_edits():
    """同锚同行号：修复面大的 finding 排后（span 在分母语义）。"""
    con = _mk_con()
    _add_finding(con, id=1, anchor="mod.fn", start=1, end=2)     # 小修复面
    _add_finding(con, id=2, anchor="mod.fn", start=1, end=200)   # 大修复面
    _add_edge(con, "mod.fn", n=2)
    result = scoring.score_findings(con, dict(scoring.DEFAULT_WEIGHTS))
    order = [e["id"] for e in result["findings"]]
    assert order == [1, 2]


def test_score_findings_evidence_cross_kind():
    """同锚 2 独立 kind（交叉佐证）比单 kind 锚高——DISTINCT kind 而非条数。"""
    con = _mk_con()
    _add_finding(con, id=1, kind="token-clone", anchor="mod.a")
    _add_finding(con, id=2, kind="layer-violation", anchor="mod.a")  # 同锚异 kind
    _add_finding(con, id=3, kind="debug-print", anchor="mod.b")
    _add_finding(con, id=4, kind="debug-print", anchor="mod.b")      # 同锚同 kind：不抬 evidence
    _add_edge(con, "mod.a", n=1)
    _add_edge(con, "mod.b", n=1)
    result = scoring.score_findings(con, dict(scoring.DEFAULT_WEIGHTS))
    by_id = {e["id"]: e for e in result["findings"]}
    assert by_id[1]["components"]["evidence"] == pytest.approx(0.6931, abs=1e-3)  # log1p(2-1)=ln(2)
    assert by_id[3]["components"]["evidence"] == 0.0  # 单 kind 锚：信号本身不抬佐证
    assert by_id[1]["score"] > by_id[3]["score"]


def test_score_findings_deterministic_rerun():
    con = _mk_con()
    _add_finding(con, id=1, anchor="mod.a", start=1, end=9)
    _add_finding(con, id=2, kind="token-clone", anchor="mod.a")
    _add_edge(con, "mod.a", n=3)
    w = dict(scoring.DEFAULT_WEIGHTS)
    first = scoring.score_findings(con, w)
    second = scoring.score_findings(con, w)
    assert first == second  # 复现性红线：同库同输出


def test_score_findings_weights_change_order():
    """权重可配（开放面）：span 权重拉满时大修复面沉底更狠——排序随权重动。"""
    con = _mk_con()
    _add_finding(con, id=1, anchor="mod.fn", start=1, end=2)
    _add_finding(con, id=2, anchor="mod.fn", start=1, end=200)
    _add_edge(con, "mod.fn", n=2)
    low_span = scoring.score_findings(con, {"impact": 1.0, "evidence": 0.0, "span": 0.01})
    high_span = scoring.score_findings(con, {"impact": 1.0, "evidence": 0.0, "span": 5.0})
    assert [e["id"] for e in low_span["findings"]] == [1, 2]
    assert [e["id"] for e in high_span["findings"]] == [1, 2]
    gap_low = low_span["findings"][0]["score"] - low_span["findings"][1]["score"]
    gap_high = high_span["findings"][0]["score"] - high_span["findings"][1]["score"]
    assert gap_high > gap_low  # span 权重越大，修复面差距越被放大


def test_score_findings_empty():
    con = _mk_con()
    result = scoring.score_findings(con, dict(scoring.DEFAULT_WEIGHTS))
    assert result["summary"]["total"] == 0
    assert result["findings"] == []


# ---- run/CLI 全链 ----


def _mk_repo_facts(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA)
    _add_finding(con, id=1, anchor="mod.hub", start=1, end=3)
    _add_finding(con, id=2, anchor="mod.quiet")
    _add_edge(con, "mod.hub", n=4)
    con.commit()
    con.close()
    return repo


def test_run_score_end_to_end_and_limit(tmp_path):
    repo = _mk_repo_facts(tmp_path)
    result = scoring.run_score(repo, limit=1)
    assert result["shown"] == 1
    assert result["summary"]["total"] == 2  # 截断不撒谎
    assert result["findings"][0]["id"] == 1  # hub 首位
    assert result["summary"]["formula"] == "score = 1.00*impact + 0.50*evidence - 0.25*span"


def test_run_score_missing_facts_exit(tmp_path):
    with pytest.raises(SystemExit, match="run 'vrp run' first"):
        scoring.run_score(tmp_path)


def test_render_text_shape(tmp_path):
    repo = _mk_repo_facts(tmp_path)
    result = scoring.run_score(repo)
    text = scoring.render_text(result)
    assert "score = 1.00*impact + 0.50*evidence - 0.25*span" in text
    assert "[impact" in text and "evidence" in text and "span" in text  # 分量明细在呈现面
    assert "showing 2" in text


def test_cli_score_end_to_end(tmp_path, capsys):
    repo = _mk_repo_facts(tmp_path)
    rc = cli.main(["score", str(repo), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["summary"]["total"] == 2
    assert out["findings"][0]["components"]["impact"] == pytest.approx(1.6094, abs=1e-3)
    # --weights 全链：impact 归零后 hub 只剩 span 罚分（1-3 行 = -0.3466）
    # 沉底，quiet 零分量居首——权重可配真改排序
    rc = cli.main(["score", str(repo), "--json", "--weights", '{"impact": 0.0}'])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [e["id"] for e in out["findings"]] == [2, 1]


def test_cli_parser_accepts_score():
    parser = cli.build_parser()
    args = parser.parse_args(["score", "some/repo", "--json", "--limit", "5"])
    assert args.repo == "some/repo"
    assert args.json and args.limit == 5 and args.weights is None
