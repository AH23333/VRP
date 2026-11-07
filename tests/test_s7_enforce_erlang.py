"""S7 Erlang 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/erlang-enforce-probe）。

引擎无外部执法器（内置源级 module/远程调用解析），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_erlang

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

ERLANG_CONTAINERS = {"queue-mod": ["queue"], "logging-mod": ["logging"]}

# 两层：app(queue) ← interface(logging)；违例 = queue 上向调用 logging
ERLANG_STRUCTURE = json.dumps(
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


def _mk_erlang_repo(tmp_path: Path, structure: str | None = ERLANG_STRUCTURE) -> Path:
    repo = tmp_path / "erlangrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'erlangrepo.queue.queue', 'Module', 'queue', 'queue/queue.erl', 1, 3, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'erlangrepo.logging.logging', 'Module', 'logging', 'logging/logging.erl', 1, 3, 'h')"
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
    # 违例：第 3 行远程调用 logging（远程调用行锚定目标）
    (repo / "queue" / "queue.erl").write_text(
        "-module(queue).\n"
        "-export([run/0]).\n"
        "run() -> logging:log_msg(x).\n",
        encoding="utf-8",
    )
    (repo / "logging" / "logging.erl").write_text(
        "-module(logging).\n"
        "-export([log_msg/1]).\n"
        "log_msg(X) -> ok.\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode_canary_forms():
    src = (
        "% logging:log_msg(x).\n"  # 行注释 canary
        'io:format("logging:log_msg(x)~n"),\n'  # 双引号串 canary（io 真外部调用）
        "logging:log_msg(real),\n"  # 真远程调用
        "ok = done.\n"
    )
    blanked = s7_enforce_erlang._blank_noncode(src)
    assert "logging:log_msg(real)," in blanked  # 代码存活
    assert "log_msg(x)" not in blanked  # canary 空白化（注释+串）
    assert len(blanked) == len(src)  # 等长空白
    assert blanked.count("\n") == src.count("\n")  # 换行保留
    rows = s7_enforce_erlang._iter_module_refs(blanked)
    assert rows == [
        {"target": "io", "line": 2},
        {"target": "logging", "line": 3},
    ]


def test_blank_char_literal_guards():
    """$% 字符字面量不误启注释；$" 字符字面量不误开串。"""
    src = 'A = $%,\nB = $",\nC = $a,\nlogging:log_msg(g),\n'
    blanked = s7_enforce_erlang._blank_noncode(src)
    assert "$" not in blanked  # 字符字面量消费
    assert '",' not in blanked  # $" 未开串（否则后续全空白化）
    rows = s7_enforce_erlang._iter_module_refs(blanked)
    assert rows == [{"target": "logging", "line": 4}]


def test_blank_unclosed_string_truncates_line():
    """Erlang 串不可跨行：未闭合串自愈截断行尾（不吞文件尾）。"""
    src = 'A = "unclosed\nlogging:log_msg(x),\n'
    blanked = s7_enforce_erlang._blank_noncode(src)
    assert "unclosed" not in blanked
    assert "logging:log_msg(x)," in blanked  # 行 2 存活——未吞文件尾
    assert blanked.count("\n") == 2  # 换行保留


def test_blank_escaped_quote_closes():
    src = 'S = "a \\" b",\nlogging:log_msg(x),\n'
    blanked = s7_enforce_erlang._blank_noncode(src)
    rows = s7_enforce_erlang._iter_module_refs(blanked)
    assert rows == [{"target": "logging", "line": 2}]


def test_blank_quoted_atom_preserved():
    """带引号原子原文保留——'my-mod':f() 远程调用模块名可见。"""
    src = "A = 'plain-atom',\nB = 'my-mod':f(),\n"
    blanked = s7_enforce_erlang._blank_noncode(src)
    assert "'plain-atom'" in blanked  # 原子保留
    rows = s7_enforce_erlang._iter_module_refs(blanked)
    assert rows == [{"target": "my-mod", "line": 2}]


def test_parse_module_forms():
    assert s7_enforce_erlang._parse_module("-module(foo).\nfoo() -> ok.\n") == "foo"
    assert s7_enforce_erlang._parse_module("-module('my-mod').\n") == "my-mod"
    assert s7_enforce_erlang._parse_module("-module (spaced).\n") == "spaced"
    assert s7_enforce_erlang._parse_module("foo() -> ok.\n") is None


def test_iter_module_refs_matrix():
    src = (
        "ok = mymod:real(x),\n"  # 远程调用
        "F = fun other:ref/2,\n"  # fun 远程引用 /arity
        "G = 'quo-mod':f(),\n"  # 带引号原子模块
        "-behaviour(gen_server).\n"  # behaviour 指令
        "-behavior(local_bh).\n"  # behavior 美式拼写
        "-import(proplists, [get_value/2]).\n"  # import 指令
        "H = Mod:Fun(x),\n"  # 变量动态派发不入图
        "I = #{key := 'value'},\n"  # map := —— fun 侧守卫拦截
        "J = 16#FF,\n"  # 数字进制 # 非注释
        "ok.\n"
    )
    blanked = s7_enforce_erlang._blank_noncode(src)
    rows = s7_enforce_erlang._iter_module_refs(blanked)
    assert rows == [
        {"target": "mymod", "line": 1},
        {"target": "other", "line": 2},
        {"target": "quo-mod", "line": 3},
        {"target": "gen_server", "line": 4},
        {"target": "local_bh", "line": 5},
        {"target": "proplists", "line": 6},
    ]


def test_iter_module_refs_map_colon_guard():
    """map `#{key := 'value'}`/typespec `::`/指令 `:-` 不误触远程调用。"""
    src = "M = #{key := 'value'},\n-spec f() -> ok :: atom().\n-module2(x),\nok.\n"
    blanked = s7_enforce_erlang._blank_noncode(src)
    assert s7_enforce_erlang._iter_module_refs(blanked) == []


def test_is_test_conventions():
    assert s7_enforce_erlang._is_test("test/a.erl")  # test/ 目录
    assert s7_enforce_erlang._is_test("tests/a.erl")  # tests/ 目录
    assert s7_enforce_erlang._is_test("apps/x/test/deep.erl")  # 嵌套
    assert s7_enforce_erlang._is_test("src/a_tests.erl")  # EUnit _tests
    assert s7_enforce_erlang._is_test("src/a_test.erl")  # EUnit _test
    assert s7_enforce_erlang._is_test("src/a_SUITE.erl")  # Common Test 套件
    assert not s7_enforce_erlang._is_test("Testing/a.erl")  # 目录段精确匹配
    assert not s7_enforce_erlang._is_test("src/a.erl")
    assert not s7_enforce_erlang._is_test("src/suite_helper.erl")  # suite 非后缀
    assert not s7_enforce_erlang._is_test("src/a.hrl")  # 非 .erl


def test_scan_erlang_layout(tmp_path):
    repo = tmp_path / "proj"
    for d in ("src", "test", "_build", "deps", "ebin", ".git"):
        (repo / d).mkdir(parents=True)
    (repo / "src" / "a.erl").write_text("-module(a).\nb:go(),\nok.\n", encoding="utf-8")
    (repo / "src" / "b.erl").write_text("-module(b).\ngo() -> ok.\n", encoding="utf-8")
    (repo / "test" / "a_tests.erl").write_text(
        "-module(a_tests).\na:go(),\nb:go(),\nok.\n", encoding="utf-8"
    )
    (repo / "_build" / "junk.erl").write_text("-module(junk).\n", encoding="utf-8")
    (repo / "deps" / "d.erl").write_text("-module(d).\n", encoding="utf-8")
    (repo / "ebin" / "e.erl").write_text("-module(e).\n", encoding="utf-8")
    (repo / ".git" / "g.erl").write_text("-module(g).\n", encoding="utf-8")
    (repo / "orphan.erl").write_text("no_module_here() -> ok.\n", encoding="utf-8")
    (repo / "notes.txt").write_text("-module(txt).\n", encoding="utf-8")
    file_mod, ref_rows, files_scanned, tests_excluded, timed_out = s7_enforce_erlang.scan_erlang(repo)
    assert timed_out is False
    assert files_scanned == 4  # _build/deps/ebin/.git 跳过
    assert tests_excluded == 1  # test/a_tests.erl
    assert file_mod == {  # 无 -module 的 orphan.erl 不入表
        "src/a.erl": "a",
        "src/b.erl": "b",
        "test/a_tests.erl": "a_tests",
    }
    assert ref_rows == [{"file": "src/a.erl", "line": 2, "target": "b"}]  # 测试调用不入列


def test_scan_erlang_timeout(tmp_path):
    repo = _mk_erlang_repo(tmp_path)
    _f, _r, _files, _e, timed_out = s7_enforce_erlang.scan_erlang(repo, timeout=0)
    assert timed_out is True


def test_build_erlang_graph_self_external_leaf():
    file_mod = {"src/self.erl": "self", "src/app.erl": "app", "src/leaf.erl": "leaf"}
    ref_rows = [
        {"file": "src/self.erl", "line": 2, "target": "self"},  # 自环跳过
        {"file": "src/app.erl", "line": 2, "target": "missing"},  # 外部模块跳过
        {"file": "src/app.erl", "line": 3, "target": "leaf"},
    ]
    graph, edges = s7_enforce_erlang.build_erlang_graph(file_mod, ref_rows)
    assert graph == {"app": ["leaf"], "leaf": [], "self": []}  # 叶模块全部入图
    assert edges == [{"file": "src/app.erl", "line": 3, "src": "app", "dst": "leaf"}]


def test_belongs_exact_match():
    assert s7_enforce_erlang._belongs("queue", ["queue"])
    assert not s7_enforce_erlang._belongs("queue2", ["queue"])  # 前缀污染守卫
    assert not s7_enforce_erlang._belongs("x", ["queue"])


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
    violations = s7_enforce_erlang._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "app" and v["imported"] == "iface"
    assert v["chain"] == ["app", "mid", "iface"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶模块（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"app": ["leaf"], "leaf": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["leaf"]}]
    violations = s7_enforce_erlang._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["app", "leaf"]


def test_find_violations_none_when_unreachable():
    graph = {"app": ["mid"], "mid": [], "iface": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["iface"]}]
    assert s7_enforce_erlang._find_violations(graph, contracts) == []


# ------------------------------------------------- derive / run_erlang E2E


def test_derive_erlang_containers(tmp_path):
    repo = _mk_erlang_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_erlang.derive_erlang_containers(repo, con, json.loads(ERLANG_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == ERLANG_CONTAINERS
    assert derived["erlang_files"] == 2  # queue/queue.erl + logging/logging.erl
    assert derived["extensions"] == []
    assert derived["orphan_files"] == 0
    assert derived["timed_out"] is False


def test_run_erlang_violation_e2e(tmp_path):
    repo = _mk_erlang_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_erlang.run_erlang(repo, con, json.loads(ERLANG_STRUCTURE), "active", ERLANG_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "erlang-module-parser"
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
    assert row[3] == "f1"  # queue/queue.erl 首符号
    assert row[4] == "queue/queue.erl"
    assert row[5] == 3 and row[6] == 3  # 违例远程调用行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "app"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "queue"  # 容器 = -module 声明
    assert payload["imported"] == "logging"
    assert payload["chain"] == ["queue", "logging"]
    assert payload["line"] == 3


def test_run_erlang_transitive_e2e(tmp_path):
    """传递链 core storage → queue → interface logging（模块容器中间层）。"""
    repo = _mk_erlang_repo(tmp_path)
    (repo / "storage").mkdir()
    (repo / "storage" / "storage.erl").write_text(
        "-module(storage).\n-export([put/1]).\nput(K) -> queue:run().\n", encoding="utf-8"
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
        out = s7_enforce_erlang.run_erlang(repo, con, payload, "active", containers)
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
    assert by_importer["storage"]["line"] == 3  # 锚定链尾直接调用边：queue.erl 第 3 行
    assert all(r[0] == "queue/queue.erl" for r in rows)


def test_run_erlang_idempotent(tmp_path):
    repo = _mk_erlang_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_erlang.run_erlang(repo, con, json.loads(ERLANG_STRUCTURE), "active", ERLANG_CONTAINERS)
        second = s7_enforce_erlang.run_erlang(repo, con, json.loads(ERLANG_STRUCTURE), "active", ERLANG_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_erlang_compliant_clears_old_findings(tmp_path):
    repo = _mk_erlang_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例调用
    (repo / "queue" / "queue.erl").write_text(
        "-module(queue).\n-export([run/0]).\nrun() -> ok.\n", encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_erlang.run_erlang(repo, con, json.loads(ERLANG_STRUCTURE), "active", ERLANG_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_erlang_no_modules(tmp_path):
    repo = _mk_erlang_repo(tmp_path)
    (repo / "queue" / "queue.erl").write_text("run() -> ok.\n", encoding="utf-8")
    (repo / "logging" / "logging.erl").write_text("log_msg(X) -> ok.\n", encoding="utf-8")
    con = _con(repo)
    try:
        out = s7_enforce_erlang.run_erlang(repo, con, json.loads(ERLANG_STRUCTURE), "active", ERLANG_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no erlang modules found" in out["reason"]


def test_run_erlang_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_erlang_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_erlang,
        "scan_erlang",
        lambda repo, timeout=600: ({}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_erlang.run_erlang(repo, con, json.loads(ERLANG_STRUCTURE), "active", ERLANG_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "erlang scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_erlang_test_file_excluded_e2e(tmp_path):
    """测试文件（EUnit _tests 约定）远程调用不产边；排除计数诚实上报。"""
    repo = _mk_erlang_repo(tmp_path)
    (repo / "test").mkdir()
    (repo / "test" / "queue_tests.erl").write_text(
        "-module(queue_tests).\nsimple_test() -> queue:run(), logging:log_msg(x).\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_erlang.run_erlang(repo, con, json.loads(ERLANG_STRUCTURE), "active", ERLANG_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["tests_excluded"] == 1
    assert out["files"] == 3  # files_scanned 含测试文件
    assert out["modules"] == 3  # 生产 queue/logging + 测试 queue_tests 入图（叶可见）
    assert out["violations"] == 1  # 仅生产调用产违例


# ------------------------------------------------------ run() 路由


def test_run_routes_erlang(tmp_path):
    """纯 Erlang 结构（.erl）→ run() 路由至 erlang 引擎。"""
    repo = _mk_erlang_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "erlang-module-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_erlang_and_py_not_enforceable(tmp_path):
    repo = _mk_erlang_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'erlangrepo.helper.mod', 'Function', 'mod', 'helper/mod.py', 1, 2, 'h')"
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
    assert "py + erlang" in out["reason"]


def test_cli_enforce_erlang_wiring(tmp_path, capsys):
    repo = _mk_erlang_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["engine"] == "erlang-module-parser"
    assert out["violations"] == 1
