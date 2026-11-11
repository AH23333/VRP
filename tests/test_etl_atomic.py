"""H2: run_etl must be atomic — a mid-run failure must not destroy the old facts.db."""
import os
import sqlite3

import pytest

from vrp import etl

SCHEMA = etl.SCHEMA_PATH


def _mk_cbm(db):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO store_meta VALUES (?,?)", [("db_uid", "uid-1"), ("mutation_gen", "7")])
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, properties TEXT)"
    )
    con.execute(
        "INSERT INTO nodes VALUES (1,'proj','Function','old','p.a.old','a.py',1,2,'{}')"
    )
    con.execute("CREATE TABLE edges (source_id INT, target_id INT, type TEXT, properties TEXT, project TEXT)")
    con.execute("CREATE TABLE index_coverage (project TEXT, rel_path TEXT, kind TEXT, detail TEXT)")
    con.execute("INSERT INTO index_coverage VALUES ('proj','a.py','ok','')")
    con.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT)")
    con.commit()
    con.close()


@pytest.fixture
def cbm_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    cbm = tmp_path / "proj.db"
    _mk_cbm(cbm)
    monkeypatch.setattr(etl.s1_cbm, "cbm_db_path", lambda name: cbm)
    return repo


def test_run_etl_mid_failure_keeps_old_db(tmp_path, monkeypatch, cbm_repo):
    repo = cbm_repo
    out_db = repo / ".vrp" / "facts.db"
    stats = etl.run_etl(repo, "proj", out_db=out_db)
    assert stats["symbols"] == 1
    good_bytes = out_db.read_bytes()

    state = {"fail": False}
    real_coverage = etl._etl_coverage

    def maybe_fail(*args, **kwargs):
        if state["fail"]:
            raise RuntimeError("coverage exploded")
        return real_coverage(*args, **kwargs)

    monkeypatch.setattr(etl, "_etl_coverage", maybe_fail)
    state["fail"] = True
    with pytest.raises(RuntimeError, match="coverage exploded"):
        etl.run_etl(repo, "proj", out_db=out_db)

    assert out_db.read_bytes() == good_bytes, "old facts.db must survive a mid-run failure"
    # artifact-hints.json（P-1）是合法持久产物（首次成功 etl 写出），非临时文件
    leftovers = [p.name for p in out_db.parent.iterdir() if p.name not in ("facts.db", "artifact-hints.json")]
    assert leftovers == [], f"temp files left behind: {leftovers}"

    state["fail"] = False
    etl.run_etl(repo, "proj", out_db=out_db)
    con = sqlite3.connect(out_db)
    assert con.execute("SELECT qualified_name FROM symbols").fetchall() == [("p.a.old",)]
    con.close()


def test_run_etl_ki_cleans_tmp_and_reraises(tmp_path, monkeypatch, cbm_repo):
    """R-P：run_etl 中途 Ctrl-C（KeyboardInterrupt 非 Exception 子类）同样走
    清理边界——dst.close() + tmp 清理后重抛；修前 KI 绕过清理裸逃，
    .vrp/facts.db.tmp-<pid> 带打开句柄泄漏（Windows 锁死文件，pid 异名
    后续运行永不清理）。"""
    repo = cbm_repo
    out_db = repo / ".vrp" / "facts.db"
    etl.run_etl(repo, "proj", out_db=out_db)  # 健康基线
    good_bytes = out_db.read_bytes()

    def _ki(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(etl, "_etl_coverage", _ki)
    with pytest.raises(KeyboardInterrupt):
        etl.run_etl(repo, "proj", out_db=out_db)
    tmp = out_db.with_name(f"{out_db.name}.tmp-{os.getpid()}")
    assert not tmp.exists(), "Ctrl-C 后 tmp facts.db 不得残留"
    assert out_db.read_bytes() == good_bytes  # 老库不受影响
