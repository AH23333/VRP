from vrp.s2_mutate import mutation_targets, write_plan


def _seed(con):
    rows = [
        ("f-hot", "m.hot", "hot", "src/m.py", 1, 3, 3, 4),
        ("f-warm", "m.warm", "warm", "src/m.py", 5, 7, 1, 4),
        ("f-cold", "m.cold", "cold", "src/m.py", 9, 11, 2, 0),
        ("f-no-tests", "m.no_tests", "no_tests", "src/m.py", 13, 15, 0, 9),
        ("f-testpath", "m.tp", "tp", "tests/test_x.py", 1, 2, 5, 5),
        ("f-dunder", "m.__repr__", "__repr__", "src/m.py", 4, 4, 1, 1),
    ]
    for fid, qn, name, fp, start, end, tests, callers in rows:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (fid, "proj", qn, "Function", name, fp, start, end, "h"),
        )
        for i in range(tests):
            con.execute(
                "INSERT INTO test_linkage (test_fid, target_fid, source) VALUES (?,?,?)",
                (f"t{i}-{fid}", fid, "cbm"),
            )
        for i in range(callers):
            con.execute(
                "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,?,?)",
                (f"c{i}-{fid}", fid, "CALLS", "hybrid-lsp"),
            )
    con.commit()


def test_mutation_targets_order_and_budget(facts_db):
    _seed(facts_db)
    targets = mutation_targets(facts_db, 10)
    assert [t["fid"] for t in targets] == ["f-hot", "f-warm", "f-cold"]
    assert targets[0]["callers"] == 4 and targets[0]["tests"] == 3
    assert targets[2]["callers"] == 0
    cut = mutation_targets(facts_db, 2)
    assert [t["fid"] for t in cut] == ["f-hot", "f-warm"]


def test_mutation_targets_exclusions(facts_db):
    _seed(facts_db)
    fids = {t["fid"] for t in mutation_targets(facts_db, 10)}
    assert "f-no-tests" not in fids
    assert "f-testpath" not in fids
    assert "f-dunder" not in fids


def test_mutation_targets_zero_budget(facts_db):
    _seed(facts_db)
    assert mutation_targets(facts_db, 0) == []


def test_write_plan(tmp_path):
    targets = [{"fid": "x", "qualified_name": "m.x", "file_path": "src/m.py", "start_line": 1, "end_line": 2, "tests": 1, "callers": 1}]
    out = write_plan(tmp_path, targets)
    assert out.is_file()
    import json
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == targets
