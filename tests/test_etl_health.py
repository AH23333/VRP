"""BB4 ETL 健康面测试：不变式清单逐项命中/豁免/schema 陈旧/rc 语义/CLI E2E。"""

import sqlite3
from pathlib import Path

from vrp import etl_health
from vrp.cli import main as cli_main

REPO_ROOT = Path(__file__).parent.parent
SCHEMA = REPO_ROOT / "vrp" / "schema" / "facts_schema.sql"

SYM = "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path) VALUES (?,?,?,?,?,?)"


def _sym(con, fid, file_path="a.py"):
    con.execute(SYM, (fid, "p", f"p.{fid}", "Function", fid, file_path))


def test_healthy_db_all_checks_zero(facts_db):
    _sym(facts_db, "f1")
    facts_db.execute("INSERT INTO etl_file_state (file_path, wt_hash) VALUES ('a.py','h1')")
    facts_db.commit()
    result = etl_health.run_health(facts_db)
    assert result["healthy"] is True
    assert result["violations"] == 0
    assert all(c["ok"] and c["count"] == 0 for c in result["checks"])
    assert {c["check"] for c in result["checks"]} == {
        "symbols_fid_unique",
        "edges_orphan_src",
        "edges_orphan_dst",
        "edges_self_loop",
        "usage_hints_orphans",
        "test_linkage_orphans",
        "edges_full_row_dupes",
        "symbols_stale_files",
    }


def test_orphan_edge_detected(facts_db):
    _sym(facts_db, "f1")
    facts_db.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('f1','ghost','CALLS','import')"
    )
    facts_db.commit()
    result = etl_health.run_health(facts_db)
    by = {c["check"]: c for c in result["checks"]}
    assert result["healthy"] is False
    assert by["edges_orphan_dst"]["ok"] is False and by["edges_orphan_dst"]["count"] == 1
    assert by["edges_orphan_src"]["ok"] is True


def test_self_loop_and_usage_orphan_detected(facts_db):
    _sym(facts_db, "f1")
    facts_db.execute("INSERT INTO etl_file_state (file_path, wt_hash) VALUES ('a.py','h')")  # 隔离 stale 面
    facts_db.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('f1','f1','CALLS','import')")
    facts_db.execute("INSERT INTO usage_hints (src_fid, dst_fid) VALUES ('ghost','f1')")
    facts_db.execute("INSERT INTO test_linkage (test_fid, target_fid, source) VALUES ('ghost2','f1','pytest')")
    facts_db.commit()
    result = etl_health.run_health(facts_db)
    by = {c["check"]: c for c in result["checks"]}
    assert by["edges_self_loop"]["count"] == 1
    assert by["usage_hints_orphans"]["count"] == 1
    assert by["test_linkage_orphans"]["count"] == 1
    assert result["violations"] == 3


def test_full_row_dupe_edge_detected(facts_db):
    _sym(facts_db, "f1")
    _sym(facts_db, "f2")
    row = ("f1", "f2", "CALLS", "import")
    facts_db.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,?,?)", row)
    facts_db.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,?,?)", row)
    facts_db.commit()
    result = etl_health.run_health(facts_db)
    by = {c["check"]: c for c in result["checks"]}
    assert by["edges_full_row_dupes"]["ok"] is False
    assert by["edges_full_row_dupes"]["count"] == 1


def test_stale_symbol_file_detected_exemptions(facts_db):
    # dogfood 实测钉死的三类合法非文件实体（fresh run 36 例全属此类）
    _sym(facts_db, "f1", file_path="gone.py")  # 真陈旧——不在 etl_file_state
    _sym(facts_db, "f2", file_path="")  # 空路径——无文件符号合法
    _sym(facts_db, "f3", file_path="<python-builtins>")  # 合成命名空间豁免
    con = facts_db
    con.execute(  # Folder label——目录节点结构性非文件
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path)"
        " VALUES ('d1','p','p.d1','Folder','docs','docs')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path)"
        " VALUES ('b1','p','p.b1','Branch','master','{}')"
    )
    con.execute("INSERT INTO etl_file_state (file_path, wt_hash) VALUES ('kept.py','h')")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path)"
        " VALUES ('f4','p','p.f4','File','kept','kept.py')"
    )  # File label 在快照内——不触发
    con.commit()
    result = etl_health.run_health(con)
    by = {c["check"]: c for c in result["checks"]}
    assert by["symbols_stale_files"]["ok"] is False
    assert by["symbols_stale_files"]["count"] == 1  # 仅 gone.py 真陈旧


def test_missing_table_schema_stale_no_crash(facts_db):
    facts_db.execute("DROP TABLE etl_file_state")
    facts_db.commit()
    result = etl_health.run_health(facts_db)
    by = {c["check"]: c for c in result["checks"]}
    assert result["healthy"] is False
    assert by["schema_stale"]["ok"] is False
    assert by["symbols_stale_files"]["count"] is None  # 依赖表缺失——跳过不猜
    assert "依赖表缺失" in by["symbols_stale_files"]["hint"]
    assert "重跑 vrp run" in by["schema_stale"]["hint"]


def test_render_text_fail_hint_lines(facts_db):
    _sym(facts_db, "f1")
    facts_db.execute("INSERT INTO etl_file_state (file_path, wt_hash) VALUES ('a.py','h')")  # 隔离 stale 面
    facts_db.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('f1','ghost','CALLS','import')")
    facts_db.commit()
    result = etl_health.run_health(facts_db)
    text = etl_health.render_text(result)
    assert "[FAIL] edges_orphan_dst: 1" in text
    assert "-> " in text  # FAIL 行带修法提示
    assert "healthy: false (1 violation(s))" in text


def test_cli_etl_health_rc_semantics(tmp_path, capsys):
    db_path = tmp_path / ".vrp" / "facts.db"
    db_path.parent.mkdir()
    disk = sqlite3.connect(db_path)
    disk.executescript(SCHEMA.read_text(encoding="utf-8"))
    disk.execute(SYM, ("f1", "p", "p.f1", "Function", "f1", "a.py"))
    disk.execute("INSERT INTO etl_file_state (file_path, wt_hash) VALUES ('a.py','h')")
    disk.commit()
    disk.close()
    rc = cli_main(["etl-health", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "healthy: true" in out
    disk = sqlite3.connect(db_path)
    disk.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('f1','ghost','CALLS','import')")
    disk.commit()
    disk.close()
    rc = cli_main(["etl-health", str(tmp_path), "--json"])
    payload = capsys.readouterr().out
    import json

    data = json.loads(payload)
    assert rc == 1  # 违例响亮红（健康检查语义，CI 可用）
    assert data["healthy"] is False
