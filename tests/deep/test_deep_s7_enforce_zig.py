"""S7 Zig 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_zig.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实布局——@import 相对路径、传递链、
test 块排除；真仓 dogfood 另见 work/zig-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：core(src/storage.zig) ← interface(src/api.zig)
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
        " VALUES ('g1', 'p', 'repo.src.storage.storagePut', 'Function', 'storagePut', 'src/storage.zig', 3, 8, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'repo.src.api.apiVersion', 'Function', 'apiVersion', 'src/api.zig', 1, 4, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "api.zig").write_text(
        "pub fn apiVersion() i32 {\n    return 1;\n}\n",
        encoding="utf-8",
    )
    # 违例：第 1 行 @import("api.zig")（@import 记号行锚定目标）
    (repo / "src" / "storage.zig").write_text(
        'const api = @import("api.zig");\n'
        'const std = @import("std");\n\n'
        "pub fn storagePut(key: []const u8) i32 {\n"
        "    if (key.len == 0) return 1;\n"
        "    return api.apiVersion();\n"
        "}\n\n"
        'test "put" {\n'
        '    _ = @import("api.zig");\n'  # test 块内 @import 排除；若泄漏会多一条违例边
        "    try std.testing.expect(storagePut(\"\") == 1);\n"
        "}\n",
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


def test_enforce_zig_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "zig-import-parser"  # 语言路由命中 Zig 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 2  # 2 个 .zig 文件（扫描计数诚实）
    assert out["modules"] == 2  # 容器=文件：两文件两容器
    assert out["test_blocks"] == 1
    assert out["tests_excluded"] == 1  # test 块内一条 @import 抑制
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 import 文件首符号
    assert file_path == "src/storage.zig"
    assert line == 1  # 违例 @import 行
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "src/storage.zig"  # 容器 = repo 相对文件路径
    assert data["imported"] == "src/api.zig"
    assert data["chain"] == ["src/storage.zig", "src/api.zig"]
    assert data["line"] == 1


def test_enforce_zig_e2e_transitive_and_test_block_excluded(tmp_path):
    """三层链 storage → queue → api + test 块不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "src" / "queue.zig").write_text(
        'const api = @import("api.zig");\n\n'
        "pub fn queueWork() i32 {\n"
        "    return api.apiVersion();\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "src" / "storage.zig").write_text(
        'const queue = @import("queue.zig");\n'
        'const std = @import("std");\n\n'
        "pub fn storagePut(key: []const u8) i32 {\n"
        "    if (key.len == 0) return 1;\n"
        "    return queue.queueWork();\n"
        "}\n\n"
        'test "put" {\n'
        '    _ = @import("api.zig");\n'  # test 块 canary：若泄漏会多 core→interface 边
        "    try std.testing.expect(storagePut(\"\") == 1);\n"
        "}\n",
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
        " VALUES ('g4', 'p', 'repo.src.queue.queueWork', 'Function', 'queueWork', 'src/queue.zig', 3, 5, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 3
    assert out["test_blocks"] == 1
    assert out["tests_excluded"] == 1
    assert out["violations"] == 2  # queue→api 直连 + storage 经 queue 传递
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("src/queue.zig", "src/api.zig")]["chain"] == ["src/queue.zig", "src/api.zig"]
    assert by_pair[("src/storage.zig", "src/api.zig")]["chain"] == [
        "src/storage.zig", "src/queue.zig", "src/api.zig"
    ]
    # test 块内 @import 不掺入（若泄漏 storage 的块内 api import 会多一条重复链）
    assert sum(1 for r in rows if json.loads(r[6])["importer"] == "src/storage.zig") == 1


def test_enforce_zig_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_zig_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 @import
        (repo / "src" / "storage.zig").write_text(
            "pub fn storagePut(key: []const u8) i32 {\n"
            "    if (key.len == 0) return 1;\n"
            "    return 0;\n"
            "}\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_zig_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "storage.zig").write_text(
        "pub fn storagePut(key: []const u8) i32 {\n"
        "    if (key.len == 0) return 1;\n"
        "    return 0;\n"
        "}\n",
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
