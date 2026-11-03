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


def test_same_repo_reuses(tmp_path, cache, monkeypatch):
    db = cache / "proj.db"
    calls: list = []
    monkeypatch.setattr(s1_cbm, "run_cli", _fake_cbm(db, calls))
    repo = _mk_repo(tmp_path, "a")

    s1_cbm.index_repository(repo, "proj")
    r2 = s1_cbm.index_repository(repo, "proj")
    assert r2["status"] == "reused"
    assert len(calls) == 1, "same repo must reuse the cache without re-indexing"
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
