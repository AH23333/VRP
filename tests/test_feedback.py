"""S7 vrp findings：反馈环降权（构想v12 亮点）。

守卫重点：红线"降权不删除"（dismiss 后 finding 仍在列表）、按 kind 类
指数降权（0.5^n）且跨类隔离、确定性（同库同输出）、dismiss 不存在 id
诚实 SystemExit、limit 截断不撒谎（summary.total 恒真）、文件库 backup
前必须 commit（#158 sqlite3 backup 死锁教训）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vrp import feedback as fb
from vrp.cli import build_parser, cmd_findings


def _seed_finding(con, pathology: str = "P1", kind: str = "debug-print",
                  path: str = "a.py", line: int = 10) -> int:
    cur = con.execute(
        "INSERT INTO findings (pathology, kind, source, file_path, start_line, payload_json)"
        " VALUES (?, ?, 'etl', ?, ?, '{}')",
        (pathology, kind, path, line),
    )
    return cur.lastrowid


def test_rank_baseline_no_decisions(facts_db):
    ids = [_seed_finding(facts_db, kind="debug-print"),
           _seed_finding(facts_db, kind="token-clone", line=20)]
    result = fb.rank_findings(facts_db)
    assert [e["id"] for e in result["findings"]] == ids  # 基础序 = id 序
    assert all(e["weight"] == 1.0 for e in result["findings"])
    assert all(e["reason"] == "" for e in result["findings"])
    s = result["summary"]
    assert s["total"] == 2
    assert s["kinds"] == 2
    assert s["dismiss_decisions"] == 0
    assert s["dismissed_kinds"] == {}


def test_dismiss_downweights_class_never_deletes(facts_db):
    """红线守卫：dismiss 后 finding 仍在（降权不删除），同 kind 类整体
    降权调序，decisions 留痕。
    """
    f1 = _seed_finding(facts_db, kind="debug-print")
    f2 = _seed_finding(facts_db, kind="debug-print", line=20)
    f3 = _seed_finding(facts_db, kind="token-clone", line=30)
    fb.dismiss_finding(facts_db, f1)
    result = fb.rank_findings(facts_db)
    by_id = {e["id"]: e for e in result["findings"]}
    assert len(result["findings"]) == 3  # 红线：一个不删
    assert [e["id"] for e in result["findings"]] == [f3, f1, f2]  # 降权调序
    assert by_id[f1]["weight"] == 0.5
    assert by_id[f2]["weight"] == 0.5  # 同类连带降权
    assert by_id[f3]["weight"] == 1.0  # 异类不动
    assert "downweighted: 1 dismissal(s) on kind 'debug-print'" in by_id[f1]["reason"]
    row = facts_db.execute(
        "SELECT kind, target_id, decision FROM decisions"
    ).fetchone()
    assert row == ("finding", str(f1), "dismiss")


def test_dismiss_exponential_per_class(facts_db):
    a1 = _seed_finding(facts_db, kind="debug-print")
    a2 = _seed_finding(facts_db, kind="debug-print", line=20)
    b1 = _seed_finding(facts_db, kind="token-clone", line=30)
    fb.dismiss_finding(facts_db, a1)
    fb.dismiss_finding(facts_db, a2)  # 同类第二次 → 0.25
    fb.dismiss_finding(facts_db, b1)  # 异类一次 → 0.5
    result = fb.rank_findings(facts_db)
    by_id = {e["id"]: e for e in result["findings"]}
    assert by_id[a1]["weight"] == 0.25
    assert by_id[a2]["weight"] == 0.25
    assert by_id[b1]["weight"] == 0.5
    assert [e["id"] for e in result["findings"]] == [b1, a1, a2]  # (类次数, id) 序
    assert result["summary"]["dismissed_kinds"] == {"debug-print": 2, "token-clone": 1}


def test_rank_deterministic(facts_db):
    _seed_finding(facts_db, kind="debug-print")
    _seed_finding(facts_db, kind="token-clone", line=20)
    fb.dismiss_finding(facts_db, 1)
    r1 = fb.rank_findings(facts_db)
    r2 = fb.rank_findings(facts_db)
    assert r1 == r2  # 复现性红线：同库同输出


def test_dismiss_missing_id_exit(facts_db):
    _seed_finding(facts_db)
    with pytest.raises(SystemExit, match="no such finding: 99"):
        fb.dismiss_finding(facts_db, 99)


def test_dismiss_receipt_shape(facts_db):
    fid_ = _seed_finding(facts_db, pathology="P2", kind="token-clone")
    receipt = fb.dismiss_finding(facts_db, fid_, note="not a real issue")
    assert receipt["finding"] == fid_
    assert receipt["pathology"] == "P2"
    assert receipt["kind"] == "token-clone"
    assert receipt["decision"] == "dismiss"
    assert receipt["note"] == "not a real issue"
    assert receipt["class_dismissals"] == 1
    assert receipt["class_weight"] == 0.5
    assert "never deletes" in receipt["red_line"]


def test_rank_cross_class_isolation(facts_db):
    a1 = _seed_finding(facts_db, kind="debug-print")
    b1 = _seed_finding(facts_db, kind="token-clone", line=20)
    fb.dismiss_finding(facts_db, a1)
    result = fb.rank_findings(facts_db)
    by_id = {e["id"]: e for e in result["findings"]}
    assert by_id[b1]["class_dismissals"] == 0
    assert by_id[b1]["reason"] == ""


def test_run_findings_end_to_end(tmp_path):
    """文件库全链：build(:memory:)→dismiss→commit→backup→run_findings 只读
    （#158 教训：backup 前必须 commit，否则 sqlite3 backup 死锁）。
    """
    schema_path = Path(fb.__file__).parent / "schema" / "facts_schema.sql"
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    src = sqlite3.connect(":memory:")
    src.executescript(schema_path.read_text(encoding="utf-8"))
    f1 = _seed_finding(src, kind="debug-print")
    _seed_finding(src, kind="debug-print", line=20)
    _seed_finding(src, kind="token-clone", line=30)
    fb.dismiss_finding(src, f1, note="false positive")
    src.commit()  # backup 前必须提交（未提交事务 backup 死锁）
    dst = sqlite3.connect(repo / ".vrp" / "facts.db")
    src.backup(dst)
    dst.close()
    src.close()
    result = fb.run_findings(repo, limit=2)
    assert result["summary"]["total"] == 3  # limit 截断不撒谎
    assert result["shown"] == 2
    assert result["limit"] == 2
    assert [e["id"] for e in result["findings"]] == [3, 1]
    text = fb.render_text(result)
    assert text.startswith("vrp findings - repo")
    assert "findings    : 3 total (2 kinds), showing 2" in text
    assert "dismissals  : 1 decision(s)" in text
    assert "debug-print dismissed 1x" in text


def test_run_findings_missing_facts_exit(tmp_path):
    with pytest.raises(SystemExit, match="facts.db missing"):
        fb.run_findings(tmp_path)


def test_run_findings_empty(tmp_path):
    schema_path = Path(fb.__file__).parent / "schema" / "facts_schema.sql"
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    src = sqlite3.connect(":memory:")
    src.executescript(schema_path.read_text(encoding="utf-8"))
    src.commit()
    dst = sqlite3.connect(repo / ".vrp" / "facts.db")
    src.backup(dst)
    dst.close()
    src.close()
    result = fb.run_findings(repo)
    assert result["summary"]["total"] == 0
    assert result["findings"] == []
    text = fb.render_text(result)
    assert "findings    : 0 total (0 kinds), showing 0" in text


def test_cli_parser_accepts_findings():
    args = build_parser().parse_args(["findings", "somerepo"])
    assert args.func is cmd_findings
    assert args.repo == "somerepo"
    assert args.limit == fb.DEFAULT_LIMIT
    assert args.dismiss is None
    assert args.json is False
    args = build_parser().parse_args(
        ["findings", "somerepo", "--json", "--limit", "5", "--dismiss", "3", "--note", "x"]
    )
    assert args.json is True
    assert args.limit == 5
    assert args.dismiss == 3
    assert args.note == "x"
