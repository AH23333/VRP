"""Open question #5: ETL must refuse CBM stores with drifted/downgraded
schemas instead of failing deep inside the copy loop with a cryptic
OperationalError. Supersets (newer CBM adding columns/tables) stay valid."""

from __future__ import annotations

import sqlite3

import pytest

from vrp import etl, s1_cbm


def _make_store(path, drop_column=None, drop_table=None, extra=False):
    con = sqlite3.connect(path)
    try:
        for table, columns in s1_cbm.CBM_SCHEMA_CONTRACT.items():
            if table == drop_table:
                continue
            if columns:
                cols = [c for c in columns if c != drop_column]
                con.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
            else:
                con.execute(f"CREATE TABLE {table} (k TEXT, v TEXT)")
        con.execute("INSERT INTO store_meta VALUES ('db_uid', 'uid-1')")
        if extra:
            con.execute("CREATE TABLE future_table (x TEXT)")
            con.execute("ALTER TABLE nodes ADD COLUMN future_col TEXT")
        con.commit()
    finally:
        con.close()
    return path


def test_guard_accepts_current_contract(tmp_path):
    store = _make_store(tmp_path / "proj.db")
    s1_cbm.verify_cbm_schema(store)


def test_guard_accepts_superset(tmp_path):
    store = _make_store(tmp_path / "proj.db", extra=True)
    s1_cbm.verify_cbm_schema(store)


def test_guard_rejects_missing_column(tmp_path):
    store = _make_store(tmp_path / "proj.db", drop_column="qualified_name")
    with pytest.raises(RuntimeError, match="qualified_name"):
        s1_cbm.verify_cbm_schema(store)


def test_guard_rejects_missing_table(tmp_path):
    store = _make_store(tmp_path / "proj.db", drop_table="edges")
    with pytest.raises(RuntimeError, match="missing table edges"):
        s1_cbm.verify_cbm_schema(store)


def test_guard_rejects_foreign_db(tmp_path):
    store = tmp_path / "foreign.db"
    con = sqlite3.connect(store)
    con.execute("CREATE TABLE stuff (a TEXT)")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="store_meta"):
        s1_cbm.verify_cbm_schema(store)


def test_run_etl_refuses_downgraded_store(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("CBM_CACHE_DIR", str(cache))
    store = _make_store(cache / "proj.db", drop_column="qualified_name")
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(RuntimeError, match="qualified_name"):
        etl.run_etl(repo, "proj", out_db=tmp_path / "out" / "facts.db")


def test_run_etl_refuses_foreign_db(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("CBM_CACHE_DIR", str(cache))
    store = cache / "proj.db"
    con = sqlite3.connect(store)
    con.execute("CREATE TABLE stuff (a TEXT)")
    con.commit()
    con.close()
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(RuntimeError, match="store_meta"):
        etl.run_etl(repo, "proj", out_db=tmp_path / "out" / "facts.db")
