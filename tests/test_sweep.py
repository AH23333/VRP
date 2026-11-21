"""S8 阈值敏感度扫描测试面：曲线单调/悬崖点/宽容解析/空数据诚实/render。"""

import json
import sqlite3
from pathlib import Path

from vrp import sweep
from vrp.cli import main as cli_main

REPO_ROOT = Path(__file__).parent.parent
SCHEMA = REPO_ROOT / "vrp" / "schema" / "facts_schema.sql"

INS = "INSERT INTO findings (pathology, kind, source, payload_json) VALUES (?,?,?,?)"


def _seed(con, tokens, lines=None, bad_rows=0):
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    for t in tokens:
        con.execute(INS, ("P2", "duplicate", "s2-jscpd", json.dumps({"tokens": t, "lines": 5})))
    if lines:
        for l in lines:
            con.execute(INS, ("P2", "duplicate", "s2-jscpd", json.dumps({"tokens": 99, "lines": l})))
    for i in range(bad_rows):
        con.execute(INS, ("P2", "duplicate", "s2-jscpd", "not-json{" if i == 0 else json.dumps({"tokens": "high"})))
    con.execute(INS, ("P3", "swallowed-error", "s2-astgrep", json.dumps({"rule": "py-bare-except"})))
    con.commit()


def test_sweep_curve_monotone_and_counts(facts_db):
    _seed(facts_db, [20, 55, 67, 120, 200])
    result = sweep.run_sweep(facts_db)
    s = result["jscpd_min_tokens"]
    assert s["findings"] == 5
    assert s["thresholds"][0] == 20 and s["thresholds"][-1] == 200
    counts = s["counts"]
    assert all(counts[i] >= counts[i + 1] for i in range(len(counts) - 1))  # 单调不增
    assert counts[0] == 5  # t=20 全部 ≥
    assert counts[-1] == 1  # t=200 恰 200 一条


def test_cliff_point_detection(facts_db):
    _seed(facts_db, [25, 26, 27, 28, 100])
    result = sweep.run_sweep(facts_db)
    cliff = result["jscpd_min_tokens"]["cliff"]
    assert cliff is not None
    assert cliff["threshold"] == 30  # t=30 时 25-28 四条齐落——最大悬崖
    assert cliff["drop"] == 4


def test_lines_curve_independent(facts_db):
    _seed(facts_db, [50], lines=[5, 10, 45])
    result = sweep.run_sweep(facts_db)
    assert result["jscpd_min_tokens"]["findings"] == 4  # 1 token 行 + 3 line 行（都含 tokens=99）
    assert result["jscpd_min_lines"]["findings"] == 4
    lines_counts = result["jscpd_min_lines"]["counts"]
    assert lines_counts[0] == 4  # t=5
    idx_45 = result["jscpd_min_lines"]["thresholds"].index(45)
    assert lines_counts[idx_45] == 1


def test_tolerant_payload_parse(facts_db):
    _seed(facts_db, [60], bad_rows=2)
    result = sweep.run_sweep(facts_db)
    assert result["jscpd_min_tokens"]["findings"] == 1  # 坏 JSON 与非 int 值跳过


def test_empty_data_honest(facts_db):
    facts_db.executescript(SCHEMA.read_text(encoding="utf-8"))
    facts_db.commit()
    result = sweep.run_sweep(facts_db)
    assert result["jscpd_min_tokens"]["findings"] == 0
    assert result["jscpd_min_tokens"]["counts"] == [0] * len(sweep.TOKEN_GRID)
    assert result["jscpd_min_tokens"]["cliff"] is None


def test_notes_and_current_marker(facts_db):
    _seed(facts_db, [50])
    result = sweep.run_sweep(facts_db)
    assert result["jscpd_min_tokens"]["current"] == 50
    assert any("boolean" in n for n in result["notes"])
    assert any("vulture" in n for n in result["notes"])


def test_render_text_bars_and_current(facts_db):
    _seed(facts_db, [30, 60, 120])
    text = sweep.render_text(sweep.run_sweep(facts_db))
    assert "-- jscpd minTokens (3 findings) --" in text
    assert "<- current" in text  # t=50 标记
    assert "cliff:" in text
    assert "no engine re-run" in text


def test_cli_sweep_end_to_end(tmp_path, facts_db, capsys):
    _seed(facts_db, [40, 80])
    db_path = tmp_path / ".vrp" / "facts.db"
    db_path.parent.mkdir()
    facts_db.backup(sqlite3.connect(db_path))
    rc = cli_main(["sweep", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["jscpd_min_tokens"]["findings"] == 2
    rc = cli_main(["sweep", str(tmp_path)])
    assert rc == 0
    assert "== sweep ==" in capsys.readouterr().out


def test_non_dict_payload_skipped(facts_db):
    _seed(facts_db, [42])
    facts_db.execute(INS, ("P2", "duplicate", "s2-jscpd", "[1, 2]"))  # 合法 JSON 非 dict
    facts_db.commit()
    result = sweep.run_sweep(facts_db)
    assert result["jscpd_min_tokens"]["findings"] == 1  # 非 dict payload 宽容跳过


def test_render_cliff_none_on_flat_or_empty(facts_db):
    facts_db.executescript(SCHEMA.read_text(encoding="utf-8"))
    facts_db.commit()
    text = sweep.render_text(sweep.run_sweep(facts_db))
    assert "cliff: none on grid (flat or empty)" in text
