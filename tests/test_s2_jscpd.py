import json

from vrp.s2_jscpd import (
    LEDGER_CATEGORY,
    SOURCE,
    _ingest_filter,
    _norm_rel,
    cbm_p2_file_pairs,
    cross_check,
    parse_clones,
    scan,
)
from pathlib import Path


def _clone(first_name="src\\a.py", second_name="src\\b.py", first_line=9, second_line=9, lines=22, tokens=210):
    return {
        "firstFile": {"name": first_name, "start": 0, "end": 30,
                      "startLoc": {"column": 18, "line": first_line, "position": 167},
                      "endLoc": {"column": 19, "line": 30, "position": 871}},
        "secondFile": {"name": second_name, "start": 0, "end": 30,
                       "startLoc": {"column": 17, "line": second_line, "position": 166},
                       "endLoc": {"column": 19, "line": 30, "position": 870}},
        "format": "python", "lines": lines, "tokens": tokens, "fragment": "x", "isNew": True, "kind": "f",
    }


def test_parse_clones_normalizes_paths(tmp_path):
    data = {"duplicates": [_clone(), _clone(first_name="", second_name="src/c.py")]}
    rows = parse_clones(data, tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["first_file"] == "src/a.py"
    assert r["second_file"] == "src/b.py"
    assert r["first_line"] == 9
    assert r["first_end"] == 30
    assert r["second_line"] == 9
    assert r["lines"] == 22
    assert r["tokens"] == 210
    assert r["format"] == "python"


def test_norm_rel_strips_repo_prefix(tmp_path):
    assert _norm_rel("src\\a.py", tmp_path) == "src/a.py"
    absolute = str(tmp_path / "src" / "a.py")
    assert _norm_rel(absolute, tmp_path) == "src/a.py"
    assert _norm_rel("", tmp_path) == ""


def test_ingest_filter():
    good = {"first_file": "src/a.py", "second_file": "src/b.py"}
    assert _ingest_filter(good)
    assert not _ingest_filter({"first_file": ".vrp/dossier.json", "second_file": "src/b.py"})
    assert not _ingest_filter({"first_file": "src/a.py", "second_file": ".code-atlas/index.db"})
    assert not _ingest_filter({"first_file": "node_modules/x.js", "second_file": "src/b.py"})
    assert not _ingest_filter({"first_file": ".codebase-memory/x", "second_file": "src/b.py"})


def test_cross_check_three_sides():
    clones = [
        {"first_file": "src/a.py", "first_line": 9, "second_file": "src/b.py", "second_line": 9, "lines": 22},
        {"first_file": "src/c.py", "first_line": 1, "second_file": "src/d.py", "second_line": 1, "lines": 6},
    ]
    cbm = {frozenset(("src/a.py", "src/b.py")), frozenset(("src/e.py", "src/f.py"))}
    ledger = cross_check(clones, cbm)
    agreed = [r for r in ledger if r[1] == "jscpd:src/a.py:9<->src/b.py:9"]
    assert len(agreed) == 1
    assert agreed[0][0] == LEDGER_CATEGORY
    assert agreed[0][2] == "cbm-p2:src/a.py|src/b.py"
    assert json.loads(agreed[0][3])["agreed"] is True
    jscpd_only = [r for r in ledger if r[1] == "jscpd:src/c.py:1<->src/d.py:1"]
    assert len(jscpd_only) == 1
    assert jscpd_only[0][2] is None
    assert json.loads(jscpd_only[0][3])["side"] == "jscpd-only"
    cbm_only = [r for r in ledger if r[1] == "cbm-p2:src/e.py|src/f.py"]
    assert len(cbm_only) == 1
    assert cbm_only[0][2] == "jscpd:none"
    assert json.loads(cbm_only[0][3])["side"] == "cbm-only"
    assert len(ledger) == 3


def test_cbm_p2_file_pairs(facts_db):
    facts_db.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1','p','m.a','Function','a','src/a.py',1,3,'h'),"
        " ('f2','p','m.b','Function','b','src/b.py',1,3,'h'),"
        " ('f3','p','m.c','Function','c','src/c.py',1,3,'h')"
    )
    facts_db.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid, file_path, start_line, end_line, payload_json)"
        " VALUES ('P2','similar_to','etl','f1','f2','src/a.py',1,3,'{}'),"
        " ('P2','token-clone','s2-jscpd','f1','f3','src/a.py',1,3,'{}')"
    )
    assert cbm_p2_file_pairs(facts_db) == {frozenset(("src/a.py", "src/b.py"))}


def test_scan_skips_when_no_bin(facts_db, tmp_path, monkeypatch):
    monkeypatch.setattr("vrp.s2_jscpd.find_jscpd", lambda: None)
    assert scan(facts_db, tmp_path) == {"jscpd": "skipped", "ledger_rows": 0}


def test_scan_reports_failed_when_run_fails(facts_db, tmp_path, monkeypatch):
    """R-E E1：bin 在但运行失败（rc/报告缺失/JSON 损坏）——stats 报 "failed" 不伪装 "skipped"。"""
    monkeypatch.setattr("vrp.s2_jscpd.find_jscpd", lambda: "fake-jscpd")
    monkeypatch.setattr("vrp.s2_jscpd.run_jscpd", lambda bin_path, repo: None)
    stats = scan(facts_db, tmp_path)
    assert stats["jscpd"] == "failed"
    assert stats["ledger_rows"] == 0


def test_run_jscpd_missing_report_returns_none(tmp_path, monkeypatch):
    """R-E E2：rc=0 但报告文件缺失——None（失败信号）不裸崩。"""
    from types import SimpleNamespace
    from unittest import mock

    from vrp.s2_jscpd import run_jscpd

    fake = SimpleNamespace(returncode=0, stdout="", stderr="")
    with mock.patch("vrp.s2_jscpd.subprocess.run", return_value=fake):
        assert run_jscpd("fake-bin", tmp_path) is None


def test_scan_writes_findings_and_ledger(facts_db, tmp_path, monkeypatch):
    clones = [
        {"first_file": "src/a.py", "first_line": 9, "first_end": 30, "second_file": "src/b.py",
         "second_line": 9, "second_end": 30, "lines": 22, "tokens": 210, "format": "python"},
        {"first_file": ".vrp/dossier.json", "first_line": 1, "first_end": 2, "second_file": "src/b.py",
         "second_line": 1, "second_end": 2, "lines": 2, "tokens": 50, "format": "json"},
    ]
    monkeypatch.setattr("vrp.s2_jscpd.find_jscpd", lambda: "fake-jscpd")
    monkeypatch.setattr("vrp.s2_jscpd.run_jscpd", lambda bin_path, repo: clones)
    facts_db.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1','p','m.a','Function','a','src/a.py',5,40,'h'),"
        " ('f2','p','m.b','Function','b','src/b.py',5,40,'h')"
    )
    stats = scan(facts_db, tmp_path)
    assert stats["jscpd"] == 1
    rows = facts_db.execute(
        "SELECT pathology, kind, source, anchor_fid, related_fid, file_path, start_line FROM findings"
        f" WHERE source='{SOURCE}'"
    ).fetchall()
    assert rows == [("P2", "token-clone", "s2-jscpd", "f1", "f2", "src/a.py", 9)]
    ledger = facts_db.execute(
        f"SELECT src_key, dst_key FROM discrepancy_ledger WHERE category='{LEDGER_CATEGORY}'"
    ).fetchall()
    assert ledger == [("jscpd:src/a.py:9<->src/b.py:9", None)]
    rerun = scan(facts_db, tmp_path)
    assert rerun["jscpd"] == 1
    count = facts_db.execute(f"SELECT COUNT(*) FROM findings WHERE source='{SOURCE}'").fetchone()[0]
    assert count == 1
