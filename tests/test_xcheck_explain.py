"""Q25 xcheck 叙事化测试面：四 category 模板/示例/agreement 阈值/未跑诚实/CLI E2E。"""

import json
import sqlite3
from pathlib import Path

from vrp import xcheck
from vrp.cli import main as cli_main

REPO_ROOT = Path(__file__).parent.parent
SCHEMA = REPO_ROOT / "vrp" / "schema" / "facts_schema.sql"

LEDGER = "INSERT INTO discrepancy_ledger (category, src_key, dst_key, cbm_edge_type, ca_confidence, ca_provenance, detail_json) VALUES (?,?,?,?,?,?,?)"


def _seed(con):
    for i in range(5):
        con.execute(LEDGER, ("ca_only", f"src{i}@a.py", f"dst{i}@b.py", None, 0.9, "call", "{}"))
    con.execute(LEDGER, ("ca_only_builtin_target", "s@a.py", "print@<builtin>", None, 1.0, "call", "{}"))
    con.execute(LEDGER, ("cbm_only", "s@a.py", "t@b.py", "CALLS", None, None, "{}"))
    con.execute(LEDGER, ("cbm_only_test_source", "test_x@t.py", "y@b.py", "CALLS", None, None, "{}"))
    con.execute(
        "INSERT INTO meta (key, value) VALUES ('xcheck_agreement_pct','27.7')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('xcheck_agreement','44')")
    con.execute("INSERT INTO meta (key, value) VALUES ('xcheck_ca_calls','159')")
    con.commit()


def test_not_ran_honest_message(facts_db):
    result = xcheck.explain(facts_db)
    assert result["ran"] is False
    assert result["categories"] == {}
    text = xcheck.render_explain(result)
    assert "先跑 'vrp xcheck'" in text


def test_four_categories_counts_and_examples(facts_db):
    _seed(facts_db)
    result = xcheck.explain(facts_db)
    cats = result["categories"]
    assert result["ran"] is True
    assert cats["ca_only"]["count"] == 5
    assert cats["ca_only"]["examples"] == [
        "src0@a.py -> dst0@b.py",
        "src1@a.py -> dst1@b.py",
        "src2@a.py -> dst2@b.py",  # 示例封顶 3，保 rowid 序
    ]
    assert cats["ca_only_builtin_target"]["count"] == 1
    assert cats["cbm_only"]["count"] == 1
    assert cats["cbm_only_test_source"]["count"] == 1


def test_narrative_phenomenon_cause_fix_templates(facts_db):
    _seed(facts_db)
    result = xcheck.explain(facts_db)
    for cat, entry in result["categories"].items():
        narr = entry["narrative"]
        assert narr["phenomenon"] and narr["cause"] and narr["fix"]
        assert str(entry["count"]) in narr["phenomenon"]  # 现象含计数
    assert "内建" in result["categories"]["ca_only_builtin_target"]["narrative"]["cause"]
    assert "测试源" in result["categories"]["cbm_only_test_source"]["narrative"]["cause"]


def test_low_agreement_threshold(facts_db):
    _seed(facts_db)
    low = xcheck.explain(facts_db)
    assert low["agreement_pct"] == 27.7
    assert low["low_agreement"] is True
    facts_db.execute("UPDATE meta SET value='90.0' WHERE key='xcheck_agreement_pct'")
    facts_db.commit()
    high = xcheck.explain(facts_db)
    assert high["low_agreement"] is False


def test_render_explain_overall_and_sections(facts_db):
    _seed(facts_db)
    result = xcheck.explain(facts_db)
    text = xcheck.render_explain(result)
    assert "agreement 27.7% (44/159) LOW" in text
    assert "[ca_only] 5 条" in text
    assert "现象:" in text and "原因:" in text and "修法:" in text
    assert "示例: src0@a.py -> dst0@b.py" in text


def test_rows_without_pct_meta_no_overall_line(facts_db):
    con = facts_db
    con.execute(LEDGER, ("ca_only", "s@a.py", "d@b.py", None, 0.9, "call", "{}"))
    con.commit()
    result = xcheck.explain(con)
    assert result["ran"] is True
    assert result["agreement_pct"] is None
    text = xcheck.render_explain(result)
    assert "overall:" not in text
    assert "[ca_only] 1 条" in text


def test_pct_meta_none_string_tolerated(facts_db):
    # run_xcheck 对空 ca_edges 写 agreement_pct=None → meta 存字符串 "None"
    facts_db.execute("INSERT INTO meta (key, value) VALUES ('xcheck_agreement_pct','None')")
    facts_db.commit()
    result = xcheck.explain(facts_db)
    assert result["ran"] is True
    assert result["agreement_pct"] is None
    assert result["low_agreement"] is False


def test_cli_xcheck_explain_end_to_end(tmp_path, facts_db, capsys):
    _seed(facts_db)
    db_path = tmp_path / ".vrp" / "facts.db"
    db_path.parent.mkdir()
    disk = sqlite3.connect(db_path)
    disk.executescript(SCHEMA.read_text(encoding="utf-8"))
    for row in facts_db.execute("SELECT category, src_key, dst_key, cbm_edge_type, ca_confidence, ca_provenance, detail_json FROM discrepancy_ledger").fetchall():
        disk.execute(LEDGER, row)
    for row in facts_db.execute("SELECT key, value FROM meta WHERE key LIKE 'xcheck_%'").fetchall():
        disk.execute("INSERT INTO meta (key, value) VALUES (?,?)", row)
    disk.commit()
    disk.close()
    rc = cli_main(["xcheck-explain", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0  # 解释面非门
    assert "agreement 27.7% (44/159) LOW" in out
    assert "[ca_only] 5 条" in out
