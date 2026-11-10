from vrp.s2_deadcode import FINDINGS_SOURCE, dead_code_candidates, is_candidate, scan


def _seed(con):
    rows = [
        ("fid-live", "mod.live", "live", "Function", "src/mod.py", 1, 3),
        ("fid-dead", "mod.dead", "dead", "Function", "src/mod.py", 5, 7),
        ("fid-main", "mod.main", "main", "Function", "src/mod.py", 9, 11),
        ("fid-dunder", "mod.__init__", "__init__", "Method", "src/mod.py", 13, 13),
        ("fid-testf", "tf.helper", "helper", "Function", "tests/test_x.py", 1, 2),
        ("fid-hinted", "mod.hinted", "hinted", "Function", "src/mod.py", 15, 17),
        ("fid-class", "mod.Klass", "Klass", "Class", "src/mod.py", 19, 25),
    ]
    for fid, qn, name, label, fp, start, end in rows:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (fid, "proj", qn, label, name, fp, start, end, "h"),
        )
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fid-live','fid-dead','CALLS','hybrid-lsp')"
    )
    con.execute(
        "INSERT INTO usage_hints (src_fid, dst_fid) VALUES ('fid-live','fid-hinted')"
    )
    con.commit()


def test_is_candidate():
    assert is_candidate("helper", "src/mod.py")
    assert not is_candidate("main", "src/mod.py")
    assert not is_candidate("__init__", "src/mod.py")
    assert not is_candidate("do_work", "tests/test_x.py")
    assert not is_candidate("test_helper", "src/mod.py")
    assert not is_candidate("helper", "tests/helper_test.py")
    assert not is_candidate("helper", "<synthetic>")
    assert not is_candidate("", "src/mod.py")
    assert not is_candidate("helper", "")


def test_dead_code_candidates(facts_db):
    _seed(facts_db)
    cands = dead_code_candidates(facts_db)
    fids = {c[0] for c in cands}
    assert fids == {"fid-live"}
    live = next(c for c in cands if c[0] == "fid-live")
    assert live[1] == "mod.live"
    assert live[3] == "src/mod.py"


def test_is_candidate_dir_segment_traps():
    """R-I：目录段子串陷阱不产生假排除（latest/contests 等真实目录是候选）。"""
    assert is_candidate("helper", "docs/latest/helper.py")
    assert is_candidate("helper", "src/contests/helper.py")
    assert is_candidate("helper", "skills/baseline-test/helper.js")
    assert is_candidate("helper", "src/mytests/helper.py")


def test_view_dir_glob_parity_with_python(facts_db):
    """R-I：v_dead_code 目录 GLOB 与 is_candidate 段式语义对齐（quick 级守卫，deep 级 30x32 组合的日常护栏）。"""
    rows = [
        ("f-latest", "docs/latest/helper.py"),
        ("f-contests", "src/contests/helper.py"),
        ("f-tests", "src/tests/helper.py"),
        ("f-test", "test/helper.ts"),
        ("f-mytests", "src/mytests/helper.py"),
    ]
    for fid, fp in rows:
        facts_db.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
            " VALUES (?,?,?,?,?,?,?)",
            (fid, "p", f"p.{fid}", "Function", "helper", fp, "h"),
        )
    facts_db.commit()
    for fid, fp in rows:
        in_view = facts_db.execute(
            "SELECT COUNT(*) FROM v_dead_code WHERE fid = ?", (fid,)
        ).fetchone()[0] == 1
        assert in_view == is_candidate("helper", fp), f"drift: {fp}"
    assert {r[0] for r in facts_db.execute("SELECT fid FROM v_dead_code")} == {
        "f-latest",
        "f-contests",
        "f-mytests",
    }


def test_scan_writes_findings(facts_db):
    _seed(facts_db)
    before = facts_db.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    assert before == 0
    result = scan(facts_db)
    assert result == {"total": 1}
    rows = facts_db.execute(
        "SELECT pathology, kind, source, anchor_fid, file_path FROM findings WHERE source = ?",
        (FINDINGS_SOURCE,),
    ).fetchall()
    assert rows == [("P4", "dead-code-candidate", FINDINGS_SOURCE, "fid-live", "src/mod.py")]
    again = scan(facts_db)
    assert again == {"total": 1}
    count = facts_db.execute(
        "SELECT COUNT(*) FROM findings WHERE source = ?", (FINDINGS_SOURCE,)
    ).fetchone()[0]
    assert count == 1
