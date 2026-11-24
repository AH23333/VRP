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


def test_run_rule_non_json_stdout_raises_runtime_error(repo, monkeypatch):
    """R-E E2：rc=0 但 stdout 非 JSON——RuntimeError 统一失败语义（非裸 ValueError）。
    R-AM 接缝移至 procs.run_tree（dict 形状）。"""
    from vrp.s2_astgrep import run_rule

    rr = {"returncode": 0, "timed_out": False, "stdout": "warning: something\nnot json", "stderr": ""}
    monkeypatch.setattr("vrp.s2_astgrep.procs.run_tree", lambda *args, **kwargs: dict(rr))
    rule = RULES_DIR / "py-bare-except.yml"
    with pytest.raises(RuntimeError, match="non-JSON"):
        run_rule("fake-bin", rule, repo)


def test_run_rule_valid_json_parses(repo, monkeypatch):
    from vrp.s2_astgrep import run_rule

    rr = {"returncode": 0, "timed_out": False, "stdout": '[{"file": "src/a.ts"}]', "stderr": ""}
    monkeypatch.setattr("vrp.s2_astgrep.procs.run_tree", lambda *args, **kwargs: dict(rr))
    rule = RULES_DIR / "py-bare-except.yml"
    assert run_rule("fake-bin", rule, repo) == [{"file": "src/a.ts"}]


def test_run_rule_nonlist_json_raises_runtime_error(repo, monkeypatch):
    """AU-5（R-AU）：rc=0 且合法 JSON 但非 list（上游形状漂移，PATH 未钉版本
    可达）——与 non-JSON 同契约 RuntimeError，不裸抛 TypeError 击穿 guard 元组。"""
    from vrp.s2_astgrep import run_rule

    rr = {"returncode": 0, "timed_out": False, "stdout": '{"wrapped": true}', "stderr": ""}
    monkeypatch.setattr("vrp.s2_astgrep.procs.run_tree", lambda *args, **kwargs: dict(rr))
    rule = RULES_DIR / "py-bare-except.yml"
    with pytest.raises(RuntimeError, match="non-list"):
        run_rule("fake-bin", rule, repo)


def test_run_rule_timeout_raises_runtime_error_tree_killed(repo, monkeypatch):
    """R-AM：ast-grep 超时（VRP_ASTGREP_BIN/which 可命中 npm .cmd 垫片→孙进程持管道，#84）——
    树杀后跟模块失败契约走 RuntimeError（TimeoutExpired 不逃逸穿 scan→cmd_s2 裸崩）。"""
    import sys
    import time as time_mod

    from vrp import s2_astgrep
    from vrp.s2_astgrep import run_rule

    slow = repo / "slow_astgrep.py"
    slow.write_text("import time\ntime.sleep(90)\nprint('[]')\n", encoding="utf-8")
    if sys.platform == "win32":
        exe = repo / "fake_astgrep.cmd"
        exe.write_text(f'@"{sys.executable}" "{slow}" %*\n', encoding="utf-8")
    else:
        exe = repo / "fake_astgrep.sh"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{slow}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)

    orig_tree = s2_astgrep.procs.run_tree
    monkeypatch.setattr(
        s2_astgrep.procs,
        "run_tree",
        lambda cmd, timeout=None: orig_tree(cmd, timeout=2),
    )
    rule = RULES_DIR / "py-bare-except.yml"
    start = time_mod.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        run_rule(str(exe), rule, repo)
    assert time_mod.monotonic() - start < 50, "tree-kill must return within kill-path budget 2+30+15s (#232); orphan-held pipe would hang to sleep(90)"


DANGEROUS_PY = """import pickle
import yaml
import sqlite3

def risky(data, f):
    obj = eval(data)
    exec(data)
    o = pickle.loads(data)
    p = pickle.load(f)
    u = yaml.load(data)
    safe = yaml.load(data, Loader=yaml.SafeLoader)
    cur = sqlite3.connect("x").cursor()
    cur.execute("SELECT * FROM t WHERE id = " + data)
    cur.executemany("INSERT INTO t VALUES(" + data + ")", [])
    cur.execute("SELECT 1")
    return obj

class Model:
    def eval(self):
        return 1

m = Model()
m.eval()
"""


@pytest.mark.skipif(BIN is None, reason="ast-grep binary not available")
def test_dangerous_rules_detect_anchor_and_pathology(tmp_path, facts_db):
    """H3 危险 API 规则族（P5）：eval/exec/pickle/yaml/SQL 拼接命中+锚定+病理码。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "danger.py").write_text(DANGEROUS_PY, encoding="utf-8")
    facts_db.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("fid-risky", "proj", "danger.risky", "Function", "risky", "src/danger.py", 5, 16, "h"),
    )
    facts_db.commit()
    result = scan(facts_db, tmp_path, bin_path=BIN)
    assert result["rejected"] == 0
    assert result["per_rule"]["py-eval-exec"] == 2
    assert result["per_rule"]["py-unsafe-deserialize"] == 3
    assert result["per_rule"]["py-sql-concat"] == 2

    rows = facts_db.execute(
        "SELECT pathology, kind, anchor_fid FROM findings WHERE source = ?",
        (FINDINGS_SOURCE,),
    ).fetchall()
    assert len(rows) == 7
    assert {r[0] for r in rows} == {"P5"}
    kinds = [r[1] for r in rows]
    assert kinds.count("dynamic-exec") == 2
    assert kinds.count("unsafe-deserialize") == 3
    assert kinds.count("sql-concat") == 2
    assert all(r[2] == "fid-risky" for r in rows), "dangerous hits must anchor inside risky"


@pytest.mark.skipif(BIN is None, reason="ast-grep binary not available")
def test_dangerous_rules_safe_forms_not_flagged(tmp_path, facts_db):
    """安全形态零误报实核：yaml.load(Loader=SafeLoader)/干净 execute/Model.eval()
    方法调用（callee 是 attribute 非 builtin eval）不进 findings。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "danger.py").write_text(DANGEROUS_PY, encoding="utf-8")
    scan(facts_db, tmp_path, bin_path=BIN)
    lines = {
        r[0]
        for r in facts_db.execute(
            "SELECT start_line FROM findings WHERE source = ?", (FINDINGS_SOURCE,)
        )
    }
    assert lines == {6, 7, 8, 9, 10, 13, 14}, f"unexpected hit lines: {sorted(lines)}"


PRINT_FACE_PY = (
    "def _print_step(title):\n"
    "    print(f\"== {title} ==\")\n"
    "\n"
    "\n"
    "def print_report(rows):\n"
    "    print(f\"{len(rows)} rows\")\n"
    "\n"
    "\n"
    "def go(x):\n"
    "    print('debug', x)\n"
    "\n"
    "\n"
    "def outer():\n"
    "    def _print_inner(y):\n"
    "        print(y)\n"
    "\n"
    "    _print_inner(1)\n"
    "    print('debug-outer')\n"
    "\n"
    "\n"
    "def helper():\n"
    "    return 1\n"
    "\n"
    "\n"
    "print('module-level')\n"
)


@pytest.mark.skipif(BIN is None, reason="ast-grep binary not available")
def test_print_debug_exempts_print_faced_functions(tmp_path, facts_db):
    """#215 dogfood 考古演练修复：py-print-debug print-face 豁免——_print_step/
    print_report（^_?print 命名）内 print 是职责输出非遗留（#10749 P1 误报锚定
    _print_step 实测）；嵌套 _print_inner 豁免（stopBy: end 祖先链）；go/
    outer/module-level 的 print 照报——豁免不吞真信号。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "printfaces.py").write_text(PRINT_FACE_PY, encoding="utf-8")
    result = scan(facts_db, tmp_path, bin_path=BIN)
    assert result["per_rule"]["py-print-debug"] == 3
    lines = {
        r[0]
        for r in facts_db.execute(
            "SELECT start_line FROM findings WHERE source = ? AND kind = 'debug-print'",
            (FINDINGS_SOURCE,),
        )
    }
    # go 的 print(10)/outer 的 print(18)/module-level print(25) 照报；
    # _print_step(2)/print_report(6)/嵌套 _print_inner(15) 豁免。
    assert lines == {10, 18, 25}, f"unexpected print hits: {sorted(lines)}"
