import json
from pathlib import Path

from vrp import cli, s6_transact


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "a.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    (repo / "sub" / "b.py").write_text("x = 1\n", encoding="utf-8")
    return repo


def _write_verdict(repo: Path, tid: str, status: str, verdict_at: str, **extra) -> Path:
    vdir = repo / ".vrp" / "verdicts"
    vdir.mkdir(parents=True, exist_ok=True)
    verdict = {"tid": tid, "type": "T2", "status": status, "verdict_at": verdict_at, "gates": [], **extra}
    path = vdir / f"{tid}.json"
    path.write_text(json.dumps(verdict, ensure_ascii=False), encoding="utf-8")
    return path


def _write_tx(tmp_path: Path, tx: dict) -> Path:
    path = tmp_path / "tx.json"
    path.write_text(json.dumps(tx, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_verdicts_sorted_and_filtered_states(tmp_path):
    repo = _mk_repo(tmp_path)
    _write_verdict(repo, "tx-b", "rolled_back", "2026-11-04T01:00:00Z", failed_gate="G3")
    _write_verdict(repo, "tx-a", "committed", "2026-11-04T00:00:00Z", commit="c0ffee")
    _write_verdict(repo, "tx-c", "needs_confirmation", "2026-11-04T02:00:00Z")
    verdicts = s6_transact.load_verdicts(repo)
    assert [v["tid"] for v in verdicts] == ["tx-a", "tx-b", "tx-c"]
    assert verdicts[0]["status"] == "committed"
    assert verdicts[1]["failed_gate"] == "G3"


def test_load_verdicts_empty_and_unreadable(tmp_path):
    repo = _mk_repo(tmp_path)
    assert s6_transact.load_verdicts(repo) == []
    vdir = repo / ".vrp" / "verdicts"
    vdir.mkdir(parents=True)
    (vdir / "tx-bad.json").write_text("{not json", encoding="utf-8")
    verdicts = s6_transact.load_verdicts(repo)
    assert len(verdicts) == 1
    assert verdicts[0]["status"] == "unreadable"
    assert verdicts[0]["tid"] == "tx-bad"


def test_preview_three_ops_and_missing_target(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s6_transact.s6_difftastic, "find_difftastic", lambda: None)
    tx = {
        "id": "tx-preview",
        "type": "T2",
        "summary": "preview probe",
        "edits": [
            {"file": "new.py", "op": "create", "content": "def n():\n    return 2\n"},
            {"file": "a.py", "op": "modify", "content": "def old():\n    return 2\n"},
            {"file": "missing.py", "op": "delete", "content": None},
            {"file": "sub/b.py", "op": "delete", "content": None},
        ],
    }
    preview = s6_transact.preview_transaction(repo, _write_tx(tmp_path, tx))
    assert preview["tid"] == "tx-preview"
    assert preview["structural_engine"] == "difflib-only"
    assert preview["ok"] is False
    by_file = {e["file"]: e for e in preview["edits"]}
    create = by_file["new.py"]
    assert create["ok"] is True
    assert create["diff"].startswith("--- /dev/null")
    assert "+++ b/new.py" in create["diff"]
    assert "+def n():" in create["diff"]
    modify = by_file["a.py"]
    assert modify["ok"] is True
    assert modify["structural"] == "unavailable"
    assert modify["diff"].startswith("--- a/a.py")
    assert "-    return 1" in modify["diff"]
    assert "+    return 2" in modify["diff"]
    missing = by_file["missing.py"]
    assert missing["ok"] is False
    assert missing["error"] == "delete target missing"
    deleted = by_file["sub/b.py"]
    assert deleted["ok"] is True
    assert deleted["diff"].startswith("--- a/sub/b.py\n+++ /dev/null\n")
    assert "-x = 1" in deleted["diff"]


def test_preview_structural_signal_with_difftastic(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    fake_binary = tmp_path / "difft.exe"
    fake_binary.write_text("", encoding="utf-8")

    def fake_find():
        return str(fake_binary)

    def fake_run(binary, old_path, new_path, timeout=60):
        assert Path(old_path) == repo / "a.py"
        return {"ok": True, "parsed": {"status": "unchanged", "chunks": []}}

    monkeypatch.setattr(s6_transact.s6_difftastic, "find_difftastic", fake_find)
    monkeypatch.setattr(s6_transact.s6_difftastic, "run_difftastic", fake_run)
    tx = {
        "id": "tx-struct",
        "type": "T1",
        "summary": "structural probe",
        "edits": [{"file": "a.py", "op": "modify", "content": "def old():\n    return 1  \n"}],
    }
    preview = s6_transact.preview_transaction(repo, _write_tx(tmp_path, tx))
    assert preview["structural_engine"] == "difftastic"
    modify = preview["edits"][0]
    assert modify["ok"] is True
    assert modify["structural"] == "unchanged"


def test_cli_verdicts_list_and_tid(tmp_path, capsys, monkeypatch):
    repo = _mk_repo(tmp_path)
    _write_verdict(repo, "tx-1", "committed", "2026-11-04T00:00:00Z", commit="c1")
    _write_verdict(
        repo,
        "tx-2",
        "rolled_back",
        "2026-11-04T01:00:00Z",
        failed_gate="G3",
        gates=[{"gate": "G3", "status": "fail", "detail": {"reason": "test", "code": "gate/G3/test"}}],
    )
    args_touched = cli.build_parser().parse_args(["verdicts", str(repo)])
    assert args_touched.func is cli.cmd_verdicts
    assert cli.main(["verdicts", str(repo)]) == 0
    out = json.loads(capsys.readouterr().out.split("== S6 verdicts ==")[1])
    assert out["count"] == 2
    assert out["verdicts"][1]["tid"] == "tx-2"
    assert out["verdicts"][1]["failed_gate"] == "G3"
    assert cli.main(["verdicts", str(repo), "--tid", "tx-2"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert full[0]["gates"][0]["detail"]["code"] == "gate/G3/test"


def test_cli_transact_preview_no_gates_no_mutation(tmp_path, capsys, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s6_transact.s6_difftastic, "find_difftastic", lambda: None)
    tx = {
        "id": "tx-p",
        "type": "T2",
        "summary": "cli preview",
        "edits": [{"file": "a.py", "op": "modify", "content": "def old():\n    return 9\n"}],
    }
    assert cli.main(["transact", str(repo), str(_write_tx(tmp_path, tx)), "--preview"]) == 0
    payload = json.loads(capsys.readouterr().out.split("== S6 transact preview T2 ==")[1])
    assert payload["ok"] is True
    assert "-    return 1" in payload["edits"][0]["diff"]
    assert (repo / "a.py").read_text(encoding="utf-8") == "def old():\n    return 1\n"
    assert not (repo / ".vrp" / "verdicts").exists()
