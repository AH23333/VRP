"""H3: CBM cache is addressed by project name only - reuse must be guarded
by the repo path recorded in store_meta, never silent."""

from __future__ import annotations

import sqlite3

import pytest

from vrp import s1_cbm


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setenv("CBM_CACHE_DIR", str(cache_dir))
    return cache_dir


def _fake_cbm(db_path, calls):
    """Mimic the CBM binary: each run rebuilds the db from scratch with a
    store_meta that has no vrp provenance row."""

    def fake(args, timeout=3600):
        calls.append(args)
        db_path.unlink(missing_ok=True)
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO store_meta VALUES ('db_uid', 'uid-1')")
        con.commit()
        con.close()
        return {"ok": True}

    return fake


def _mk_repo(tmp_path, parent, name="proj"):
    repo = tmp_path / parent / name
    repo.mkdir(parents=True)
    return repo


def test_same_name_different_repo_rebuilds(tmp_path, cache, monkeypatch):
    db = cache / "proj.db"
    calls: list = []
    monkeypatch.setattr(s1_cbm, "run_cli", _fake_cbm(db, calls))
    repo_a = _mk_repo(tmp_path, "a")
    repo_b = _mk_repo(tmp_path, "b")

    r1 = s1_cbm.index_repository(repo_a, "proj")
    assert r1["status"] == "indexed" and len(calls) == 1
    assert s1_cbm._recorded_repo_path(db) == str(repo_a.resolve())

    r3 = s1_cbm.index_repository(repo_b, "proj")
    assert r3["status"] == "indexed", "same-name different repo must NOT be reused"
    assert len(calls) == 2
    assert r3["rebuild_reason"] == "repo-path-mismatch"
    assert "re-indexed" in r3["note"]
    assert s1_cbm._recorded_repo_path(db) == str(repo_b.resolve())


def test_same_repo_second_call_refreshes(tmp_path, cache, monkeypatch):
    """R6' 病根五：reused 短路取消——同仓二次调用必须恒走 CBM 增量刷新。
    （旧语义 reused 不重扫，仓库演进后陈旧 CBM 内容被 etl 盖新鲜 git_head，
    M4 被击穿——dogfood 实证 facts.git_head=37d1c6f 而零 R4'-R6' 符号。）"""
    db = cache / "proj.db"
    calls: list = []
    monkeypatch.setattr(s1_cbm, "run_cli", _fake_cbm(db, calls))
    repo = _mk_repo(tmp_path, "a")

    s1_cbm.index_repository(repo, "proj")
    r2 = s1_cbm.index_repository(repo, "proj")
    assert r2["status"] == "indexed", "同仓二次调用必须重扫（CBM 自身增量）"
    assert len(calls) == 2
    assert "rebuild_reason" not in r2


def test_old_db_without_provenance_rebuilds(tmp_path, cache, monkeypatch):
    db = cache / "proj.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO store_meta VALUES ('db_uid', 'legacy')")
    con.commit()
    con.close()

    calls: list = []
    monkeypatch.setattr(s1_cbm, "run_cli", _fake_cbm(db, calls))
    repo = _mk_repo(tmp_path, "a")
    r = s1_cbm.index_repository(repo, "proj")
    assert r["status"] == "indexed", "legacy db without provenance must be treated as unknown"
    assert r["rebuild_reason"] == "unknown-source"


def test_cbm_renamed_project_uses_returned_project(tmp_path, cache, monkeypatch):
    """Dogfood 2026-11-04: CBM may ignore --name and derive the project from the
    repo path (observed in a workspace context). index_repository must consume
    the returned project and locate the db accordingly."""
    slug_db = cache / "D-path-slug-proj.db"
    calls: list = []

    def fake(args, timeout=3600):
        calls.append(args)
        con = sqlite3.connect(slug_db)
        con.execute("CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT OR REPLACE INTO store_meta VALUES ('db_uid', 'uid-1')")
        con.commit()
        con.close()
        return {"ok": True, "project": "D-path-slug-proj"}

    monkeypatch.setattr(s1_cbm, "run_cli", fake)
    repo = _mk_repo(tmp_path, "a")
    r = s1_cbm.index_repository(repo, "proj")
    assert r["status"] == "indexed"
    assert r["project"] == "D-path-slug-proj"
    assert r["db"] == str(slug_db)

    r2 = s1_cbm.index_repository(repo, "proj")
    assert r2["status"] == "indexed", "provenance fallback must find the renamed db（且恒增量刷新）"
    assert r2["project"] == "D-path-slug-proj"
    assert len(calls) == 2


def test_find_cbm_db_provenance_fallback(tmp_path, cache):
    repo = _mk_repo(tmp_path, "a")
    slug_db = cache / "D-path-slug-proj.db"
    con = sqlite3.connect(slug_db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany(
        "INSERT INTO store_meta VALUES (?,?)",
        [("db_uid", "uid-1"), (s1_cbm.REPO_PATH_META_KEY, str(repo.resolve()))],
    )
    con.commit()
    con.close()

    assert s1_cbm.find_cbm_db("proj", str(repo.resolve())) == slug_db
    assert s1_cbm.find_cbm_db("proj", str((_mk_repo(tmp_path, "b")).resolve())) is None


def test_run_etl_resolves_renamed_project(tmp_path, cache):
    """ETL must filter nodes by the actual CBM project (db stem), not the name hint."""
    from vrp import etl

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    project = "D-path-slug-repo"
    slug_db = cache / f"{project}.db"
    con = sqlite3.connect(slug_db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany(
        "INSERT INTO store_meta VALUES (?,?)",
        [("db_uid", "uid-1"), (s1_cbm.REPO_PATH_META_KEY, str(repo.resolve()))],
    )
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, properties TEXT)"
    )
    con.execute(
        f"INSERT INTO nodes VALUES (1,'{project}','Function','old','p.a.old','a.py',1,2,'{{}}')"
    )
    con.execute("CREATE TABLE edges (source_id INT, target_id INT, type TEXT, properties TEXT, project TEXT)")
    con.execute("CREATE TABLE index_coverage (project TEXT, rel_path TEXT, kind TEXT, detail TEXT)")
    con.execute(f"INSERT INTO index_coverage VALUES ('{project}','a.py','ok','')")
    con.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT)")
    con.commit()
    con.close()

    stats = etl.run_etl(repo, "repo")
    assert stats["symbols"] == 1, "ETL must resolve the renamed project via provenance"
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    assert con.execute("SELECT qualified_name FROM symbols").fetchall() == [("p.a.old",)]
    con.close()


def test_record_repo_path_survives_quote_bearing_column(tmp_path):
    """R-D C4：畸形 store_meta 列名含引号——SQLite 标识符转义不产生语法错。"""
    db = tmp_path / "weird.db"
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE store_meta ("we""ird" TEXT PRIMARY KEY, v TEXT NOT NULL)')
    con.execute('INSERT INTO store_meta VALUES ("db_uid", "u1")')
    con.commit()
    con.close()
    s1_cbm._record_repo_path(db, str(tmp_path))  # must not raise
    assert s1_cbm._recorded_repo_path(db) == str(tmp_path)


def test_index_repository_rejects_separator_in_project(tmp_path, cache, monkeypatch):
    """R-D C5：CBM 报告的 project 名含路径分隔符——拒绝（cache 子目录写/逃逸面）。"""
    calls: list = []

    def fake(args, timeout=3600):
        calls.append(args)
        return {"ok": True, "project": "evil/name"}

    monkeypatch.setattr(s1_cbm, "run_cli", fake)
    repo = _mk_repo(tmp_path, "a")
    with pytest.raises(RuntimeError, match="unsafe project name"):
        s1_cbm.index_repository(repo, "proj")
    assert not (cache / "evil").exists()
