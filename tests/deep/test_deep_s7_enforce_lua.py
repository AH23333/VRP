"""S7 Lua 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_lua.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实布局——require 全形态、词法
canary（注释/长字符串内伪 require）、测试文件排除、传递链；真仓 dogfood
另见 work/lua-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：core(src/storage.lua) ← interface(src/api.lua)
STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["g1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["g2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
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
        " VALUES ('g1', 'p', 'repo.src.storage.storagePut', 'Function', 'storagePut', 'src/storage.lua', 6, 12, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'repo.src.api.apiVersion', 'Function', 'apiVersion', 'src/api.lua', 2, 4, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "src").mkdir(parents=True)
    (repo / "spec").mkdir()
    (repo / "src" / "api.lua").write_text(
        "local M = {}\n"
        "function M.apiVersion()\n"
        "  return 1\n"
        "end\n"
        "return M\n",
        encoding="utf-8",
    )
    # 违例：第 7 行 require('src.api')（require 记号行锚定目标）；词法 canary——
    # 注释（1）/块注释（3）/长字符串（6）内伪 require 若泄漏会多 storage→interface 边
    (repo / "src" / "storage.lua").write_text(
        "-- local x = require('src.api')\n"
        "--[[ block\n"
        "local y = require('src.api')\n"
        "]]\n"
        "local z = [[long\n"
        "local w = require('src.api')]]\n"
        "local api = require('src.api')\n"
        "\n"
        "local M = {}\n"
        "function M.storagePut(key)\n"
        "  _ = key\n"
        "  return api.apiVersion()\n"
        "end\n"
        "return M\n",
        encoding="utf-8",
    )
    # 测试文件：busted 形态蓄意 require 双模块——排除后不产边（若泄漏会引入
    # spec→storage→api 路径与 tests_excluded 计数漂移）
    (repo / "spec" / "storage_spec.lua").write_text(
        "local storage = require('src.storage')\n"
        "local api = require('src.api')\n"
        "describe('storage', function()\n"
        "  it('puts', function()\n"
        "    assert.is_not_nil(storage)\n"
        "  end)\n"
        "end)\n",
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


def test_enforce_lua_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "lua-require-parser"  # 语言路由命中 Lua 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # 3 个 .lua 文件（扫描计数诚实，含测试文件）
    assert out["modules"] == 3  # 容器=文件：三文件三容器
    assert out["tests_excluded"] == 1  # spec/storage_spec.lua 文件级排除
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 require 文件首符号
    assert file_path == "src/storage.lua"
    assert line == 7  # 违例 require 行（canary 3 行不算：注释/块注释/长字符串内）
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "src/storage.lua"  # 容器 = repo 相对文件路径
    assert data["imported"] == "src/api.lua"
    assert data["chain"] == ["src/storage.lua", "src/api.lua"]
    assert data["line"] == 7


def test_enforce_lua_e2e_transitive_and_test_file_excluded(tmp_path):
    """三层链 storage → queue → api + 测试文件 require 不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "src" / "queue.lua").write_text(
        "local api = require('src.api')\n"
        "\n"
        "local M = {}\n"
        "function M.queueWork()\n"
        "  return api.apiVersion()\n"
        "end\n"
        "return M\n",
        encoding="utf-8",
    )
    (repo / "src" / "storage.lua").write_text(
        "local queue = require('src.queue')\n"
        "-- local x = require('src.api')\n"
        "local z = [[local w = require('src.api')]]\n"
        "\n"
        "local M = {}\n"
        "function M.storagePut(key)\n"
        "  _ = key\n"
        "  return queue.queueWork()\n"
        "end\n"
        "return M\n",
        encoding="utf-8",
    )
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["g1", "g4"], "layer": "core"},
                {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["g2"], "layer": "interface"},
            ],
            "layers": [
                {"name": "core", "may_depend_on": []},
                {"name": "interface", "may_depend_on": ["core"]},
            ],
        },
        ensure_ascii=False,
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g4', 'p', 'repo.src.queue.queueWork', 'Function', 'queueWork', 'src/queue.lua', 4, 6, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 4
    assert out["tests_excluded"] == 1
    assert out["violations"] == 2  # queue→api 直连 + storage 经 queue 传递
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("src/queue.lua", "src/api.lua")]["chain"] == ["src/queue.lua", "src/api.lua"]
    assert by_pair[("src/storage.lua", "src/api.lua")]["chain"] == [
        "src/storage.lua", "src/queue.lua", "src/api.lua"
    ]
    # 测试文件/注释/长字符串内 require 不掺入（若泄漏 storage 会多一条直连链）
    assert sum(1 for r in rows if json.loads(r[6])["importer"] == "src/storage.lua") == 1


def test_enforce_lua_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_lua_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 require（保留词法 canary——修复后 canary 也不得复活违例）
        (repo / "src" / "storage.lua").write_text(
            "-- local x = require('src.api')\n"
            "local M = {}\n"
            "function M.storagePut(key)\n"
            "  _ = key\n"
            "  return 0\n"
            "end\n"
            "return M\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_lua_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "storage.lua").write_text(
        "local M = {}\n"
        "function M.storagePut(key)\n"
        "  _ = key\n"
        "  return 0\n"
        "end\n"
        "return M\n",
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
