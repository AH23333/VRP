import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from vrp import s6_transact

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".vrp/\n", encoding="utf-8")
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    return repo


def _mk_facts(repo: Path, symbols, edges=()) -> Path:
    db = repo / ".vrp" / "facts.db"
    db.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    for fid, qn, name, path in symbols:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
            " VALUES (?,?,?,?,?,?,?)",
            (fid, "p", qn, "Function", name, path, "h-" + fid),
        )
    for src, dst in edges:
        con.execute(
            "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,'CALLS','E2')",
            (src, dst),
        )
    con.commit()
    con.close()
    return db


def _write_tx(tmp_path: Path, tx: dict) -> Path:
    path = tmp_path / "tx.json"
    path.write_text(json.dumps(tx, ensure_ascii=False), encoding="utf-8")
    return path


def _patch_pipeline(monkeypatch, post_symbols, facts_symbols_after=None, post_edges=()):
    calls = {"index": 0, "etl": []}

    def fake_index(repo, name, reindex=False, timeout=3600):
        calls["index"] += 1
        return {"status": "indexed"}

    def fake_etl(repo, cbm_name, out_db=None):
        out = Path(out_db)
        calls["etl"].append(out.name)
        con = sqlite3.connect(out)
        if out.name.startswith("facts.db.post-"):
            con.executescript(SCHEMA.read_text(encoding="utf-8"))
        else:
            con.execute("DELETE FROM symbols")
        for fid, qn in post_symbols if out.name.startswith("facts.db.post-") else (facts_symbols_after or post_symbols):
            con.execute(
                "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
                " VALUES (?,?,?,?,?,?,?)",
                (fid, "p", qn, "Function", qn.split(".")[-1], "a.py", "h-" + fid),
            )
        if out.name.startswith("facts.db.post-"):
            for src, dst in post_edges:
                con.execute(
                    "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,'CALLS','E2')",
                    (src, dst),
                )
        con.commit()
        con.close()
        return {}

    def fake_call_code_atlas(repo, tool, arguments, timeout=120):
        return {"status": "skipped", "reason": "mocked in tests"}

    monkeypatch.setattr(s6_transact.s1_cbm, "index_repository", fake_index)
    monkeypatch.setattr(s6_transact.etl, "run_etl", fake_etl)
    monkeypatch.setattr(s6_transact.s0_precheck, "discover_commands", lambda repo: {})
    monkeypatch.setattr(s6_transact, "call_code_atlas", fake_call_code_atlas)
    return calls


def test_load_transaction_valid(tmp_path):
    path = _write_tx(
        tmp_path,
        {
            "id": "tx-0001",
            "type": "T2",
            "summary": "cleanup",
            "declared_moves": [{"from": "p.a.old", "to": "p.a.new"}],
            "edits": [{"file": "pkg/a.py", "op": "modify", "content": "x = 1\n"}],
        },
    )
    tx = s6_transact.load_transaction(path)
    assert tx["type"] == "T2"
    assert tx["edits"][0]["file"] == Path("pkg/a.py")
    assert tx["declared_moves"][0]["from"] == "p.a.old"


@pytest.mark.parametrize(
    "tx",
    [
        {"id": "", "type": "T1", "edits": [{"file": "a", "op": "delete"}]},
        {"id": "t", "type": "T9", "edits": [{"file": "a", "op": "delete"}]},
        {"id": "t", "type": "T1", "edits": []},
        {"id": "t", "type": "T1", "edits": [{"file": "C:/abs.py", "op": "delete"}]},
        {"id": "t", "type": "T1", "edits": [{"file": "/posix-abs.py", "op": "delete"}]},
        {"id": "t", "type": "T1", "edits": [{"file": "../up.py", "op": "delete"}]},
        {"id": "t", "type": "T1", "edits": [{"file": "a.py", "op": "create"}]},
        {"id": "t", "type": "T1", "edits": [{"file": "a.py", "op": "destroy", "content": "x"}]},
        {"id": "t", "type": "T1", "edits": [{"file": "a.py", "op": "delete"}], "declared_moves": ["bad"]},
    ],
)
def test_load_transaction_rejects(tmp_path, tx):
    path = _write_tx(tmp_path, tx)
    with pytest.raises(ValueError):
        s6_transact.load_transaction(path)


def test_apply_and_rollback_edits(tmp_path):
    repo = _mk_repo(tmp_path)
    before = (repo / "a.py").read_text(encoding="utf-8")
    edits = [
        {"file": Path("a.py"), "op": "modify", "content": "changed = True\n"},
        {"file": Path("new_dir/b.py"), "op": "create", "content": "x = 1\n"},
    ]
    s6_transact.apply_edits(repo, edits)
    assert (repo / "a.py").read_text(encoding="utf-8") != before
    assert (repo / "new_dir/b.py").is_file()
    s6_transact.rollback_edits(repo, edits)
    assert (repo / "a.py").read_text(encoding="utf-8") == before
    assert not (repo / "new_dir/b.py").exists()
    assert s6_transact.git_clean(repo)


def test_transact_t1_committed(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(repo, [("f1", "p.a.old", "old", "a.py")])
    calls = _patch_pipeline(monkeypatch, [("f2", "p.a.new")])
    tx = _write_tx(
        tmp_path,
        {
            "id": "tx-1",
            "type": "T1",
            "summary": "rename old to new",
            "declared_moves": [{"from": "p.a.old", "to": "p.a.new"}],
            "edits": [{"file": "a.py", "op": "modify", "content": "def new():\n    return 1\n"}],
        },
    )
    verdict = s6_transact.transact(repo, tx, "proj")
    assert verdict["status"] == "committed"
    assert verdict["failed_gate"] is None
    assert verdict["confidence"] == "degraded"
    assert [g["gate"] for g in verdict["gates"]] == ["G1", "G2", "G3", "G4"]
    assert calls["index"] == 1
    assert "facts.db.post-tx-1" in calls["etl"]
    assert s6_transact.git_clean(repo)
    head = s6_transact.git_head(repo)
    assert head
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=repo, capture_output=True, text=True)
    assert log.stdout.strip() == "vrp tx-1: rename old to new [T1]"
    verdict_file = repo / ".vrp" / "verdicts" / "tx-1.json"
    assert json.loads(verdict_file.read_text(encoding="utf-8"))["status"] == "committed"
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    assert con.execute("SELECT status FROM transactions WHERE tid = 'tx-1'").fetchone()[0] == "committed"
    assert con.execute("SELECT COUNT(*) FROM gate_results").fetchone()[0] == 4
    assert con.execute("SELECT qualified_name FROM symbols").fetchall() == [("p.a.new",)]
    con.close()


def test_transact_t1_undeclared_rolled_back(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(repo, [("f1", "p.a.old", "old", "a.py")])
    before = (repo / "a.py").read_text(encoding="utf-8")
    _patch_pipeline(monkeypatch, [("f2", "p.a.new"), ("f3", "p.a.sneaky")])
    tx = _write_tx(
        tmp_path,
        {
            "id": "tx-2",
            "type": "T1",
            "summary": "rename with extra",
            "declared_moves": [{"from": "p.a.old", "to": "p.a.new"}],
            "edits": [{"file": "a.py", "op": "modify", "content": "def new():\n    return 1\n"}],
        },
    )
    verdict = s6_transact.transact(repo, tx, "proj")
    assert verdict["status"] == "rolled_back"
    assert verdict["failed_gate"] == "G4"
    assert (repo / "a.py").read_text(encoding="utf-8") == before
    assert s6_transact.git_clean(repo)
    assert not (repo / ".vrp" / "facts.db.post-tx-2").exists()
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    assert con.execute("SELECT status FROM transactions WHERE tid = 'tx-2'").fetchone()[0] == "rolled_back"
    assert con.execute("SELECT qualified_name FROM symbols").fetchall() == [("p.a.old",)]
    con.close()


def test_transact_t2_dangling_rolled_back(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(
        repo,
        [("f1", "p.a.helper", "helper", "a.py"), ("f2", "p.a.user", "user", "a.py")],
        edges=[("f2", "f1")],
    )
    _patch_pipeline(monkeypatch, [("f2", "p.a.user")])
    tx = _write_tx(
        tmp_path,
        {
            "id": "tx-3",
            "type": "T2",
            "summary": "remove used helper",
            "edits": [{"file": "a.py", "op": "modify", "content": "def user():\n    return 2\n"}],
        },
    )
    verdict = s6_transact.transact(repo, tx, "proj")
    assert verdict["status"] == "rolled_back"
    assert verdict["failed_gate"] == "G4"
    g4 = next(g for g in verdict["gates"] if g["gate"] == "G4")
    assert g4["detail"]["reason"] == "dangling-reference"
    assert g4["detail"]["code"] == "gate/G4/dangling-reference"
    assert s6_transact.git_clean(repo)


def test_transact_t2_dead_removal_committed(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(
        repo,
        [("f1", "p.a.helper", "helper", "a.py"), ("f2", "p.a.user", "user", "a.py")],
        edges=[("f1", "f2")],
    )
    _patch_pipeline(monkeypatch, [("f2", "p.a.user")])
    tx = _write_tx(
        tmp_path,
        {
            "id": "tx-4",
            "type": "T2",
            "summary": "remove dead helper",
            "edits": [{"file": "a.py", "op": "modify", "content": "def user():\n    return 2\n"}],
        },
    )
    verdict = s6_transact.transact(repo, tx, "proj")
    assert verdict["status"] == "committed"
    g5 = next(g for g in verdict["gates"] if g["gate"] == "G5")
    assert g5["status"] == "pass"
    assert g5["detail"]["post"] <= g5["detail"]["pre"]


def test_transact_t2_deadcode_increase_rolled_back(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(repo, [("f1", "p.a.user", "user", "a.py")])
    _patch_pipeline(monkeypatch, [("f1", "p.a.user"), ("f3", "p.a.orphan")])
    tx = _write_tx(
        tmp_path,
        {
            "id": "tx-5",
            "type": "T2",
            "summary": "adds dead code",
            "edits": [{"file": "a.py", "op": "modify", "content": "def user():\n    return 1\n\ndef orphan():\n    return 2\n"}],
        },
    )
    verdict = s6_transact.transact(repo, tx, "proj")
    assert verdict["status"] == "rolled_back"
    assert verdict["failed_gate"] == "G5"
    assert s6_transact.git_clean(repo)


def test_transact_t4_needs_confirmation(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(repo, [("f1", "p.a.old", "old", "a.py")])
    _patch_pipeline(monkeypatch, [("f2", "p.a.new")])
    tx = _write_tx(
        tmp_path,
        {
            "id": "tx-6",
            "type": "T4",
            "summary": "change behavior",
            "edits": [{"file": "a.py", "op": "modify", "content": "def old():\n    return 2\n"}],
        },
    )
    verdict = s6_transact.transact(repo, tx, "proj")
    assert verdict["status"] == "needs_confirmation"
    assert (repo / "a.py").read_text(encoding="utf-8") == "def old():\n    return 1\n"
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    assert con.execute("SELECT status FROM transactions WHERE tid = 'tx-6'").fetchone()[0] == "needs_confirmation"
    con.close()


def test_transact_dirty_tree_refused_by_cli(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "a.py").write_text("dirty = True\n", encoding="utf-8")
    assert not s6_transact.git_clean(repo)


def test_transact_t1_edge_mismatch_rolled_back(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(
        repo,
        [("f1", "p.a.old", "old", "a.py"), ("f2", "p.a.caller", "caller", "b.py")],
        edges=[("f2", "f1")],
    )
    _patch_pipeline(monkeypatch, [("f2", "p.a.caller"), ("f3", "p.a.new")], post_edges=[])
    tx = _write_tx(
        tmp_path,
        {
            "id": "tx-edge",
            "type": "T1",
            "summary": "rename forgot caller",
            "declared_moves": [{"from": "p.a.old", "to": "p.a.new"}],
            "edits": [{"file": "a.py", "op": "modify", "content": "def new():\n    return 1\n"}],
        },
    )
    verdict = s6_transact.transact(repo, tx, "proj")
    assert verdict["status"] == "rolled_back"
    assert verdict["failed_gate"] == "G4"
    g4 = next(g for g in verdict["gates"] if g["gate"] == "G4")
    assert g4["detail"]["reason"] == "edge-mismatch"
    assert g4["detail"]["code"] == "gate/G4/edge-mismatch"
    assert g4["detail"]["missing"] == ["p.a.caller -CALLS-> p.a.new"]
    assert s6_transact.git_clean(repo)


def test_transact_t1_edge_carried_committed(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(
        repo,
        [("f1", "p.a.old", "old", "a.py"), ("f2", "p.a.caller", "caller", "b.py")],
        edges=[("f2", "f1")],
    )
    _patch_pipeline(
        monkeypatch,
        [("f2", "p.a.caller"), ("f3", "p.a.new")],
        post_edges=[("f2", "f3")],
    )
    tx = _write_tx(
        tmp_path,
        {
            "id": "tx-edge2",
            "type": "T1",
            "summary": "rename with caller updated",
            "declared_moves": [{"from": "p.a.old", "to": "p.a.new"}],
            "edits": [{"file": "a.py", "op": "modify", "content": "def new():\n    return 1\n"}],
        },
    )
    verdict = s6_transact.transact(repo, tx, "proj")
    assert verdict["status"] == "committed"
    g4 = next(g for g in verdict["gates"] if g["gate"] == "G4")
    assert g4["detail"]["verify_changes"]["status"] == "skipped"


def test_transact_t2_verify_broken_rolled_back(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(
        repo,
        [("f1", "p.a.helper", "helper", "a.py"), ("f2", "p.a.user", "user", "a.py")],
        edges=[("f1", "f2")],
    )
    _patch_pipeline(monkeypatch, [("f2", "p.a.user")])
    monkeypatch.setattr(
        s6_transact,
        "call_code_atlas",
        lambda repo, tool, arguments, timeout=120: {
            "status": "ok",
            "text": (
                "verified 1 changed file(s) against HEAD: 1 broken, 0 to check, 0 informational\n\n"
                "BROKEN a.py: exported function helper was removed and is still referenced — b.py:3 (call)"
            ),
            "chars": 140,
        },
    )
    tx = _write_tx(
        tmp_path,
        {
            "id": "tx-vb",
            "type": "T2",
            "summary": "remove helper",
            "edits": [{"file": "a.py", "op": "modify", "content": "def user():\n    return 2\n"}],
        },
    )
    verdict = s6_transact.transact(repo, tx, "proj")
    assert verdict["status"] == "rolled_back"
    assert verdict["failed_gate"] == "G4"
    g4 = next(g for g in verdict["gates"] if g["gate"] == "G4")
    assert g4["detail"]["reason"] == "verify-broken"
    assert g4["detail"]["code"] == "gate/G4/verify-broken"
    assert any("helper" in b for b in g4["detail"]["broken"])
    assert s6_transact.git_clean(repo)


def test_transact_t2_verify_ok_committed(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(
        repo,
        [("f1", "p.a.helper", "helper", "a.py"), ("f2", "p.a.user", "user", "a.py")],
        edges=[("f1", "f2")],
    )
    _patch_pipeline(monkeypatch, [("f2", "p.a.user")])
    monkeypatch.setattr(
        s6_transact,
        "call_code_atlas",
        lambda repo, tool, arguments, timeout=120: {
            "status": "ok",
            "text": "verified 1 changed file(s) against HEAD: 0 broken, 0 to check, 1 informational\n\nINFO a.py: exported function helper removed — no remaining references in the index",
            "chars": 130,
        },
    )
    tx = _write_tx(
        tmp_path,
        {
            "id": "tx-vo",
            "type": "T2",
            "summary": "remove dead helper",
            "edits": [{"file": "a.py", "op": "modify", "content": "def user():\n    return 2\n"}],
        },
    )
    verdict = s6_transact.transact(repo, tx, "proj")
    assert verdict["status"] == "committed"
    g4 = next(g for g in verdict["gates"] if g["gate"] == "G4")
    assert g4["detail"]["verify_changes"] == {"broken": 0, "checks": 0}


def test_transact_batch_all_committed(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(repo, [("f1", "p.a.old", "old", "a.py")])
    _patch_pipeline(monkeypatch, [("f2", "p.a.new")])
    tx_dir = tmp_path / "txs"
    tx_dir.mkdir()
    (tx_dir / "01-rename.json").write_text(
        json.dumps(
            {
                "id": "tx-b1",
                "type": "T1",
                "summary": "rename",
                "declared_moves": [{"from": "p.a.old", "to": "p.a.new"}],
                "edits": [{"file": "a.py", "op": "modify", "content": "def new():\n    return 1\n"}],
            }
        ),
        encoding="utf-8",
    )
    (tx_dir / "02-touch.json").write_text(
        json.dumps(
            {
                "id": "tx-b2",
                "type": "T3",
                "summary": "touch",
                "edits": [{"file": "a.py", "op": "modify", "content": "def new():\n    return 2\n"}],
            }
        ),
        encoding="utf-8",
    )
    result = s6_transact.transact_batch(repo, tx_dir, "proj")
    assert result["total"] == 2
    assert result["count"] == 2
    assert result["committed"] == 2
    assert result["statuses"] == ["committed", "committed"]
    assert result["stopped_at"] is None
    assert s6_transact.git_clean(repo)
    log = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True)
    assert "vrp tx-b2: touch [T3]" in log.stdout
    assert "vrp tx-b1: rename [T1]" in log.stdout


def test_transact_batch_stops_on_failure(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(repo, [("f1", "p.a.old", "old", "a.py")])
    _patch_pipeline(monkeypatch, [("f2", "p.a.new"), ("f3", "p.a.sneaky")])
    tx_dir = tmp_path / "txs"
    tx_dir.mkdir()
    (tx_dir / "01-bad.json").write_text(
        json.dumps(
            {
                "id": "tx-c1",
                "type": "T1",
                "summary": "undeclared extra",
                "declared_moves": [{"from": "p.a.old", "to": "p.a.new"}],
                "edits": [{"file": "a.py", "op": "modify", "content": "def new():\n    return 1\n"}],
            }
        ),
        encoding="utf-8",
    )
    (tx_dir / "02-good.json").write_text(
        json.dumps(
            {
                "id": "tx-c2",
                "type": "T3",
                "summary": "touch",
                "edits": [{"file": "a.py", "op": "modify", "content": "def new():\n    return 2\n"}],
            }
        ),
        encoding="utf-8",
    )
    result = s6_transact.transact_batch(repo, tx_dir, "proj")
    assert result["count"] == 1
    assert result["statuses"] == ["rolled_back"]
    assert result["stopped_at"] == "01-bad.json"
    assert s6_transact.git_clean(repo)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    assert con.execute("SELECT COUNT(*) FROM transactions WHERE tid = 'tx-c2'").fetchone()[0] == 0
    con.close()


def test_transact_batch_continue_on_fail(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _mk_facts(repo, [("f1", "p.a.old", "old", "a.py")])
    _patch_pipeline(monkeypatch, [("f2", "p.a.new"), ("f3", "p.a.sneaky")])
    tx_dir = tmp_path / "txs"
    tx_dir.mkdir()
    (tx_dir / "01-bad.json").write_text(
        json.dumps(
            {
                "id": "tx-d1",
                "type": "T1",
                "summary": "undeclared extra",
                "declared_moves": [{"from": "p.a.old", "to": "p.a.new"}],
                "edits": [{"file": "a.py", "op": "modify", "content": "def new():\n    return 1\n"}],
            }
        ),
        encoding="utf-8",
    )
    (tx_dir / "02-good.json").write_text(
        json.dumps(
            {
                "id": "tx-d2",
                "type": "T3",
                "summary": "touch",
                "edits": [{"file": "a.py", "op": "modify", "content": "def new():\n    return 2\n"}],
            }
        ),
        encoding="utf-8",
    )
    result = s6_transact.transact_batch(repo, tx_dir, "proj", continue_on_fail=True)
    assert result["count"] == 2
    assert result["statuses"] == ["rolled_back", "committed"]
    assert result["stopped_at"] is None
    assert result["committed"] == 1
    assert s6_transact.git_clean(repo)
