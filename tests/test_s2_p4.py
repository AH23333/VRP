import json

import pytest

from vrp.s2_p4 import (
    _ingest_filter,
    cross_check,
    knip_rows,
    ledger_category,
    parse_vulture_line,
    scan,
)


def test_knip_rows_normalization():
    issues = [
        {
            "file": "src/a.ts",
            "exports": [{"name": "foo", "line": 10, "col": 1, "pos": 100}],
            "types": [{"name": "Foo", "line": 3, "col": 1, "pos": 20}],
            "files": [],
        },
        {
            "file": "package.json",
            "devDependencies": [{"name": "vitest"}],
            "files": [{"name": "scripts\\old.mjs"}],
        },
    ]
    rows = knip_rows(issues)
    assert rows == [
        {"kind": "unused-export", "file": "src/a.ts", "name": "foo", "line": 10},
        {"kind": "unused-type", "file": "src/a.ts", "name": "Foo", "line": 3},
        {"kind": "unused-dependency", "file": "package.json", "name": "vitest", "line": None},
        {"kind": "unused-file", "file": "scripts/old.mjs", "name": "", "line": None},
    ]


def test_parse_vulture_line():
    row = parse_vulture_line("pkg/mod.py:42: unused method 'helper' (60% confidence)")
    assert row == {"kind": "unused-method", "file": "pkg/mod.py", "line": 42, "name": "helper"}
    assert parse_vulture_line("garbage line") is None
    assert parse_vulture_line("") is None


def test_ingest_filter():
    good = {"kind": "unused-method", "file": "pkg/mod.py", "name": "x"}
    assert _ingest_filter(good)
    assert not _ingest_filter({"kind": "unused-method", "file": "", "name": "x"})
    assert not _ingest_filter({"kind": "unused-method", "file": "<synthetic>", "name": "x"})
    assert not _ingest_filter({"kind": "unused-import", "file": "tests/test_x.py", "name": "x"})
    assert _ingest_filter({"kind": "unused-method", "file": "tests/test_x.py", "name": "x"})


def test_cross_check_three_sides():
    candidates = [
        ("f1", "m.agreed", "agreed", "src/m.py", 1, 3, "Function"),
        ("f2", "m.missed", "missed", "src/m.py", 5, 7, "Function"),
    ]
    tool_rows = [
        {"kind": "unused-method", "file": "src/m.py", "line": 2, "name": "agreed"},
        {"kind": "unused-method", "file": "src/m.py", "line": 20, "name": "toolonly"},
    ]
    ledger = cross_check(candidates, tool_rows, "knip")
    assert len(ledger) == 3
    agreed = [r for r in ledger if r[3] == json.dumps({"tool": "knip", "agreed": True})]
    assert len(agreed) == 1
    assert agreed[0][0] == ledger_category("knip")
    assert agreed[0][1] == "knip:agreed@src/m.py"
    assert agreed[0][2] == "deadcode:m.agreed"
    tool_only = [r for r in ledger if r[2] is None]
    assert len(tool_only) == 1
    assert tool_only[0][1] == "knip:toolonly@src/m.py"
    cand_only = [r for r in ledger if r[1] == "deadcode:m.missed"]
    assert len(cand_only) == 1
    assert cand_only[0][2] == "knip:none"


def test_scan_skips_when_no_tools(facts_db, tmp_path, monkeypatch):
    monkeypatch.setattr("vrp.s2_p4.find_knip", lambda: None)
    monkeypatch.setattr("vrp.s2_p4.find_vulture", lambda: None)
    stats = scan(facts_db, tmp_path)
    assert stats == {"knip": "skipped", "vulture": "skipped", "ledger_rows": 0}


def test_scan_knip_failure_reports_failed(facts_db, tmp_path, monkeypatch):
    """R-E E1：knip bin+package.json 在但运行失败——"failed" 不伪装 "skipped"。"""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("vrp.s2_p4.find_knip", lambda: "fake-knip")
    monkeypatch.setattr("vrp.s2_p4.find_vulture", lambda: None)
    monkeypatch.setattr("vrp.s2_p4.run_knip", lambda bin_path, repo: None)
    stats = scan(facts_db, tmp_path)
    assert stats["knip"] == "failed"


def test_scan_vulture_no_py_stays_skipped(facts_db, tmp_path, monkeypatch):
    """R-E E1：vulture bin 在但仓无 .py——"skipped"（不适用非失败）。"""
    monkeypatch.setattr("vrp.s2_p4.find_knip", lambda: None)
    monkeypatch.setattr("vrp.s2_p4.find_vulture", lambda: "fake-vulture")
    monkeypatch.setattr("vrp.s2_p4.run_vulture", lambda bin_path, repo: None)
    stats = scan(facts_db, tmp_path)
    assert stats["vulture"] == "skipped"


def test_scan_vulture_failure_reports_failed(facts_db, tmp_path, monkeypatch):
    """R-E E1：vulture bin+py 文件在但运行失败——"failed"。"""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr("vrp.s2_p4.find_knip", lambda: None)
    monkeypatch.setattr("vrp.s2_p4.find_vulture", lambda: "fake-vulture")
    monkeypatch.setattr("vrp.s2_p4.run_vulture", lambda bin_path, repo: None)
    stats = scan(facts_db, tmp_path)
    assert stats["vulture"] == "failed"


def test_has_py_detection(tmp_path):
    """R-E：_has_py 三层深度检测——恒真 bug（any-of-generators）修复锁定。"""
    from vrp.s2_p4 import _has_py

    assert not _has_py(tmp_path), "empty repo must report no python"
    (tmp_path / "deep" / "deeper").mkdir(parents=True)
    (tmp_path / "deep" / "deeper" / "x.py").write_text("x = 1\n", encoding="utf-8")
    assert _has_py(tmp_path), "depth-2-dir python file must be detected (glob '*/*/*.py')"
    other = tmp_path / "other"
    other.mkdir()
    (other / "a" / "b" / "c").mkdir(parents=True)
    (other / "a" / "b" / "c" / "y.py").write_text("y = 1\n", encoding="utf-8")
    assert not _has_py(other), "depth-3-dir python file is out of probe depth (documented limit)"


def test_run_knip_corrupt_json_returns_none(tmp_path, monkeypatch):
    """R-E E2：knip stdout 损坏 JSON——None（失败信号）不裸崩。"""
    from types import SimpleNamespace
    from unittest import mock

    from vrp.s2_p4 import run_knip

    fake = SimpleNamespace(returncode=0, stdout="{broken json", stderr="")
    with mock.patch("vrp.s2_p4.subprocess.run", return_value=fake):
        assert run_knip("fake-bin", tmp_path) is None


def test_scan_vulture_writes_findings_and_ledger(facts_db, tmp_path, monkeypatch):
    rows = [
        {"kind": "unused-method", "file": "src/m.py", "line": 2, "name": "agreed"},
        {"kind": "unused-method", "file": "src/m.py", "line": 20, "name": "toolonly"},
    ]
    monkeypatch.setattr("vrp.s2_p4.find_knip", lambda: None)
    monkeypatch.setattr("vrp.s2_p4.find_vulture", lambda: "fake-vulture")
    monkeypatch.setattr("vrp.s2_p4.run_vulture", lambda bin_path, repo: rows)
    facts_db.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1','proj','m.agreed','Function','agreed','src/m.py',1,3,'h')"
    )
    stats = scan(facts_db, tmp_path)
    assert stats["vulture"] == 2
    found = facts_db.execute(
        "SELECT kind, source, anchor_fid, file_path, start_line FROM findings WHERE source='s2-vulture'"
        " ORDER BY start_line"
    ).fetchall()
    assert found == [
        ("unused-method", "s2-vulture", "f1", "src/m.py", 2),
        ("unused-method", "s2-vulture", None, "src/m.py", 20),
    ]
    ledger_count = facts_db.execute(
        "SELECT COUNT(*) FROM discrepancy_ledger WHERE category = ?", (ledger_category("vulture"),)
    ).fetchone()[0]
    assert ledger_count == 2
