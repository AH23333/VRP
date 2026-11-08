"""S7 OCaml 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_ocaml.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实源布局——编译单元文件容器、
词法 canary（嵌套注释/注释内串含 *)/字符串/quoted string/字符字面量/类型
变量/跨行串内伪 open）、test/ 目录测试排除、传递链、functor 应用实参产
边；真仓 dogfood 另见 work/ocaml-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：app(queuey) ← interface(logging)
STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "app-mod", "name": "队列", "responsibility": "队", "symbol_fids": ["g1"], "layer": "app"},
            {"module_id": "iface-mod", "name": "日志", "responsibility": "志", "symbol_fids": ["g2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "interface", "may_depend_on": ["app"]},
            {"name": "app", "may_depend_on": []},
        ],
    },
    ensure_ascii=False,
)

# 词法 canary——注释（1）/注释内串含 *)（2）/串内伪 open（3）/quoted
# string 内伪 open（4）/字符字面量（5）/转义字符字面量（6）/跨行串内伪
# open（7-8）/类型变量（9）；若泄漏会多 queuey→logging 边或行号漂移
QUEUEY_ML = (
    "(* (open Logging) *)\n"                   # 1
    '(* "*)" open Logging *)\n'                 # 2
    'let s = "text (open Logging) more"\n'      # 3
    "let q = {sql|open Logging|sql}\n"          # 4
    "let c = 'a'\n"                             # 5
    "let d = '\\n'\n"                           # 6
    'let m = "line1\n'                          # 7 串开（跨行）
    '(open Logging)"\n'                         # 8 串闭——内伪 open 不算
    "let f (x : 'a) = x\n"                      # 9
    "open Logging\n"                            # 10 真违例 open
    "let run () = Logging.msg 1\n"              # 11 真依赖 ref
)

# 修复版：保留全部 canary——修复后 canary 也不得复活违例
QUEUEY_FIXED_ML = (
    "(* (open Logging) *)\n"
    '(* "*)" open Logging *)\n'
    'let s = "text (open Logging) more"\n'
    "let q = {sql|open Logging|sql}\n"
    "let c = 'a'\n"
    "let d = '\\n'\n"
    'let m = "line1\n'
    '(open Logging)"\n'
    "let f (x : 'a) = x\n"
    "let run () = 0\n"
)


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g1', 'p', 'repo.queuey', 'Module', 'queuey', 'src/queuey.ml', 1, 11, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'repo.logging', 'Module', 'logging', 'src/logging.ml', 1, 2, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "src").mkdir(parents=True)
    (repo / "test").mkdir(parents=True)
    (repo / "src" / "queuey.ml").write_text(QUEUEY_ML, encoding="utf-8")
    (repo / "src" / "logging.ml").write_text("let msg x = x\n", encoding="utf-8")
    # 测试文件：test/ 目录约定蓄意 open 双模块——排除后不产边（若泄漏会引
    # 入 tests_excluded 计数漂移与伪边）
    (repo / "test" / "queuey_test.ml").write_text(
        "open Queuey\nopen Logging\n", encoding="utf-8"
    )
    return repo


def _rows(repo: Path) -> list[tuple]:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        return con.execute(
            "SELECT pathology, kind, source, anchor_fid, file_path, start_line, payload_json"
            " FROM findings WHERE source = 's7-enforce'"
        ).fetchall()
    finally:
        con.close()


def test_enforce_ocaml_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "ocaml-module-parser"  # 语言路由命中 OCaml 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # queuey.ml + logging.ml + queuey_test.ml（扫描计数诚实）
    assert out["modules"] == 3  # queuey/logging/测试编译单元容器入图（叶可见）
    assert out["tests_excluded"] == 1  # test/queuey_test.ml 文件级排除
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 open 文件首符号
    assert file_path == "src/queuey.ml"
    assert line == 10  # 违例 open 行（canary 8 处不算：注释/注释内串/串/quoted/字符/跨行串/类型变量）
    data = json.loads(payload)
    assert data["source_layer"] == "app"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "src/queuey.ml"  # 容器 = 编译单元文件
    assert data["imported"] == "src/logging.ml"
    assert data["chain"] == ["src/queuey.ml", "src/logging.ml"]
    assert data["line"] == 10


def test_enforce_ocaml_e2e_transitive_and_test_file_excluded(tmp_path):
    """三层链 storage → queuey → logging + 测试文件依赖不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "src" / "storage.ml").write_text("open Queuey\n", encoding="utf-8")
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["g3"], "layer": "core"},
                {"module_id": "app-mod", "name": "队列", "responsibility": "队", "symbol_fids": ["g1"], "layer": "app"},
                {"module_id": "iface-mod", "name": "日志", "responsibility": "志", "symbol_fids": ["g2"], "layer": "interface"},
            ],
            "layers": [
                {"name": "interface", "may_depend_on": ["app", "core"]},
                {"name": "app", "may_depend_on": ["core"]},
                {"name": "core", "may_depend_on": []},
            ],
        },
        ensure_ascii=False,
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g3', 'p', 'repo.storage', 'Module', 'storage', 'src/storage.ml', 1, 2, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 4
    assert out["modules"] == 4
    assert out["tests_excluded"] == 1
    assert out["violations"] == 3  # queuey→logging 直连 + storage→queuey 直连 + storage 经 queuey 传递
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("src/queuey.ml", "src/logging.ml")]["chain"] == ["src/queuey.ml", "src/logging.ml"]
    assert by_pair[("src/storage.ml", "src/queuey.ml")]["chain"] == ["src/storage.ml", "src/queuey.ml"]
    assert by_pair[("src/storage.ml", "src/logging.ml")]["chain"] == ["src/storage.ml", "src/queuey.ml", "src/logging.ml"]
    # 锚定：queuey/storage→logging 锚 queuey.ml:10（链尾直接边）；storage→queuey 锚 storage.ml:1
    anchored = {(r[4], r[5]) for r in rows}
    assert anchored == {("src/queuey.ml", 10), ("src/storage.ml", 1)}


def test_enforce_ocaml_e2e_functor_arg_edge(tmp_path):
    """functor 应用实参产边（`module Impl = Make(Logging)` 锚实参行）；
    串内伪 open/类型变量 canary 不复活。"""
    repo = _mk_repo(tmp_path)
    (repo / "src" / "queuey.ml").write_text(
        'let s = "text (open Logging) more"\n'   # 1 串内伪 open canary
        "module Impl = Make(Logging)\n"          # 2 functor 实参真违例
        "let f (x : 'a) = x\n",                  # 3 类型变量 canary
        encoding="utf-8",
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    assert rows[0][5] == 2  # 锚定 functor 实参行（canary 不漂移）
    data = json.loads(rows[0][6])
    assert data["chain"] == ["src/queuey.ml", "src/logging.ml"]
    assert data["line"] == 2


def test_enforce_ocaml_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_ocaml_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 open（保留词法 canary——修复后 canary 也不得复活违例）
        (repo / "src" / "queuey.ml").write_text(QUEUEY_FIXED_ML, encoding="utf-8")
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_ocaml_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "queuey.ml").write_text(
        "let run () = 0\n", encoding="utf-8"
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    assert out["findings_written"] == 0
    assert _rows(repo) == []
