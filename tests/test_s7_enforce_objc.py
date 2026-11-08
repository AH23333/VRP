"""S7 ObjC 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/objc-enforce-probe）。

引擎无外部执法器（内置源级 #import/#include 解析），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_objc

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

OBJC_CONTAINERS = {"queue-mod": ["queue"], "logging-mod": ["logging"]}

# 两层：app(queue/) ← interface(logging/)；违例 = queue 上向 import logging
OBJC_STRUCTURE = json.dumps(
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


def _mk_objc_repo(tmp_path: Path, structure: str | None = OBJC_STRUCTURE) -> Path:
    repo = tmp_path / "objcrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'objcrepo.queue.Queue', 'Class', 'Queue', 'queue/Queue.m', 3, 4, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'objcrepo.logging.Logging', 'Class', 'Logging', 'logging/Logging.m', 2, 3, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "queue").mkdir(parents=True)
    (repo / "logging").mkdir()
    (repo / "queue" / "Queue.h").write_text(
        "@interface Queue : NSObject\n@end\n", encoding="utf-8"
    )
    # 违例：第 2 行 #import "Logging.h"（import 指令行锚定目标）
    (repo / "queue" / "Queue.m").write_text(
        '#import "Queue.h"\n'
        '#import "Logging.h"\n'
        "@implementation Queue\n"
        "@end\n",
        encoding="utf-8",
    )
    (repo / "logging" / "Logging.h").write_text(
        "@interface Logging : NSObject\n@end\n", encoding="utf-8"
    )
    (repo / "logging" / "Logging.m").write_text(
        '#import "Logging.h"\n'
        "@implementation Logging\n"
        "@end\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode_canary_forms():
    src = (
        '// #import "evil1.h"\n'  # 行注释 canary
        '/* #import "evil2.h" */\n'  # 块注释 canary
        'NSString *s = @"evil3 #import \\"Api.h\\"";\n'  # @字符串 canary
        '#import "Real.h"\n'  # 真 import
        '#  include   "Util.h"\n'  # 真 include（记号间多空白）
        '#import <Foundation/Foundation.h>\n'  # 尖括号框架头（不入图）
        "char c = 'x';\n"
        "int keep = 1;\n"
    )
    out = s7_enforce_objc._blank_noncode(src)
    assert "Real.h" in out and "Util.h" in out  # 目标区保留（import 与 include 双指令）
    assert "evil1" not in out and "evil2" not in out and "evil3" not in out  # canary 空白化
    assert "keep = 1" in out  # 代码存活
    assert len(out) == len(src)  # 等长空白
    assert out.count("\n") == src.count("\n")  # 换行保留
    rows = s7_enforce_objc._iter_imports(out)
    assert rows == [("Real.h", 4), ("Util.h", 5)]  # 指令行锚定；尖括号不入列


def test_blank_bom_first_char():
    """文件首 BOM 空白化：bol 追踪不被污染——修复前行 1 #import 目标被串词法吞掉（假阴）。"""
    src = "\ufeff" + '#import "Bom.h"\nint keep = 1;\n'
    out = s7_enforce_objc._blank_noncode(src)
    assert len(out) == len(src)  # 等长空白（BOM 空白化非删除）
    assert "keep = 1" in out
    rows = s7_enforce_objc._iter_imports(out)
    assert rows == [("Bom.h", 1)]  # 修复前 []（BOM→bol=False→指令未跳过→串词法吞目标）
    # 无 BOM 对照不变
    rows2 = s7_enforce_objc._iter_imports(
        s7_enforce_objc._blank_noncode('#import "Bom.h"\nint keep = 1;\n')
    )
    assert rows2 == [("Bom.h", 1)]


def test_blank_directive_continuation():
    """反斜杠续行指令跨行保留；逻辑行锚定首行行号。"""
    src = '#import \\\n"Split.h"\nint x = 1;\n'
    out = s7_enforce_objc._blank_noncode(src)
    assert '"Split.h"' in out
    assert "int x = 1" in out
    rows = s7_enforce_objc._iter_imports(out)
    assert rows == [("Split.h", 1)]


def test_blank_unclosed_block_comment_truncates_eof():
    src = 'int a = 1;\n/* unterminated\n#import "Evil.h"\n'
    out = s7_enforce_objc._blank_noncode(src)
    assert "Evil" not in out  # 截断文件尾
    assert out.count("\n") == src.count("\n")  # 换行保留（行号不漂移）


def test_blank_unclosed_string_truncates_line():
    src = 'NSString *s = @"unclosed\nint keep = 2;\n'
    out = s7_enforce_objc._blank_noncode(src)
    assert "unclosed" not in out  # 未闭合串截断行尾
    assert "keep = 2" in out  # 后续代码不受损


def test_blank_raw_string_mm():
    """.mm ObjC++ raw string（R"delim(...)delim"）整体空白化。"""
    src = 'NSString *r = R"x(#import "Evil5.h")x";\nint keep = 3;\n'
    out = s7_enforce_objc._blank_noncode(src)
    assert "Evil5" not in out
    assert "keep = 3" in out
    assert 'R"x(' not in out  # raw 前缀一并空白化


def test_nsstring_escaped_quote_closes():
    src = 'NSString *s = @"a \\" b";\nint keep = 4;\n'
    out = s7_enforce_objc._blank_noncode(src)
    assert "keep = 4" in out  # 转义引号不提前闭串，其后代码存活


def test_directive_end_rejects_non_directives():
    # #pragma 非指令目标（不保留，按普通文本词法）
    out = s7_enforce_objc._blank_noncode("#pragma once\n")
    assert "pragma" in out  # 普通文本保留
    # 行中 # 非行首——不触发指令保留；其引号串正常空白化
    out2 = s7_enforce_objc._blank_noncode('x = 1 #import "Mid.h"\n')
    assert "Mid.h" not in out2
    # #importer 标识符前缀——import 后非引号 → 不保留
    out3 = s7_enforce_objc._blank_noncode('#importer "X.h"\n')
    assert s7_enforce_objc._iter_imports(out3) == []
    # 尖括号目标 → -1（不入图）
    out4 = s7_enforce_objc._blank_noncode('#import <Foundation/Foundation.h>\n')
    assert s7_enforce_objc._iter_imports(out4) == []


def test_is_test_conventions():
    assert s7_enforce_objc._is_test("Tests/StorageTests.m")  # Apple Tests/ 大写目录
    assert s7_enforce_objc._is_test("test/storage.m")
    assert s7_enforce_objc._is_test("tests/b.m")
    assert s7_enforce_objc._is_test("src/StorageTests.m")  # XCTest 驼峰 Tests
    assert s7_enforce_objc._is_test("src/StorageTest.m")  # XCTest 驼峰 Test
    assert s7_enforce_objc._is_test("src/test.m")  # 裸名
    assert s7_enforce_objc._is_test("src/test_storage.m")
    assert s7_enforce_objc._is_test("src/storage_test.m")
    assert s7_enforce_objc._is_test("src/storage_tests.m")
    assert s7_enforce_objc._is_test("src/StorageTests.h")  # 测试头文件同排除
    assert not s7_enforce_objc._is_test("src/TestingHelper.m")  # 驼峰误报守卫
    assert not s7_enforce_objc._is_test("testing/a.m")  # 目录段精确匹配
    assert not s7_enforce_objc._is_test("src/Storage.m")
    assert not s7_enforce_objc._is_test("src/Storage.h")


def test_scan_objc_layout(tmp_path):
    repo = tmp_path / "proj"
    (repo / "a").mkdir(parents=True)
    (repo / "b").mkdir()
    (repo / "Tests").mkdir()
    (repo / "DerivedData").mkdir()
    (repo / "Pods").mkdir()
    (repo / "a" / "Hdr.h").write_text("@interface Hdr : NSObject\n@end\n", encoding="utf-8")
    (repo / "a" / "Impl.m").write_text(
        '#import "Hdr.h"\n#import "b/Hdr.h"\n@implementation Hdr\n@end\n',
        encoding="utf-8",
    )
    (repo / "b" / "Hdr.h").write_text("@interface Hdr2 : NSObject\n@end\n", encoding="utf-8")
    (repo / "b" / "Impl2.mm").write_text(
        '#import "a/Hdr.h"\n@implementation Hdr2\n@end\n', encoding="utf-8"
    )
    (repo / "Tests" / "T.m").write_text('#import "a/Hdr.h"\n', encoding="utf-8")
    (repo / "DerivedData" / "Junk.m").write_text("#import \"a/Hdr.h\"\n", encoding="utf-8")
    (repo / "Pods" / "P.m").write_text("", encoding="utf-8")
    (repo / "root.m").write_text("int r = 0;\n", encoding="utf-8")
    objc_files, import_rows, files_scanned, tests_excluded, timed_out = s7_enforce_objc.scan_objc(repo)
    assert timed_out is False
    assert files_scanned == 6  # DerivedData/Pods 跳过；Tests 计入扫描
    assert tests_excluded == 1  # Tests/T.m
    assert objc_files == ["Tests/T.m", "a/Hdr.h", "a/Impl.m", "b/Hdr.h", "b/Impl2.mm", "root.m"]
    assert [r["target"] for r in import_rows] == ["Hdr.h", "b/Hdr.h", "a/Hdr.h"]  # 测试文件 import 不入列


def test_scan_objc_timeout(tmp_path):
    repo = _mk_objc_repo(tmp_path)
    _f, _r, _files, _e, timed_out = s7_enforce_objc.scan_objc(repo, timeout=0)
    assert timed_out is True


def test_resolve_import():
    file_set = {"a/Hdr.h", "b/Hdr.h", "sub/x/Deep.h", "root.h", "lib/Ext.h"}
    header_dirs = ["a", "b", "sub/x", "lib"]
    assert s7_enforce_objc._resolve_import("Hdr.h", "a", file_set, header_dirs) == "a/Hdr.h"  # 源目录首查
    assert s7_enforce_objc._resolve_import("Hdr.h", "zzz", file_set, header_dirs) == "a/Hdr.h"  # 含头目录兜底（header map 近似）
    assert s7_enforce_objc._resolve_import("b/Hdr.h", "a", file_set, header_dirs) == "b/Hdr.h"  # 子路径目标
    assert s7_enforce_objc._resolve_import("Deep.h", "a", file_set, header_dirs) == "sub/x/Deep.h"  # 嵌套头目录
    assert s7_enforce_objc._resolve_import("../a/Hdr.h", "b", file_set, header_dirs) == "a/Hdr.h"  # .. 弹栈
    assert s7_enforce_objc._resolve_import("Missing.h", "a", file_set, header_dirs) is None  # 未命中
    assert s7_enforce_objc._resolve_import("/abs/X.h", "a", file_set, header_dirs) is None  # 绝对路径
    assert s7_enforce_objc._resolve_import("C:\\x.h", "a", file_set, header_dirs) is None  # 盘符


def test_build_objc_graph_self_external_leaf():
    objc_files = ["lib/Ext.h", "src/a/Hdr.h", "src/a/Impl.m", "src/other.h"]
    import_rows = [
        {"file": "src/a/Impl.m", "line": 1, "target": "Hdr.h"},  # 同目录自环跳过
        {"file": "src/a/Impl.m", "line": 2, "target": "Missing.h"},  # 未命中跳过
        {"file": "src/a/Impl.m", "line": 3, "target": "../../lib/Ext.h"},  # 跨目录
    ]
    graph, edges = s7_enforce_objc.build_objc_graph(objc_files, import_rows)
    assert graph == {"lib": [], "src": [], "src/a": ["lib"]}  # 叶目录全部入图
    assert edges == [
        {"file": "src/a/Impl.m", "line": 3, "src": "src/a", "dst": "lib"}
    ]


def test_belongs_exact_match():
    assert s7_enforce_objc._belongs("src/a", ["src/a"])
    assert s7_enforce_objc._belongs("src/a/inner", ["src/a"])  # 父目录捕获子目录
    assert not s7_enforce_objc._belongs("src/ab", ["src/a"])  # 前缀污染守卫
    assert not s7_enforce_objc._belongs("x", ["src/a"])


def test_find_violations_bfs_shortest_chain():
    graph = {
        "app": ["mid", "queue"],
        "mid": ["iface"],  # 2 跳
        "queue": ["mid"],  # 3 跳路径
        "iface": [],
        "other": [],
    }
    contracts = [
        {
            "name": "layer: app must not import interface",
            "source_layer": "app",
            "forbidden_layer": "interface",
            "source_modules": ["app"],
            "forbidden_modules": ["iface"],
        }
    ]
    violations = s7_enforce_objc._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "app" and v["imported"] == "iface"
    assert v["chain"] == ["app", "mid", "iface"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶目录（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"app": ["leaf"], "leaf": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["leaf"]}]
    violations = s7_enforce_objc._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["app", "leaf"]


def test_find_violations_none_when_unreachable():
    graph = {"app": ["mid"], "mid": [], "iface": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["iface"]}]
    assert s7_enforce_objc._find_violations(graph, contracts) == []


# ------------------------------------------------- derive / run_objc E2E


def test_derive_objc_containers(tmp_path):
    repo = _mk_objc_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_objc.derive_objc_containers(con, json.loads(OBJC_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == OBJC_CONTAINERS
    assert derived["objc_files"] == 2  # 仅 .m 计数（.h 与 C 引擎歧义不计入）
    assert derived["extensions"] == []


def test_run_objc_violation_e2e(tmp_path):
    repo = _mk_objc_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_objc.run_objc(repo, con, json.loads(OBJC_STRUCTURE), "active", OBJC_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "objc-import-parser"
    assert out["contracts"] == 1
    assert out["files"] == 4
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
    assert row[3] == "f1"  # queue/Queue.m 首符号
    assert row[4] == "queue/Queue.m"
    assert row[5] == 2 and row[6] == 2  # 违例 import 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "app"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "queue"  # 容器 = repo 相对目录
    assert payload["imported"] == "logging"
    assert payload["chain"] == ["queue", "logging"]
    assert payload["line"] == 2


def test_run_objc_transitive_e2e(tmp_path):
    """传递链 core storage → queue → interface logging（目录容器中间层）。"""
    repo = _mk_objc_repo(tmp_path)
    (repo / "storage").mkdir()
    (repo / "storage" / "Storage.m").write_text(
        '#import "Queue.h"\n@implementation Storage\n@end\n', encoding="utf-8"
    )
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
    containers = {"core-mod": ["storage", "queue"], "iface-mod": ["logging"]}
    con = _con(repo)
    try:
        out = s7_enforce_objc.run_objc(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queue→logging 直连 + storage 经 queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["queue"]["chain"] == ["queue", "logging"]
    assert by_importer["storage"]["chain"] == ["storage", "queue", "logging"]
    assert by_importer["storage"]["line"] == 2  # 锚定链尾直接 import 边：Queue.m 第 2 行
    assert all(r[0] == "queue/Queue.m" for r in rows)


def test_run_objc_idempotent(tmp_path):
    repo = _mk_objc_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_objc.run_objc(repo, con, json.loads(OBJC_STRUCTURE), "active", OBJC_CONTAINERS)
        second = s7_enforce_objc.run_objc(repo, con, json.loads(OBJC_STRUCTURE), "active", OBJC_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_objc_compliant_clears_old_findings(tmp_path):
    repo = _mk_objc_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 import
    (repo / "queue" / "Queue.m").write_text(
        '#import "Queue.h"\n@implementation Queue\n@end\n', encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_objc.run_objc(repo, con, json.loads(OBJC_STRUCTURE), "active", OBJC_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_objc_no_files(tmp_path):
    repo = _mk_objc_repo(tmp_path)
    for rel in ("queue/Queue.h", "queue/Queue.m", "logging/Logging.h", "logging/Logging.m"):
        (repo / rel).unlink()
    con = _con(repo)
    try:
        out = s7_enforce_objc.run_objc(repo, con, json.loads(OBJC_STRUCTURE), "active", OBJC_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no objective-c sources" in out["reason"]


def test_run_objc_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_objc_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_objc,
        "scan_objc",
        lambda repo, timeout=600: ([], [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_objc.run_objc(repo, con, json.loads(OBJC_STRUCTURE), "active", OBJC_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "objective-c scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_objc_test_file_excluded_e2e(tmp_path):
    """测试文件（XCTest 驼峰命名 + Tests/ 目录）import 不产边；排除计数诚实上报。"""
    repo = _mk_objc_repo(tmp_path)
    (repo / "Tests").mkdir()
    (repo / "Tests" / "QueueTests.m").write_text(
        '#import "Queue.h"\n#import "Logging.h"\n@implementation QueueTests\n@end\n',
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_objc.run_objc(repo, con, json.loads(OBJC_STRUCTURE), "active", OBJC_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["tests_excluded"] == 1
    assert out["files"] == 5  # files_scanned 含测试文件
    assert out["dirs"] == 3  # queue/logging/Tests 叶目录全部入图
    assert out["violations"] == 1  # 仅生产 import 产违例（测试 import 不入图）


def test_run_objc_bom_first_line_import_e2e(tmp_path):
    """BOM+行 1 违例 import E2E：修复前 bol 被 BOM 污染→指令未跳过→目标被串词法吞（0 违例假阴）。"""
    repo = _mk_objc_repo(tmp_path)
    (repo / "queue" / "Queue.m").write_text(
        "\ufeff" + '#import "Logging.h"\n'  # 行 1 违例 import（BOM 后）
        '#import "Queue.h"\n'
        "@implementation Queue\n"
        "@end\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_objc.run_objc(repo, con, json.loads(OBJC_STRUCTURE), "active", OBJC_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 修复前 0（BOM 吞行 1 违例 import）
    assert out["anchored"] == 1
    row = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, start_line FROM findings"
    ).fetchone()
    assert row[0] == "queue/Queue.m"
    assert row[1] == 1  # 行 1 违例 import 锚定


# ------------------------------------------------------ run() 路由


def test_run_routes_objc(tmp_path):
    """纯 ObjC 结构（.h+.m）→ run() 路由至 objc 引擎（.h 不触发 c+objc 混仓）。"""
    repo = _mk_objc_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "objc-import-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_c_and_objc_not_enforceable(tmp_path):
    """真混仓（.c 源文件 + .m 并存）→ c + objc 诚实 not-enforceable。"""
    repo = _mk_objc_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'objcrepo.sub.x', 'Function', 'x', 'sub/x.c', 1, 2, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps({
        "modules": [
            {"module_id": "queue-mod", "name": "队列", "responsibility": "队", "symbol_fids": ["f1"], "layer": "app"},
            {"module_id": "logging-mod", "name": "日志", "responsibility": "志", "symbol_fids": ["f2"], "layer": "interface"},
            {"module_id": "c-mod", "name": "杂", "responsibility": "混", "symbol_fids": ["f3"], "layer": "app"},
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
    assert "c + objc" in out["reason"]


def test_run_mixed_objc_and_py_not_enforceable(tmp_path):
    repo = _mk_objc_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'objcrepo.helper.mod', 'Function', 'h', 'helper/mod.py', 1, 2, 'h')"
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
    assert "py + objc" in out["reason"]


def test_cli_enforce_objc_wiring(tmp_path, capsys):
    repo = _mk_objc_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["engine"] == "objc-import-parser"
    assert out["violations"] == 1
