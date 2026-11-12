"""R-S dbpath.ro_connect 行为锁定。

SQLite file: URI 语法给 '?'（query 分隔）'#'（fragment 起点）'%'（百分号
转义）赋予结构含义——裸 f-string 内插路径时含这些字符的仓库路径被截断
或改写（'#' 截断后静默打开错误/空库=最坏失败类）。ro_connect 统一
percent-encode；本文件逐字符类锁定修复行为（修复前 '#'/'%23' 两类红）。
"""

import sqlite3
import tempfile

import pytest

from vrp.dbpath import ro_connect


def _make_db(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE t (x)")
        con.execute("INSERT INTO t VALUES (42)")
        con.commit()
    finally:
        con.close()


def _read_back(db_path) -> int:
    con = ro_connect(db_path)
    try:
        row = con.execute("SELECT x FROM t").fetchone()
    finally:
        con.close()
    assert row is not None, "opened the wrong (empty) database"
    return row[0]


def test_ro_connect_hash_path(tmp_path):
    """'#' 路径：裸 URI 会截断成 fragment 静默打开错误/空库（活红主案）。"""
    db = tmp_path / "C#" / "proj" / "facts.db"
    _make_db(db)
    assert _read_back(db) == 42


def test_ro_connect_percent_path(tmp_path):
    """'%' 路径：裸 URI 依赖 sqlite 对孤立 '%' 的宽容解码。"""
    db = tmp_path / "100% backup" / "facts.db"
    _make_db(db)
    assert _read_back(db) == 42


def test_ro_connect_percent_encoded_trap_path(tmp_path):
    """字面 '%23' 目录：裸 URI 把 '%23' 解码成 '#' 后指向不存在的路径。"""
    db = tmp_path / "v0.23%23release" / "facts.db"
    _make_db(db)
    assert _read_back(db) == 42


def test_ro_connect_cjk_path(tmp_path):
    """非 ASCII 路径：percent-encode UTF-8 字节后 URI 往返无损。"""
    db = tmp_path / "中文目录" / "facts.db"
    _make_db(db)
    assert _read_back(db) == 42


def test_ro_connect_clean_path_unchanged(tmp_path):
    """干净路径行为不变（等价旧裸 URI 形态）。"""
    db = tmp_path / "facts.db"
    _make_db(db)
    assert _read_back(db) == 42


def test_ro_connect_is_read_only(tmp_path):
    """mode=ro 仍在：写被拒。"""
    db = tmp_path / "facts.db"
    _make_db(db)
    con = ro_connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("CREATE TABLE t2 (y)")
            con.commit()
    finally:
        con.close()


def test_ro_connect_missing_db_does_not_create(tmp_path):
    """mode=ro 语义保持：缺失库不静默新建（报错而非空库）。"""
    missing = tmp_path / "nope" / "facts.db"
    with pytest.raises(sqlite3.Error):
        ro_connect(missing)
