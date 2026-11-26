"""B4 测试：测试覆盖对账 vrp testcov（批⑤，#203；traces #144 同构断言族）。"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import cli, testcov

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")  # M4: 非 git 仓 = ''
    con.commit()
    con.close()
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


def _add_symbol(repo: Path, fid: str, qn: str, file_path: str = "src/x.py",
                start_line: int = 10, end_line: int = 20) -> None:
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES (?, 'p', ?, 'Function', ?, ?, ?, ?, 'h')",
        (fid, qn, qn, file_path, start_line, end_line),
    )
    con.commit()
    con.close()


def _add_link(repo: Path, test_fid: str, target_fid: str) -> None:
    con = _con(repo)
    con.execute(
        "INSERT INTO test_linkage (test_fid, target_fid, source, properties_json)"
        " VALUES (?, ?, 'cbm', '{}')",
        (test_fid, target_fid),
    )
    con.commit()
    con.close()


def _write_cov(tmp_path: Path, name: str, files: dict, label: str | None = None) -> Path:
    payload: dict = {"files": files}
    if label is not None:
        payload["label"] = label
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _cov_entry(executed: list[int], missing: list[int]) -> dict:
    return {"executed_lines": executed, "missing_lines": missing}


def _rows(repo: Path) -> list[tuple]:
    con = _con(repo)
    try:
        return con.execute(
            "SELECT label, file_path, executed_lines_json, missing_lines_json"
            " FROM test_coverage ORDER BY file_path"
        ).fetchall()
    finally:
        con.close()


# ---------- 导入存储语义 ----------


def test_ingest_stores_per_file_lines(tmp_path):
    """逐文件落库（行 sorted-set 序列化）；receipt 计数面真。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a")
    f = _write_cov(tmp_path, "cov.json", {
        "src/x.py": _cov_entry([12, 11, 12], [13]),
        "src/other.py": _cov_entry([1], [2, 3]),
    })
    con = _con(repo)
    try:
        out = testcov.ingest_testcov(con, f, label="run-1")
    finally:
        con.close()
    assert out["ok"] and out["label"] == "run-1"
    assert out["files_in_run"] == 2 and out["files_matched"] == 1  # other.py 不在 facts
    assert out["lines_executed_total"] == 3 and out["lines_missing_total"] == 3
    assert _rows(repo) == [
        ("run-1", "src/other.py", "[1]", "[2, 3]"),
        ("run-1", "src/x.py", "[11, 12]", "[13]"),  # sorted-set 去重
    ]


def test_per_label_whole_run_replace_idempotent(tmp_path):
    """per label 整批替换：同 label 重导入旧行清空，他 label 不受累。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a")
    f1 = _write_cov(tmp_path, "c1.json", {"src/x.py": _cov_entry([11], [])})
    f2 = _write_cov(tmp_path, "c2.json", {"src/y.py": _cov_entry([1], [])})
    f3 = _write_cov(tmp_path, "c3.json", {})
    con = _con(repo)
    try:
        testcov.ingest_testcov(con, f1, label="run-1")
        testcov.ingest_testcov(con, f2, label="run-2")
        assert len(_rows(repo)) == 2
        out = testcov.ingest_testcov(con, f3, label="run-1")  # run-1 重导入（空）——整批替换
    finally:
        con.close()
    assert out["ok"] and out["files_in_run"] == 0
    assert _rows(repo) == [("run-2", "src/y.py", "[1]", "[]")]


def test_ingest_empty_files_ok(tmp_path):
    repo = _mk_repo(tmp_path)
    f = _write_cov(tmp_path, "cov.json", {})
    con = _con(repo)
    try:
        out = testcov.ingest_testcov(con, f)
    finally:
        con.close()
    assert out["ok"] and out["files_in_run"] == 0 and out["label"] == "unlabeled"


def test_windows_path_normalization(tmp_path):
    """coverage.json 反斜杠路径 ↔ facts 前斜杠口径：归一后精确匹配。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a", file_path="src/x.py")
    f = _write_cov(tmp_path, "cov.json", {"src\\x.py": _cov_entry([11], [])})
    con = _con(repo)
    try:
        out = testcov.ingest_testcov(con, f)
    finally:
        con.close()
    assert out["files_matched"] == 1 and out["unresolved_files_total"] == 0


# ---------- 对账五桶 ----------


def test_linked_and_covered(tmp_path):
    """关联真：body 行命中 executed → linked_and_covered。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a")  # body (10, 20]
    _add_link(repo, "t1", "f1")
    f = _write_cov(tmp_path, "cov.json", {"src/x.py": _cov_entry([15], [16])})
    con = _con(repo)
    try:
        out = testcov.ingest_testcov(con, f)
    finally:
        con.close()
    ag = out["agreement"]
    assert ag["linked_and_covered"] == 1 and ag["samples"]["linked_and_covered"] == ["pkg.a"]


def test_def_line_exclusion_core_precision(tmp_path):
    """def 行排除（核心精度件）：executed 仅命中 start_line（声明行 import 执行）
    + body 有 missing 行 → linked_not_covered_measured（"导入过"不误判"测过"）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a", start_line=10, end_line=20)
    _add_link(repo, "t1", "f1")
    f = _write_cov(tmp_path, "cov.json", {"src/x.py": _cov_entry([10], [11, 12])})
    con = _con(repo)
    try:
        out = testcov.ingest_testcov(con, f)
    finally:
        con.close()
    ag = out["agreement"]
    assert ag["linked_and_covered"] == 0
    assert ag["linked_not_covered_measured"] == 1
    assert ag["samples"]["linked_not_covered_measured"] == ["pkg.a"]


def test_linked_not_covered_unmeasured(tmp_path):
    """目标文件不在覆盖运行 → 范围信号（弱桶，不与 measured 混）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a", file_path="src/x.py")
    _add_link(repo, "t1", "f1")
    f = _write_cov(tmp_path, "cov.json", {"src/other.py": _cov_entry([1], [])})
    con = _con(repo)
    try:
        out = testcov.ingest_testcov(con, f)
    finally:
        con.close()
    assert out["agreement"]["linked_not_covered_unmeasured"] == 1


def test_linked_undetermined_single_line_symbol(tmp_path):
    """单行符号（start==end，body 空）：文件被测但区间无 executed/missing → 诚实未定。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a", start_line=10, end_line=10)
    _add_link(repo, "t1", "f1")
    f = _write_cov(tmp_path, "cov.json", {"src/x.py": _cov_entry([10], [])})
    con = _con(repo)
    try:
        out = testcov.ingest_testcov(con, f)
    finally:
        con.close()
    ag = out["agreement"]
    assert ag["linked_undetermined"] == 1 and ag["linked_and_covered"] == 0


def test_covered_not_linked(tmp_path):
    """静态漏关联（假阴性面）：body 执行但无 test_linkage → covered_not_linked。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a", start_line=10, end_line=20)   # 无链接
    _add_symbol(repo, "f2", "pkg.b", start_line=30, end_line=40)   # 无链接、body 未执行
    f = _write_cov(tmp_path, "cov.json", {"src/x.py": _cov_entry([12], [17])})
    con = _con(repo)
    try:
        out = testcov.ingest_testcov(con, f)
    finally:
        con.close()
    ag = out["agreement"]
    assert ag["covered_not_linked"] == 1
    assert ag["samples"]["covered_not_linked"] == ["pkg.a"]


def test_linked_unresolved_dangling_anchor(tmp_path):
    """悬挂锚：test_linkage 指向无现行符号的 fid → linked_unresolved 计数+样本回显。"""
    repo = _mk_repo(tmp_path)
    _add_link(repo, "t1", "f-gone")
    f = _write_cov(tmp_path, "cov.json", {"src/x.py": _cov_entry([11], [])})
    con = _con(repo)
    try:
        out = testcov.ingest_testcov(con, f)
    finally:
        con.close()
    ag = out["agreement"]
    assert ag["linkage_targets"] == 1 and ag["linked_unresolved"] == 1
    assert ag["linked_unresolved_sample"] == ["f-gone"]


def test_five_way_classification_all_buckets(tmp_path):
    """五桶同盘对账（多符号一次导入全桶命中）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a", file_path="src/a.py")                      # and_covered
    _add_symbol(repo, "f2", "pkg.b", file_path="src/b.py")                      # not_covered_measured
    _add_symbol(repo, "f3", "pkg.c", file_path="src/c.py")                      # not_covered_unmeasured
    _add_symbol(repo, "f4", "pkg.d", file_path="src/d.py", start_line=5, end_line=5)  # undetermined
    _add_symbol(repo, "f5", "pkg.e", file_path="src/e.py")                      # covered_not_linked
    for fid in ("f1", "f2", "f3", "f4"):
        _add_link(repo, f"t-{fid}", fid)
    f = _write_cov(tmp_path, "cov.json", {
        "src/a.py": _cov_entry([11], []),
        "src/b.py": _cov_entry([10], [11, 12]),   # def 行 executed + body missing
        "src/d.py": _cov_entry([5], []),
        "src/e.py": _cov_entry([12], []),
    })
    con = _con(repo)
    try:
        out = testcov.ingest_testcov(con, f)
    finally:
        con.close()
    ag = out["agreement"]
    assert ag["linkage_targets"] == 4
    assert ag["linked_and_covered"] == 1
    assert ag["linked_not_covered_measured"] == 1
    assert ag["linked_not_covered_unmeasured"] == 1
    assert ag["linked_undetermined"] == 1
    assert ag["linked_unresolved"] == 0
    assert ag["covered_not_linked"] == 1


def test_unresolved_files_honest_report(tmp_path):
    """覆盖文件不在 facts → unresolved_files 上报（cap 10）+总数。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a", file_path="src/x.py")
    files = {"src/x.py": _cov_entry([11], [])}
    for i in range(12):
        files[f"src/gone{i}.py"] = _cov_entry([1], [])
    f = _write_cov(tmp_path, "cov.json", files)
    con = _con(repo)
    try:
        out = testcov.ingest_testcov(con, f)
    finally:
        con.close()
    assert out["unresolved_files_total"] == 12
    assert len(out["unresolved_files"]) == 10
    assert "src/gone0.py" in out["unresolved_files"]


# ---------- 确定性与拒绝面 ----------


def test_ingest_deterministic_business_columns(tmp_path):
    """两跑业务列（label/path/行数组）确定——created_at 时间戳除外。"""
    runs = []
    for i in (1, 2):
        repo = _mk_repo(tmp_path / f"r{i}")
        _add_symbol(repo, "f1", "pkg.a")
        f = _write_cov(tmp_path / f"r{i}", "cov.json", {"src/x.py": _cov_entry([12, 11], [13])})
        con = _con(repo)
        try:
            testcov.ingest_testcov(con, f, label="run")
        finally:
            con.close()
        runs.append(_rows(repo))
    assert runs[0] == runs[1]


def test_ingest_invalid_shapes_rejected_zero_write(tmp_path):
    """形状违例整文件拒绝（诚实失败优于部分采信），拒绝路径零写入。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    con = _con(repo)
    try:
        with pytest.raises(SystemExit, match="not valid JSON"):
            testcov.ingest_testcov(con, bad)
        with pytest.raises(SystemExit, match="must be a JSON object"):
            testcov.ingest_testcov(con, _write_cov_arr(tmp_path, "arr.json", [1, 2]))
        with pytest.raises(SystemExit, match="'files' must be an object"):
            testcov.ingest_testcov(con, _write_cov_arr(tmp_path, "nof.json", {"files": []}))
        with pytest.raises(SystemExit, match="must be an object"):
            testcov.ingest_testcov(con, _write_cov_arr(tmp_path, "ent.json", {"files": {"a.py": 1}}))
        with pytest.raises(SystemExit, match="'executed_lines' must be an array"):
            testcov.ingest_testcov(con, _write_cov_arr(tmp_path, "ex.json", {"files": {"a.py": {"missing_lines": []}}}))
        with pytest.raises(SystemExit, match="positive integers"):
            testcov.ingest_testcov(con, _write_cov_arr(tmp_path, "bool.json", {"files": {"a.py": _cov_entry([True], [])}}))
        with pytest.raises(SystemExit, match="positive integers"):
            testcov.ingest_testcov(con, _write_cov_arr(tmp_path, "zero.json", {"files": {"a.py": _cov_entry([0], [])}}))
        with pytest.raises(SystemExit, match="file not found"):
            testcov.ingest_testcov(con, tmp_path / "missing.json")
    finally:
        con.close()
    assert _rows(repo) == []  # 拒绝路径零写入


def _write_cov_arr(tmp_path: Path, name: str, payload) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ---------- CLI 接线 ----------


def test_cli_testcov_wiring(tmp_path, capsys):
    """CLI vrp testcov 接线：--file/--label 经 _open_facts_rw（M4 闸）落库。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a")
    _add_link(repo, "t1", "f1")
    f = _write_cov(tmp_path, "cov.json", {"src/x.py": _cov_entry([11], [])})
    parser = cli.build_parser()
    args = parser.parse_args(["testcov", str(repo), "--file", str(f), "--label", "cli-run", "--json"])
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["label"] == "cli-run"
    assert out["agreement"]["linked_and_covered"] == 1
    assert _rows(repo) == [("cli-run", "src/x.py", "[11]", "[]")]


def test_cli_testcov_missing_facts_rejected(tmp_path):
    repo = tmp_path / "empty"
    (repo / ".vrp").mkdir(parents=True)
    f = _write_cov(tmp_path, "cov.json", {})
    parser = cli.build_parser()
    args = parser.parse_args(["testcov", str(repo), "--file", str(f)])
    with pytest.raises(SystemExit, match="facts.db missing"):
        args.func(args)
