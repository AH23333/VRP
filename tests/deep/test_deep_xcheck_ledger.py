import json
import sqlite3

import pytest

from tests.deep.conftest import ROOT, run_vrp

pytestmark = pytest.mark.deep

XCHECK_CATEGORIES = (
    "ca_only",
    "ca_only_builtin_target",
    "cbm_only",
    "cbm_only_test_source",
)

P4_CATEGORIES = ("p4-cross-check-vulture", "p4-cross-check-knip")

LEDGER_BUSINESS_COLUMNS = "category, src_key, dst_key, cbm_edge_type, ca_confidence, ca_provenance, detail_json"


def _db(repo):
    return repo / ".vrp" / "facts.db"


def _ledger_rows(repo):
    con = sqlite3.connect(f"file:{_db(repo)}?mode=ro", uri=True)
    try:
        return con.execute(
            f"SELECT {LEDGER_BUSINESS_COLUMNS} FROM discrepancy_ledger"
            " ORDER BY category, src_key, dst_key, cbm_edge_type, detail_json"
        ).fetchall()
    finally:
        con.close()


def _ledger_total(repo):
    con = sqlite3.connect(f"file:{_db(repo)}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM discrepancy_ledger").fetchone()[0]
    finally:
        con.close()


def _ledger_p4_count(repo):
    con = sqlite3.connect(f"file:{_db(repo)}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM discrepancy_ledger WHERE category IN (?,?)", P4_CATEGORIES
        ).fetchone()[0]
    finally:
        con.close()


def _status_ledger_count(repo):
    out = json.loads(run_vrp(["status", str(repo)], cwd=ROOT))
    return out["counts"]["discrepancy_ledger"]


@pytest.fixture(scope="module")
def xcheck_state(lusiem_repo):
    repo = lusiem_repo
    if not _db(repo).exists():
        pytest.skip("lusiem facts.db not prepared")
    s1 = json.loads(run_vrp(["xcheck", str(repo)], cwd=ROOT))
    rows1 = _ledger_rows(repo)
    status1 = _status_ledger_count(repo)
    s2 = json.loads(run_vrp(["xcheck", str(repo)], cwd=ROOT))
    rows2 = _ledger_rows(repo)
    status2 = _status_ledger_count(repo)
    return {
        "repo": repo,
        "s1": s1,
        "s2": s2,
        "rows1": rows1,
        "rows2": rows2,
        "status1": status1,
        "status2": status2,
    }


def test_xcheck_rerun_ledger_stable(xcheck_state):
    state = xcheck_state
    assert state["s2"]["ledger_rows"] > 0
    xcheck_family = lambda rows: [r for r in rows if r[0] in XCHECK_CATEGORIES]
    assert xcheck_family(state["rows2"]) == xcheck_family(state["rows1"])
    assert len(xcheck_family(state["rows2"])) == state["s2"]["ledger_rows"]


def test_xcheck_summary_reproducible(xcheck_state):
    s1 = xcheck_state["s1"]
    s2 = xcheck_state["s2"]
    for key in ("ca_calls", "cbm_calls", "agreement", "agreement_pct", "ledger_rows"):
        assert s1[key] == s2[key]


def test_status_ledger_count_not_inflated_by_rerun(xcheck_state):
    state = xcheck_state
    repo = state["repo"]
    assert state["status1"] == state["status2"]
    assert state["status2"] - _ledger_p4_count(repo) == state["s2"]["ledger_rows"]


def test_ledger_categories_are_xcheck_or_p4(xcheck_state):
    categories = {row[0] for row in xcheck_state["rows2"]}
    assert categories <= set(XCHECK_CATEGORIES) | set(P4_CATEGORIES)
    assert categories & set(XCHECK_CATEGORIES)


def test_ledger_counts_match_summary(xcheck_state):
    s2 = xcheck_state["s2"]
    per_category = {}
    for row in xcheck_state["rows2"]:
        if row[0] in XCHECK_CATEGORIES:
            per_category[row[0]] = per_category.get(row[0], 0) + 1
    for category in XCHECK_CATEGORIES:
        assert per_category.get(category, 0) == s2[category]
    assert sum(per_category.values()) == s2["ledger_rows"]


def test_ledger_agreement_identities(xcheck_state):
    s2 = xcheck_state["s2"]
    assert s2["agreement"] + s2["ca_only"] + s2["ca_only_builtin_target"] == s2["ca_calls"]
    assert s2["cbm_only"] + s2["cbm_only_test_source"] <= s2["cbm_calls"]
    assert s2["agreement"] >= s2["cbm_calls"] - s2["cbm_only"] - s2["cbm_only_test_source"]


def test_ledger_detail_json_shape(xcheck_state):
    rows = [row for row in xcheck_state["rows2"] if row[0] in XCHECK_CATEGORIES]
    for category, _src, _dst, _edge_type, _conf, _prov, detail_json in rows:
        detail = json.loads(detail_json)
        assert {"src_name", "src_file", "dst_name", "dst_file"} <= set(detail)
        if category.startswith("cbm_only"):
            assert "strategy" in detail


def test_ledger_cbm_keys_join_to_symbols(xcheck_state):
    repo = xcheck_state["repo"]
    con = sqlite3.connect(f"file:{_db(repo)}?mode=ro", uri=True)
    try:
        symbol_files = {
            row[0]
            for row in con.execute("SELECT DISTINCT file_path FROM symbols WHERE file_path != ''")
        }
    finally:
        con.close()
    cbm_rows = [row for row in xcheck_state["rows2"] if row[0].startswith("cbm_only")]
    if not cbm_rows:
        pytest.skip("no cbm_only rows on this repo")
    for _cat, src_key, dst_key, *_rest in cbm_rows:
        for key in (src_key, dst_key):
            _name, _, file_part = key.rpartition("@")
            assert file_part in symbol_files


def test_xcheck_meta_keys_written(xcheck_state):
    repo = xcheck_state["repo"]
    s2 = xcheck_state["s2"]
    con = sqlite3.connect(f"file:{_db(repo)}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta WHERE key LIKE 'xcheck_%'"))
    finally:
        con.close()
    for key in (
        "ca_calls",
        "cbm_calls",
        "agreement",
        "agreement_pct",
        "ca_only",
        "ca_only_builtin_target",
        "cbm_only",
        "cbm_only_test_source",
        "ledger_rows",
    ):
        assert meta[f"xcheck_{key}"] == str(s2[key])


def test_xcheck_json_file_matches_summary(xcheck_state):
    repo = xcheck_state["repo"]
    payload = json.loads((repo / ".vrp" / "xcheck.json").read_text(encoding="utf-8"))
    s2 = xcheck_state["s2"]
    assert payload["ledger_rows"] == s2["ledger_rows"]
    assert payload["agreement_pct"] == s2["agreement_pct"]


def test_xcheck_does_not_touch_p4_categories(lusiem_repo):
    repo = lusiem_repo
    if not _db(repo).exists():
        pytest.skip("lusiem facts.db not prepared")
    con = sqlite3.connect(_db(repo))
    con.execute(
        "INSERT INTO discrepancy_ledger (category, src_key, dst_key, detail_json)"
        " VALUES ('p4-cross-check-vulture', 'fake@fake.py', 'fake2@fake.py', '{}')"
    )
    con.commit()
    marker_id = con.execute("SELECT MAX(id) FROM discrepancy_ledger").fetchone()[0]
    con.close()
    try:
        summary = json.loads(run_vrp(["xcheck", str(repo)], cwd=ROOT))
        con = sqlite3.connect(f"file:{_db(repo)}?mode=ro", uri=True)
        try:
            marker_present = con.execute(
                "SELECT COUNT(*) FROM discrepancy_ledger WHERE id = ?", (marker_id,)
            ).fetchone()[0]
            xcheck_rows = con.execute(
                f"SELECT COUNT(*) FROM discrepancy_ledger WHERE category IN ({','.join('?' * len(XCHECK_CATEGORIES))})",
                XCHECK_CATEGORIES,
            ).fetchone()[0]
        finally:
            con.close()
        assert marker_present == 1
        assert xcheck_rows == summary["ledger_rows"]
    finally:
        con = sqlite3.connect(_db(repo))
        con.execute("DELETE FROM discrepancy_ledger WHERE id = ?", (marker_id,))
        con.commit()
        con.close()
