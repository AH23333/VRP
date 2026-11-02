from pathlib import Path

import pytest

from vrp.s2_astgrep import (
    FINDINGS_SOURCE,
    RULE_META,
    RULES_DIR,
    find_astgrep,
    rule_id_of,
    scan,
)

BIN = find_astgrep()

A_TS = """function go(x: number) {
  console.log('debug', x);
  try { open(x); } catch (e) {}
  return x;
}
// TODO: rewrite this
function temp_helper() { return 1; }
class C {
  bar(): void { console.log('m'); }
}
"""

B_PY = """def go(x):
    print('debug', x)
    try:
        return x
    except:
        pass
    # TODO fix later
def temp_helper():
    return 1
"""


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text(A_TS, encoding="utf-8")
    (src / "b.py").write_text(B_PY, encoding="utf-8")
    return tmp_path


def _seed(con):
    rows = [
        ("fid-go-ts", "a.go", "go", "src/a.ts", 1, 5),
        ("fid-temp-ts", "a.temp_helper", "temp_helper", "src/a.ts", 7, 7),
        ("fid-go-py", "b.go", "go", "src/b.py", 1, 6),
        ("fid-temp-py", "b.temp_helper", "temp_helper", "src/b.py", 8, 9),
    ]
    for fid, qn, name, fp, start, end in rows:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (fid, "proj", qn, "Function", name, fp, start, end, "h"),
        )
    con.commit()


def test_rules_and_meta_consistent():
    files = sorted(RULES_DIR.glob("*.yml"))
    assert files, "rule dir is empty"
    ids = {rule_id_of(f) for f in files}
    assert None not in ids
    assert ids == set(RULE_META), f"rule files {ids} != meta {set(RULE_META)}"


def test_rule_id_of_parses_id_line(tmp_path):
    rf = tmp_path / "r.yml"
    rf.write_text("id: my-rule\nlanguage: Python\nrule:\n  pattern: pass\n", encoding="utf-8")
    assert rule_id_of(rf) == "my-rule"
    empty = tmp_path / "e.yml"
    empty.write_text("language: Python\n", encoding="utf-8")
    assert rule_id_of(empty) is None


@pytest.mark.skipif(BIN is None, reason="ast-grep binary not available")
def test_scan_counts_and_anchoring(repo, facts_db):
    _seed(facts_db)
    result = scan(facts_db, repo, bin_path=BIN)
    assert result["rejected"] == 0
    assert result["per_rule"]["ts-console-log"] == 2
    assert result["per_rule"]["ts-empty-catch"] == 1
    assert result["per_rule"]["ts-todo-comment"] == 1
    assert result["per_rule"]["ts-prototype-name"] == 1
    assert result["per_rule"]["py-print-debug"] == 1
    assert result["per_rule"]["py-bare-except"] == 1
    assert result["per_rule"]["py-todo-comment"] == 1
    assert result["per_rule"]["py-prototype-name"] == 1
    assert result["per_rule"]["js-console-log"] == 0
    assert result["total"] == 9
    assert result["anchored"] == 6

    rows = facts_db.execute(
        "SELECT kind, file_path, start_line, anchor_fid, payload_json FROM findings"
        " WHERE source = ? ORDER BY kind, start_line",
        (FINDINGS_SOURCE,),
    ).fetchall()
    assert len(rows) == 9
    by_key = {(r[0], r[1], r[2]): r for r in rows}

    anchored_log = by_key[("debug-print", "src/a.ts", 2)]
    assert anchored_log[3] == "fid-go-ts"
    assert '"rule": "ts-console-log"' in anchored_log[4]

    method_log = by_key[("debug-print", "src/a.ts", 9)]
    assert method_log[3] is None

    todo = by_key[("debris-marker", "src/a.ts", 6)]
    assert todo[3] is None

    proto = by_key[("prototype-name", "src/a.ts", 7)]
    assert proto[3] == "fid-temp-ts"

    py_log = by_key[("debug-print", "src/b.py", 2)]
    assert py_log[3] == "fid-go-py"

    bare = by_key[("swallowed-error", "src/b.py", 5)]
    assert bare[3] == "fid-go-py"

    py_proto = by_key[("prototype-name", "src/b.py", 8)]
    assert py_proto[3] == "fid-temp-py"

    pathologies = {r[0] for r in facts_db.execute("SELECT DISTINCT pathology FROM findings")}
    assert pathologies == {"P1", "P3"}


@pytest.mark.skipif(BIN is None, reason="ast-grep binary not available")
def test_scan_idempotent(repo, facts_db):
    _seed(facts_db)
    first = scan(facts_db, repo, bin_path=BIN)
    second = scan(facts_db, repo, bin_path=BIN)
    assert first["total"] == second["total"]
    count = facts_db.execute(
        "SELECT COUNT(*) FROM findings WHERE source = ?", (FINDINGS_SOURCE,)
    ).fetchone()[0]
    assert count == first["total"]


def test_scan_missing_binary_raises(repo, facts_db, monkeypatch):
    monkeypatch.setattr("vrp.s2_astgrep.find_astgrep", lambda: None)
    with pytest.raises(RuntimeError, match="ast-grep binary not found"):
        scan(facts_db, repo, bin_path=None)
