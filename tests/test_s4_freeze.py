import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from vrp import s4_freeze

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


@pytest.fixture
def con():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    yield con
    con.close()


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def _py(args: str) -> str:
    return f'"{sys.executable}" {args}'


def test_run_one_ok(repo):
    run = s4_freeze._run_one(repo, _py("-c \"print('ok')\""), timeout=60)
    assert run["rc"] == 0
    assert not run["timed_out"]
    assert "ok" in run["stdout_tail"]
    assert run["duration_s"] >= 0


def test_run_one_failure(repo):
    run = s4_freeze._run_one(repo, _py("-c \"raise SystemExit(3)\""), timeout=60)
    assert run["rc"] == 3
    assert not run["timed_out"]


def test_run_one_timeout(repo):
    run = s4_freeze._run_one(repo, _py("-c \"import time; time.sleep(30)\""), timeout=2)
    assert run["timed_out"]
    assert run["rc"] == -1
    assert "timeout" in run["stderr_tail"]
    assert run["duration_s"] < 15


def test_run_commands_slots(repo):
    commands = {"build": [_py("--version")], "test": [_py("-c \"print(1)\"")], "lint": []}
    results = s4_freeze.run_commands(repo, commands, timeout=60)
    assert set(results.keys()) == {"build", "test"}
    assert results["build"][0]["rc"] == 0
    assert results["test"][0]["rc"] == 0


def test_write_runtime_evidence(con):
    results = {"test": [{"command": "x", "rc": 0, "duration_s": 0.1, "timed_out": False,
                         "stdout_tail": "", "stderr_tail": ""}]}
    written = s4_freeze.write_runtime_evidence(con, results)
    assert written == 1
    row = con.execute("SELECT caller_key, callee_key, payload_json FROM runtime_evidence").fetchone()
    assert row[0] is None
    assert row[1] == "test"
    assert json.loads(row[2])["command"] == "x"


def test_graph_baseline_artifact_missing(repo, monkeypatch):
    monkeypatch.setattr("vrp.s1_cbm.run_cli", lambda args, timeout=3600: {"ok": True})
    with pytest.raises(RuntimeError, match="artifact missing"):
        s4_freeze.graph_baseline(repo, "proj")


def test_graph_baseline_ok(repo, monkeypatch):
    def fake_run_cli(args, timeout=3600):
        artifact = repo / ".codebase-memory" / "graph.db.zst"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"zst-bytes")
        return {"status": "ok"}
    monkeypatch.setattr("vrp.s1_cbm.run_cli", fake_run_cli)
    result = s4_freeze.graph_baseline(repo, "proj")
    assert result["status"] == "written"
    assert result["bytes"] == len(b"zst-bytes")
    assert result["artifact"].endswith("graph.db.zst")


def test_freeze_skips_and_records_baseline(repo, tmp_path, monkeypatch):
    facts_db = tmp_path / "facts.db"
    con = sqlite3.connect(facts_db)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()
    monkeypatch.setattr(
        "vrp.s4_freeze.run_commands",
        lambda r, commands, timeout=900: {"test": [{"command": "pytest", "rc": 0, "duration_s": 0.2,
                                                    "timed_out": False, "stdout_tail": "", "stderr_tail": ""}]},
    )
    summary = s4_freeze.freeze(repo, facts_db, "proj", skip_graph=True)
    assert "frozen_at" in summary
    con = sqlite3.connect(facts_db)
    try:
        rows = con.execute("SELECT kind, payload_json FROM baseline").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "freeze"
        payload = json.loads(rows[0][1])
        assert payload["commands"]["test"][0]["rc"] == 0
        assert payload["graph_baseline"] is None
        assert con.execute("SELECT COUNT(*) FROM runtime_evidence").fetchone()[0] == 1
    finally:
        con.close()
