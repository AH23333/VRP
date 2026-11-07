"""S7 C# 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/cs-enforce-probe）。

引擎无外部执法器（内置源级 namespace/using 解析器），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import shutil
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_cs

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

CS_CONTAINERS = {"core-mod": ["Demo.Store"], "iface-mod": ["Demo.Api"]}

# 两层：core(Demo.Store) ← interface(Demo.Api)；违例 = store 上向 using api
CS_STRUCTURE = json.dumps(
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


def _mk_cs_repo(tmp_path: Path, structure: str | None = CS_STRUCTURE) -> Path:
    repo = tmp_path / "csrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'demo.Store', 'Class', 'Store', 'src/Store.cs', 5, 9, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'demo.Api', 'Class', 'Api', 'src/Api.cs', 2, 6, 'h')"
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
    (repo / "src" / "Api.cs").write_text(
        "namespace Demo.Api\n{\n    public class Api\n    {\n        public string Handle() => \"h\";\n    }\n}\n",
        encoding="utf-8",
    )
    # 违例：第 3 行 using Demo.Api（using 行号锚定目标）
    (repo / "src" / "Store.cs").write_text(
        "namespace Demo.Store\n"
        "{\n"
        "    using Demo.Api;\n"
        "\n"
        "    public class Store\n"
        "    {\n"
        "        public string Get() => \"s\";\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode():
    src = (
        "int a = 1; // line\n"
        'string b = "x/*y*/z"; /* block */ int c = 3;\n'
        'string v = @"a""b""c";\n'
        'string iv = $"hello {name}";\n'
        'string rv = $@"raw "" {a}";\n'
        "char ch = '\\n';\n"
        'string raw = """\nraw "q" /* no */\n""";\n'
    )
    out = s7_enforce_cs._blank_noncode(src)
    assert out.splitlines()[0].startswith("int a = 1;")  # 行注释空白化保行号
    assert '"x/*y*/z"' not in out  # 字符串整体空白化
    assert "block" not in out  # 块注释空白化
    assert "int c = 3;" in out  # 注释后代码保留
    assert 'a""b""c' not in out  # verbatim（"" 转义）空白化
    assert "hello" not in out  # 插值字符串空白化
    assert 'raw ""' not in out  # 插值 verbatim 空白化
    assert "'\\n'" not in out  # 字符字面量空白化
    assert "raw \"q\" /* no */" not in out  # raw string 空白化


def test_blank_noncode_unclosed_char_literal_advances():
    """非闭合字符字面量（WIP 文件）不卡死扫描——回退前进（c/rs 同守卫）。"""
    src = "char broken = 'x; int next = 1;\n"
    out = s7_enforce_cs._blank_noncode(src)
    assert "int next = 1;" in out


def test_parse_ns_spans_block_file_scoped_nested():
    src = (
        "namespace Demo.A\n"
        "{\n"
        "    namespace B\n"
        "    {\n"
        "        class X {}\n"
        "    }\n"
        "}\n"
        "namespace Demo.F;\n"
        "class Y {}\n"
    )
    spans = s7_enforce_cs._parse_ns_spans(s7_enforce_cs._blank_noncode(src))
    by_name = {(s["kind"], s["name"]): s for s in spans}
    assert by_name[("block", "Demo.A")]["full"] == "Demo.A"
    assert by_name[("block", "B")]["full"] == "Demo.A.B"  # 嵌套全名拼接
    assert by_name[("file", "Demo.F")]["close"] == len(src)  # 文件级到 EOF


def test_iter_usings_forms_and_attribution():
    src = (
        "// using Fake.One;\n"
        "/* using Fake.Two; */\n"
        'string s = "using Fake.Three;";\n'
        "using System.Collections.Generic;\n"  # 外部：仍解析（解析阶段决定内外）
        "using Demo.Api;\n"
        "namespace Demo.A\n"
        "{\n"
        "    using Demo.InA;\n"
        "    namespace B\n"
        "    {\n"
        "        using Demo.InB;\n"
        "    }\n"
        "}\n"
        "using var res = GetRes();\n"  # using 语句：不匹配
        "using (var res2 = GetRes()) { }\n"  # using 语句：不匹配
    )
    blanked = s7_enforce_cs._blank_noncode(src)
    spans = s7_enforce_cs._parse_ns_spans(blanked)
    rows = s7_enforce_cs._iter_usings(blanked, spans)
    assert [r["target"] for r in rows] == [
        "System.Collections.Generic",
        "Demo.Api",
        "Demo.InA",
        "Demo.InB",
    ]  # 伪 using（注释/字符串）与 using 语句不出现
    assert rows[0]["ns"] is None  # 文件级
    assert rows[1]["ns"] is None  # 文件级
    assert rows[2]["ns"] == "Demo.A"  # 块内归属
    assert rows[3]["ns"] == "Demo.A.B"  # 嵌套块取最内全名
    assert [r["line"] for r in rows] == [4, 5, 8, 11]


def test_iter_usings_static_alias_global():
    src = (
        "using static Demo.Util.Log;\n"
        "using Alias = Demo.Other.Thing;\n"
        "global using Demo.Global;\n"
    )
    blanked = s7_enforce_cs._blank_noncode(src)
    rows = s7_enforce_cs._iter_usings(blanked, [])
    assert [r["target"] for r in rows] == [
        "Demo.Util.Log",  # static 剥壳
        "Demo.Other.Thing",  # 别名取右侧
        "Demo.Global",  # global 归一
    ]


def test_resolve_ns_context_then_global():
    namespaces = {"Demo", "Demo.Api", "Demo.Store", "Other"}
    assert s7_enforce_cs._resolve("Demo.Api.Helper", None, namespaces) == "Demo.Api"
    assert s7_enforce_cs._resolve("Demo.Api", None, namespaces) == "Demo.Api"
    assert s7_enforce_cs._resolve("Store.Util", "Demo", namespaces) == "Demo.Store"  # 上下文优先
    assert s7_enforce_cs._resolve("System.Collections.Generic", None, namespaces) is None  # 外部
    assert s7_enforce_cs._resolve("Com.External.Foo", None, namespaces) is None


def test_scan_cs_tests_and_skip_dirs(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "Foo.Tests").mkdir()
    (repo / "bin").mkdir()
    (repo / "obj").mkdir()
    (repo / "src" / "Store.cs").write_text(
        "namespace Cs.Store;\nusing Cs.Api;\n", encoding="utf-8"
    )
    (repo / "src" / "Api.cs").write_text("namespace Cs.Api;\n", encoding="utf-8")
    # 测试目录：命名空间入图但不计 using
    (repo / "tests" / "StoreTests.cs").write_text(
        "namespace Cs.Store;\nusing Cs.Hidden.Forbidden;\n", encoding="utf-8"
    )
    # *.Tests 段（.NET 测试工程命名约定）：同上排除
    (repo / "Foo.Tests" / "Dir.cs").write_text(
        "namespace Cs.Tests;\nusing Cs.Hidden.Also;\n", encoding="utf-8"
    )
    # 构建产物：整体跳过
    (repo / "bin" / "Gen.cs").write_text("namespace Cs.Gen;\n", encoding="utf-8")
    (repo / "obj" / "Gen.cs").write_text("namespace Cs.Gen;\n", encoding="utf-8")
    file_ns, import_rows, files, tests_excluded, timed_out = s7_enforce_cs.scan_cs(repo)
    assert timed_out is False
    assert file_ns == {
        "src/Store.cs": ["Cs.Store"],
        "src/Api.cs": ["Cs.Api"],
        "tests/StoreTests.cs": ["Cs.Store"],
        "Foo.Tests/Dir.cs": ["Cs.Tests"],
    }  # bin/obj 跳过；测试文件命名空间仍入表（叶可见）
    assert tests_excluded == 2
    assert files == 4  # bin/obj 不计入扫描
    assert [r["target"] for r in import_rows] == ["Cs.Api"]  # 测试 using 不入列
    graph, edges = s7_enforce_cs.build_cs_graph(file_ns, import_rows)
    assert graph == {"Cs.Store": ["Cs.Api"], "Cs.Api": [], "Cs.Tests": []}
    assert edges == [
        {"file": "src/Store.cs", "line": 2, "src": "Cs.Store", "dst": "Cs.Api"}
    ]


def test_scan_cs_no_namespace_orphan(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "Default.cs").write_text(
        "public class Default {}\n", encoding="utf-8"  # 无 namespace 声明
    )
    file_ns, import_rows, files, tests_excluded, timed_out = s7_enforce_cs.scan_cs(repo)
    assert file_ns == {}
    assert import_rows == []
    assert files == 1  # 扫描计数仍诚实


def test_scan_cs_timeout(tmp_path):
    repo = _mk_cs_repo(tmp_path)
    file_ns, import_rows, files, tests, timed_out = s7_enforce_cs.scan_cs(repo, timeout=0)
    assert timed_out is True


def test_build_cs_graph_self_and_external_skipped():
    file_ns = {
        "a/A.cs": ["Ns.A"],
        "b/B.cs": ["Ns.B"],
    }
    import_rows = [
        {"file": "a/A.cs", "line": 1, "ns": "Ns.A", "target": "Ns.A.Same"},  # 同命名空间：无自环
        {"file": "a/A.cs", "line": 2, "ns": "Ns.A", "target": "Ns.B.Helper"},
        {"file": "a/A.cs", "line": 3, "ns": "Ns.A", "target": "System.Math"},  # 外部：跳过
        {"file": "missing/C.cs", "line": 1, "ns": None, "target": "Ns.B"},  # orphan 文件：跳过
    ]
    graph, edges = s7_enforce_cs.build_cs_graph(file_ns, import_rows)
    assert graph == {"Ns.A": ["Ns.B"], "Ns.B": []}  # 叶命名空间入图
    assert edges == [{"file": "a/A.cs", "line": 2, "src": "Ns.A", "dst": "Ns.B"}]


def test_belongs_parent_namespace_container():
    assert s7_enforce_cs._belongs("Demo.Store", ["Demo"])
    assert s7_enforce_cs._belongs("Demo", ["Demo"])
    assert not s7_enforce_cs._belongs("Demox", ["Demo"])  # 前缀污染
    assert not s7_enforce_cs._belongs("Other", ["Demo"])


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
    violations = s7_enforce_cs._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "a.x" and v["imported"] == "c.x"
    assert v["chain"] == ["a.x", "b.x", "c.x"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶命名空间（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"a.x": ["leaf.x"], "leaf.x": []}
    contracts = [{"name": "n", "source_modules": ["a.x"], "forbidden_modules": ["leaf.x"]}]
    violations = s7_enforce_cs._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["a.x", "leaf.x"]


def test_find_violations_none_when_unreachable():
    graph = {"a.x": ["b.x"], "b.x": [], "c.x": []}
    contracts = [{"name": "n", "source_modules": ["a.x"], "forbidden_modules": ["c.x"]}]
    assert s7_enforce_cs._find_violations(graph, contracts) == []


def test_derive_cs_containers(tmp_path):
    repo = _mk_cs_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_cs.derive_cs_containers(repo, con, json.loads(CS_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == CS_CONTAINERS
    assert derived["cs_files"] == 2
    assert derived["orphan_files"] == 0
    assert derived["timed_out"] is False
    assert derived["extensions"] == []


def test_derive_cs_containers_orphans_counted(tmp_path):
    repo = _mk_cs_repo(tmp_path)
    (repo / "src" / "Loose.cs").write_text(
        "public class Loose {}\n", encoding="utf-8"  # 无 namespace：orphan
    )
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'demo.Loose', 'Class', 'Loose', 'src/Loose.cs', 1, 1, 'h')"
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
        derived = s7_enforce_cs.derive_cs_containers(repo, con, json.loads(con.execute("SELECT payload_json FROM structure").fetchone()[0]))
    finally:
        con.close()
    assert derived["containers"] == CS_CONTAINERS  # orphan 不进容器
    assert derived["cs_files"] == 3
    assert derived["orphan_files"] == 1  # 无 namespace 声明诚实计数


# ------------------------------------------------- run_cs / run() E2E


def test_run_cs_violation_e2e(tmp_path):
    repo = _mk_cs_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_cs.run_cs(repo, con, json.loads(CS_STRUCTURE), "active", CS_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "cs-namespace-parser"
    assert out["contracts"] == 1
    assert out["files"] == 2
    assert out["namespaces"] == 2
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
    assert row[3] == "f1"  # Store.cs 首符号
    assert row[4] == "src/Store.cs"
    assert row[5] == 3 and row[6] == 3  # 违例 using 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "Demo.Store"
    assert payload["imported"] == "Demo.Api"
    assert payload["chain"] == ["Demo.Store", "Demo.Api"]
    assert payload["line"] == 3


def test_run_cs_transitive_e2e(tmp_path):
    """store → queue → api：core 层内传递链触达 interface（import-linter forbidden 同义）。"""
    repo = _mk_cs_repo(tmp_path)
    (repo / "src" / "Queue.cs").write_text(
        "namespace Demo.Queue\n"
        "{\n"
        "    using Demo.Api;\n"
        "\n"
        "    public class Queue\n"
        "    {\n"
        "        public string Work() => \"q\";\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "src" / "Store.cs").write_text(
        "namespace Demo.Store\n"
        "{\n"
        "    using Demo.Queue;\n"
        "\n"
        "    public class Store\n"
        "    {\n"
        "        public string Get() => \"s\";\n"
        "    }\n"
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
    containers = {"core-mod": ["Demo.Store", "Demo.Queue"], "iface-mod": ["Demo.Api"]}
    try:
        out = s7_enforce_cs.run_cs(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queue→api 直连 + store 经 queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["Demo.Queue"]["chain"] == ["Demo.Queue", "Demo.Api"]
    assert by_importer["Demo.Store"]["chain"] == ["Demo.Store", "Demo.Queue", "Demo.Api"]
    assert by_importer["Demo.Store"]["line"] == 3  # 锚定链尾直接 using 边：Queue.cs 第 3 行
    assert all(r[0] == "src/Queue.cs" for r in rows)


def test_run_cs_idempotent(tmp_path):
    repo = _mk_cs_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_cs.run_cs(repo, con, json.loads(CS_STRUCTURE), "active", CS_CONTAINERS)
        second = s7_enforce_cs.run_cs(repo, con, json.loads(CS_STRUCTURE), "active", CS_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_cs_compliant_clears_old_findings(tmp_path):
    repo = _mk_cs_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 using
    (repo / "src" / "Store.cs").write_text(
        "namespace Demo.Store\n{\n    public class Store\n    {\n        public string Get() => \"s\";\n    }\n}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_cs.run_cs(repo, con, json.loads(CS_STRUCTURE), "active", CS_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_cs_no_namespaces(tmp_path):
    repo = _mk_cs_repo(tmp_path)
    shutil.rmtree(repo / "src")
    con = _con(repo)
    try:
        out = s7_enforce_cs.run_cs(repo, con, json.loads(CS_STRUCTURE), "active", CS_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no c# namespaces" in out["reason"]


def test_run_cs_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_cs_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_cs,
        "scan_cs",
        lambda repo, timeout=600: ({}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_cs.run_cs(repo, con, json.loads(CS_STRUCTURE), "active", CS_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "c# scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_cs_test_dir_excluded_e2e(tmp_path):
    """tests/ 目录文件的 using 不产违例（go TestImports / java test-root 对齐）。"""
    repo = _mk_cs_repo(tmp_path)
    test = repo / "tests"
    test.mkdir()
    # 测试文件再造同向违例 using——若泄漏会双计违例
    (test / "StoreTests.cs").write_text(
        "namespace Demo.Store\n"
        "{\n"
        "    using Demo.Api;\n"
        "\n"
        "    public class StoreTests\n"
        "    {\n"
        "        public void T() { }\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_cs.run_cs(repo, con, json.loads(CS_STRUCTURE), "active", CS_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 仅 Store.cs 的生产 using
    assert out["tests_excluded"] == 1
    assert out["files"] == 3  # 测试文件扫描计数诚实


# ------------------------------------------------------ run() 路由


def test_run_routes_cs(tmp_path):
    """纯 C# 结构 → run() 路由至 cs 引擎并落 P9。"""
    repo = _mk_cs_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "cs-namespace-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_routes_cs_orphan_surfaced(tmp_path):
    repo = _mk_cs_repo(tmp_path)
    (repo / "src" / "Loose.cs").write_text(
        "public class Loose {}\n", encoding="utf-8"  # 无 namespace：orphan
    )
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'demo.Loose', 'Class', 'Loose', 'src/Loose.cs', 1, 1, 'h')"
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
    assert out["orphan_cs_files"] == 1  # 无 namespace 声明诚实上报


def test_run_mixed_cs_and_py_not_enforceable(tmp_path):
    repo = _mk_cs_repo(tmp_path)
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
    assert "py + cs" in out["reason"]


def test_run_cs_derive_timeout(tmp_path, monkeypatch):
    repo = _mk_cs_repo(tmp_path)
    monkeypatch.setattr(
        s7_enforce_cs,
        "scan_cs",
        lambda repo, timeout=600: ({}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "c# scan exceeded" in out["reason"]


def test_cli_enforce_cs_wiring(tmp_path, capsys):
    repo = _mk_cs_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["engine"] == "cs-namespace-parser"
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
