"""S7 OCaml 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/ocaml-enforce-probe）。

引擎无外部执法器（内置源级 module/open 解析），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_ocaml

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

OCAML_CONTAINERS = {"queue-mod": ["src/queuey.ml"], "logging-mod": ["src/logging.ml"]}

# 两层：app(queuey) ← interface(logging)；违例 = queuey 上向 logging
OCAML_STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "queue-mod", "name": "队列", "responsibility": "队", "symbol_fids": ["f1"], "layer": "app"},
            {"module_id": "logging-mod", "name": "日志", "responsibility": "志", "symbol_fids": ["f2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "interface", "may_depend_on": ["app"]},
            {"name": "app", "may_depend_on": []},
        ],
    },
    ensure_ascii=False,
)


def _mk_ocaml_repo(tmp_path: Path, structure: str | None = OCAML_STRUCTURE) -> Path:
    repo = tmp_path / "ocamlrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'ocamlrepo.queuey', 'Module', 'queuey', 'src/queuey.ml', 1, 2, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'ocamlrepo.logging', 'Module', 'logging', 'src/logging.ml', 1, 1, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "src").mkdir(parents=True)
    # 违例：第 1 行 open Logging（边锚定目标）
    (repo / "src" / "queuey.ml").write_text(
        "open Logging\nlet run () = Logging.msg 1\n",
        encoding="utf-8",
    )
    (repo / "src" / "logging.ml").write_text(
        "let msg x = x\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


def _extract(src: str) -> list[dict]:
    """词法 → 依赖行。"""
    return s7_enforce_ocaml._iter_refs(s7_enforce_ocaml._blank_noncode(src))


# ------------------------------------------------------------------ unit


def test_blank_noncode_canary_forms():
    src = (
        '(* (open Hidden) *)\n'  # 注释 canary
        'let s = "text (open In.Str) more"\n'  # 串内容 canary
        "let c = 'a'\n"  # 字符字面量 canary
        'open Kept\n'  # 真 open
    )
    blanked = s7_enforce_ocaml._blank_noncode(src)
    assert "open Kept" in blanked  # 代码存活
    assert "Hidden" not in blanked  # 注释空白化
    assert "In.Str" not in blanked  # 串内容空白化
    assert "'a'" not in blanked  # 字符字面量空白化
    assert len(blanked) == len(src)  # 等长空白
    assert blanked.count("\n") == src.count("\n")  # 换行保留


def test_blank_nested_comment():
    """OCaml 注释可嵌套（depth 计数）——外层闭合需配对内层。"""
    src = '(* outer (* nested *) tail *)\nopen After\n'
    blanked = s7_enforce_ocaml._blank_noncode(src)
    assert "outer" not in blanked and "nested" not in blanked and "tail" not in blanked
    assert "open After" in blanked


def test_comment_string_close_inside():
    """注释内串/quoted string 整体跳过——`*)` 在注释内串里不闭注释。"""
    src = '(* "*)" *)\nopen After\n(* {|*)|} *)\nopen After2\n'
    blanked = s7_enforce_ocaml._blank_noncode(src)
    assert "open After" in blanked
    assert "open After2" in blanked


def test_blank_unclosed_string_swallows_eof():
    """OCaml 串合法跨行：未闭合吞 EOF（保守）；换行保留。"""
    src = 'let s = "unclosed\nopen Swallowed\n'
    blanked = s7_enforce_ocaml._blank_noncode(src)
    assert "Swallowed" not in blanked
    assert blanked.count("\n") == src.count("\n")


def test_blank_unclosed_comment_truncates():
    src = '(* unclosed\nopen Gone\n'
    blanked = s7_enforce_ocaml._blank_noncode(src)
    assert "Gone" not in blanked
    assert blanked.count("\n") == src.count("\n")


def test_char_literals_and_typevars():
    """`'c'` 字符字面量全转义形态；`'a` 类型变量不吞（歧义消解）。"""
    src = (
        "let a = 'a'\n"
        "let b = '\\n'\n"
        "let c = '\\''\n"
        "let d = '\\x41'\n"
        "let e = '\\065'\n"
        "(x : 'a) list\n"
        "open After\n"
    )
    blanked = s7_enforce_ocaml._blank_noncode(src)
    assert "open After" in blanked  # 无假串/假字符吞代码
    assert "'a'" not in blanked  # 字符字面量整体空白化
    assert "'\\n'" not in blanked
    assert "'\\x41'" not in blanked
    assert "(x : 'a) list" in blanked  # 类型变量保留


def test_quoted_string_forms():
    """`{id|...|id}` quoted string（同 id 嵌套、未闭合吞 EOF）；记录非 quoted。"""
    src = (
        'let q = {sql|SELECT *) FROM|sql}\n'
        'let r = {|raw {|nested|} tail|}\n'
        'let s = {x = 1; y = 2}\n'
        'open After\n'
    )
    blanked = s7_enforce_ocaml._blank_noncode(src)
    assert "SELECT" not in blanked  # quoted 内容空白化
    assert "raw" not in blanked  # 嵌套 quoted 整体空白化
    assert "{x = 1; y = 2}" in blanked  # 记录字面量非 quoted string
    assert "open After" in blanked
    unclosed = 'let u = {|unclosed\nopen Gone\n'
    b2 = s7_enforce_ocaml._blank_noncode(unclosed)
    assert "Gone" not in b2  # 未闭合 quoted 吞 EOF


def test_blank_shebang():
    src = '#!/usr/bin/env ocaml\nopen After\n'
    blanked = s7_enforce_ocaml._blank_noncode(src)
    assert "/usr/bin" not in blanked  # shebang 行空白化
    assert "open After" in blanked


def test_extract_open_include_alias_matrix():
    """open/open!/let open/include/module(rec/let module) 别名提取矩阵。"""
    src = (
        "open Foo\n"                      # 1
        "open! Bar\n"                     # 2
        "let open Baz in x\n"             # 3
        "include Quux\n"                  # 4
        "module M = Dep\n"                # 5
        "module rec MR = Dep2\n"          # 6
        "let module LM = Dep3 in y\n"     # 7
    )
    rows = _extract(src)
    assert rows == [
        {"kind": "open", "target": "Foo", "line": 1},
        {"kind": "open", "target": "Bar", "line": 2},
        {"kind": "open", "target": "Baz", "line": 3},
        {"kind": "include", "target": "Quux", "line": 4},
        {"kind": "alias", "target": "Dep", "line": 5},
        {"kind": "alias", "target": "Dep2", "line": 6},
        {"kind": "alias", "target": "Dep3", "line": 7},
    ]


def test_extract_ref_paths_and_constructors():
    """大写路径访问取路径头；构造器（无点）天然免疫；外部模块照提（建图跳过）。"""
    src = (
        "Foo.bar 1\n"          # 1
        "M.(f x)\n"            # 2
        "M.{a = 1}\n"          # 3
        "M.Sub.baz\n"          # 4
        "Stdlib.List.map f\n"  # 5
        "let a = Some 1\n"     # 6
        "let b = Ok 2\n"       # 7
    )
    rows = _extract(src)
    refs = [(r["target"], r["line"]) for r in rows if r["kind"] == "ref"]
    assert refs == [("Foo", 1), ("M", 2), ("M", 3), ("M", 4), ("Stdlib", 5)]
    assert not any(r["target"] in ("Some", "Ok") for r in rows)  # 构造器不产边


def test_extract_guards_and_non_modules():
    """struct/functor/module type/署名别名不误取；成员 `.open` 与 `open_` 标识符守卫。"""
    src = (
        "module Sub = struct end\n"  # RHS 非大写路径
        "module type T = sig end\n"  # 模块类型≠编译单元
        "module F (A : T) = A\n"     # functor
        "module M : S = Dep\n"       # 署名别名不提取（文档化局限）
        "c.open Foo\n"               # 成员访问守卫
        "open_Foo.close\n"           # open_ 标识符守卫
        "a.reopen b\n"               # reopen 前缀标识符守卫
    )
    rows = _extract(src)
    assert rows == []


def test_is_test_conventions():
    assert s7_enforce_ocaml._is_test("test/api.ml")  # dune test/ 标准布局
    assert s7_enforce_ocaml._is_test("tests/core_test.ml")
    assert s7_enforce_ocaml._is_test("src/api_test.ml")  # alcotest *_test.ml 惯例
    assert s7_enforce_ocaml._is_test("src/api_test.mli")
    assert s7_enforce_ocaml._is_test("src/test.ml")  # 裸 test.ml
    assert s7_enforce_ocaml._is_test("test.ml")
    assert not s7_enforce_ocaml._is_test("src/api.ml")
    assert not s7_enforce_ocaml._is_test("src/Testing/x.ml")  # 目录段精确小写
    assert not s7_enforce_ocaml._is_test("src/testing/x.ml")
    assert not s7_enforce_ocaml._is_test("src/latest.ml")
    assert not s7_enforce_ocaml._is_test("src/apitest.ml")
    assert not s7_enforce_ocaml._is_test("src/api.py")  # 非 .ml/.mli


def test_scan_ocaml_layout(tmp_path):
    repo = tmp_path / "proj"
    for d in ("src", "test", "tests", "_build", "_opam", ".git", "node_modules", "vendor"):
        (repo / d).mkdir(parents=True)
    (repo / "src" / "a.ml").write_text("open B\nlet _ = B.x\n", encoding="utf-8")
    (repo / "src" / "b.ml").write_text("let x = 1\n", encoding="utf-8")
    (repo / "src" / "b.mli").write_text("val x : int\n", encoding="utf-8")
    (repo / "test" / "a_test.ml").write_text("open A\nopen B\n", encoding="utf-8")
    (repo / "tests" / "c_test.ml").write_text("open A\n", encoding="utf-8")
    for rel in ("_build/x.ml", "_opam/y.ml", ".git/z.ml", "node_modules/n.ml", "vendor/v.ml"):
        (repo / rel).write_text("open A\n", encoding="utf-8")
    (repo / "notes.txt").write_text("open Txt\n", encoding="utf-8")
    file_set, ref_rows, files_scanned, tests_excluded, timed_out = s7_enforce_ocaml.scan_ocaml(repo)
    assert timed_out is False
    assert files_scanned == 5  # _build/_opam/.git/node_modules/vendor 跳过
    assert tests_excluded == 2  # test/a_test.ml + tests/c_test.ml
    assert file_set == {"src/a.ml", "src/b.ml", "src/b.mli", "test/a_test.ml", "tests/c_test.ml"}
    assert ref_rows == [  # 测试依赖不入列
        {"file": "src/a.ml", "kind": "open", "target": "B", "line": 1},
        {"file": "src/a.ml", "kind": "ref", "target": "B", "line": 2},
    ]


def test_scan_ocaml_timeout(tmp_path):
    repo = _mk_ocaml_repo(tmp_path)
    _fs, _r, _files, _e, timed_out = s7_enforce_ocaml.scan_ocaml(repo, timeout=0)
    assert timed_out is True


def test_build_ocaml_graph_module_index():
    """词干索引（.ml 优先 .mli）、同编译单元自引用跳过、路径头解析、外部跳过。"""
    file_set = {"src/api.ml", "src/api.mli", "src/util.ml", "src/bar.ml", "src/dep.ml", "src/dep.mli"}
    ref_rows = [
        {"file": "src/api.mli", "line": 1, "kind": "ref", "target": "Api"},  # 同编译单元自引用
        {"file": "src/util.ml", "line": 2, "kind": "ref", "target": "Api"},  # .ml 优先命中
        {"file": "src/util.ml", "line": 3, "kind": "open", "target": "Bar.Sub"},  # 路径头解析
        {"file": "src/bar.ml", "line": 4, "kind": "ref", "target": "Stdlib"},  # 外部跳过
        {"file": "src/bar.ml", "line": 5, "kind": "ref", "target": "Dep"},  # .ml 优先 .mli
    ]
    graph, edges = s7_enforce_ocaml.build_ocaml_graph(file_set, ref_rows)
    assert graph == {
        "src/api.ml": [],
        "src/api.mli": [],
        "src/bar.ml": ["src/dep.ml"],
        "src/dep.ml": [],
        "src/dep.mli": [],
        "src/util.ml": ["src/api.ml", "src/bar.ml"],
    }
    assert edges == [
        {"file": "src/util.ml", "line": 2, "src": "src/util.ml", "dst": "src/api.ml"},
        {"file": "src/util.ml", "line": 3, "src": "src/util.ml", "dst": "src/bar.ml"},
        {"file": "src/bar.ml", "line": 5, "src": "src/bar.ml", "dst": "src/dep.ml"},
    ]


def test_belongs_exact_match():
    assert s7_enforce_ocaml._belongs("src/queuey.ml", ["src/queuey.ml"])
    assert not s7_enforce_ocaml._belongs("src/queuey2.ml", ["src/queuey.ml"])  # 前缀污染守卫
    assert not s7_enforce_ocaml._belongs("x", ["src/queuey.ml"])


def test_find_violations_bfs_shortest_chain():
    graph = {
        "app.ml": ["mid.ml", "queue.ml"],
        "mid.ml": ["iface.ml"],  # 2 跳
        "queue.ml": ["mid.ml"],  # 3 跳路径
        "iface.ml": [],
        "other.ml": [],
    }
    contracts = [
        {
            "name": "layer: app must not import interface",
            "source_layer": "app",
            "forbidden_layer": "interface",
            "source_modules": ["app.ml"],
            "forbidden_modules": ["iface.ml"],
        }
    ]
    violations = s7_enforce_ocaml._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "app.ml" and v["imported"] == "iface.ml"
    assert v["chain"] == ["app.ml", "mid.ml", "iface.ml"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶容器（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"app.ml": ["leaf.ml"], "leaf.ml": []}
    contracts = [{"name": "n", "source_modules": ["app.ml"], "forbidden_modules": ["leaf.ml"]}]
    violations = s7_enforce_ocaml._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["app.ml", "leaf.ml"]


def test_find_violations_none_when_unreachable():
    graph = {"app.ml": ["mid.ml"], "mid.ml": [], "iface.ml": []}
    contracts = [{"name": "n", "source_modules": ["app.ml"], "forbidden_modules": ["iface.ml"]}]
    assert s7_enforce_ocaml._find_violations(graph, contracts) == []


# ------------------------------------------------- derive / run_ocaml E2E


def test_derive_ocaml_containers(tmp_path):
    repo = _mk_ocaml_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_ocaml.derive_ocaml_containers(con, json.loads(OCAML_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == OCAML_CONTAINERS
    assert derived["ocaml_files"] == 2  # src/queuey.ml + logging.ml
    assert derived["extensions"] == []


def test_run_ocaml_violation_e2e(tmp_path):
    repo = _mk_ocaml_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_ocaml.run_ocaml(repo, con, json.loads(OCAML_STRUCTURE), "active", OCAML_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "ocaml-module-parser"
    assert out["contracts"] == 1
    assert out["files"] == 2
    assert out["modules"] == 2
    assert out["tests_excluded"] == 0
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    row = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT pathology, kind, source, anchor_fid, file_path, start_line, end_line, payload_json"
        " FROM findings"
    ).fetchone()
    assert row[0] == "P9"
    assert row[1] == "layer-violation"
    assert row[2] == "s7-enforce"
    assert row[3] == "f1"  # src/queuey.ml 首符号
    assert row[4] == "src/queuey.ml"
    assert row[5] == 1 and row[6] == 1  # 违例 open 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "app"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "src/queuey.ml"  # 容器 = 编译单元文件
    assert payload["imported"] == "src/logging.ml"
    assert payload["chain"] == ["src/queuey.ml", "src/logging.ml"]
    assert payload["line"] == 1


def test_run_ocaml_transitive_e2e(tmp_path):
    """传递链 storage → queuey → logging（文件容器中间层）。"""
    repo = _mk_ocaml_repo(tmp_path)
    (repo / "src" / "storage.ml").write_text("open Queuey\n", encoding="utf-8")
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
                {"module_id": "iface-mod", "name": "日志", "responsibility": "志", "symbol_fids": ["f2"], "layer": "interface"},
            ],
            "layers": [
                {"name": "core", "may_depend_on": []},
                {"name": "interface", "may_depend_on": ["core"]},
            ],
        },
        ensure_ascii=False,
    )
    con = _con(repo)
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    con.close()
    payload = json.loads(structure)
    containers = {"core-mod": ["src/storage.ml", "src/queuey.ml"], "iface-mod": ["src/logging.ml"]}
    con = _con(repo)
    try:
        out = s7_enforce_ocaml.run_ocaml(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queuey→logging 直连 + storage 经 queuey 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["src/queuey.ml"]["chain"] == ["src/queuey.ml", "src/logging.ml"]
    assert by_importer["src/storage.ml"]["chain"] == ["src/storage.ml", "src/queuey.ml", "src/logging.ml"]
    assert by_importer["src/storage.ml"]["line"] == 1  # 锚定链尾直接边：queuey.ml open 行
    assert all(r[0] == "src/queuey.ml" for r in rows)


def test_run_ocaml_idempotent(tmp_path):
    repo = _mk_ocaml_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_ocaml.run_ocaml(repo, con, json.loads(OCAML_STRUCTURE), "active", OCAML_CONTAINERS)
        second = s7_enforce_ocaml.run_ocaml(repo, con, json.loads(OCAML_STRUCTURE), "active", OCAML_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_ocaml_compliant_clears_old_findings(tmp_path):
    repo = _mk_ocaml_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例依赖
    (repo / "src" / "queuey.ml").write_text(
        "let run () = 0\n", encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_ocaml.run_ocaml(repo, con, json.loads(OCAML_STRUCTURE), "active", OCAML_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_ocaml_no_files(tmp_path):
    repo = _mk_ocaml_repo(tmp_path)
    (repo / "src" / "queuey.ml").unlink()
    (repo / "src" / "logging.ml").unlink()
    con = _con(repo)
    try:
        out = s7_enforce_ocaml.run_ocaml(repo, con, json.loads(OCAML_STRUCTURE), "active", OCAML_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no ocaml files found" in out["reason"]


def test_run_ocaml_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_ocaml_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_ocaml,
        "scan_ocaml",
        lambda repo, timeout=600: (set(), [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_ocaml.run_ocaml(repo, con, json.loads(OCAML_STRUCTURE), "active", OCAML_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "ocaml scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_ocaml_test_file_excluded_e2e(tmp_path):
    """测试文件（test/ 目录 + *_test.ml 约定）依赖不产边；排除计数诚实上报。"""
    repo = _mk_ocaml_repo(tmp_path)
    (repo / "test").mkdir(parents=True)
    (repo / "test" / "queuey_test.ml").write_text(
        "open Queuey\nopen Logging\n", encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_ocaml.run_ocaml(repo, con, json.loads(OCAML_STRUCTURE), "active", OCAML_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["tests_excluded"] == 1
    assert out["files"] == 3  # files_scanned 含测试文件
    assert out["modules"] == 3  # 生产 queuey/logging + 测试容器入图（叶可见）
    assert out["violations"] == 1  # 仅生产 open 产违例


# ------------------------------------------------------ run() 路由


def test_run_routes_ocaml(tmp_path):
    """纯 OCaml 结构（.ml）→ run() 路由至 OCaml 引擎。"""
    repo = _mk_ocaml_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "ocaml-module-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_ocaml_and_py_not_enforceable(tmp_path):
    repo = _mk_ocaml_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'ocamlrepo.helper.mod', 'Function', 'mod', 'helper/mod.py', 1, 2, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps({
        "modules": [
            {"module_id": "queue-mod", "name": "队列", "responsibility": "队", "symbol_fids": ["f1"], "layer": "app"},
            {"module_id": "logging-mod", "name": "日志", "responsibility": "志", "symbol_fids": ["f2"], "layer": "interface"},
            {"module_id": "py-mod", "name": "杂", "responsibility": "混", "symbol_fids": ["f3"], "layer": "app"},
        ],
        "layers": [
            {"name": "interface", "may_depend_on": ["app"]},
            {"name": "app", "may_depend_on": []},
        ],
    }, ensure_ascii=False),))
    con.commit()
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "mixed-language" in out["reason"]
    assert "py + ocaml" in out["reason"]


def test_cli_enforce_ocaml_wiring(tmp_path, capsys):
    repo = _mk_ocaml_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["engine"] == "ocaml-module-parser"
    assert out["violations"] == 1
