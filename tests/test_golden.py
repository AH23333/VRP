"""F15+BB3 golden 审计单元（批⑥ #204）：真值形状守卫+双向计数数学+payload
守卫+域语义+fixture 构建+CLI 接线。

golden=committed 独立真值（vrp-golden-1）——与被审计 facts.db 物理分离
（循环完美门防线）；provenance 非空守卫=「手工改过的 golden 不落盘=自欺」
防线工程化。precision/recall 分母零→null 诚实（不造 1.0——「期望零且
得零」靠计数面呈现，D3）；rc 恒 0（度量非门禁，D5）。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import cli, golden

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _golden(data: dict, tmp_path: Path) -> Path:
    path = tmp_path / "g.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _base() -> dict:
    return {
        "format": "vrp-golden-1",
        "name": "unit",
        "language": "x",
        "provenance": "unit fixture",
        "truth": {"violations": [], "symbols": []},
    }


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    con.close()
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


def _finding(con, kind: str, file: str, line: int, payload: str) -> None:
    con.execute(
        "INSERT INTO findings (pathology, kind, source, file_path, start_line, payload_json)"
        " VALUES ('P9', ?, 's7-enforce', ?, ?, ?)",
        (kind, file, line, payload),
    )


def _symbol(con, qn: str, label: str, file: str, start: int, end: int) -> None:
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
        " start_line, end_line, content_hash) VALUES (?, 'p', ?, ?, ?, ?, ?, ?, 'h')",
        (f"fid-{qn}", qn, label, qn, file, start, end),
    )


# ------------------------------------------------------------ 形状守卫


def test_load_minimal_truth_only(tmp_path):
    g = golden.load_golden(_golden(_base(), tmp_path))
    assert g["format"] == "vrp-golden-1"
    assert g["provenance"] == "unit fixture"
    assert g["fixture"] is None  # fixture 可选（CLI 对真仓审计时缺省）
    assert g["truth"]["findings_source"] == "s7-enforce"  # 默认 source
    assert g["truth"]["violations"] == []  # 空数组=「期望零」有效面
    assert g["truth"]["symbols"] == []


def test_load_rejects_wrong_format(tmp_path):
    data = _base()
    data["format"] = "vrp-golden-0"
    with pytest.raises(SystemExit):
        golden.load_golden(_golden(data, tmp_path))


def test_load_rejects_empty_provenance(tmp_path):
    """「手工改过的 golden 不落盘=自欺」防线：provenance 非空守卫（D4）。"""
    data = _base()
    data["provenance"] = ""
    with pytest.raises(SystemExit):
        golden.load_golden(_golden(data, tmp_path))


def test_load_rejects_truth_without_face(tmp_path):
    data = _base()
    data["truth"] = {}
    with pytest.raises(SystemExit):
        golden.load_golden(_golden(data, tmp_path))


def test_load_rejects_unknown_top_key(tmp_path):
    data = _base()
    data["extra"] = 1
    with pytest.raises(SystemExit):
        golden.load_golden(_golden(data, tmp_path))


def test_load_rejects_violation_missing_kind(tmp_path):
    data = _base()
    data["truth"]["violations"] = [{"file": "a", "line": 1}]
    with pytest.raises(SystemExit):
        golden.load_golden(_golden(data, tmp_path))


def test_load_rejects_bool_line(tmp_path):
    data = _base()
    data["truth"]["violations"] = [{"kind": "k", "file": "a", "line": True}]
    with pytest.raises(SystemExit):
        golden.load_golden(_golden(data, tmp_path))


def test_load_rejects_symbol_missing_end(tmp_path):
    data = _base()
    data["truth"]["symbols"] = [{"qn": "q", "label": "Class", "file": "f", "start": 1}]
    with pytest.raises(SystemExit):
        golden.load_golden(_golden(data, tmp_path))


def test_load_rejects_backslash_fixture_path(tmp_path):
    data = _base()
    data["fixture"] = {"repo_name": "r", "files": {"a\\b.py": "x\n"}}
    with pytest.raises(SystemExit):
        golden.load_golden(_golden(data, tmp_path))


# ------------------------------------------------------------ 计数数学


def test_face_multiset_math():
    face = golden._face([("a",), ("a",), ("b",)], [("a",), ("b",), ("b",)], str)
    assert face["truth"] == 3
    assert face["actual"] == 3
    assert face["matched"] == 2  # 同键按 min 计（重复违例诚实不折叠）
    assert face["missing"] == 1
    assert face["spurious"] == 1
    assert face["precision"] == round(2 / 3, 4)
    assert face["recall"] == round(2 / 3, 4)
    assert face["missing_samples"] == ["('a',)"]
    assert face["spurious_samples"] == ["('b',)"]


def test_face_null_denominators_honest():
    """D3：分母零→null 诚实，不造 1.0。"""
    empty = golden._face([], [], str)
    assert empty["precision"] is None and empty["recall"] is None
    spurious_only = golden._face([], [("a",)], str)
    assert spurious_only["precision"] == 0.0
    assert spurious_only["recall"] is None
    missing_only = golden._face([("a",)], [], str)
    assert missing_only["precision"] is None
    assert missing_only["recall"] == 0.0


def test_face_sample_cap():
    truth = [(f"k{i}",) for i in range(8)]
    face = golden._face(truth, [], str)
    assert len(face["missing_samples"]) == golden.SAMPLE_CAP  # cap 5/侧
    expected = sorted(str((f"k{i}",)) for i in range(golden.SAMPLE_CAP))
    assert face["missing_samples"] == expected


# ------------------------------------------------------------ violations 面


def test_audit_violations_multiset_and_source_filter(tmp_path):
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    _finding(con, "layer-violation", "a.py", 3, json.dumps({"importer": "x", "imported": "y"}))
    _finding(con, "layer-violation", "a.py", 3, json.dumps({"importer": "x", "imported": "y"}))
    _finding(con, "layer-violation", "a.py", 3, json.dumps({"importer": "x", "imported": "y"}))
    _finding(con, "noise", "a.py", 9, "{}")  # 不同 kind：truth 无此键
    con.execute(
        "INSERT INTO findings (pathology, kind, source, file_path, start_line, payload_json)"
        " VALUES ('P1', 'other-kind', 'other-source', 'a.py', 3, '{}')"  # 不同 source：过滤面外
    )
    con.commit()
    con.close()
    truth = [
        {"kind": "layer-violation", "file": "a.py", "line": 3, "importer": "x", "imported": "y"},
        {"kind": "layer-violation", "file": "a.py", "line": 3, "importer": "x", "imported": "y"},
    ]
    con = _con(repo)
    try:
        face = golden.audit_violations(con, truth, "s7-enforce")
    finally:
        con.close()
    assert face["truth"] == 2
    assert face["actual"] == 4  # 第三条重复+noise 均计入 actual（spurious 诚实显形）
    assert face["matched"] == 2
    assert face["missing"] == 0
    assert face["spurious"] == 2
    assert face["precision"] == 0.5
    assert face["recall"] == 1.0
    assert face["spurious_samples"] == [
        "layer-violation a.py:3 x -> y",
        "noise a.py:9 None -> None",
    ]


def test_audit_violations_payload_guarded(tmp_path):
    """R-AD：payload 不可解析/非对象/字段非 str → 键位 None 降级不炸（D2）。"""
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    _finding(con, "layer-violation", "a.py", 3, "not-json")  # 不可解析 payload
    _finding(con, "layer-violation", "b.py", 1, json.dumps([1, 2]))  # 非对象
    _finding(con, "layer-violation", "c.py", 1, json.dumps({"importer": 5}))  # 字段非 str
    con.commit()
    con.close()
    truth = [
        {"kind": "layer-violation", "file": "a.py", "line": 3},
        {"kind": "layer-violation", "file": "b.py", "line": 1},
    ]
    con = _con(repo)
    try:
        face = golden.audit_violations(con, truth, "s7-enforce")
    finally:
        con.close()
    assert face["matched"] == 2  # truth 无 importer/imported → 键不含两可字段
    assert face["missing"] == 0
    assert face["spurious"] == 1  # c.py 行：truth 无此键


# ------------------------------------------------------------ symbols 面


def test_audit_symbols_strict_near_miss_pairs(tmp_path):
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    _symbol(con, "q.Hello", "Class", "f.py", 1, 9)  # 近失：truth end=10
    _symbol(con, "q.exact", "Function", "g.py", 2, 3)
    con.commit()
    con.close()
    truth = [
        {"qn": "q.Hello", "label": "Class", "file": "f.py", "start": 1, "end": 10},
        {"qn": "q.exact", "label": "Function", "file": "g.py", "start": 2, "end": 3},
    ]
    con = _con(repo)
    try:
        face = golden.audit_symbols(con, truth)
    finally:
        con.close()
    assert face["matched"] == 1
    assert face["missing"] == 1  # 近失两侧成对（解析不精度不掩埋，D2）
    assert face["spurious"] == 1


def test_audit_symbols_scope_bounded(tmp_path):
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    _symbol(con, "in.scope", "Function", "in.py", 1, 2)
    _symbol(con, "out.scope", "Function", "out.py", 1, 2)  # 域外：不触碰
    con.commit()
    con.close()
    truth = [{"qn": "in.scope", "label": "Function", "file": "in.py", "start": 1, "end": 2}]
    con = _con(repo)
    try:
        face = golden.audit_symbols(con, truth)
    finally:
        con.close()
    assert face["actual"] == 1  # 域外不入 actual（有界语料完整枚举，D1）
    assert (face["matched"], face["missing"], face["spurious"]) == (1, 0, 0)


# ------------------------------------------------------------ fixture 构建


def test_write_fixture_files_and_seed_db(tmp_path):
    data = _base()
    data["fixture"] = {
        "repo_name": "frepo",
        "files": {"src/a.py": "x = 1\n", "src/deep/b.py": "y = 2\n"},
        "meta": {"git_head": ""},
        "symbols": [["f1", "p", "a.x", "Function", "x", "src/a.py", 1, 1, "h"]],
        "structure": {"modules": [], "layers": []},
    }
    spec = golden.load_golden(_golden(data, tmp_path))
    repo = golden.write_fixture_files(spec, tmp_path)
    assert repo == tmp_path / "frepo"
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\n"  # 字节=JSON 内容
    assert (repo / "src" / "deep" / "b.py").read_text(encoding="utf-8") == "y = 2\n"
    db = golden.seed_fixture_db(spec, repo)
    assert db == repo / ".vrp" / "facts.db"
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT fid, qualified_name, label, file_path, start_line, end_line FROM symbols"
        ).fetchall()
        meta = dict(con.execute("SELECT key, value FROM meta"))
        structure = json.loads(con.execute("SELECT payload_json FROM structure").fetchone()[0])
    finally:
        con.close()
    assert rows == [("f1", "a.x", "Function", "src/a.py", 1, 1)]
    assert meta == {"git_head": ""}
    assert structure == {"modules": [], "layers": []}


def test_write_fixture_files_refuses_existing(tmp_path):
    data = _base()
    data["fixture"] = {"repo_name": "frepo", "files": {"a.py": "x\n"}}
    spec = golden.load_golden(_golden(data, tmp_path))
    golden.write_fixture_files(spec, tmp_path)
    with pytest.raises(SystemExit):
        golden.write_fixture_files(spec, tmp_path)  # 幂等护栏：不静默覆盖


# ------------------------------------------------------------ CLI 接线


def test_cli_golden_wiring_and_determinism(tmp_path, capsys):
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    _finding(con, "layer-violation", "a.py", 3, json.dumps({"importer": "x", "imported": "y"}))
    _symbol(con, "q.a", "Class", "a.py", 1, 2)
    con.commit()
    con.close()
    data = _base()
    data["name"] = "cli-unit"
    data["truth"] = {
        "violations": [
            {"kind": "layer-violation", "file": "a.py", "line": 3, "importer": "x", "imported": "y"}
        ],
        "symbols": [{"qn": "q.a", "label": "Class", "file": "a.py", "start": 1, "end": 2}],
    }
    path = _golden(data, tmp_path)
    assert cli.main(["golden", str(repo), "--file", str(path)]) == 0  # rc 恒 0（D5）
    first = json.loads(capsys.readouterr().out)
    assert first["ok"] is True
    assert first["name"] == "cli-unit"
    assert first["faces"]["violations"]["matched"] == 1
    assert first["faces"]["violations"]["missing"] == 0
    assert first["faces"]["violations"]["spurious"] == 0
    assert first["faces"]["symbols"]["matched"] == 1
    assert "note" in first
    assert cli.main(["golden", str(repo), "--file", str(path)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert first == second  # determinism：同库同真值同 receipt


def test_cli_golden_missing_db(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    path = _golden(_base(), tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["golden", str(repo), "--file", str(path)])
