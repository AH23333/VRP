"""S7 Zig 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/zig-enforce-probe）。

引擎无外部执法器（内置源级 @import 解析），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_zig

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

ZIG_CONTAINERS = {"core-mod": ["src/storage.zig"], "iface-mod": ["src/api.zig"]}

# 两层：core(src/storage.zig) ← interface(src/api.zig)；违例 = storage 上向 import api
ZIG_STRUCTURE = json.dumps(
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


def _mk_zig_repo(tmp_path: Path, structure: str | None = ZIG_STRUCTURE) -> Path:
    repo = tmp_path / "zigrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'zigrepo.src.storage.storagePut', 'Function', 'storagePut', 'src/storage.zig', 3, 6, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'zigrepo.src.api.apiVersion', 'Function', 'apiVersion', 'src/api.zig', 1, 3, 'h')"
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
    (repo / "src" / "api.zig").write_text(
        "pub fn apiVersion() i32 {\n    return 1;\n}\n", encoding="utf-8"
    )
    # 违例：第 1 行 @import("api.zig")（import 行号锚定目标）
    (repo / "src" / "storage.zig").write_text(
        'const api = @import("api.zig");\n\npub fn storagePut(key: []const u8) i32 {\n    _ = key;\n    return api.apiVersion();\n}\n',
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode_canary_forms():
    src = (
        'const std = @import("std");\n'
        '// @import("evil1.zig")\n'
        "const s =\n"
        '    \\\\ @import("evil2.zig")\n'
        ";\n"
        'const t = "@import(nope)";\n'
        "const c = 'x';\n"
        "const c2 = '\\\\';\n"
        'const keep = @import("api.zig");\n'
        'const dyn = @import(name ++ ".zig");\n'
        'const spaced = @import (\n  "storage.zig"\n);\n'
    )
    out = s7_enforce_zig._blank_noncode(src)
    assert "std" in out and "api.zig" in out and "storage.zig" in out  # 目标区保留
    assert "evil1" not in out and "evil2" not in out  # 注释/多行串 canary 空白化
    assert "nope" not in out  # 普通串空白化
    assert "api.zig" in out  # @import 目标保留
    assert len(out) == len(src)  # 等长空白
    assert out.count("\n") == src.count("\n")  # 换行保留
    rows, suppressed = s7_enforce_zig._iter_imports(out, [])
    assert [(r["path"], r["line"]) for r in rows] == [
        ("std", 1), ("api.zig", 9), ("storage.zig", 11)
    ]  # @import 记号行锚定（跨行实参记声明行）
    assert suppressed == 0


def test_blank_unclosed_string_truncates_line():
    src = 'const a = "unclosed\nconst b = 2;\n'
    out = s7_enforce_zig._blank_noncode(src)
    assert "unclosed" not in out  # 未闭合单行串截断行尾
    assert "const b = 2;" in out  # 后续代码不受损


def test_blank_unclosed_char_truncates_line():
    src = "const a = 'x\nconst b = 2;\n"
    out = s7_enforce_zig._blank_noncode(src)
    assert "= 'x" not in out  # 未闭合字符字面量截断行尾
    assert "const b = 2;" in out


def test_blank_multiline_string_only_lines():
    """多行字符串 = 双反斜杠前缀行；行内 // 是内容不启注释。"""
    src = (
        "const m =\n"
        "    \\\\hello // world\n"
        '    \\\\tab\\there\n'
        ";\n"
        "const t = 1;\n"
    )
    out = s7_enforce_zig._blank_noncode(src)
    assert "hello" not in out and "world" not in out  # 内容行空白化
    assert "tab" not in out  # 第二内容行空白化（含转义）
    assert "const t = 1;" in out
    assert len(out) == len(src)


def test_import_at_boundaries():
    text = "x@import y @import @importx"
    assert not s7_enforce_zig._import_at(text, text.index("x@import") + 1)  # 标识符尾 @
    assert s7_enforce_zig._import_at(text, text.index(" @import") + 1)  # 空白后 @
    assert not s7_enforce_zig._import_at(text, text.index("@importx"))  # @importx 非记号


def test_iter_imports_test_block_suppression():
    blanked = s7_enforce_zig._blank_noncode(
        'const a = @import("api.zig");\n'
        'test {\n'
        '    _ = @import("hidden1.zig");\n'
        "}\n"
        'test "label" {\n'
        '    _ = @import("hidden2.zig");\n'
        "}\n"
    )
    ranges = s7_enforce_zig._test_block_ranges(blanked)
    assert len(ranges) == 2  # 裸 test 块 + 带标签 test 块
    rows, suppressed = s7_enforce_zig._iter_imports(blanked, ranges)
    assert [(r["path"], r["line"]) for r in rows] == [("api.zig", 1)]
    assert suppressed == 2  # 块内 @import 抑制计数


def test_test_block_ranges_rejections():
    blanked = s7_enforce_zig._blank_noncode(
        'const t = testing.test;\n'  # 字段访问 .test
        "const testing = 1;\n"
        "const latest = 2;\n"  # 标识符尾 test
    )
    assert s7_enforce_zig._test_block_ranges(blanked) == []


def test_test_block_nested_in_struct():
    blanked = s7_enforce_zig._blank_noncode(
        "const S = struct {\n"
        "    test {\n"
        '        _ = @import("hidden.zig");\n'
        "    }\n"
        "};\n"
    )
    ranges = s7_enforce_zig._test_block_ranges(blanked)
    assert len(ranges) == 1  # 容器内嵌 test 块同抓
    rows, suppressed = s7_enforce_zig._iter_imports(blanked, ranges)
    assert rows == [] and suppressed == 1


def test_test_block_unterminated_brace():
    blanked = s7_enforce_zig._blank_noncode('test {\n    const x = 1;\n')
    ranges = s7_enforce_zig._test_block_ranges(blanked)
    assert len(ranges) == 1
    assert ranges[0][1] == len(blanked)  # 未闭合花括号截断文件尾（前进保证）


def test_scan_zig_layout(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "api.zig").write_text(
        "pub fn apiVersion() i32 {\n    return 1;\n}\n", encoding="utf-8"
    )
    (repo / "src" / "storage.zig").write_text(
        'const api = @import("api.zig");\nconst std = @import("std");\n\npub fn storagePut() i32 {\n    return api.apiVersion();\n}\n',
        encoding="utf-8",
    )
    (repo / "src" / "main.zig").write_text(
        'const storage = @import("storage.zig");\n'
        'const std = @import("std");\n'
        'test {\n    _ = storage;\n    _ = @import("api.zig");\n}\n',
        encoding="utf-8",
    )
    (repo / ".zig-cache").mkdir()
    (repo / ".zig-cache" / "gen.zig").write_text("const x = 1;\n", encoding="utf-8")
    (repo / "zig-out").mkdir()
    (repo / "zig-out" / "bin.zig").write_text("const y = 2;\n", encoding="utf-8")
    file_set, import_rows, files, blocks, excl, timed_out = s7_enforce_zig.scan_zig(repo)
    assert timed_out is False
    assert files == 3  # .zig-cache（隐藏）/zig-out 跳过
    assert blocks == 1 and excl == 1  # main.zig 一个 test 块一条抑制
    assert file_set == {"src/api.zig", "src/main.zig", "src/storage.zig"}
    assert import_rows == [
        {"path": "storage.zig", "line": 1, "file": "src/main.zig"},
        {"path": "std", "line": 2, "file": "src/main.zig"},
        {"path": "api.zig", "line": 1, "file": "src/storage.zig"},
        {"path": "std", "line": 2, "file": "src/storage.zig"},
    ]  # test 块内 api.zig 不入列


def test_scan_zig_timeout(tmp_path):
    repo = _mk_zig_repo(tmp_path)
    _f, _r, _files, _b, _e, timed_out = s7_enforce_zig.scan_zig(repo, timeout=0)
    assert timed_out is True


def test_resolve_import():
    file_set = {"src/api.zig", "src/deep/mod.zig"}
    assert s7_enforce_zig._resolve_import("src/storage.zig", "api.zig", file_set) == "src/api.zig"
    assert s7_enforce_zig._resolve_import("src/deep/mod.zig", "../api.zig", file_set) == "src/api.zig"
    assert s7_enforce_zig._resolve_import("src/a.zig", "std", file_set) is None  # 包名跳过
    assert s7_enforce_zig._resolve_import("src/a.zig", "my_module", file_set) is None  # 命名模块跳过
    assert s7_enforce_zig._resolve_import("src/a.zig", "missing.zig", file_set) is None  # 未命中
    assert s7_enforce_zig._resolve_import("src/a.zig", "deep/mod.zig", file_set) == "src/deep/mod.zig"


def test_build_zig_graph_self_external_leaf():
    file_set = {"src/api.zig", "src/storage.zig", "src/other.zig"}
    import_rows = [
        {"path": "api.zig", "line": 1, "file": "src/storage.zig"},
        {"path": "std", "line": 2, "file": "src/storage.zig"},  # 外部跳过
        {"path": "missing.zig", "line": 3, "file": "src/storage.zig"},  # 未命中跳过
        {"path": "storage.zig", "line": 4, "file": "src/storage.zig"},  # 自环跳过
    ]
    graph, edges = s7_enforce_zig.build_zig_graph(file_set, import_rows)
    assert graph == {
        "src/api.zig": [],  # 全部容器叶可见
        "src/other.zig": [],
        "src/storage.zig": ["src/api.zig"],
    }
    assert edges == [
        {"file": "src/storage.zig", "line": 1, "src": "src/storage.zig", "dst": "src/api.zig"}
    ]


def test_belongs_exact_match():
    assert s7_enforce_zig._belongs("src/storage.zig", ["src/storage.zig"])
    assert not s7_enforce_zig._belongs("src/storage.zig", ["src/storage.zig.bak"])
    assert not s7_enforce_zig._belongs("src/storage.zig", ["src/api.zig"])


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
    violations = s7_enforce_zig._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "app" and v["imported"] == "iface"
    assert v["chain"] == ["app", "mid", "iface"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶容器（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"app": ["leaf"], "leaf": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["leaf"]}]
    violations = s7_enforce_zig._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["app", "leaf"]


def test_find_violations_none_when_unreachable():
    graph = {"app": ["mid"], "mid": [], "iface": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["iface"]}]
    assert s7_enforce_zig._find_violations(graph, contracts) == []


# ------------------------------------------------- derive / run_zig E2E


def test_derive_zig_containers(tmp_path):
    repo = _mk_zig_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_zig.derive_zig_containers(repo, con, json.loads(ZIG_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == ZIG_CONTAINERS
    assert derived["zig_files"] == 2
    assert derived["extensions"] == []


def test_run_zig_violation_e2e(tmp_path):
    repo = _mk_zig_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_zig.run_zig(repo, con, json.loads(ZIG_STRUCTURE), "active", ZIG_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "zig-import-parser"
    assert out["contracts"] == 1
    assert out["files"] == 2
    assert out["modules"] == 2  # 容器=文件：两文件两容器
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
    assert row[3] == "f1"  # storage.zig 首符号
    assert row[4] == "src/storage.zig"
    assert row[5] == 1 and row[6] == 1  # 违例 @import 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "src/storage.zig"
    assert payload["imported"] == "src/api.zig"
    assert payload["chain"] == ["src/storage.zig", "src/api.zig"]
    assert payload["line"] == 1


def test_run_zig_transitive_e2e(tmp_path):
    """传递链 core storage → queue → interface api（中间容器细分）。"""
    repo = _mk_zig_repo(tmp_path)
    (repo / "src" / "queue.zig").write_text(
        'const api = @import("api.zig");\n\npub fn queueWork() i32 {\n    return api.apiVersion();\n}\n',
        encoding="utf-8",
    )
    (repo / "src" / "storage.zig").write_text(
        'const queue = @import("queue.zig");\n\npub fn storagePut(key: []const u8) i32 {\n    _ = key;\n    return queue.queueWork();\n}\n',
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
    payload = json.loads(structure)
    containers = {"core-mod": ["src/storage.zig", "src/queue.zig"], "iface-mod": ["src/api.zig"]}
    con = _con(repo)
    try:
        out = s7_enforce_zig.run_zig(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queue→api 直连 + storage 经 queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["src/queue.zig"]["chain"] == ["src/queue.zig", "src/api.zig"]
    assert by_importer["src/storage.zig"]["chain"] == ["src/storage.zig", "src/queue.zig", "src/api.zig"]
    assert by_importer["src/storage.zig"]["line"] == 1  # 锚定链尾直接 @import 边：queue.zig 第 1 行
    assert all(r[0] == "src/queue.zig" for r in rows)


def test_run_zig_idempotent(tmp_path):
    repo = _mk_zig_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_zig.run_zig(repo, con, json.loads(ZIG_STRUCTURE), "active", ZIG_CONTAINERS)
        second = s7_enforce_zig.run_zig(repo, con, json.loads(ZIG_STRUCTURE), "active", ZIG_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_zig_compliant_clears_old_findings(tmp_path):
    repo = _mk_zig_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 @import
    (repo / "src" / "storage.zig").write_text(
        "pub fn storagePut(key: []const u8) i32 {\n    _ = key;\n    return 0;\n}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_zig.run_zig(repo, con, json.loads(ZIG_STRUCTURE), "active", ZIG_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_zig_no_files(tmp_path):
    repo = _mk_zig_repo(tmp_path)
    (repo / "src" / "storage.zig").unlink()
    (repo / "src" / "api.zig").unlink()
    con = _con(repo)
    try:
        out = s7_enforce_zig.run_zig(repo, con, json.loads(ZIG_STRUCTURE), "active", ZIG_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no zig files" in out["reason"]


def test_run_zig_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_zig_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_zig,
        "scan_zig",
        lambda repo, timeout=600: (set(), [], 0, 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_zig.run_zig(repo, con, json.loads(ZIG_STRUCTURE), "active", ZIG_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "zig scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_zig_test_block_excluded_e2e(tmp_path):
    """test 块内 @import 不产违例（refAllDecls/测试引用模式）；块计数诚实上报。"""
    repo = _mk_zig_repo(tmp_path)
    (repo / "src" / "storage.zig").write_text(
        'const api = @import("api.zig");\n\npub fn storagePut(key: []const u8) i32 {\n    _ = key;\n    return api.apiVersion();\n}\n\ntest "refs" {\n    _ = @import("api.zig");\n}\n',
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_zig.run_zig(repo, con, json.loads(ZIG_STRUCTURE), "active", ZIG_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["test_blocks"] == 1
    assert out["tests_excluded"] == 1  # 块内一条 @import 抑制
    assert out["violations"] == 1  # 仅块外生产 @import


# ------------------------------------------------------ run() 路由


def test_run_routes_zig(tmp_path):
    """纯 Zig 结构 → run() 路由至 zig 引擎并落 P9。"""
    repo = _mk_zig_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "zig-import-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_zig_and_py_not_enforceable(tmp_path):
    repo = _mk_zig_repo(tmp_path)
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
    assert "py + zig" in out["reason"]


def test_cli_enforce_zig_wiring(tmp_path, capsys):
    repo = _mk_zig_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["engine"] == "zig-import-parser"
    assert out["violations"] == 1
