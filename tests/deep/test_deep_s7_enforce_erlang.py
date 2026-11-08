"""S7 Erlang 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_erlang.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实布局——module/远程调用解析、
词法 canary（行注释/字符串/字符字面量内伪调用）、EUnit _tests 测试文件排除、
传递链；真仓 dogfood 另见 work/erlang-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：app(queue) ← interface(logging)
STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "app-mod", "name": "队列", "responsibility": "队", "symbol_fids": ["g1"], "layer": "app"},
            {"module_id": "iface-mod", "name": "日志", "responsibility": "志", "symbol_fids": ["g2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "interface", "may_depend_on": ["app"]},
            {"name": "app", "may_depend_on": []},
        ],
    },
    ensure_ascii=False,
)


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g1', 'p', 'repo.queue.queue', 'Module', 'queue', 'queue/queue.erl', 5, 8, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'repo.logging.logging', 'Module', 'logging', 'logging/logging.erl', 1, 3, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "queue").mkdir(parents=True)
    (repo / "logging").mkdir()
    (repo / "test").mkdir()
    # 违例：第 7 行远程调用 logging（调用行锚定目标）；词法 canary——
    # 行注释（1）/字符字面量 $%（2）/双引号串（3）/未闭合串截断（4）内伪调用
    # 若泄漏会多 queue→logging 边
    (repo / "queue" / "queue.erl").write_text(
        "% logging:log_msg(x).\n"
        "A = $%,\n"
        'B = "logging:log_msg(y)",\n'
        'C = "unterminated\n'
        "-module(queue).\n"
        "-export([run/0]).\n"
        "run() -> logging:log_msg(x).\n"
        "ok.\n",
        encoding="utf-8",
    )
    (repo / "logging" / "logging.erl").write_text(
        "-module(logging).\n"
        "-export([log_msg/1]).\n"
        "log_msg(X) -> ok.\n",
        encoding="utf-8",
    )
    # 测试文件：EUnit _tests 约定蓄意调用双模块——排除后不产边（若泄漏会引入
    # tests_excluded 计数漂移与伪边）
    (repo / "test" / "queue_tests.erl").write_text(
        "-module(queue_tests).\n"
        "simple_test() -> queue:run(), logging:log_msg(x).\n",
        encoding="utf-8",
    )
    return repo


def _rows(repo: Path) -> list[tuple]:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        return con.execute(
            "SELECT pathology, kind, source, anchor_fid, file_path, start_line, payload_json"
            " FROM findings WHERE source = 's7-enforce'"
        ).fetchall()
    finally:
        con.close()


def test_enforce_erlang_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "erlang-module-parser"  # 语言路由命中 Erlang 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # queue.erl + logging.erl + queue_tests.erl（扫描计数诚实）
    assert out["modules"] == 3  # queue/logging/queue_tests 入图（叶模块可见）
    assert out["tests_excluded"] == 1  # test/queue_tests.erl 文件级排除
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例调用文件首符号
    assert file_path == "queue/queue.erl"
    assert line == 7  # 违例调用行（canary 4 处不算：注释/字符/字符串/未闭合串内）
    data = json.loads(payload)
    assert data["source_layer"] == "app"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "queue"  # 容器 = -module 声明
    assert data["imported"] == "logging"
    assert data["chain"] == ["queue", "logging"]
    assert data["line"] == 7


def test_enforce_erlang_e2e_transitive_and_test_file_excluded(tmp_path):
    """三层链 storage → queue → logging + 测试文件调用不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "storage").mkdir()
    (repo / "storage" / "storage.erl").write_text(
        "-module(storage).\n"
        "-export([put/1]).\n"
        "put(K) -> queue:run().\n",
        encoding="utf-8",
    )
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["g3"], "layer": "core"},
                {"module_id": "app-mod", "name": "队列", "responsibility": "队", "symbol_fids": ["g1"], "layer": "app"},
                {"module_id": "iface-mod", "name": "日志", "responsibility": "志", "symbol_fids": ["g2"], "layer": "interface"},
            ],
            "layers": [
                {"name": "interface", "may_depend_on": ["app", "core"]},
                {"name": "app", "may_depend_on": ["core"]},
                {"name": "core", "may_depend_on": []},
            ],
        },
        ensure_ascii=False,
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g3', 'p', 'repo.storage.storage', 'Module', 'storage', 'storage/storage.erl', 1, 3, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 4
    assert out["modules"] == 4
    assert out["tests_excluded"] == 1
    assert out["violations"] == 3  # queue→logging 直连 + storage→queue 直连 + storage 经 queue 传递
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("queue", "logging")]["chain"] == ["queue", "logging"]
    assert by_pair[("storage", "queue")]["chain"] == ["storage", "queue"]
    assert by_pair[("storage", "logging")]["chain"] == ["storage", "queue", "logging"]
    # 锚定：queue 两条违例锚 queue.erl:7；storage→queue 锚 storage.erl:3
    anchored = {(r[4], r[5]) for r in rows}
    assert anchored == {("queue/queue.erl", 7), ("storage/storage.erl", 3)}


def test_enforce_erlang_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_erlang_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例调用（保留词法 canary——修复后 canary 也不得复活违例）
        (repo / "queue" / "queue.erl").write_text(
            "% logging:log_msg(x).\n"
            "A = $%,\n"
            'B = "logging:log_msg(y)",\n'
            'C = "unterminated\n'
            "-module(queue).\n"
            "-export([run/0]).\n"
            "run() -> ok.\n"
            "ok.\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_erlang_e2e_orphan_and_multiline_edge(tmp_path):
    """orphan 文件远程调用声明模块不崩溃（此前 build_erlang_graph KeyError:
    None）+ 多行远程调用违例（长实参换行合法 Erlang）——注释/串 canary
    包裹，锚定=模块原子行。"""
    repo = _mk_repo(tmp_path)
    # 违例调用改多行形态（保留注释/串 canary）
    (repo / "queue" / "queue.erl").write_text(
        "% logging:log_msg(x).\n"  # 1 注释 canary
        "-module(queue).\n"  # 2
        "-export([run/0]).\n"  # 3
        'B = "logging:log_msg(y)",\n'  # 4 串 canary
        "run() ->\n"  # 5
        "    logging:\n"  # 6 多行远程调用——模块原子行锚定
        "        log_msg(x),\n"  # 7
        "    ok.\n",  # 8
        encoding="utf-8",
    )
    # orphan：无 -module 但远程调用已声明模块 logging（非测试文件）
    (repo / "orphan.erl").write_text(
        "helper() -> logging:log_msg(x).\n", encoding="utf-8"
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 4  # + orphan.erl
    assert out["modules"] == 3  # orphan 无 -module 不入图
    assert out["tests_excluded"] == 1
    assert out["violations"] == 1
    assert out["anchored"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"
    assert file_path == "queue/queue.erl"
    assert line == 6  # 模块原子行（非 fun 行 7）
    data = json.loads(payload)
    assert data["chain"] == ["queue", "logging"]
    assert data["line"] == 6


def test_enforce_erlang_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "queue" / "queue.erl").write_text(
        "-module(queue).\n"
        "-export([run/0]).\n"
        "run() -> ok.\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    assert out["findings_written"] == 0
    assert _rows(repo) == []
