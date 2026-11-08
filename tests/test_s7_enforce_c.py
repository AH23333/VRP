"""S7 C/C++ 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/c-enforce-probe）。

引擎无外部执法器（内置源级 #include 解析，目录容器），quick 层直接跑真
解析——mock 只用于超时分支。
"""

import json
import shutil
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_c

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

C_CONTAINERS = {"core-mod": ["src/store"], "iface-mod": ["src/api"]}

# 两层：core(src/store) ← interface(src/api)；违例 = store 上向 include api
C_STRUCTURE = json.dumps(
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


def _mk_c_repo(tmp_path: Path, structure: str | None = C_STRUCTURE) -> Path:
    repo = tmp_path / "crepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'demo.src.store.store_put', 'Function', 'store_put', 'src/store/storage.c', 3, 7, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'demo.src.api.api_handle', 'Function', 'api_handle', 'src/api/api.c', 3, 6, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "src" / "store").mkdir(parents=True)
    (repo / "src" / "api").mkdir(parents=True)
    (repo / "src" / "api" / "api.h").write_text(
        "#ifndef DEMO_API_H\n#define DEMO_API_H\n\nconst char *api_handle(void);\n\n#endif\n",
        encoding="utf-8",
    )
    (repo / "src" / "api" / "api.c").write_text(
        '#include "api.h"\n\nconst char *api_handle(void)\n{\n    return "ok";\n}\n',
        encoding="utf-8",
    )
    # 违例：第 1 行 include ../api/api.h（include 行号锚定目标）
    (repo / "src" / "store" / "storage.c").write_text(
        '#include "../api/api.h"\n\n'
        "int store_put(const char *key)\n"
        "{\n"
        "    (void)key;\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode_c_semantics():
    src = (
        "int a = 1; // line\n"
        "/* outer /* inner */ still-code int b = 2;\n"  # C 块注释不可嵌套：首个 */ 闭合，still-code 是代码（异于 Kotlin）
        'const char *s = "x/*y*/z";\n'
        "char c = '\\n';\n"
        "char q = '\"';\n"
        'const char *r = R"(raw "q" /* no */)";\n'
        'const char *u = u8R"delim(raw)delim";\n'
        'const char *id = VAR"x";\n'  # 标识符尾接引号：非 raw string（按普通字符串，VAR 保留）
    )
    out = s7_enforce_c._blank_noncode(src)
    assert out.splitlines()[0].startswith("int a = 1;")  # 行注释空白化保行号
    assert "inner" not in out  # 块注释内容空白化
    assert "still-code" in out  # 嵌套星斜后是代码（异于 Kotlin）
    assert "int b = 2;" in out  # 注释闭合后代码保留
    assert '"x/*y*/z"' not in out  # 字符串整体空白化
    assert "'\\n'" not in out and "'\"'" not in out  # 字符字面量空白化
    assert "raw" not in out and "no" not in out  # raw string 空白化（含内部引号/注释）
    assert "delim" not in out  # u8R 带分隔符 raw string 空白化
    assert "VAR" in out  # 标识符尾接引号：非 raw 前缀，VAR 是代码


def test_iter_includes_forms_and_continuation():
    blanked = s7_enforce_c._blank_noncode(
        "#include <stdio.h>\n"  # 1 尖括号系统头：不入图
        '#include "a.h"\n'  # 2
        "  #  include \"b.h\"  // trailing\n"  # 3 缩进+多空格+尾注释
        '#include"c.h"\n'  # 4 无空格
        "// #include \"comment.h\"\n"  # 5 注释：跳过
        '/* #include "block.h" */\n'  # 6 块注释：跳过
        'const char *s = "#include \\"str.h\\"";\n'  # 7 字符串：跳过
        '#include \\\n"cont.h"\n'  # 8-9 反斜杠续行：逻辑行 8
        "#include \"d.h\"\n"  # 10
    )
    rows = s7_enforce_c._iter_includes(blanked)
    assert rows == [("a.h", 2), ("b.h", 3), ("c.h", 4), ("cont.h", 8), ("d.h", 10)]


def test_resolve_include_priority():
    file_set = {
        "src/store/store.h",
        "src/api/api.h",
        "inc/common.h",
        "include/probe/api.h",
        "store.h",
    }
    header_dirs = ["inc", "include", "src"]
    # 源文件目录优先（C 标准引号查找首查）
    assert (
        s7_enforce_c._resolve_include("store.h", "src/store", file_set, header_dirs)
        == "src/store/store.h"
    )
    # ../ 词法规范化
    assert (
        s7_enforce_c._resolve_include("../api/api.h", "src/store", file_set, header_dirs)
        == "src/api/api.h"
    )
    # 仓根兜底
    assert (
        s7_enforce_c._resolve_include("inc/common.h", "src/store", file_set, header_dirs)
        == "inc/common.h"
    )
    # 含头文件目录（近似 -I）：源目录与仓根均未命中时
    assert (
        s7_enforce_c._resolve_include("probe/api.h", "src/store", file_set, header_dirs)
        == "include/probe/api.h"
    )
    # 系统头/绝对路径/盘符：跳过
    assert s7_enforce_c._resolve_include("stdio.h", "src/store", file_set, header_dirs) is None
    assert s7_enforce_c._resolve_include("/usr/include/x.h", "src/store", file_set, header_dirs) is None
    assert s7_enforce_c._resolve_include("C:\\x.h", "src/store", file_set, header_dirs) is None


def test_is_test_conventions():
    assert s7_enforce_c._is_test("tests/test_storage.c")  # 目录段
    assert s7_enforce_c._is_test("src/test/helper.c")  # 深层目录段
    assert s7_enforce_c._is_test("src/store/test_storage.c")  # test_* 文件名
    assert s7_enforce_c._is_test("src/store/storage_test.c")  # *_test 文件名
    assert s7_enforce_c._is_test("src/store/storage_tests.cc")  # *_tests 文件名
    assert s7_enforce_c._is_test("src/store/test.c")  # 裸名 test.c
    assert not s7_enforce_c._is_test("src/store/storage.c")
    assert not s7_enforce_c._is_test("src/testing/harness.c")  # testing 不是 test 段
    assert not s7_enforce_c._is_test("latest/holder.c")  # 子串污染防护


def test_scan_c_skip_dirs_and_tests(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src" / "store").mkdir(parents=True)
    (repo / "src" / "api").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "build" / "gen").mkdir(parents=True)
    (repo / "vendor" / "third").mkdir(parents=True)
    (repo / ".hidden").mkdir()
    (repo / "src" / "api" / "api.h").write_text("#pragma once\n", encoding="utf-8")
    (repo / "src" / "api" / "api.c").write_text('#include "api.h"\n', encoding="utf-8")
    (repo / "src" / "store" / "storage.c").write_text(
        '#include "../api/api.h"\n#include <stdio.h>\n', encoding="utf-8"
    )
    # 测试文件（目录段）：include 不入列
    (repo / "tests" / "test_main.c").write_text(
        '#include "../src/api/api.h"\n', encoding="utf-8"
    )
    # 测试文件（文件名约定）：include 不入列
    (repo / "src" / "store" / "test_storage.c").write_text(
        '#include "../api/api.h"\n', encoding="utf-8"
    )
    # 构建/第三方/隐藏目录：整体跳过
    (repo / "build" / "gen" / "gen.c").write_text('#include "x.h"\n', encoding="utf-8")
    (repo / "vendor" / "third" / "third.c").write_text('#include "x.h"\n', encoding="utf-8")
    (repo / ".hidden" / "h.c").write_text('#include "x.h"\n', encoding="utf-8")
    # 非法后缀：不扫
    (repo / "src" / "notes.txt").write_text('#include "x.h"\n', encoding="utf-8")
    c_files, include_rows, files, tests_excluded, timed_out = s7_enforce_c.scan_c(repo)
    assert timed_out is False
    assert c_files == [
        "src/api/api.c",
        "src/api/api.h",
        "src/store/storage.c",
        "src/store/test_storage.c",
        "tests/test_main.c",
    ]  # build/vendor/.hidden 跳过；.txt 不扫
    assert files == 5
    assert tests_excluded == 2  # 目录段 + 文件名约定各一
    assert [r["target"] for r in include_rows] == [
        "api.h",
        "../api/api.h",
    ]  # 测试 include 不入列；<stdio.h> 尖括号不入列


def test_scan_c_timeout(tmp_path):
    repo = _mk_c_repo(tmp_path)
    c_files, include_rows, files, tests, timed_out = s7_enforce_c.scan_c(repo, timeout=0)
    assert timed_out is True


def test_build_c_graph_self_unresolved_leaf():
    c_files = [
        "a/a.c",
        "a/a.h",
        "b/b.c",
        "hdr/only.h",  # 只含头文件的叶目录：无出边也入图
    ]
    include_rows = [
        {"file": "a/a.c", "line": 1, "target": "a.h"},  # 同目录：自环跳过
        {"file": "a/a.c", "line": 2, "target": "b/b.h"},  # 目标不在仓内：跳过
        {"file": "b/b.c", "line": 1, "target": "../hdr/only.h"},  # ../ 解析成功
        {"file": "b/b.c", "line": 2, "target": "stdlib.h"},  # 系统：跳过
    ]
    graph, edges = s7_enforce_c.build_c_graph(c_files, include_rows)
    assert graph == {"a": [], "b": ["hdr"], "hdr": []}  # 叶目录 hdr 入图
    assert edges == [{"file": "b/b.c", "line": 1, "src": "b", "dst": "hdr"}]


def test_belongs_prefix_pollution():
    assert s7_enforce_c._belongs("src/core/inner", ["src/core"])
    assert s7_enforce_c._belongs("src/core", ["src/core"])
    assert not s7_enforce_c._belongs("src/corefish", ["src/core"])  # 前缀污染
    assert not s7_enforce_c._belongs("other", ["src/core"])


def test_find_violations_bfs_shortest_chain():
    graph = {"src/a": ["src/b", "src/mid"], "src/mid": ["src/c"], "src/b": ["src/c"], "src/c": [], "src/w": []}
    contracts = [
        {
            "name": "layer: core must not import interface",
            "source_layer": "core",
            "forbidden_layer": "interface",
            "source_modules": ["src/a"],
            "forbidden_modules": ["src/c"],
        }
    ]
    violations = s7_enforce_c._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "src/a" and v["imported"] == "src/c"
    assert v["chain"] == ["src/a", "src/b", "src/c"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶目录（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"src/a": ["leaf"], "leaf": []}
    contracts = [{"name": "n", "source_modules": ["src/a"], "forbidden_modules": ["leaf"]}]
    violations = s7_enforce_c._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["src/a", "leaf"]


def test_find_violations_none_when_unreachable():
    graph = {"src/a": ["src/b"], "src/b": [], "src/c": []}
    contracts = [{"name": "n", "source_modules": ["src/a"], "forbidden_modules": ["src/c"]}]
    assert s7_enforce_c._find_violations(graph, contracts) == []


def test_derive_c_containers(tmp_path):
    repo = _mk_c_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_c.derive_c_containers(con, json.loads(C_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == C_CONTAINERS
    assert derived["c_files"] == 2
    assert derived["extensions"] == []


def test_derive_c_containers_root_file_and_extensions(tmp_path):
    repo = _mk_c_repo(tmp_path)
    (repo / "main.c").write_text("int main(void)\n{\n    return 0;\n}\n", encoding="utf-8")
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'demo.main', 'Function', 'main', 'main.c', 1, 3, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f4', 'p', 'demo.zig', 'Other', 'z', 'helper.zig', 1, 2, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps({
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1", "f3", "f4"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    }, ensure_ascii=False),))
    con.commit()
    try:
        derived = s7_enforce_c.derive_c_containers(con, json.loads(con.execute("SELECT payload_json FROM structure").fetchone()[0]))
    finally:
        con.close()
    assert derived["containers"] == C_CONTAINERS  # 根级 main.c 无目录容器
    assert derived["c_files"] == 3
    assert derived["extensions"] == [".zig"]  # 非 C 后缀诚实观测


# ------------------------------------------------- run_c / run() E2E


def test_run_c_violation_e2e(tmp_path):
    repo = _mk_c_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_c.run_c(repo, con, json.loads(C_STRUCTURE), "active", C_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "c-include-parser"
    assert out["contracts"] == 1
    assert out["files"] == 3
    assert out["dirs"] == 2
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
    assert row[3] == "f1"  # storage.c 首符号
    assert row[4] == "src/store/storage.c"
    assert row[5] == 1 and row[6] == 1  # 违例 include 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "src/store"  # 容器 = 目录
    assert payload["imported"] == "src/api"
    assert payload["chain"] == ["src/store", "src/api"]
    assert payload["line"] == 1


def test_run_c_transitive_e2e(tmp_path):
    """store → queue → api：core 层内传递链触达 interface（八引擎传递闭包同义）。"""
    repo = _mk_c_repo(tmp_path)
    (repo / "src" / "queue").mkdir()
    (repo / "src" / "queue" / "queue.h").write_text(
        "#ifndef DEMO_QUEUE_H\n#define DEMO_QUEUE_H\n\nint task_enqueue(const char *task);\n\n#endif\n",
        encoding="utf-8",
    )
    (repo / "src" / "queue" / "queue.c").write_text(
        '#include "../api/api.h"\n\n'
        "int task_enqueue(const char *task)\n"
        "{\n"
        "    (void)task;\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "src" / "store" / "storage.c").write_text(
        '#include "../queue/queue.h"\n\n'
        "int store_put(const char *key)\n"
        "{\n"
        "    (void)key;\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
                {"module_id": "queue-mod", "name": "队列", "responsibility": "队", "symbol_fids": [], "layer": "core"},
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
    containers = {"core-mod": ["src/store", "src/queue"], "queue-mod": [], "iface-mod": ["src/api"]}
    try:
        out = s7_enforce_c.run_c(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queue→api 直连 + store 经 queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["src/queue"]["chain"] == ["src/queue", "src/api"]
    assert by_importer["src/store"]["chain"] == ["src/store", "src/queue", "src/api"]
    assert by_importer["src/store"]["line"] == 1  # 锚定链尾直接 include 边：queue.c 第 1 行
    assert all(r[0] == "src/queue/queue.c" for r in rows)


def test_run_c_idempotent(tmp_path):
    repo = _mk_c_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_c.run_c(repo, con, json.loads(C_STRUCTURE), "active", C_CONTAINERS)
        second = s7_enforce_c.run_c(repo, con, json.loads(C_STRUCTURE), "active", C_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_c_compliant_clears_old_findings(tmp_path):
    repo = _mk_c_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 include
    (repo / "src" / "store" / "storage.c").write_text(
        "int store_put(const char *key)\n{\n    (void)key;\n    return 0;\n}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_c.run_c(repo, con, json.loads(C_STRUCTURE), "active", C_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_c_no_sources(tmp_path):
    repo = _mk_c_repo(tmp_path)
    shutil.rmtree(repo / "src")
    con = _con(repo)
    try:
        out = s7_enforce_c.run_c(repo, con, json.loads(C_STRUCTURE), "active", C_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no c/c++ sources" in out["reason"]


def test_run_c_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_c_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_c,
        "scan_c",
        lambda repo, timeout=600: ([], [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_c.run_c(repo, con, json.loads(C_STRUCTURE), "active", C_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "c/c++ scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_c_test_file_excluded_e2e(tmp_path):
    """src/store/test_storage.c（文件名约定）的 include 不产违例——泄漏则链变形。"""
    repo = _mk_c_repo(tmp_path)
    # 测试 canary 与生产同向 include api——泄漏则双计违例
    (repo / "src" / "store" / "test_storage.c").write_text(
        '#include "../api/api.h"\n\n'
        "void test_store(void)\n"
        "{\n"
        "    (void)api_handle();\n"
        "}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_c.run_c(repo, con, json.loads(C_STRUCTURE), "active", C_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 仅 storage.c 的生产 include
    assert out["tests_excluded"] == 1
    assert out["files"] == 4  # 测试文件扫描计数诚实


# ------------------------------------------------------ run() 路由


def test_run_routes_c(tmp_path):
    """纯 C 结构 → run() 路由至 C 引擎并落 P9。"""
    repo = _mk_c_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "c-include-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_c_and_py_not_enforceable(tmp_path):
    repo = _mk_c_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'demo.helper', 'Function', 'helper', 'pyhelper/mod.py', 1, 2, 'h')"
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
    assert "py + c" in out["reason"]


def test_cli_enforce_c_wiring(tmp_path, capsys):
    repo = _mk_c_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["engine"] == "c-include-parser"
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


# ------------------------------------------- review 批回归（BOM+指令间隙注释）


def test_blank_bom_first_include():
    # BOM+行 1 include（review 批：BOM 空白化——bol 追踪/行首指令词法不再被污染，
    # 目标不再被串词法吞、`^[ \t]*#` 匹配恢复）
    out = s7_enforce_c._blank_noncode("\ufeff#include \"Bom.h\"\nint x;\n")
    rows = s7_enforce_c._iter_includes(out)
    assert rows == [("Bom.h", 1)]
    assert "Bom.h" in out  # include 目标保留
    assert out[0] == " "  # BOM 字符空白化
    # BOM 无 include：无影响
    out2 = s7_enforce_c._blank_noncode("\ufeffint x;\n")
    assert "int x;" in out2
    # 无 BOM 对照不回归
    out3 = s7_enforce_c._blank_noncode('#include "Bom.h"\nint x;\n')
    assert s7_enforce_c._iter_includes(out3) == [("Bom.h", 1)]


def test_iter_includes_directive_gap_comments():
    # 指令记号间注释（C 预处理注释=空白语义——review 批修正：间隙注释不再丢边）
    blanked = s7_enforce_c._blank_noncode(
        '#include /* c */ "Gap.h"\n'  # 1 目标前间隙块注释
        '#/* c */include "Hash.h"\n'  # 2 # 与 include 之间注释
        '#include "Trail.h" /* 尾注释 */\n'  # 3 目标后尾注释：现状已正确
        '#include // c\n"x.h"\n'  # 4-5 行注释吞目标（非法 C）：拒（"x.h" 落行 5）
        '#include // c "x.h"\n'  # 6 注释体吞目标（非法 C）：拒
        '#include "Ok.h"\n'  # 7
    )
    rows = s7_enforce_c._iter_includes(blanked)
    assert rows == [("Gap.h", 1), ("Hash.h", 2), ("Trail.h", 3), ("Ok.h", 7)]
    # 间隙注释空白化后正则命中，目标保留
    assert "Gap.h" in blanked and "Hash.h" in blanked and "Trail.h" in blanked


def test_scan_c_bom_and_gap_comment(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.c").write_text(
        '\ufeff#include /* c */ "b.h"\nint main(void)\n{\n    return 0;\n}\n',
        encoding="utf-8",
    )
    (repo / "src" / "b.h").write_text("int g;\n", encoding="utf-8")
    c_files, include_rows, files, tests_excluded, timed_out = s7_enforce_c.scan_c(repo)
    assert timed_out is False
    assert files == 2 and tests_excluded == 0
    assert [r["target"] for r in include_rows] == ["b.h"]  # BOM+间隙注释不丢边
