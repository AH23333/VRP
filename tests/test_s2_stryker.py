import json
import os
import stat

import pytest

from vrp import s2_stryker
from vrp.s2_stryker import (
    CONF_NAME,
    REPORT_NAME,
    TMP_DIR_NAME,
    cleanup,
    find_stryker,
    parse_report,
    run_stryker,
    write_config,
)


def test_find_stryker_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "stryker.cmd"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("VRP_STRYKER_BIN", str(fake))
    assert find_stryker() == str(fake)


def test_find_stryker_shim_then_which(tmp_path, monkeypatch):
    monkeypatch.delenv("VRP_STRYKER_BIN", raising=False)
    shim = tmp_path / "stryker.cmd"
    shim.write_text("", encoding="utf-8")
    monkeypatch.setattr("vrp.s2_stryker._shim", lambda: shim)
    assert find_stryker() == str(shim)
    monkeypatch.setattr("vrp.s2_stryker._shim", lambda: tmp_path / "missing.cmd")
    monkeypatch.setattr("shutil.which", lambda name: "global-stryker")
    assert find_stryker() == "global-stryker"
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert find_stryker() is None


def test_write_config_bom_free_with_json_reporter(tmp_path):
    conf = write_config(tmp_path, ["src/b.ts", "src/a.ts"])
    assert conf == tmp_path / CONF_NAME
    raw = conf.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    data = json.loads(raw.decode("utf-8"))
    assert data["mutate"] == ["src/a.ts", "src/b.ts"]
    assert data["jsonReporter"] == {"fileName": REPORT_NAME}
    assert data["tempDirName"] == TMP_DIR_NAME
    assert data["testRunner"] == "vitest"


def test_run_stryker_passes_positional_config(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_run_tree(cmd, cwd=None, timeout=None):
        seen["cmd"] = list(cmd)
        seen["cwd"] = cwd
        seen["timeout"] = timeout
        return {"returncode": 0, "timed_out": False, "stdout": "", "stderr": ""}

    monkeypatch.setattr("vrp.procs.run_tree", fake_run_tree)
    rr = run_stryker("fake-stryker", tmp_path, ["src/a.ts"], 120)
    assert rr["ok"] is True and rr["returncode"] == 0
    assert seen["cmd"] == ["fake-stryker", "run", CONF_NAME]
    assert seen["cwd"] == tmp_path
    assert seen["timeout"] == 120


def test_run_stryker_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vrp.procs.run_tree",
        lambda cmd, cwd=None, timeout=None: {
            "returncode": -1, "timed_out": True, "stdout": "", "stderr": "",
        },
    )
    rr = run_stryker("fake-stryker", tmp_path, ["src/a.ts"], 120)
    assert rr["ok"] is False
    assert rr["returncode"] is None
    assert "timed out" in rr["stderr_tail"]


def _report(tmp_path, mutants):
    report = tmp_path / REPORT_NAME
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps({"schemaVersion": "1.0", "files": {"src\\mathfun.ts": {
        "language": "typescript", "mutants": mutants}}}), encoding="utf-8")
    return report


def _m(line, status):
    return {"status": status, "location": {"start": {"line": line, "column": 8}},
            "id": 1, "mutatorName": "x", "replacement": "y"}


def test_parse_report_maps_statuses_and_counts(tmp_path):
    report = _report(tmp_path, [
        _m(2, "Killed"), _m(3, "Survived"), _m(4, "NoCoverage"),
        _m(5, "Timeout"), _m(6, "CompileError"), _m(7, "RuntimeError"), _m(8, "WeirdStatus"),
    ])
    records, raw = parse_report(report)
    statuses = [r["status"] for r in records]
    assert statuses == ["killed", "survived", "survived", "timeout", "suspicious", "suspicious", "untested"]
    assert records[0]["file"] == "src/mathfun.ts"
    assert records[0]["line"] == 2
    assert raw == {"Killed": 1, "Survived": 1, "NoCoverage": 1, "Timeout": 1,
                   "CompileError": 1, "RuntimeError": 1, "WeirdStatus": 1}


def test_cleanup_removes_scratch_tolerant(tmp_path):
    conf = tmp_path / CONF_NAME
    report = tmp_path / REPORT_NAME
    tmp = tmp_path / TMP_DIR_NAME / "sandbox-x" / ".git" / "objects"
    tmp.mkdir(parents=True)
    obj = tmp / "abc"
    obj.write_text("", encoding="utf-8")
    os.chmod(obj, stat.S_IREAD)
    conf.parent.mkdir(exist_ok=True)
    conf.write_text("{}", encoding="utf-8")
    report.write_text("{}", encoding="utf-8")
    cleanup(tmp_path)
    assert not conf.exists() and not report.exists()
    # failed Stryker runs leak the full sandbox copy (success cleans it up
    # itself) - cleanup must take the readonly-marked .git objects with it
    assert not (tmp_path / TMP_DIR_NAME).exists()
    cleanup(tmp_path)
def test_parse_report_nondict_report_raises_valueerror(tmp_path):
    """R-AD：stryker 报告合法 JSON 非 dict → 诚实 ValueError（与 JSONDecodeError 同语义）。"""
    report = tmp_path / "stryker-report.json"
    report.write_text(json.dumps(["x"]), encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        parse_report(report)
