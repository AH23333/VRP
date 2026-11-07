"""S7 Scala 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/scala-enforce-probe）。

引擎无外部执法器（内置源级 package/import 解析器），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import shutil
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_scala

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

SCALA_CONTAINERS = {"core-mod": ["com.demo.store"], "iface-mod": ["com.demo.api"]}

# 两层：core(com.demo.store) ← interface(com.demo.api)；违例 = store 上向 import api
SCALA_STRUCTURE = json.dumps(
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


def _mk_scala_repo(tmp_path: Path, structure: str | None = SCALA_STRUCTURE) -> Path:
    repo = tmp_path / "srepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'com.demo.store.Storage', 'Class', 'Storage', 'src/main/scala/com/demo/store/Storage.scala', 5, 7, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'com.demo.api.Handler', 'Class', 'Handler', 'src/main/scala/com/demo/api/Handler.scala', 3, 5, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    base = repo / "src" / "main" / "scala" / "com" / "demo"
    (base / "store").mkdir(parents=True)
    (base / "api").mkdir(parents=True)
    (base / "api" / "Handler.scala").write_text(
        "package com.demo.api\n\nclass Handler {\n    def handle(): String = \"h\"\n}\n",
        encoding="utf-8",
    )
    # 违例：第 3 行 import com.demo.api（import 行号锚定目标）
    (base / "store" / "Storage.scala").write_text(
        "package com.demo.store\n\n"
        "import com.demo.api.Handler\n\n"
        "class Storage {\n"
        "    def get(): String = new Handler().handle()\n"
        "}\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode():
    src = (
        "val a = 1 // line\n"
        'val b = "x/*y*/z"; /* outer /* nested */ still */ val c = 3\n'
        'val t = """\ntext block "q" /* no */\n""";\n'
        'val four = """q""""; val after = 1\n'
        "val ch = '\\n'; val sym = 'foo\n"
        'val s = s"interp $name"\n'
    )
    out = s7_enforce_scala._blank_noncode(src)
    assert out.splitlines()[0].startswith("val a = 1")  # 行注释空白化保行号
    assert '"x/*y*/z"' not in out  # 字符串整体空白化
    assert "text block" not in out  # 三引号字符串空白化（换行保留）
    assert "outer" not in out and "still" not in out  # 嵌套块注释整段空白化
    assert "val c = 3" in out  # 嵌套注释闭合后代码保留
    assert "val after" in out  # 4 连引号收尾：内容归属第 1 枚，不吞后续代码
    assert "'\\n'" not in out  # 字符字面量空白化
    assert "foo" in out  # 'symbol 无闭合引号 → 短距回退不吞
    assert "interp" not in out  # 插值字符串整体空白化


def test_parse_package_and_imports():
    blanked = s7_enforce_scala._blank_noncode(
        "package com.demo.store\n"
        "// import com.fake.One;\n"
        "/* import com.fake.Two */\n"
        'val s = "import com.fake.Three"\n'
        'val t = """import com.fake.Four"""\n'
        "import java.util.List\n"  # 外部：仍解析（解析阶段决定内外）
        "import com.demo.api.Handler\n"
        "import com.demo.api._\n"  # Scala 2 通配：随链捕获
        "import com.demo.api.*\n"  # Scala 3 通配：* 不入链
        "import com.demo.api.{C, D}\n"  # 选择器：链后截断
        "import com.demo.api.{C => D}\n"  # 重命名：链后截断
        "import com.demo.api.Handler._\n"  # 成员通配：随链捕获
    )
    assert s7_enforce_scala._parse_package(blanked) == "com.demo.store"
    rows = s7_enforce_scala._iter_imports(blanked)
    assert rows == [
        ("java.util.List", 6),
        ("com.demo.api.Handler", 7),
        ("com.demo.api._", 8),
        ("com.demo.api", 9),
        ("com.demo.api", 10),
        ("com.demo.api", 11),
        ("com.demo.api.Handler._", 12),
    ]  # 伪 import（注释/字符串/三引号串）不出现；通配/选择器统一剥壳


def test_parse_package_packaging_block():
    blanked = s7_enforce_scala._blank_noncode(
        "package com.probe.store {\n"
        "  class Storage {\n"
        "    def get(): String = \"s\"\n"
        "  }\n"
        "}\n"
    )
    assert s7_enforce_scala._parse_package(blanked) == "com.probe.store"


def test_parse_package_nested_chain():
    blanked = s7_enforce_scala._blank_noncode(
        "package com {\n"
        "  package demo {\n"
        "    package probe {\n"
        "      class Storage\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    assert s7_enforce_scala._parse_package(blanked) == "com.demo.probe"  # 嵌套链拼接首链


def test_parse_package_multiple_blocks_first():
    blanked = s7_enforce_scala._blank_noncode(
        "package com.first {\n  class A\n}\n"
        "package com.second {\n  class B\n}\n"
    )
    assert s7_enforce_scala._parse_package(blanked) == "com.first"  # 多 packaging 块取首个声明


def test_parse_package_plain_with_code_braces():
    blanked = s7_enforce_scala._blank_noncode(
        "package com.probe.store\n\n"
        "object Holder {\n  val m = scala.collection.mutable.Map.empty[String, Int]\n}\n"
    )
    assert s7_enforce_scala._parse_package(blanked) == "com.probe.store"  # 类体花括号不误判 packaging


def test_resolve_import_longest_prefix():
    packages = {"com.demo", "com.demo.api", "com.demo.store", "com.other"}
    assert s7_enforce_scala._resolve_import("com.demo.api.Handler", packages) == "com.demo.api"
    assert s7_enforce_scala._resolve_import("com.demo.api.Nested.Inner", packages) == "com.demo.api"
    assert s7_enforce_scala._resolve_import("com.demo.api._", packages) == "com.demo.api"  # Scala 2 通配前缀剥落
    assert s7_enforce_scala._resolve_import("com.demo.api.Handler._", packages) == "com.demo.api"  # 成员通配
    assert s7_enforce_scala._resolve_import("com.demo.api", packages) == "com.demo.api"
    assert s7_enforce_scala._resolve_import("java.util.List", packages) is None  # 外部
    assert s7_enforce_scala._resolve_import("scala.collection.immutable.List", packages) is None
    assert s7_enforce_scala._resolve_import("com.external.Foo", packages) is None


def test_scan_scala_packages_tests_and_skip_dirs(tmp_path):
    repo = tmp_path / "proj"
    main = repo / "src" / "main" / "scala" / "com" / "x"
    test = repo / "src" / "test" / "scala" / "com" / "x"
    gen = repo / "target" / "generated-sources" / "com" / "x"
    (main / "store").mkdir(parents=True)
    (test / "store").mkdir(parents=True)
    (gen).mkdir(parents=True)
    (main / "store" / "Storage.scala").write_text(
        "package com.x.store\nimport com.x.api.Api\n", encoding="utf-8"
    )
    (main / "api").mkdir()
    (main / "api" / "Api.scala").write_text("package com.x.api\n", encoding="utf-8")
    # 测试源根：包入图但不计 import
    (test / "store" / "StorageSpec.scala").write_text(
        "package com.x.store\nimport com.x.hidden.Forbidden\n", encoding="utf-8"
    )
    # target 产物：整体跳过
    (gen / "Gen.scala").write_text("package com.x.gen\n", encoding="utf-8")
    file_pkg, import_rows, files, tests_excluded, timed_out = s7_enforce_scala.scan_scala(repo)
    assert timed_out is False
    assert file_pkg == {
        "src/main/scala/com/x/store/Storage.scala": "com.x.store",
        "src/main/scala/com/x/api/Api.scala": "com.x.api",
        "src/test/scala/com/x/store/StorageSpec.scala": "com.x.store",
    }  # target 跳过；测试文件包仍入表（叶包可见）
    assert tests_excluded == 1
    assert files == 3
    assert [r["target"] for r in import_rows] == ["com.x.api.Api"]  # 测试 import 不入列
    graph, edges = s7_enforce_scala.build_scala_graph(file_pkg, import_rows)
    assert graph == {"com.x.store": ["com.x.api"], "com.x.api": []}
    assert edges == [
        {"file": "src/main/scala/com/x/store/Storage.scala", "line": 2, "src": "com.x.store", "dst": "com.x.api"}
    ]


def test_scan_scala_ignores_sc_worksheet(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src" / "main" / "scala").mkdir(parents=True)
    (repo / "src" / "main" / "scala" / "Api.scala").write_text(
        "package com.x.api\n", encoding="utf-8"
    )
    # .sc（worksheet/Ammonite）：不匹配 .scala 后缀，不参与执法
    (repo / "src" / "main" / "scala" / "Notebook.sc").write_text(
        "package com.x.worksheet\nimport com.x.api.Api\n", encoding="utf-8"
    )
    file_pkg, import_rows, files, tests_excluded, timed_out = s7_enforce_scala.scan_scala(repo)
    assert timed_out is False
    assert file_pkg == {"src/main/scala/Api.scala": "com.x.api"}
    assert import_rows == []
    assert files == 1  # .sc 不计入扫描


def test_scan_scala_no_package_orphan(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "Default.scala").write_text(
        "class Default {}\n", encoding="utf-8"  # 无 package 声明（默认包）
    )
    file_pkg, import_rows, files, tests_excluded, timed_out = s7_enforce_scala.scan_scala(repo)
    assert file_pkg == {}
    assert import_rows == []
    assert files == 1  # 扫描计数仍诚实


def test_scan_scala_timeout(tmp_path):
    repo = _mk_scala_repo(tmp_path)
    file_pkg, import_rows, files, tests, timed_out = s7_enforce_scala.scan_scala(repo, timeout=0)
    assert timed_out is True


def test_build_scala_graph_self_and_external_skipped():
    file_pkg = {
        "a/A.scala": "com.a",
        "b/B.scala": "com.b",
    }
    import_rows = [
        {"file": "a/A.scala", "line": 1, "target": "com.a.Same"},  # 同包：无自环
        {"file": "a/A.scala", "line": 2, "target": "com.b.B"},
        {"file": "a/A.scala", "line": 3, "target": "com.b._"},  # 通配解析到同包：另一条边明细
        {"file": "a/A.scala", "line": 4, "target": "scala.util.Random"},  # 外部：跳过
        {"file": "missing/C.scala", "line": 1, "target": "com.b.B"},  # orphan 文件：跳过
    ]
    graph, edges = s7_enforce_scala.build_scala_graph(file_pkg, import_rows)
    assert graph == {"com.a": ["com.b"], "com.b": []}  # 叶包入图
    assert edges == [
        {"file": "a/A.scala", "line": 2, "src": "com.a", "dst": "com.b"},
        {"file": "a/A.scala", "line": 3, "src": "com.a", "dst": "com.b"},
    ]


def test_belongs_parent_package_container():
    assert s7_enforce_scala._belongs("com.probe.store", ["com.probe"])
    assert s7_enforce_scala._belongs("com.probe", ["com.probe"])
    assert not s7_enforce_scala._belongs("com.probex", ["com.probe"])  # 前缀污染
    assert not s7_enforce_scala._belongs("com.other", ["com.probe"])


def test_find_violations_bfs_shortest_chain():
    graph = {"a.x": ["b.x", "mid.x"], "mid.x": ["c.x"], "b.x": ["c.x"], "c.x": [], "w.x": []}
    contracts = [
        {
            "name": "layer: core must not import interface",
            "source_layer": "core",
            "forbidden_layer": "interface",
            "source_modules": ["a.x"],
            "forbidden_modules": ["c.x"],
        }
    ]
    violations = s7_enforce_scala._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "a.x" and v["imported"] == "c.x"
    assert v["chain"] == ["a.x", "b.x", "c.x"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶包（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"a.x": ["leaf.x"], "leaf.x": []}
    contracts = [{"name": "n", "source_modules": ["a.x"], "forbidden_modules": ["leaf.x"]}]
    violations = s7_enforce_scala._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["a.x", "leaf.x"]


def test_find_violations_none_when_unreachable():
    graph = {"a.x": ["b.x"], "b.x": [], "c.x": []}
    contracts = [{"name": "n", "source_modules": ["a.x"], "forbidden_modules": ["c.x"]}]
    assert s7_enforce_scala._find_violations(graph, contracts) == []


def test_derive_scala_containers(tmp_path):
    repo = _mk_scala_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_scala.derive_scala_containers(repo, con, json.loads(SCALA_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == SCALA_CONTAINERS
    assert derived["scala_files"] == 2
    assert derived["orphan_files"] == 0
    assert derived["timed_out"] is False
    assert derived["extensions"] == []


def test_derive_scala_containers_orphans_counted(tmp_path):
    repo = _mk_scala_repo(tmp_path)
    (repo / "src" / "main" / "scala" / "com" / "demo" / "loose.scala").write_text(
        "class Loose {}\n", encoding="utf-8"  # 无 package：orphan
    )
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'demo.Loose', 'Class', 'Loose', 'src/main/scala/com/demo/loose.scala', 1, 1, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps({
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1", "f3"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    }, ensure_ascii=False),))
    con.commit()
    try:
        derived = s7_enforce_scala.derive_scala_containers(repo, con, json.loads(con.execute("SELECT payload_json FROM structure").fetchone()[0]))
    finally:
        con.close()
    assert derived["containers"] == SCALA_CONTAINERS  # orphan 不进容器
    assert derived["scala_files"] == 3
    assert derived["orphan_files"] == 1  # 无 package 声明诚实计数


# ------------------------------------------------- run_scala / run() E2E


def test_run_scala_violation_e2e(tmp_path):
    repo = _mk_scala_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_scala.run_scala(repo, con, json.loads(SCALA_STRUCTURE), "active", SCALA_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "scala-package-parser"
    assert out["contracts"] == 1
    assert out["files"] == 2
    assert out["packages"] == 2
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
    assert row[3] == "f1"  # Storage.scala 首符号
    assert row[4] == "src/main/scala/com/demo/store/Storage.scala"
    assert row[5] == 3 and row[6] == 3  # 违例 import 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "com.demo.store"
    assert payload["imported"] == "com.demo.api"
    assert payload["chain"] == ["com.demo.store", "com.demo.api"]
    assert payload["line"] == 3


def test_run_scala_transitive_e2e(tmp_path):
    """store → queue → api：core 层内传递链触达 interface（import-linter forbidden 同义）。"""
    repo = _mk_scala_repo(tmp_path)
    base = repo / "src" / "main" / "scala" / "com" / "demo"
    (base / "queue").mkdir()
    (base / "queue" / "Queue.scala").write_text(
        "package com.demo.queue\n\n"
        "import com.demo.api.Handler\n\n"
        "class Queue {\n"
        "    def work(): String = new Handler().handle()\n"
        "}\n",
        encoding="utf-8",
    )
    (base / "store" / "Storage.scala").write_text(
        "package com.demo.store\n\n"
        "import com.demo.queue.Queue\n\n"
        "class Storage {\n"
        "    def get(): String = new Queue().work()\n"
        "}\n",
        encoding="utf-8",
    )
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
                {"module_id": "app-mod", "name": "队列", "responsibility": "队", "symbol_fids": [], "layer": "core"},
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
    containers = {"core-mod": ["com.demo.store", "com.demo.queue"], "iface-mod": ["com.demo.api"]}
    try:
        out = s7_enforce_scala.run_scala(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queue→api 直连 + store 经 queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["com.demo.queue"]["chain"] == ["com.demo.queue", "com.demo.api"]
    assert by_importer["com.demo.store"]["chain"] == ["com.demo.store", "com.demo.queue", "com.demo.api"]
    assert by_importer["com.demo.store"]["line"] == 3  # 锚定链尾直接 import 边：Queue.scala 第 3 行
    assert all(r[0] == "src/main/scala/com/demo/queue/Queue.scala" for r in rows)


def test_run_scala_idempotent(tmp_path):
    repo = _mk_scala_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_scala.run_scala(repo, con, json.loads(SCALA_STRUCTURE), "active", SCALA_CONTAINERS)
        second = s7_enforce_scala.run_scala(repo, con, json.loads(SCALA_STRUCTURE), "active", SCALA_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_scala_compliant_clears_old_findings(tmp_path):
    repo = _mk_scala_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 import
    (repo / "src" / "main" / "scala" / "com" / "demo" / "store" / "Storage.scala").write_text(
        "package com.demo.store\n\nclass Storage {\n    def get(): String = \"s\"\n}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_scala.run_scala(repo, con, json.loads(SCALA_STRUCTURE), "active", SCALA_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_scala_no_packages(tmp_path):
    repo = _mk_scala_repo(tmp_path)
    shutil.rmtree(repo / "src")
    con = _con(repo)
    try:
        out = s7_enforce_scala.run_scala(repo, con, json.loads(SCALA_STRUCTURE), "active", SCALA_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no scala packages" in out["reason"]


def test_run_scala_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_scala_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_scala,
        "scan_scala",
        lambda repo, timeout=600: ({}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_scala.run_scala(repo, con, json.loads(SCALA_STRUCTURE), "active", SCALA_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "scala scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_scala_test_root_excluded_e2e(tmp_path):
    """src/test/scala 测试文件的 import 不产违例（java /test/java/、kt /test/kotlin/ 对齐）。"""
    repo = _mk_scala_repo(tmp_path)
    test = repo / "src" / "test" / "scala" / "com" / "demo" / "store"
    test.mkdir(parents=True)
    # 测试里 import api（同生产违例方向）——若泄漏会双计违例
    (test / "StorageSpec.scala").write_text(
        "package com.demo.store\n\n"
        "import com.demo.api.Handler\n\n"
        "class StorageSpec {\n"
        "    def t(): Unit = new Handler()\n"
        "}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_scala.run_scala(repo, con, json.loads(SCALA_STRUCTURE), "active", SCALA_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 仅 Storage.scala 的生产 import
    assert out["tests_excluded"] == 1
    assert out["files"] == 3  # 测试文件扫描计数诚实


# ------------------------------------------------------ run() 路由


def test_run_routes_scala(tmp_path):
    """纯 Scala 结构 → run() 路由至 scala 引擎并落 P9。"""
    repo = _mk_scala_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "scala-package-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_routes_scala_orphan_surfaced(tmp_path):
    repo = _mk_scala_repo(tmp_path)
    (repo / "src" / "main" / "scala" / "com" / "demo" / "loose.scala").write_text(
        "class Loose {}\n", encoding="utf-8"  # 无 package：orphan
    )
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'demo.Loose', 'Class', 'Loose', 'src/main/scala/com/demo/loose.scala', 1, 1, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps({
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1", "f3"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
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
    assert out["status"] == "ok"
    assert out["violations"] == 1
    assert out["orphan_scala_files"] == 1  # 无 package 声明诚实上报


def test_run_mixed_scala_and_py_not_enforceable(tmp_path):
    repo = _mk_scala_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'pyhelper.h', 'Function', 'h', 'pyhelper/mod.py', 1, 2, 'h')"
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
    assert "py + scala" in out["reason"]


def test_run_scala_derive_timeout(tmp_path, monkeypatch):
    repo = _mk_scala_repo(tmp_path)
    monkeypatch.setattr(
        s7_enforce_scala,
        "scan_scala",
        lambda repo, timeout=600: ({}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "scala scan exceeded" in out["reason"]


def test_cli_enforce_scala_wiring(tmp_path, capsys):
    repo = _mk_scala_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["engine"] == "scala-package-parser"
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
