import sqlite3

import pytest

from tests.deep.conftest import run_vrp, ROOT

pytestmark = pytest.mark.deep


def _open_ro(repo) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)


def test_big_repo_no_orphan_edges(big_repo):
    con = _open_ro(big_repo)
    try:
        orphan_src = con.execute(
            "SELECT COUNT(*) FROM edges WHERE src_fid NOT IN (SELECT fid FROM symbols)"
        ).fetchone()[0]
        orphan_dst = con.execute(
            "SELECT COUNT(*) FROM edges WHERE dst_fid NOT IN (SELECT fid FROM symbols)"
        ).fetchone()[0]
    finally:
        con.close()
    assert orphan_src == 0
    assert orphan_dst == 0


def test_big_repo_no_orphan_hints_and_linkage(big_repo):
    con = _open_ro(big_repo)
    try:
        for table, cols in (
            ("usage_hints", ("src_fid", "dst_fid")),
            ("test_linkage", ("test_fid", "target_fid")),
        ):
            for col in cols:
                n = con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} NOT IN (SELECT fid FROM symbols)"
                ).fetchone()[0]
                assert n == 0, f"{table}.{col} orphans: {n}"
    finally:
        con.close()


def test_big_repo_no_self_loop_edges(big_repo):
    con = _open_ro(big_repo)
    try:
        n = con.execute("SELECT COUNT(*) FROM edges WHERE src_fid = dst_fid").fetchone()[0]
    finally:
        con.close()
    assert n == 0


def test_big_repo_fid_unique(big_repo):
    con = _open_ro(big_repo)
    try:
        dup = con.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT fid) FROM symbols"
        ).fetchone()[0]
    finally:
        con.close()
    assert dup == 0


def test_big_repo_edges_are_evidence_rows_not_duplicates(big_repo):
    con = _open_ro(big_repo)
    try:
        total = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        distinct_full = con.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT src_fid, dst_fid, type, evidence,"
            " strategy, confidence, line, properties_json FROM edges)"
        ).fetchone()[0]
        multi = con.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM edges GROUP BY src_fid, dst_fid, type HAVING COUNT(*) > 1)"
        ).fetchone()[0]
    finally:
        con.close()
    assert total == distinct_full, "full-row duplicate edges exist (redundant ETL rows)"
    assert multi >= 50, "expected multi-evidence edges (same logical edge, distinct line/properties)"


def test_tx_scratch_no_orphans(tx_scratch):
    repo = tx_scratch
    con = _open_ro(repo)
    try:
        checks = [
            ("edges.src_fid", "SELECT COUNT(*) FROM edges WHERE src_fid NOT IN (SELECT fid FROM symbols)"),
            ("edges.dst_fid", "SELECT COUNT(*) FROM edges WHERE dst_fid NOT IN (SELECT fid FROM symbols)"),
            ("usage_hints", "SELECT COUNT(*) FROM usage_hints WHERE dst_fid NOT IN (SELECT fid FROM symbols)"),
            ("test_linkage", "SELECT COUNT(*) FROM test_linkage WHERE target_fid NOT IN (SELECT fid FROM symbols)"),
        ]
        for name, sql in checks:
            assert con.execute(sql).fetchone()[0] == 0, name
    finally:
        con.close()


def test_tx_scratch_etl_rerun_keeps_integrity(tx_scratch):
    run_vrp(["etl", str(tx_scratch)], cwd=ROOT)
    con = _open_ro(tx_scratch)
    try:
        orphan = con.execute(
            "SELECT COUNT(*) FROM edges WHERE src_fid NOT IN (SELECT fid FROM symbols)"
            " OR dst_fid NOT IN (SELECT fid FROM symbols)"
        ).fetchone()[0]
        self_loop = con.execute("SELECT COUNT(*) FROM edges WHERE src_fid = dst_fid").fetchone()[0]
    finally:
        con.close()
    assert orphan == 0
    assert self_loop == 0
