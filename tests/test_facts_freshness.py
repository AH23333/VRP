"""M4: facts.db must carry the git_head it was built against, and consumers
must hard-fail when HEAD moved (or provenance is missing)."""

from __future__ import annotations

import sqlite3

import pytest

from vrp import cli, etl


@pytest.fixture()
def facts(tmp_path):
    db = tmp_path / "facts.db"
    con = sqlite3.connect(db)
    etl.ensure_schema(con)
    return con, db


def _set_head(con, value):
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('git_head', ?)", (value,))
    con.commit()


def test_head_moved_hard_fails(facts, monkeypatch):
    con, _ = facts
    _set_head(con, "aaaa1111")
    monkeypatch.setattr(etl, "current_git_head", lambda repo: "bbbb2222")
    with pytest.raises(RuntimeError) as exc:
        etl.check_facts_freshness(con, monkeypatch)
    msg = str(exc.value)
    assert "aaaa1111" in msg and "bbbb2222" in msg and "vrp etl" in msg


def test_head_match_passes(facts, monkeypatch):
    con, _ = facts
    _set_head(con, "cccc3333")
    monkeypatch.setattr(etl, "current_git_head", lambda repo: "cccc3333")
    etl.check_facts_freshness(con, monkeypatch)  # must not raise


def test_missing_git_head_hard_fails(facts, monkeypatch):
    con, _ = facts
    monkeypatch.setattr(etl, "current_git_head", lambda repo: "dddd4444")
    with pytest.raises(RuntimeError) as exc:
        etl.check_facts_freshness(con, monkeypatch)
    assert "git_head" in str(exc.value) and "vrp etl" in str(exc.value)


def test_current_git_head_timeout_returns_empty(tmp_path, monkeypatch):
    """R-AP：current_git_head 收编 procs.run_tree——timed_out 按既有契约降级 ''。"""
    def fake_tree(cmd, cwd=None, timeout=None, env=None):
        return {"returncode": -1, "timed_out": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(etl.procs, "run_tree", fake_tree)
    assert etl.current_git_head(tmp_path) == ""


def test_open_facts_rw_enforces_freshness(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    etl.ensure_schema(con)
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', 'old-head')")
    con.commit()
    con.close()
    monkeypatch.setattr(etl, "current_git_head", lambda r: "new-head")
    with pytest.raises(RuntimeError, match="stale"):
        cli._open_facts_rw(repo)


def test_open_facts_rw_closes_handle_on_stale(tmp_path, monkeypatch):
    """R-G N2: stale 硬拒路径必须 con.close()——未关句柄在 Windows 锁住 facts.db。

    修复前 unlink 探测抛 PermissionError；仅 Windows 有守卫力，其他平台恒绿无害。
    """
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    etl.ensure_schema(con)
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', 'old-head')")
    con.commit()
    con.close()
    monkeypatch.setattr(etl, "current_git_head", lambda r: "new-head")
    with pytest.raises(RuntimeError, match="stale"):
        cli._open_facts_rw(repo)
    (repo / ".vrp" / "facts.db").unlink()  # leaked handle would lock this on Windows


def test_etl_records_git_head(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    cbm = tmp_path / "proj.db"
    con = sqlite3.connect(cbm)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO store_meta VALUES ('db_uid', 'uid-1')")
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, properties TEXT)"
    )
    con.execute("INSERT INTO nodes VALUES (1,'proj','Function','old','p.a.old','a.py',1,2,'{}')")
    con.execute("CREATE TABLE edges (source_id INT, target_id INT, type TEXT, properties TEXT, project TEXT)")
    con.execute("CREATE TABLE index_coverage (project TEXT, rel_path TEXT, kind TEXT, detail TEXT)")
    con.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT)")
    con.commit()
    con.close()
    monkeypatch.setattr(etl.s1_cbm, "cbm_db_path", lambda name: cbm)
    monkeypatch.setattr(etl, "current_git_head", lambda r: "feedface")

    out_db = repo / ".vrp" / "facts.db"
    etl.run_etl(repo, "proj", out_db=out_db)
    dst = sqlite3.connect(out_db)
    assert dst.execute("SELECT value FROM meta WHERE key='git_head'").fetchone()[0] == "feedface"
    dst.close()
