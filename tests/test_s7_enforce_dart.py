"""S7 Dart 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/dart-enforce-probe）。

引擎无外部执法器（内置源级 import URI 解析器），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import shutil
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_dart

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

DART_CONTAINERS = {"core-mod": ["lib/src/storage.dart"], "iface-mod": ["lib/src/api.dart"]}

# 两层：core(lib/src/storage.dart) ← interface(lib/src/api.dart)；违例 = storage 上向 import api
DART_STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    },
    ensure_ascii=False,
)


def _mk_dart_repo(tmp_path: Path, structure: str | None = DART_STRUCTURE) -> Path:
    repo = tmp_path / "dartrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'probe.lib.src.storage', 'Module', 'storage', 'lib/src/storage.dart', 3, 9, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'probe.lib.src.api', 'Module', 'api', 'lib/src/api.dart', 1, 4, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "lib" / "src").mkdir(parents=True)
    (repo / "lib" / "src" / "api.dart").write_text(
        "int apiVersion() {\n"
        "  return 1;\n"
        "}\n",
        encoding="utf-8",
    )
    # 违例：第 1 行 import api（import 行号锚定目标）
    (repo / "lib" / "src" / "storage.dart").write_text(
        "import 'api.dart';\n\n"
        "int storagePut(String key, String value) {\n"
        "  if (key.isEmpty) return 1;\n"
        "  return apiVersion() > 0 ? 0 : 1;\n"
        "}\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode():
    src = (
        "import 'a.dart'; // line comment\n"
        "/* outer /* inner */ tail */ var x = 1;\n"
        "s = 'single $var str';\n"
        't = "double ${x} str";\n'
        "raw = r'no\\escape';\n"
        "export 'b.dart' show Foo;\n"
        "part 'p.dart';\n"
        "part of 'owner.dart';\n"
        "f(import: 'x.dart');\n"
        "obj.part = 2;\n"
    )
    out = s7_enforce_dart._blank_noncode(src)
    lines = out.split("\n")
    assert lines[0].startswith("import 'a.dart';")  # 行注释空白化，指令目标保留
    assert "outer" not in out and "inner" not in out  # 嵌套块注释整段空白化
    assert "tail */" not in out and "var x = 1;" in out  # 嵌套闭合后代码保留
    assert "single" not in out  # 单引号串空白化（插值记号不穿透）
    assert "double" not in out  # 双引号插值串空白化
    assert "no" not in out.split("raw = r")[1].split(";")[0]  # raw 串空白化
    assert "export 'b.dart'" in out  # export 指令目标保留
    assert "part 'p.dart';" in out  # part 指令目标保留
    assert "owner.dart" not in out  # part of 的串不保留（of 挡住目标判定）
    assert "x.dart" not in out  # 命名实参 import: 的串空白化
    assert "obj.part = 2;" in out  # 成员访问 part 不是指令
    assert len(out) == len(src)  # 等长空白
    assert out.count("\n") == src.count("\n")  # 换行保留


def test_blank_nested_block_comment_unterminated():
    src = "/* outer /* inner never closed\nvar keep = 1;\n"
    out = s7_enforce_dart._blank_noncode(src)
    assert "inner" not in out  # 未闭合嵌套注释截断文件尾（前进保证）
    assert "keep" not in out  # 注释体吞没后续代码（WIP 保守处理）


def test_blank_raw_string_closes_without_escape():
    src = "a = r'back\\slash';\nb = r\"dq\\x\";\nc = 1;\n"
    out = s7_enforce_dart._blank_noncode(src)
    assert "back" not in out  # raw 单引号串：反斜杠不转义，引号闭合
    assert "dq" not in out  # raw 双引号串
    assert "c = 1;" in out
    assert len(out) == len(src)


def test_blank_triple_quoted_strings():
    src = (
        "a = '''tri\n"
        "import 'fake.dart';\n"
        "''';\n"
        'b = """dq\n'
        "export 'fake2.dart';\n"
        '""";\n'
        "c = 2;\n"
    )
    out = s7_enforce_dart._blank_noncode(src)
    assert "fake.dart" not in out  # ''' 体空白化（内嵌单引号不闭合）
    assert "fake2.dart" not in out  # """ 体空白化
    assert "tri" not in out and "dq" not in out
    assert "c = 2;" in out
    assert len(out) == len(src)


def test_blank_unclosed_single_line_string():
    src = "a = 'unclosed\nb = 2;\n"
    out = s7_enforce_dart._blank_noncode(src)
    assert "unclosed" not in out  # 未闭合单行串截断行尾
    assert "b = 2;" in out  # 后续代码不受损


def test_blank_raw_triple_backslash_before_close():
    # raw 三引号串尾反斜杠不转义闭引号（Dart 语义）——review 批修正回归
    src = "var p = r'''C:\\dir\\''';\nimport 'real.dart';\n"
    out = s7_enforce_dart._blank_noncode(src)
    assert "dir" not in out  # raw 三引号体空白化（\''' 原文闭三引号终止）
    assert "import 'real.dart';" in out  # 后续真指令保留（原整文件假阴性）
    assert len(out) == len(src)


def test_blank_nonraw_triple_backslash_escape_stays_open():
    # 非 raw 对照：尾 \' 是转义——闭三引号未闭合，串体延伸吞后续（Dart 语义）
    src = "var p = '''C:\\dir\\''';\nimport 'gone.dart';\n"
    out = s7_enforce_dart._blank_noncode(src)
    assert "gone.dart" not in out  # 仍在串体内（合法语义，非 bug）
    assert "dir" not in out


def test_iter_directives_comment_in_gap():
    # 指令关键字-目标间隙注释：URI 保留——review 批修正回归
    blanked = s7_enforce_dart._blank_noncode(
        "import /* block */ 'x.dart';\n"
        "import // line\n    'y.dart';\n"
        "export /* c */ r'z.dart';\n"
        "part /* p */ 'piece.dart';\n"
    )
    rows = s7_enforce_dart._iter_directives(blanked)
    got = {(r["kind"], r["uri"], r["line"]) for r in rows}
    assert ("import", "x.dart", 1) in got  # 块注释间隙（原 URI 空白化丢边）
    assert ("import", "y.dart", 2) in got  # 行注释间隙（行号=关键字行）
    assert ("export", "z.dart", 4) in got  # 注释+raw 前缀组合
    assert ("part", "piece.dart", 5) in got
    assert len(rows) == 4


def test_iter_directives_bom_immune():
    # UTF-8 BOM 首行指令不误伤（\ufeff 非阻断字符，\b 词界对 Cf 类成立）
    blanked = s7_enforce_dart._blank_noncode("\ufeffimport 'bom.dart';\n")
    rows = s7_enforce_dart._iter_directives(blanked)
    assert [(r["kind"], r["uri"], r["line"]) for r in rows] == [("import", "bom.dart", 1)]


def test_iter_directives_forms():
    blanked = s7_enforce_dart._blank_noncode(
        "import 'package:probe/src/api.dart';\n"
        "import 'dart:async';\n"
        "import '../rel/thing.dart' as t show A, B hide C;\n"
        "import 'default.dart' if (dart.library.io) 'io.dart';\n"
        "export 'exp.dart';\n"
        "part 'part.dart';\n"
        "import r'raw_uri.dart';\n"
        "import 'interp_${name}.dart';\n"
        "import 'deferred.dart' deferred as d;\n"
    )
    rows = s7_enforce_dart._iter_directives(blanked)
    got = [(r["kind"], r["uri"], r["line"]) for r in rows]
    assert ("import", "package:probe/src/api.dart", 1) in got
    assert ("import", "dart:async", 2) in got  # SDK 目标解析时跳过，此处保留原始 URI
    assert ("import", "../rel/thing.dart", 3) in got  # as/show/hide 子句不碍解析
    assert ("import", "default.dart", 4) in got  # 条件 import 取默认 URI
    assert ("export", "exp.dart", 5) in got
    assert ("part", "part.dart", 6) in got
    assert ("import", "raw_uri.dart", 7) in got  # raw 前缀 URI
    assert ("import", "deferred.dart", 9) in got  # deferred 后缀子句
    assert not any(r["uri"].startswith("interp_") for r in rows)  # 插值 URI（动态依赖）跳过
    assert not any(r["uri"] == "io.dart" for r in rows)  # 条件分支 URI 不入列


def test_iter_directives_pseudo_forms_lexer_guarded():
    blanked = s7_enforce_dart._blank_noncode(
        "final s = \"import 'package:probe/src/contract.dart';\";\n"
        "// import 'package:probe/src/contract.dart';\n"
        "final t = '''\n"
        "import 'package:probe/src/contract.dart';\n"
        "''';\n"
        "import 'real.dart';\n"
    )
    rows = s7_enforce_dart._iter_directives(blanked)
    assert [(r["kind"], r["uri"]) for r in rows] == [("import", "real.dart")]  # 伪 import（字符串/注释/三引号）不出现


def test_is_test_conventions():
    assert s7_enforce_dart._is_test("test/store_test.dart") is True
    assert s7_enforce_dart._is_test("tests/helper.dart") is True
    assert s7_enforce_dart._is_test("integration_test/app_test.dart") is True
    assert s7_enforce_dart._is_test("lib/src/store_test.dart") is True
    assert s7_enforce_dart._is_test("lib/src/store.dart") is False
    assert s7_enforce_dart._is_test("lib/testing/store.dart") is False  # 目录段精确匹配


def test_scan_dart_layout(tmp_path):
    repo = tmp_path / "proj"
    (repo / "lib" / "src").mkdir(parents=True)
    (repo / "lib" / "src" / "storage.dart").write_text(
        "import 'api.dart';\n\nint storagePut() => 1;\n", encoding="utf-8"
    )
    (repo / "lib" / "src" / "api.dart").write_text(
        "int apiVersion() => 1;\n", encoding="utf-8"
    )
    # 测试文件：容器仍入表（叶可见），import 不入图
    (repo / "test").mkdir()
    (repo / "test" / "storage_test.dart").write_text(
        "import '../lib/src/api.dart';\n\nvoid main() {}\n", encoding="utf-8"
    )
    # 产物/依赖目录：整体跳过
    (repo / "build").mkdir()
    (repo / "build" / "gen.dart").write_text(
        "import 'package:probe/src/api.dart';\n", encoding="utf-8"
    )
    (repo / ".dart_tool").mkdir()
    (repo / ".dart_tool" / "t.dart").write_text("var x = 1;\n", encoding="utf-8")
    file_set, part_map, import_rows, files, tests_excluded, timed_out = s7_enforce_dart.scan_dart(repo)
    assert timed_out is False
    assert file_set == {"lib/src/storage.dart", "lib/src/api.dart", "test/storage_test.dart"}
    assert part_map == {}
    assert tests_excluded == 1
    assert files == 3  # 扫描计数诚实（build/.dart_tool 跳过）
    assert import_rows == [
        {"kind": "import", "uri": "api.dart", "line": 1, "file": "lib/src/storage.dart"}
    ]  # 测试 import 不入列


def test_scan_dart_part_map(tmp_path):
    repo = tmp_path / "proj"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "lib.dart").write_text(
        "library;\n\npart 'src/part.dart';\n\nint libFn() => 1;\n", encoding="utf-8"
    )
    (repo / "lib" / "src").mkdir()
    (repo / "lib" / "src" / "part.dart").write_text(
        "part of '../lib.dart';\n\nint partFn() => 2;\n", encoding="utf-8"
    )
    file_set, part_map, import_rows, files, tests_excluded, timed_out = s7_enforce_dart.scan_dart(repo)
    assert timed_out is False
    assert file_set == {"lib/lib.dart", "lib/src/part.dart"}
    assert part_map == {"lib/src/part.dart": "lib/lib.dart"}  # owner 侧 part 指令权威
    assert import_rows == []  # part/part of 不产依赖边


def test_scan_dart_timeout(tmp_path):
    repo = _mk_dart_repo(tmp_path)
    file_set, part_map, import_rows, files, tests, timed_out = s7_enforce_dart.scan_dart(repo, timeout=0)
    assert timed_out is True


def test_resolve_uri_relative():
    repo = Path("Z:/nowhere")
    file_set = {"lib/src/api.dart", "lib/src/store/storage.dart", "lib/x.dart"}
    assert s7_enforce_dart._resolve_uri(repo, "lib/src/store/storage.dart", "../api.dart", file_set) == "lib/src/api.dart"
    assert s7_enforce_dart._resolve_uri(repo, "lib/src/store/storage.dart", "api.dart", file_set) is None  # 同目录未命中
    assert s7_enforce_dart._resolve_uri(repo, "lib/x.dart", "src/api.dart", file_set) == "lib/src/api.dart"
    assert s7_enforce_dart._resolve_uri(repo, "lib/x.dart", "../../outside.dart", file_set) is None  # 逃逸仓外
    assert s7_enforce_dart._resolve_uri(repo, "lib/x.dart", "missing.dart", file_set) is None
    assert s7_enforce_dart._resolve_uri(repo, "lib/x.dart", "dart:async", file_set) is None  # SDK


def test_resolve_uri_package(tmp_path):
    repo = tmp_path / "pkg"
    (repo / "lib" / "src").mkdir(parents=True)
    (repo / "pubspec.yaml").write_text("name: probe\n", encoding="utf-8")
    (repo / "lib" / "src" / "api.dart").write_text("", encoding="utf-8")
    file_set = {"lib/src/api.dart"}
    assert s7_enforce_dart._resolve_uri(repo, "lib/src/storage.dart", "package:probe/src/api.dart", file_set) == "lib/src/api.dart"
    assert s7_enforce_dart._resolve_uri(repo, "lib/src/storage.dart", "package:other/src/api.dart", file_set) is None  # 外部包
    assert s7_enforce_dart._resolve_uri(repo, "lib/src/storage.dart", "package:probe/src/missing.dart", file_set) is None
    assert s7_enforce_dart._resolve_uri(repo, "lib/src/storage.dart", "package:probe", file_set) is None  # 无子路径


def test_resolve_uri_package_nested_pubspec(tmp_path):
    """monorepo 子包就近 pubspec：包内 package: URI 命中，跨包视为外部。"""
    repo = tmp_path / "mono"
    sub = repo / "packages" / "sub"
    (sub / "lib").mkdir(parents=True)
    (sub / "pubspec.yaml").write_text("name: sub\n", encoding="utf-8")
    (sub / "lib" / "x.dart").write_text("", encoding="utf-8")
    file_set = {"packages/sub/lib/x.dart"}
    assert s7_enforce_dart._resolve_uri(
        repo, "packages/sub/lib/y.dart", "package:sub/x.dart", file_set
    ) == "packages/sub/lib/x.dart"
    assert s7_enforce_dart._resolve_uri(
        repo, "packages/sub/lib/y.dart", "package:sibling/x.dart", file_set
    ) is None  # 跨包外部


def test_pubspec_name_trailing_comment(tmp_path):
    """pubspec name 行尾注释容忍（YAML `#` 前空白语义）——review 批修正回归。"""
    repo = tmp_path / "pkg"
    (repo / "lib").mkdir(parents=True)
    (repo / "pubspec.yaml").write_text(
        "name: my_pkg # my package\n", encoding="utf-8"
    )
    (repo / "lib" / "a.dart").write_text("", encoding="utf-8")
    file_set = {"lib/a.dart"}
    assert s7_enforce_dart._pubspec_name(repo / "pubspec.yaml") == "my_pkg"
    assert s7_enforce_dart._resolve_uri(
        repo, "lib/main.dart", "package:my_pkg/a.dart", file_set
    ) == "lib/a.dart"  # 原 name=None 全外部假阴性
    (repo / "pubspec.yaml").write_text(
        "name: \"my_pkg\"\t# tab comment\n", encoding="utf-8"
    )
    assert s7_enforce_dart._pubspec_name(repo / "pubspec.yaml") == "my_pkg"  # 引号+tab 注释
    (repo / "pubspec.yaml").write_text("name: my_pkg#c\n", encoding="utf-8")
    assert s7_enforce_dart._pubspec_name(repo / "pubspec.yaml") is None  # # 前无空白非注释（YAML 语义）


def test_build_dart_graph_self_external_part():
    repo = Path("Z:/nowhere")
    file_set = {"lib/a.dart", "lib/b.dart", "lib/part.dart"}
    part_map = {"lib/part.dart": "lib/a.dart"}
    import_rows = [
        {"kind": "import", "uri": "b.dart", "line": 1, "file": "lib/a.dart"},  # a→b
        {"kind": "import", "uri": "dart:async", "line": 2, "file": "lib/a.dart"},  # SDK 跳过
        {"kind": "import", "uri": "missing.dart", "line": 3, "file": "lib/a.dart"},  # 未命中跳过
        {"kind": "import", "uri": "part.dart", "line": 4, "file": "lib/a.dart"},  # part 归并 owner：自环跳过
    ]
    graph, edges = s7_enforce_dart.build_dart_graph(repo, file_set, part_map, import_rows)
    assert graph == {"lib/a.dart": ["lib/b.dart"], "lib/b.dart": []}  # part 不是独立容器；叶可见
    assert edges == [{"file": "lib/a.dart", "line": 1, "src": "lib/a.dart", "dst": "lib/b.dart"}]


def test_belongs_exact_match():
    assert s7_enforce_dart._belongs("lib/src/a.dart", ["lib/src/a.dart"])
    assert not s7_enforce_dart._belongs("lib/src/a.dart", ["lib/src/a.dartx"])  # 前缀污染
    assert not s7_enforce_dart._belongs("lib/src/a.dart", ["lib/src/b.dart"])


def test_find_violations_bfs_shortest_chain():
    graph = {
        "A/S.dart": ["B/M.dart", "A/Q.dart"],
        "B/M.dart": ["I/F.dart"],  # 2 跳
        "A/Q.dart": ["B/M.dart"],  # 3 跳路径
        "I/F.dart": [],
        "W/X.dart": [],
    }
    contracts = [
        {
            "name": "layer: core must not import interface",
            "source_layer": "core",
            "forbidden_layer": "interface",
            "source_modules": ["A/S.dart"],
            "forbidden_modules": ["I/F.dart"],
        }
    ]
    violations = s7_enforce_dart._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "A/S.dart" and v["imported"] == "I/F.dart"
    assert v["chain"] == ["A/S.dart", "B/M.dart", "I/F.dart"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶容器（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"A/X.dart": ["L/F.dart"], "L/F.dart": []}
    contracts = [{"name": "n", "source_modules": ["A/X.dart"], "forbidden_modules": ["L/F.dart"]}]
    violations = s7_enforce_dart._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["A/X.dart", "L/F.dart"]


def test_find_violations_none_when_unreachable():
    graph = {"A/X.dart": ["B/X.dart"], "B/X.dart": [], "C/X.dart": []}
    contracts = [{"name": "n", "source_modules": ["A/X.dart"], "forbidden_modules": ["C/X.dart"]}]
    assert s7_enforce_dart._find_violations(graph, contracts) == []


# ------------------------------------------------- derive / run_dart E2E


def test_derive_dart_containers(tmp_path):
    repo = _mk_dart_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_dart.derive_dart_containers(repo, con, json.loads(DART_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == DART_CONTAINERS
    assert derived["dart_files"] == 2
    assert derived["timed_out"] is False
    assert derived["extensions"] == []


def test_derive_dart_containers_part_fids(tmp_path):
    """part 文件符号容器 = owner 文件（part 不是独立 library）。"""
    repo = _mk_dart_repo(tmp_path)
    (repo / "lib" / "src" / "api_impl.dart").write_text(
        "part of 'api.dart';\n\nint implFn() => 2;\n", encoding="utf-8"
    )
    (repo / "lib" / "src" / "api.dart").write_text(
        "part 'api_impl.dart';\n\nint apiVersion() {\n  return 1;\n}\n", encoding="utf-8"
    )
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'probe.lib.src.api_impl', 'Function', 'implFn', 'lib/src/api_impl.dart', 3, 3, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps({
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2", "f3"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    }, ensure_ascii=False),))
    con.commit()
    try:
        derived = s7_enforce_dart.derive_dart_containers(
            repo, con, json.loads(con.execute("SELECT payload_json FROM structure").fetchone()[0])
        )
    finally:
        con.close()
    assert derived["containers"] == {
        "core-mod": ["lib/src/storage.dart"],
        "iface-mod": ["lib/src/api.dart"],  # part fid 归并 owner 容器
    }
    assert derived["dart_files"] == 3


def test_run_dart_violation_e2e(tmp_path):
    repo = _mk_dart_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_dart.run_dart(repo, con, json.loads(DART_STRUCTURE), "active", DART_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "dart-import-parser"
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
    assert row[3] == "f1"  # storage.dart 首符号
    assert row[4] == "lib/src/storage.dart"
    assert row[5] == 1 and row[6] == 1  # 违例 import 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "lib/src/storage.dart"
    assert payload["imported"] == "lib/src/api.dart"
    assert payload["chain"] == ["lib/src/storage.dart", "lib/src/api.dart"]
    assert payload["line"] == 1


def test_run_dart_package_uri_transitive_e2e(tmp_path):
    """package: URI（pubspec 判包）+ 传递链 Storage → Queue → Api。"""
    repo = _mk_dart_repo(tmp_path)
    (repo / "pubspec.yaml").write_text("name: probe\n", encoding="utf-8")
    (repo / "lib" / "src" / "queue.dart").write_text(
        "import 'package:probe/src/api.dart';\n\nint queueWork() => apiVersion();\n",
        encoding="utf-8",
    )
    (repo / "lib" / "src" / "storage.dart").write_text(
        "import 'package:probe/src/queue.dart';\n\n"
        "int storagePut(String key) => queueWork() + (key.isEmpty ? 1 : 0);\n",
        encoding="utf-8",
    )
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
                {"module_id": "app-mod", "name": "队列", "responsibility": "派", "symbol_fids": [], "layer": "core"},
                {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
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
    payload = json.loads(structure)
    containers = {"core-mod": ["lib/src/storage.dart", "lib/src/queue.dart"], "iface-mod": ["lib/src/api.dart"]}
    try:
        out = s7_enforce_dart.run_dart(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queue→api 直连 + storage 经 queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["lib/src/queue.dart"]["chain"] == ["lib/src/queue.dart", "lib/src/api.dart"]
    assert by_importer["lib/src/storage.dart"]["chain"] == [
        "lib/src/storage.dart", "lib/src/queue.dart", "lib/src/api.dart"
    ]
    assert by_importer["lib/src/storage.dart"]["line"] == 1  # 锚定链尾直接 import 边：queue.dart 第 1 行
    assert all(r[0] == "lib/src/queue.dart" for r in rows)


def test_run_dart_idempotent(tmp_path):
    repo = _mk_dart_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_dart.run_dart(repo, con, json.loads(DART_STRUCTURE), "active", DART_CONTAINERS)
        second = s7_enforce_dart.run_dart(repo, con, json.loads(DART_STRUCTURE), "active", DART_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_dart_compliant_clears_old_findings(tmp_path):
    repo = _mk_dart_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 import
    (repo / "lib" / "src" / "storage.dart").write_text(
        "int storagePut(String key, String value) {\n"
        "  if (key.isEmpty) return 1;\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_dart.run_dart(repo, con, json.loads(DART_STRUCTURE), "active", DART_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_dart_no_dart_files(tmp_path):
    repo = _mk_dart_repo(tmp_path)
    shutil.rmtree(repo / "lib")
    con = _con(repo)
    try:
        out = s7_enforce_dart.run_dart(repo, con, json.loads(DART_STRUCTURE), "active", DART_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no dart libraries" in out["reason"]


def test_run_dart_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_dart_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_dart,
        "scan_dart",
        lambda repo, timeout=600: (set(), {}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_dart.run_dart(repo, con, json.loads(DART_STRUCTURE), "active", DART_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "dart scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_dart_test_root_excluded_e2e(tmp_path):
    """test/ 目录与 *_test.dart 命名约定的 import 不产违例（Dart test 约定对齐）。"""
    repo = _mk_dart_repo(tmp_path)
    test = repo / "test"
    test.mkdir()
    # 测试里 import api（同生产违例方向）——若泄漏会掺入测试 import 边
    (test / "storage_test.dart").write_text(
        "import '../lib/src/api.dart';\n\nvoid main() {\n  apiVersion();\n}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_dart.run_dart(repo, con, json.loads(DART_STRUCTURE), "active", DART_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 仅 storage.dart 的生产 import
    assert out["tests_excluded"] == 1
    assert out["files"] == 3  # 测试文件扫描计数诚实


# ------------------------------------------------------ run() 路由


def test_run_routes_dart(tmp_path):
    """纯 Dart 结构 → run() 路由至 dart 引擎并落 P9。"""
    repo = _mk_dart_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "dart-import-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_dart_and_py_not_enforceable(tmp_path):
    repo = _mk_dart_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'pyhelper.mod', 'Function', 'h', 'pyhelper/mod.py', 1, 2, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps({
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
            {"module_id": "py-mod", "name": "杂", "responsibility": "混", "symbol_fids": ["f3"], "layer": "core"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    }, ensure_ascii=False),))
    con.commit()
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "mixed-language" in out["reason"]
    assert "py + dart" in out["reason"]


def test_run_dart_derive_timeout(tmp_path, monkeypatch):
    repo = _mk_dart_repo(tmp_path)
    monkeypatch.setattr(
        s7_enforce_dart,
        "scan_dart",
        lambda repo, timeout=600: (set(), {}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "dart scan exceeded" in out["reason"]


def test_cli_enforce_dart_wiring(tmp_path, capsys):
    repo = _mk_dart_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
