"""S7 Lua 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/lua-enforce-probe）。

引擎无外部执法器（内置源级 require 解析），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_lua

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

LUA_CONTAINERS = {"core-mod": ["src/storage.lua"], "iface-mod": ["src/api.lua"]}

# 两层：core(src/storage.lua) ← interface(src/api.lua)；违例 = storage 上向 require api
LUA_STRUCTURE = json.dumps(
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


def _mk_lua_repo(tmp_path: Path, structure: str | None = LUA_STRUCTURE) -> Path:
    repo = tmp_path / "luarepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'luarepo.src.storage.storagePut', 'Function', 'storagePut', 'src/storage.lua', 3, 7, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'luarepo.src.api.apiVersion', 'Function', 'apiVersion', 'src/api.lua', 2, 4, 'h')"
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
    (repo / "src" / "api.lua").write_text(
        "local M = {}\nfunction M.apiVersion()\n  return 1\nend\nreturn M\n",
        encoding="utf-8",
    )
    # 违例：第 1 行 require('src.api')（require 记号行锚定目标）
    (repo / "src" / "storage.lua").write_text(
        "local api = require('src.api')\n\nlocal M = {}\nfunction M.storagePut(key)\n  _ = key\n  return api.apiVersion()\nend\nreturn M\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode_canary_forms():
    src = (
        "-- require('evil1')\n"  # 行注释 canary
        "--[[ block\n"
        "require('evil2')\n"  # 块注释 canary（跨行）
        "]]\n"
        "local s = [[long\n"
        "require('evil3')]]\n"  # 长字符串 canary（跨行）
        "local m = require('src.api')\n"  # 真 require：括号
        "local n = require 'src.queue'\n"  # 真 require：无括号
        "local o = require'src.storage'\n"  # 真 require：无空格
        "local p = require[[src.init]]\n"  # 真 require：level-0 长括号
        "local d = require(mod_var)\n"  # 动态：变量实参
        "local c = require('src.' .. name)\n"  # 动态：拼接
        "local t = 'plain string'\n"
    )
    out = s7_enforce_lua._blank_noncode(src)
    assert "src.api" in out and "src.queue" in out and "src.storage" in out  # 目标区保留
    assert "src.init" in out  # 长括号实参保留
    assert "evil1" not in out and "evil2" not in out and "evil3" not in out  # 注释/长串 canary 空白化
    assert "plain string" not in out  # 普通串空白化
    assert len(out) == len(src)  # 等长空白
    assert out.count("\n") == src.count("\n")  # 换行保留
    rows = s7_enforce_lua._iter_requires(out)
    assert [(r["path"], r["line"]) for r in rows] == [
        ("src.api", 7), ("src.queue", 8), ("src.storage", 9), ("src.init", 10)
    ]  # require 记号行锚定；动态（变量/拼接）不入列


def test_blank_unclosed_string_truncates_line():
    src = "local a = 'unclosed\nlocal b = 2\n"
    out = s7_enforce_lua._blank_noncode(src)
    assert "unclosed" not in out  # 未闭合单行串截断行尾
    assert "local b = 2" in out  # 后续代码不受损


def test_blank_greedy_long_bracket_index_form():
    """贪心 [[ = 长字符串（正确 Lua 词法：t[[key]] 是索引字符串字面量）。"""
    src = "local v = t[[hidden]]\nlocal keep = 1\n"
    out = s7_enforce_lua._blank_noncode(src)
    assert "hidden" not in out
    assert "local keep = 1" in out
    assert len(out) == len(src)


def test_blank_z_escape_continues_string():
    """\\z 跳后续空白（含换行）延续字符串——后续代码不误吞。"""
    src = "local u = 'a\\z\n   b' local zc = 1\n"
    out = s7_enforce_lua._blank_noncode(src)
    assert "zc" in out  # 串正确闭合，其后代码存活
    assert "   b'" not in out  # 续串段空白化


def test_long_bracket_levels():
    assert s7_enforce_lua._long_open("[[x", 0) == 0
    assert s7_enforce_lua._long_open("[==[x", 0) == 2
    assert s7_enforce_lua._long_open("[=x", 0) is None  # 单 [ 非长括号
    assert s7_enforce_lua._long_open("[x", 0) is None
    # level 匹配闭合；低 level 闭合记号是内容
    assert s7_enforce_lua._long_end("[==[a]=]b]==]tail", 0, 2) == len("[==[a]=]b]==]")
    assert s7_enforce_lua._long_end("[[a]]tail", 0, 0) == len("[[a]]")


def test_unclosed_long_bracket_truncates_eof():
    src = "local s = [[unterminated\nmore\n"
    out = s7_enforce_lua._blank_noncode(src)
    assert "unterminated" not in out and "more" not in out  # 截断文件尾
    assert out.count("\n") == src.count("\n")  # 换行保留（行号不漂移）


def test_require_at_boundaries():
    text = "xrequire y require requirex z.require"
    assert not s7_enforce_lua._require_at(text, text.index("xrequire") + 1)  # 标识符尾
    assert s7_enforce_lua._require_at(text, text.index(" require") + 1)  # 空白后
    assert not s7_enforce_lua._require_at(text, text.index("requirex"))  # requirex 非记号
    assert s7_enforce_lua._require_at(text, text.index("z.require") + 2)  # 点前缀按全局（V1 局限）


def test_is_test_conventions():
    assert s7_enforce_lua._is_test("spec/a_spec.lua")
    assert s7_enforce_lua._is_test("tests/b.lua")
    assert s7_enforce_lua._is_test("x/test/c.lua")
    assert s7_enforce_lua._is_test("a_spec.lua")
    assert s7_enforce_lua._is_test("b_test.lua")
    assert s7_enforce_lua._is_test("test_c.lua")
    assert not s7_enforce_lua._is_test("testing/a.lua")  # 目录段精确匹配
    assert not s7_enforce_lua._is_test("src/mod.lua")
    assert not s7_enforce_lua._is_test("spec.lua")  # 文件名整段非目录段


def test_scan_lua_layout(tmp_path):
    repo = tmp_path / "proj"
    (repo / "lua").mkdir(parents=True)
    (repo / "spec").mkdir()
    (repo / "tests").mkdir()
    (repo / "lua" / "api.lua").write_text("return {}\n", encoding="utf-8")
    (repo / "lua" / "storage.lua").write_text(
        "local api = require('lua.api')\nreturn {}\n", encoding="utf-8"
    )
    (repo / "spec" / "a_spec.lua").write_text(
        "local s = require('lua.storage')\n", encoding="utf-8"
    )
    (repo / "tests" / "b.lua").write_text(
        "local a = require('lua.api')\n", encoding="utf-8"
    )
    (repo / "vendor").mkdir()
    (repo / "vendor" / "v.lua").write_text("", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "n.lua").write_text("", encoding="utf-8")
    (repo / ".luarocks").mkdir()
    (repo / ".luarocks" / "r.lua").write_text("", encoding="utf-8")
    file_set, import_rows, files_scanned, tests_excluded, timed_out = s7_enforce_lua.scan_lua(repo)
    assert timed_out is False
    assert files_scanned == 4  # vendor/node_modules/.luarocks 跳过
    assert tests_excluded == 2  # spec/a_spec.lua + tests/b.lua
    assert file_set == {
        "lua/api.lua", "lua/storage.lua", "spec/a_spec.lua", "tests/b.lua",
    }  # 测试文件容器仍入表（叶可见）
    assert import_rows == [
        {"path": "lua.api", "line": 1, "file": "lua/storage.lua"},
    ]  # 测试文件 require 不入列


def test_scan_lua_timeout(tmp_path):
    repo = _mk_lua_repo(tmp_path)
    _f, _r, _files, _e, timed_out = s7_enforce_lua.scan_lua(repo, timeout=0)
    assert timed_out is True


def test_resolve_require():
    file_set = {"lua/api.lua", "lua/sub/init.lua", "lua/b.lua", "mod.lua"}
    assert s7_enforce_lua._resolve_require("x/y.lua", "lua.api", file_set) == "lua/api.lua"
    assert s7_enforce_lua._resolve_require("x/y.lua", "lua.sub", file_set) == "lua/sub/init.lua"
    assert s7_enforce_lua._resolve_require("x/y.lua", "mod", file_set) == "mod.lua"
    assert s7_enforce_lua._resolve_require("lua/b.lua", "./api", file_set) == "lua/api.lua"  # ./ 相对当前文件目录
    assert s7_enforce_lua._resolve_require("lua/sub/init.lua", "../b", file_set) == "lua/b.lua"  # ../ 上溯一级
    assert s7_enforce_lua._resolve_require("lua/b.lua", "../api", file_set) is None  # ../ 出目录未命中诚实 miss
    assert s7_enforce_lua._resolve_require("x/y.lua", "lfs", file_set) is None  # 外部包跳过
    assert s7_enforce_lua._resolve_require("x/y.lua", "missing", file_set) is None  # 未命中
    assert s7_enforce_lua._resolve_require("x/y.lua", "lua/api.lua", file_set) == "lua/api.lua"  # 字面 .lua 宽容


def test_build_lua_graph_self_external_leaf():
    file_set = {"src/api.lua", "src/storage.lua", "src/other.lua"}
    import_rows = [
        {"path": "src.api", "line": 1, "file": "src/storage.lua"},
        {"path": "lfs", "line": 2, "file": "src/storage.lua"},  # 外部跳过
        {"path": "missing", "line": 3, "file": "src/storage.lua"},  # 未命中跳过
        {"path": "src.storage", "line": 4, "file": "src/storage.lua"},  # 自环跳过
    ]
    graph, edges = s7_enforce_lua.build_lua_graph(file_set, import_rows)
    assert graph == {
        "src/api.lua": [],  # 全部容器叶可见
        "src/other.lua": [],
        "src/storage.lua": ["src/api.lua"],
    }
    assert edges == [
        {"file": "src/storage.lua", "line": 1, "src": "src/storage.lua", "dst": "src/api.lua"}
    ]


def test_belongs_exact_match():
    assert s7_enforce_lua._belongs("src/storage.lua", ["src/storage.lua"])
    assert not s7_enforce_lua._belongs("src/storage.lua", ["src/storage.lua.bak"])
    assert not s7_enforce_lua._belongs("src/storage.lua", ["src/api.lua"])


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
    violations = s7_enforce_lua._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "app" and v["imported"] == "iface"
    assert v["chain"] == ["app", "mid", "iface"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶容器（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"app": ["leaf"], "leaf": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["leaf"]}]
    violations = s7_enforce_lua._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["app", "leaf"]


def test_find_violations_none_when_unreachable():
    graph = {"app": ["mid"], "mid": [], "iface": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["iface"]}]
    assert s7_enforce_lua._find_violations(graph, contracts) == []


# ------------------------------------------------- derive / run_lua E2E


def test_derive_lua_containers(tmp_path):
    repo = _mk_lua_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_lua.derive_lua_containers(repo, con, json.loads(LUA_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == LUA_CONTAINERS
    assert derived["lua_files"] == 2
    assert derived["extensions"] == []


def test_run_lua_violation_e2e(tmp_path):
    repo = _mk_lua_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_lua.run_lua(repo, con, json.loads(LUA_STRUCTURE), "active", LUA_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "lua-require-parser"
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
    assert row[3] == "f1"  # storage.lua 首符号
    assert row[4] == "src/storage.lua"
    assert row[5] == 1 and row[6] == 1  # 违例 require 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "src/storage.lua"
    assert payload["imported"] == "src/api.lua"
    assert payload["chain"] == ["src/storage.lua", "src/api.lua"]
    assert payload["line"] == 1


def test_run_lua_transitive_e2e(tmp_path):
    """传递链 core storage → queue → interface api（中间容器细分）。"""
    repo = _mk_lua_repo(tmp_path)
    (repo / "src" / "queue.lua").write_text(
        "local api = require('src.api')\n\nlocal M = {}\nfunction M.queueWork()\n  return api.apiVersion()\nend\nreturn M\n",
        encoding="utf-8",
    )
    (repo / "src" / "storage.lua").write_text(
        "local queue = require('src.queue')\n\nlocal M = {}\nfunction M.storagePut(key)\n  _ = key\n  return queue.queueWork()\nend\nreturn M\n",
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
    containers = {"core-mod": ["src/storage.lua", "src/queue.lua"], "iface-mod": ["src/api.lua"]}
    con = _con(repo)
    try:
        out = s7_enforce_lua.run_lua(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queue→api 直连 + storage 经 queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["src/queue.lua"]["chain"] == ["src/queue.lua", "src/api.lua"]
    assert by_importer["src/storage.lua"]["chain"] == ["src/storage.lua", "src/queue.lua", "src/api.lua"]
    assert by_importer["src/storage.lua"]["line"] == 1  # 锚定链尾直接 require 边：queue.lua 第 1 行
    assert all(r[0] == "src/queue.lua" for r in rows)


def test_run_lua_idempotent(tmp_path):
    repo = _mk_lua_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_lua.run_lua(repo, con, json.loads(LUA_STRUCTURE), "active", LUA_CONTAINERS)
        second = s7_enforce_lua.run_lua(repo, con, json.loads(LUA_STRUCTURE), "active", LUA_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_lua_compliant_clears_old_findings(tmp_path):
    repo = _mk_lua_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 require
    (repo / "src" / "storage.lua").write_text(
        "local M = {}\nfunction M.storagePut(key)\n  _ = key\n  return 0\nend\nreturn M\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_lua.run_lua(repo, con, json.loads(LUA_STRUCTURE), "active", LUA_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_lua_no_files(tmp_path):
    repo = _mk_lua_repo(tmp_path)
    (repo / "src" / "storage.lua").unlink()
    (repo / "src" / "api.lua").unlink()
    con = _con(repo)
    try:
        out = s7_enforce_lua.run_lua(repo, con, json.loads(LUA_STRUCTURE), "active", LUA_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no lua files" in out["reason"]


def test_run_lua_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_lua_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_lua,
        "scan_lua",
        lambda repo, timeout=600: (set(), [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_lua.run_lua(repo, con, json.loads(LUA_STRUCTURE), "active", LUA_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "lua scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_lua_test_file_excluded_e2e(tmp_path):
    """测试文件（spec 目录 + _spec 命名）require 不产边；排除计数诚实上报。"""
    repo = _mk_lua_repo(tmp_path)
    (repo / "spec").mkdir()
    (repo / "spec" / "storage_spec.lua").write_text(
        "local s = require('src.storage')\nlocal a = require('src.api')\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_lua.run_lua(repo, con, json.loads(LUA_STRUCTURE), "active", LUA_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["tests_excluded"] == 1
    assert out["files"] == 3  # files_scanned 含测试文件
    assert out["violations"] == 1  # 仅生产 require 产违例（spec 两条 require 不入图）


# ------------------------------------------------------ run() 路由


def test_run_routes_lua(tmp_path):
    """纯 Lua 结构 → run() 路由至 lua 引擎并落 P9。"""
    repo = _mk_lua_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "lua-require-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_lua_and_py_not_enforceable(tmp_path):
    repo = _mk_lua_repo(tmp_path)
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
    assert "py + lua" in out["reason"]


def test_cli_enforce_lua_wiring(tmp_path, capsys):
    repo = _mk_lua_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["engine"] == "lua-require-parser"
    assert out["violations"] == 1
