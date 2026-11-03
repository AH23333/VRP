import json
import sqlite3

from vrp import s2_mutate_run
from vrp.s2_mutate_run import (
    SOURCE,
    _COUNT_KEYS,
    find_mutmut,
    rc_ok,
    read_cache,
    reconcile,
    run,
    write_findings,
    write_runtime_evidence,
)


def _target(fid="f1", qn="m.clamp", fp="calc/mathfun.py", start=1, end=6):
    return {
        "fid": fid,
        "qualified_name": qn,
        "file_path": fp,
        "start_line": start,
        "end_line": end,
        "tests": 2,
        "callers": 3,
    }


def _mutant(file="calc/mathfun.py", line=3, status="survived"):
    return {"file": file, "line": line, "status": status}


def test_rc_ok_bitmask():
    for rc in (0, 2, 4, 6, 8, 10, 12, 14):
        assert rc_ok(rc)
    for rc in (1, 3, 5, 9):
        assert not rc_ok(rc)
    assert not rc_ok(-1)


def test_find_mutmut_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "mutmut.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("VRP_MUTMUT_BIN", str(fake))
    assert find_mutmut() == str(fake)
    monkeypatch.setenv("VRP_MUTMUT_BIN", str(tmp_path / "missing.exe"))
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert find_mutmut() is None


def _make_cache(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE SourceFile (id INTEGER PRIMARY KEY, filename TEXT, hash TEXT)")
    con.execute("CREATE TABLE Line (id INTEGER PRIMARY KEY, sourcefile INTEGER, line TEXT, line_number INTEGER)")
    con.execute('CREATE TABLE Mutant (id INTEGER PRIMARY KEY, line INTEGER, "index" INTEGER,'
                " tested_against_hash TEXT, status TEXT)")
    con.execute("INSERT INTO SourceFile (id, filename, hash) VALUES (1, 'calc\\mathfun.py', 'h')")
    con.execute("INSERT INTO Line (id, sourcefile, line, line_number) VALUES (10, 1, 'x', 3), (11, 1, 'y', 9)")
    con.execute("INSERT INTO Mutant (line, \"index\", status) VALUES (10, 0, 'bad_survived'), (11, 0, 'ok_killed')")
    con.commit()
    con.close()


def test_read_cache_maps_and_normalizes(tmp_path):
    cache = tmp_path / ".mutmut-cache"
    _make_cache(cache)
    rows = read_cache(cache)
    assert rows == [
        {"file": "calc/mathfun.py", "line": 3, "status": "survived"},
        {"file": "calc/mathfun.py", "line": 9, "status": "killed"},
    ]


def test_reconcile_counts_rate_and_side():
    targets = [_target(), _target(fid="f2", qn="m.safe_div", start=9, end=12)]
    mutants = [
        _mutant(line=3, status="survived"),
        _mutant(line=1, status="killed"),
        _mutant(line=9, status="killed"),
        _mutant(line=10, status="timeout"),
        _mutant(line=11, status="killed"),
        _mutant(file="other/mod.py", line=1, status="survived"),
    ]
    per_target, side = reconcile(targets, mutants)
    first = per_target[0]
    assert first["mutants"] == 2
    assert first["killed"] == 1
    assert first["survived"] == 1
    assert first["survival_rate"] == 0.5
    second = per_target[1]
    assert second["mutants"] == 3
    assert second["killed"] == 2
    assert second["timeout"] == 1
    assert second["survival_rate"] == 0.0
    assert side == 1


def test_reconcile_empty_range_rate_none():
    per_target, side = reconcile([_target()], [])
    assert per_target[0]["mutants"] == 0
    assert per_target[0]["survival_rate"] is None
    assert side == 0


def test_write_findings_only_survivors_idempotent(facts_db):
    per_target = [
        {**_target(), "mutants": 2, "killed": 1, "survived": 1, "timeout": 0,
         "suspicious": 0, "skipped": 0, "untested": 0, "survival_rate": 0.5},
        {**_target(fid="f2", qn="m.safe_div", start=9, end=12), "mutants": 1, "killed": 1,
         "survived": 0, "timeout": 0, "suspicious": 0, "skipped": 0, "untested": 0,
         "survival_rate": 0.0},
    ]
    assert write_findings(facts_db, per_target) == 1
    rows = facts_db.execute(
        f"SELECT pathology, kind, source, anchor_fid FROM findings WHERE source='{SOURCE}'"
    ).fetchall()
    assert rows == [("P8", "mutation-survivors", SOURCE, "f1")]
    assert write_findings(facts_db, per_target) == 1
    count = facts_db.execute(f"SELECT COUNT(*) FROM findings WHERE source='{SOURCE}'").fetchone()[0]
    assert count == 1


def test_write_runtime_evidence_namespace(facts_db):
    facts_db.execute(
        "INSERT INTO runtime_evidence (caller_key, callee_key, payload_json)"
        " VALUES (NULL, 'test', '{\"exit_code\": 0}')"
    )
    write_runtime_evidence(facts_db, {"budget": 10, "targets": 2, "mutants_total": 6,
                                      "survived": 3, "surviving_targets": 1, "survival_rate": 0.5})
    rows = facts_db.execute(
        "SELECT caller_key, callee_key, payload_json FROM runtime_evidence ORDER BY id"
    ).fetchall()
    assert rows[0] == (None, "test", '{"exit_code": 0}')
    assert rows[1][0] == SOURCE and rows[1][1] == SOURCE
    payload = json.loads(rows[1][2])
    assert payload["runner"] == "mutmut" and payload["survival_rate"] == 0.5
    write_runtime_evidence(facts_db, {"budget": 10, "targets": 2, "mutants_total": 6,
                                      "survived": 3, "surviving_targets": 1, "survival_rate": 0.5})
    count = facts_db.execute(
        f"SELECT COUNT(*) FROM runtime_evidence WHERE caller_key='{SOURCE}'"
    ).fetchone()[0]
    assert count == 1


def _seed_targets(facts_db):
    for fid, qn, start, end in (("f1", "m.clamp", 1, 6), ("f2", "m.safe_div", 9, 12)):
        facts_db.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?,'p',?,'Function',?,'calc/mathfun.py',?,?,'h')",
            (fid, qn, qn.rsplit(".", 1)[-1], start, end),
        )
        facts_db.execute(
            "INSERT INTO test_linkage (test_fid, target_fid, source) VALUES ('t1', ?, 'cbm')",
            (fid,),
        )
        facts_db.execute(
            "INSERT INTO edges (src_fid, dst_fid, type, evidence, properties_json)"
            " VALUES ('t1', ?, 'CALLS', 'structural', '{}')",
            (fid,),
        )
    facts_db.commit()


def test_run_no_targets(facts_db, tmp_path):
    result = run(tmp_path, facts_db, 5, 60)
    assert result == {"status": "no-targets"}


def test_run_mutmut_not_found(facts_db, tmp_path, monkeypatch):
    _seed_targets(facts_db)
    monkeypatch.setattr("vrp.s2_mutate_run.find_mutmut", lambda: None)
    result = run(tmp_path, facts_db, 5, 60)
    assert result["status"] == "mutmut-not-found"


def test_run_mutmut_failed(facts_db, tmp_path, monkeypatch):
    _seed_targets(facts_db)
    monkeypatch.setattr("vrp.s2_mutate_run.find_mutmut", lambda: "fake-mutmut")
    monkeypatch.setattr(
        "vrp.s2_mutate_run.run_mutmut",
        lambda bin_path, repo, files, timeout: {"ok": False, "returncode": 1, "stderr_tail": "boom"},
    )
    result = run(tmp_path, facts_db, 5, 60)
    assert result == {"status": "mutmut-failed", "returncode": 1, "stderr_tail": "boom"}


def test_run_full_flow(facts_db, tmp_path, monkeypatch):
    _seed_targets(facts_db)
    seen: dict = {}

    def fake_run_mutmut(bin_path, repo, files, timeout):
        seen["files"] = list(files)
        cache = repo / ".mutmut-cache"
        _make_cache(cache)
        return {"ok": True, "returncode": 2, "stderr_tail": ""}

    monkeypatch.setattr("vrp.s2_mutate_run.find_mutmut", lambda: "fake-mutmut")
    monkeypatch.setattr("vrp.s2_mutate_run.run_mutmut", fake_run_mutmut)
    result = run(tmp_path, facts_db, 5, 60)
    assert result["status"] == "ok"
    assert seen["files"] == ["calc/mathfun.py"]
    assert result["targets"] == 2
    assert result["mutants_total"] == 2
    assert result["surviving_targets"] == 1
    assert result["findings"] == 1
    assert not (tmp_path / ".mutmut-cache").exists()
    report = json.loads((tmp_path / ".vrp" / "mutation-report.json").read_text(encoding="utf-8"))
    assert len(report["per_target"]) == 2
    assert report["side_mutants"] == result["side_mutants"]
    rows = facts_db.execute(
        f"SELECT COUNT(*) FROM findings WHERE source='{SOURCE}'"
    ).fetchone()[0]
    assert rows == 1
    evidence = facts_db.execute(
        f"SELECT COUNT(*) FROM runtime_evidence WHERE caller_key='{SOURCE}'"
    ).fetchone()[0]
    assert evidence == 1
    plan = json.loads((tmp_path / ".vrp" / "mutation-plan.json").read_text(encoding="utf-8"))
    assert len(plan) == 2


def test_run_filters_non_python_targets(facts_db, tmp_path):
    for fid, qn, path in (("f1", "m.clamp", "src/clamp.ts"), ("f2", "m.safe_div", "src/safe_div.py")):
        facts_db.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?,'p',?,'Function',?,?,1,6,'h')",
            (fid, qn, qn.rsplit(".", 1)[-1], path),
        )
        facts_db.execute(
            "INSERT INTO test_linkage (test_fid, target_fid, source) VALUES ('t1', ?, 'cbm')",
            (fid,),
        )
    facts_db.commit()
    seen: dict = {}

    def fake_run_mutmut(bin_path, repo, files, timeout):
        seen["files"] = list(files)
        cache = repo / ".mutmut-cache"
        _make_cache(cache)
        return {"ok": True, "returncode": 0, "stderr_tail": ""}

    import vrp.s2_mutate_run as m

    orig = m.find_mutmut
    m.find_mutmut = lambda: "fake-mutmut"
    m.run_mutmut = fake_run_mutmut
    try:
        result = run(tmp_path, facts_db, 5, 60)
    finally:
        m.find_mutmut = orig
        del m.run_mutmut
    assert result["status"] == "ok"
    assert seen["files"] == ["src/safe_div.py"]
