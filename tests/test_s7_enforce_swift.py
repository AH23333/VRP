"""S7 Swift 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/swift-enforce-probe）。

引擎无外部执法器（内置源级 import 解析 + Package.swift targets 权威清单），
quick 层直接跑真解析——mock 只用于超时分支。
"""

import json
import shutil
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_swift

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

SWIFT_CONTAINERS = {"core-mod": ["Storage"], "iface-mod": ["Api"]}

# 两层：core(Storage) ← interface(Api)；违例 = Storage 上向 import Api
SWIFT_STRUCTURE = json.dumps(
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

MANIFEST = """// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "probe",
    products: [
        .library(name: "probe", targets: ["Api", "Storage"]),
    ],
    targets: [
        .target(name: "Api"),
        .target(
            name: "Storage",
            dependencies: [.target(name: "Api")]
        ),
        .testTarget(
            name: "StorageTests",
            dependencies: ["Storage"]
        ),
    ]
)
"""


def _mk_swift_repo(tmp_path: Path, structure: str | None = SWIFT_STRUCTURE) -> Path:
    repo = tmp_path / "swiftrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'probe.Sources.Storage.Storage', 'Function', 'storagePut', 'Sources/Storage/Storage.swift', 3, 6, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'probe.Sources.Api.Api', 'Function', 'apiVersion', 'Sources/Api/Api.swift', 1, 3, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "Package.swift").write_text(MANIFEST, encoding="utf-8")
    (repo / "Sources" / "Api").mkdir(parents=True)
    (repo / "Sources" / "Api" / "Api.swift").write_text(
        "public func apiVersion() -> Int {\n    return 1\n}\n", encoding="utf-8"
    )
    (repo / "Sources" / "Storage").mkdir(parents=True)
    # 违例：第 1 行 import Api（import 行号锚定目标）
    (repo / "Sources" / "Storage" / "Storage.swift").write_text(
        "import Api\n\nfunc storagePut(key: String) -> Int {\n    return apiVersion()\n}\n",
        encoding="utf-8",
    )
    (repo / "Tests" / "StorageTests").mkdir(parents=True)
    (repo / "Tests" / "StorageTests" / "StorageTests.swift").write_text(
        "@testable import Storage\n@testable import Api\nimport XCTest\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode():
    src = (
        "import Foundation // line import Evil\n"
        "/* outer /* inner */ import Evil2 */ var x = 1\n"
        'let s = "import Evil3"\n'
        'let esc = "a\\(x) b\\n c"\n'
        "let m = \"\"\"\n"
        "import Evil4\n"
        "\"\"\"\n"
        'let r = #"import Evil5"#\n'
        "let keep = 2\n"
    )
    out = s7_enforce_swift._blank_noncode(src)
    assert out.split("\n")[0].startswith("import Foundation")  # 行注释空白化
    assert "Evil2" not in out and "outer" not in out  # 嵌套块注释整段空白化
    assert "var x = 1" in out  # 嵌套闭合后代码保留
    assert "Evil3" not in out  # 字符串 canary 空白化
    assert "Evil4" not in out  # 多行三引号串体空白化
    assert "Evil5" not in out  # raw 串空白化
    assert "let keep = 2;" in out.replace(";", "") or "let keep = 2" in out
    assert len(out) == len(src)  # 等长空白
    assert out.count("\n") == src.count("\n")  # 换行保留
    rows = s7_enforce_swift._iter_imports(out)
    assert [r["module"] for r in rows] == ["Foundation"]  # 仅真 import 存活


def test_blank_nested_block_comment_unterminated():
    src = "/* outer /* inner never closed\nvar keep = 1;\n"
    out = s7_enforce_swift._blank_noncode(src)
    assert "inner" not in out  # 未闭合嵌套注释截断文件尾（前进保证）
    assert "keep" not in out  # 注释体吞没后续代码（保守处理）


def test_blank_raw_string_closes_without_escape():
    src = 'let r = #"a "quoted" b"#\nlet s = #"tab\\t raw"#\nlet t = 1\n'
    out = s7_enforce_swift._blank_noncode(src)
    assert "quoted" not in out  # raw 串内引号不闭合（无 # 后缀）
    assert "tab" not in out  # raw 串内反斜杠不转义不闭合
    assert "let t = 1" in out
    assert len(out) == len(src)


def test_blank_raw_string_escape_skips_pound_quote():
    src = 'let r = #"a\\#("x") b"#\nlet t = 2\n'
    out = s7_enforce_swift._blank_noncode(src)
    assert '"x"' not in out  # 反斜杠井号转义整体跳过
    assert "let t = 2" in out


def test_blank_multiline_trailing_quote_run():
    """闭引号取引号串末三枚——四连引号收尾首枚属内容。"""
    src = 'let m = """a"""\nlet n = """b""""\nlet t = 3\n'
    out = s7_enforce_swift._blank_noncode(src)
    assert '"a' not in out  # 三引号闭
    assert "b" not in out  # 四连引号：末三枚闭合，首枚属内容（整串空白化）
    assert "let t = 3" in out


def test_blank_unclosed_single_line_string():
    src = 'let a = "unclosed\nlet b = 2\n'
    out = s7_enforce_swift._blank_noncode(src)
    assert "unclosed" not in out  # 未闭合单行串截断行尾
    assert "let b = 2" in out  # 后续代码不受损


def test_iter_imports_forms():
    blanked = s7_enforce_swift._blank_noncode(
        "import Foundation\n"
        "@testable import Storage\n"
        "import class Api.Version\n"
        "import struct Api.Thing\n"
        "import enum Api.Kind\n"
        "import func Api.make\n"
        "import let Api.count\n"
        "import var Api.state\n"
        "import typealias Api.Alias\n"
        "import indirect Api.Node\n"
        "import\n"
        "let x = y.import Evil\n"
        "let z = myimport Evil2\n"
        "if canImport(Foundation) { }\n"
    )
    rows = s7_enforce_swift._iter_imports(blanked)
    got = [(r["module"], r["line"]) for r in rows]
    assert ("Foundation", 1) in got
    assert ("Storage", 2) in got  # @testable
    assert ("Api", 3) in got  # 限定 import class
    assert ("Api", 4) in got  # import struct
    assert ("Api", 5) in got  # import enum
    assert ("Api", 6) in got  # import func
    assert ("Api", 7) in got  # import let
    assert ("Api", 8) in got  # import var
    assert ("Api", 9) in got  # import typealias
    assert ("Api", 10) in got  # import indirect
    assert not any(r["module"] == "Evil" for r in rows)  # 成员访问拒绝
    assert not any(r["module"] == "Evil2" for r in rows)  # 标识符尾拒绝（无边界）
    assert not any("anImport" in r["module"] for r in rows)  # canImport 大写 I 无边界
    assert len(got) == 10


def test_iter_imports_pseudo_forms_lexer_guarded():
    blanked = s7_enforce_swift._blank_noncode(
        'let s = "import Evil"\n'
        "// import Evil2\n"
        "/* import Evil3 */\n"
        'let m = """\n'
        "import Evil4\n"
        '"""\n'
        'let r = #"import Evil5"#\n'
        "import Real\n"
    )
    rows = s7_enforce_swift._iter_imports(blanked)
    assert [(r["module"], r["line"]) for r in rows] == [("Real", 8)]  # 伪 import 全部词法消化


def test_parse_package_swift_full_forms(tmp_path):
    manifest = """// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "probe",
    products: [
        .library(name: "probe", targets: ["Api", "Storage"]),
    ],
    dependencies: [
        .package(url: "https://example.com/x", from: "1.0.0"),
    ],
    targets: [
        .target(name: "Api"),
        .executableTarget(
            name: "Storage",
            dependencies: [
                .target(name: "Api"),
                .product(name: "X", package: "x"),
            ]
        ),
        .testTarget(
            name: "StorageTests",
            dependencies: ["Storage"],
            path: "Tests/Storage"
        ),
        .target(
            name: "Custom",
            path: "Custom/Root",
            dependencies: []
        ),
        .target(
            name: dynName,
            dependencies: ["Api"]
        ),
        // .target(name: "Commented")
        /* .target(name: "BlockCommented") */
    ]
)
"""
    repo = tmp_path / "pkg"
    repo.mkdir()
    (repo / "Package.swift").write_text(manifest, encoding="utf-8")
    pkg = s7_enforce_swift.parse_package_swift(repo)
    assert pkg["found"] is True
    t = pkg["targets"]
    assert t["Api"] == {"path": "Sources/Api", "test": False}  # SwiftPM 默认路径
    assert t["Storage"] == {"path": "Sources/Storage", "test": False}  # executableTarget 同产 target
    assert t["StorageTests"] == {"path": "Tests/Storage", "test": True}  # path 实参覆盖默认
    assert t["Custom"] == {"path": "Custom/Root", "test": False}
    assert "dynName" not in t  # 计算名（变量）不可见
    assert "X" not in t  # .product 依赖引用不产 target
    assert "Commented" not in t and "BlockCommented" not in t  # 注释内伪声明拒绝
    assert "probe" not in t  # products 的 targets 数组不产 target


def test_parse_package_swift_missing(tmp_path):
    repo = tmp_path / "pkg"
    repo.mkdir()
    pkg = s7_enforce_swift.parse_package_swift(repo)
    assert pkg == {"found": False, "targets": {}}


def test_parse_package_swift_label_in_string_ignored(tmp_path):
    """大字符串内伪 targets 数组/伪 label 拒绝（string_starts 真串首校验）。"""
    manifest = (
        'let doc = "targets: [\\n .target(name: \\"Fake\\")\\n]"\n'
        "let package = Package(\n"
        "    targets: [\n"
        '        .target(name: "Real"),\n'
        "    ]\n"
        ")\n"
    )
    repo = tmp_path / "pkg"
    repo.mkdir()
    (repo / "Package.swift").write_text(manifest, encoding="utf-8")
    pkg = s7_enforce_swift.parse_package_swift(repo)
    assert pkg["found"] is True
    assert list(pkg["targets"]) == ["Real"]


def test_scan_swift_layout(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "Package.swift").write_text(MANIFEST, encoding="utf-8")
    (repo / "Sources" / "Api").mkdir(parents=True)
    (repo / "Sources" / "Api" / "Api.swift").write_text(
        "public func apiVersion() -> Int { return 1 }\n", encoding="utf-8"
    )
    (repo / "Sources" / "Storage").mkdir(parents=True)
    (repo / "Sources" / "Storage" / "Storage.swift").write_text(
        "import Api\nimport Foundation\n\nfunc storagePut() -> Int { return apiVersion() }\n",
        encoding="utf-8",
    )
    (repo / "Tests" / "StorageTests").mkdir(parents=True)
    (repo / "Tests" / "StorageTests" / "StorageTests.swift").write_text(
        "@testable import Storage\nimport XCTest\n", encoding="utf-8"
    )
    (repo / "Sources" / "extra.swift").write_text(
        "import Foundation\nvar orphan = 1\n", encoding="utf-8"
    )
    (repo / ".build").mkdir()
    (repo / ".build" / "gen.swift").write_text("var x = 1\n", encoding="utf-8")
    target_map, import_rows, files, tests, orphans, timed_out = s7_enforce_swift.scan_swift(repo)
    assert timed_out is False
    assert files == 4  # Package.swift 非源码不计；.build 跳过
    assert tests == 1
    assert orphans == 1  # Sources/extra.swift
    assert import_rows == [
        {"module": "Api", "line": 1, "file": "Sources/Storage/Storage.swift", "src_target": "Storage"},
        {"module": "Foundation", "line": 2, "file": "Sources/Storage/Storage.swift", "src_target": "Storage"},
    ]  # 测试/orphan import 不入列


def test_scan_swift_timeout(tmp_path):
    repo = _mk_swift_repo(tmp_path)
    target_map, import_rows, files, tests, orphans, timed_out = s7_enforce_swift.scan_swift(
        repo, timeout=0
    )
    assert timed_out is True


def test_owning_target_longest_prefix():
    target_map = {
        "A": {"path": "Sources/A", "test": False},
        "AB": {"path": "Sources/A/B", "test": False},
    }
    assert s7_enforce_swift._owning_target("Sources/A/a.swift", target_map) == "A"
    assert s7_enforce_swift._owning_target("Sources/A/B/b.swift", target_map) == "AB"  # 最长前缀
    assert s7_enforce_swift._owning_target("Sources/AB/x.swift", target_map) is None  # 前缀污染拒绝
    assert s7_enforce_swift._owning_target("Sources/Ax/y.swift", target_map) is None


def test_build_swift_graph_self_external():
    target_map = {
        "Api": {"path": "Sources/Api", "test": False},
        "Storage": {"path": "Sources/Storage", "test": False},
    }
    import_rows = [
        {"module": "Api", "line": 1, "file": "Sources/Storage/Storage.swift", "src_target": "Storage"},  # 真 edge
        {"module": "Foundation", "line": 2, "file": "Sources/Storage/Storage.swift", "src_target": "Storage"},  # 未声明模块跳过
        {"module": "Missing", "line": 3, "file": "Sources/Storage/Storage.swift", "src_target": "Storage"},  # 未声明跳过
        {"module": "Storage", "line": 4, "file": "Sources/Storage/Storage.swift", "src_target": "Storage"},  # 自导入自环跳过
    ]
    graph, edges = s7_enforce_swift.build_swift_graph(target_map, import_rows)
    assert graph == {"Api": [], "Storage": ["Api"]}  # 全部声明 target 入图（叶可见）
    assert edges == [
        {"file": "Sources/Storage/Storage.swift", "line": 1, "src": "Storage", "dst": "Api"}
    ]


def test_belongs_exact_match():
    assert s7_enforce_swift._belongs("Storage", ["Storage", "Api"])
    assert not s7_enforce_swift._belongs("Storage", ["StorageTests"])  # 前缀污染
    assert not s7_enforce_swift._belongs("Storage", ["Api"])


def test_find_violations_bfs_shortest_chain():
    graph = {
        "App": ["Mid", "Queue"],
        "Mid": ["Iface"],  # 2 跳
        "Queue": ["Mid"],  # 3 跳路径
        "Iface": [],
        "Other": [],
    }
    contracts = [
        {
            "name": "layer: app must not import interface",
            "source_layer": "app",
            "forbidden_layer": "interface",
            "source_modules": ["App"],
            "forbidden_modules": ["Iface"],
        }
    ]
    violations = s7_enforce_swift._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "App" and v["imported"] == "Iface"
    assert v["chain"] == ["App", "Mid", "Iface"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶 target（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"App": ["Leaf"], "Leaf": []}
    contracts = [{"name": "n", "source_modules": ["App"], "forbidden_modules": ["Leaf"]}]
    violations = s7_enforce_swift._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["App", "Leaf"]


def test_find_violations_none_when_unreachable():
    graph = {"App": ["Mid"], "Mid": [], "Iface": []}
    contracts = [{"name": "n", "source_modules": ["App"], "forbidden_modules": ["Iface"]}]
    assert s7_enforce_swift._find_violations(graph, contracts) == []


# ------------------------------------------------- derive / run_swift E2E


def test_derive_swift_containers(tmp_path):
    repo = _mk_swift_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_swift.derive_swift_containers(repo, con, json.loads(SWIFT_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == SWIFT_CONTAINERS
    assert derived["swift_files"] == 2  # Storage.swift + Api.swift（manifest fid 不在 structure）
    assert derived["extensions"] == []


def test_derive_swift_containers_no_manifest(tmp_path):
    """无 Package.swift：fid 仍计数（路由进 swift 引擎给可行动 not-enforceable），
    容器为空（无 target 权威）。"""
    repo = _mk_swift_repo(tmp_path)
    (repo / "Package.swift").unlink()
    con = _con(repo)
    try:
        derived = s7_enforce_swift.derive_swift_containers(repo, con, json.loads(SWIFT_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == {"core-mod": [], "iface-mod": []}
    assert derived["swift_files"] == 2


def test_run_swift_violation_e2e(tmp_path):
    repo = _mk_swift_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_swift.run_swift(repo, con, json.loads(SWIFT_STRUCTURE), "active", SWIFT_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "swift-target-parser"
    assert out["contracts"] == 1
    assert out["files"] == 3  # Api.swift + Storage.swift + StorageTests.swift
    assert out["modules"] == 3  # Api + Storage + StorageTests（test target 容器可见）
    assert out["tests_excluded"] == 1
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
    assert row[3] == "f1"  # Storage.swift 首符号
    assert row[4] == "Sources/Storage/Storage.swift"
    assert row[5] == 1 and row[6] == 1  # 违例 import 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "Storage"
    assert payload["imported"] == "Api"
    assert payload["chain"] == ["Storage", "Api"]
    assert payload["line"] == 1


def test_run_swift_transitive_e2e(tmp_path):
    """传递链 Core → Queue → Api（中间 target 容器细分）。"""
    repo = _mk_swift_repo(tmp_path)
    (repo / "Sources" / "Queue").mkdir(parents=True)
    (repo / "Sources" / "Queue" / "Queue.swift").write_text(
        "import Api\n\npublic func queueWork() -> Int { return apiVersion() }\n",
        encoding="utf-8",
    )
    (repo / "Sources" / "Storage" / "Storage.swift").write_text(
        "import Queue\n\nfunc storagePut(key: String) -> Int { return queueWork() }\n",
        encoding="utf-8",
    )
    structure = json.dumps(
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
    con = _con(repo)
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    con.close()
    # 修 manifest 加 Queue target
    manifest = MANIFEST.replace(
        '        .target(name: "Api"),',
        '        .target(name: "Api"),\n        .target(name: "Queue"),',
    )
    (repo / "Package.swift").write_text(manifest, encoding="utf-8")
    payload = json.loads(structure)
    containers = {"core-mod": ["Storage", "Queue"], "iface-mod": ["Api"]}
    con = _con(repo)
    try:
        out = s7_enforce_swift.run_swift(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # Queue→Api 直连 + Storage 经 Queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["Queue"]["chain"] == ["Queue", "Api"]
    assert by_importer["Storage"]["chain"] == ["Storage", "Queue", "Api"]
    assert by_importer["Storage"]["line"] == 1  # 锚定链尾直接 import 边：Queue.swift 第 1 行
    assert all(r[0] == "Sources/Queue/Queue.swift" for r in rows)


def test_run_swift_idempotent(tmp_path):
    repo = _mk_swift_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_swift.run_swift(repo, con, json.loads(SWIFT_STRUCTURE), "active", SWIFT_CONTAINERS)
        second = s7_enforce_swift.run_swift(repo, con, json.loads(SWIFT_STRUCTURE), "active", SWIFT_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_swift_compliant_clears_old_findings(tmp_path):
    repo = _mk_swift_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 import
    (repo / "Sources" / "Storage" / "Storage.swift").write_text(
        "func storagePut(key: String) -> Int {\n    return 0\n}\n", encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_swift.run_swift(repo, con, json.loads(SWIFT_STRUCTURE), "active", SWIFT_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_swift_no_package(tmp_path):
    repo = _mk_swift_repo(tmp_path)
    (repo / "Package.swift").unlink()
    con = _con(repo)
    try:
        out = s7_enforce_swift.run_swift(repo, con, json.loads(SWIFT_STRUCTURE), "active", SWIFT_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no Package.swift" in out["reason"]


def test_run_swift_no_targets(tmp_path):
    repo = _mk_swift_repo(tmp_path)
    (repo / "Package.swift").write_text(
        "// swift-tools-version:5.9\nimport PackageDescription\nlet package = Package(name: \"empty\")\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_swift.run_swift(repo, con, json.loads(SWIFT_STRUCTURE), "active", SWIFT_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no targets declared" in out["reason"]


def test_run_swift_no_swift_files(tmp_path):
    repo = _mk_swift_repo(tmp_path)
    shutil.rmtree(repo / "Sources")
    shutil.rmtree(repo / "Tests")
    con = _con(repo)
    try:
        out = s7_enforce_swift.run_swift(repo, con, json.loads(SWIFT_STRUCTURE), "active", SWIFT_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no swift sources" in out["reason"]


def test_run_swift_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_swift_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_swift,
        "scan_swift",
        lambda repo, timeout=600: ({}, [], 0, 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_swift.run_swift(repo, con, json.loads(SWIFT_STRUCTURE), "active", SWIFT_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "swift scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_swift_test_target_excluded_e2e(tmp_path):
    """testTarget 的 import 不产违例（XCTest/SUT 双向自由）；容器仍入表。"""
    repo = _mk_swift_repo(tmp_path)
    # 测试里再 import Api（若泄漏会掺入测试 import 边）
    (repo / "Tests" / "StorageTests" / "StorageTests.swift").write_text(
        "@testable import Storage\n@testable import Api\nimport XCTest\n\nfinal class T: XCTestCase {}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_swift.run_swift(repo, con, json.loads(SWIFT_STRUCTURE), "active", SWIFT_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 仅 Storage.swift 的生产 import
    assert out["tests_excluded"] == 1


def test_run_swift_orphan_reported(tmp_path):
    """未归属 target 的 .swift 文件 orphan 诚实上报（import 不入图）。"""
    repo = _mk_swift_repo(tmp_path)
    (repo / "Sources" / "loose.swift").write_text(
        "import Api\nvar x = 1\n", encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_swift.run_swift(repo, con, json.loads(SWIFT_STRUCTURE), "active", SWIFT_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["orphan_swift_files"] == 1
    assert out["violations"] == 1  # loose.swift 的 import 不产第二违例


# ------------------------------------------------------ run() 路由


def test_run_routes_swift(tmp_path):
    """纯 Swift 结构 → run() 路由至 swift 引擎并落 P9。"""
    repo = _mk_swift_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "swift-target-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_swift_and_py_not_enforceable(tmp_path):
    repo = _mk_swift_repo(tmp_path)
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
    assert "py + swift" in out["reason"]


def test_cli_enforce_swift_wiring(tmp_path, capsys):
    repo = _mk_swift_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["engine"] == "swift-target-parser"
    assert out["violations"] == 1
