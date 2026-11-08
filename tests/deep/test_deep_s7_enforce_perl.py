"""S7 Perl 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_perl.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实布局——package/use 解析、
词法 canary（行注释/POD/字符串内伪 use）、t/ 测试文件排除、传递链；
真仓 dogfood 另见 work/perl-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：app(Queue) ← interface(Logging)
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
        " VALUES ('g1', 'p', 'repo.queue.Queue', 'Package', 'Queue', 'queue/Queue.pm', 6, 8, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'repo.logging.Logging', 'Package', 'Logging', 'logging/Logging.pm', 1, 3, 'h')"
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
    (repo / "t").mkdir()
    # 违例：第 7 行 use Logging（use 语句行锚定目标）；词法 canary——
    # 行注释（1）/POD（2-4）/双引号串（5）内伪 use 若泄漏会多 queue→Logging 边
    (repo / "queue" / "Queue.pm").write_text(
        "# use Logging;\n"
        "=pod\n"
        "use Logging;\n"
        "=cut\n"
        'my $s = "use Logging;";\n'
        "package Queue;\n"
        "use Logging;\n"
        "sub work { 1; }\n"
        "1;\n",
        encoding="utf-8",
    )
    (repo / "logging" / "Logging.pm").write_text(
        "package Logging;\n"
        "sub log_msg { 1; }\n"
        "1;\n",
        encoding="utf-8",
    )
    # 测试文件：t/ 目录 TAP 约定蓄意 use 双包——排除后不产边（若泄漏会引入
    # tests_excluded 计数漂移与伪边）
    (repo / "t" / "00-queue.t").write_text(
        "use Queue;\n"
        "use Logging;\n",
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


def test_enforce_perl_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "perl-package-parser"  # 语言路由命中 Perl 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # Queue.pm + Logging.pm + 00-queue.t（扫描计数诚实）
    assert out["packages"] == 2  # Queue/Logging 入图（.t 无包不入图）
    assert out["tests_excluded"] == 1  # t/00-queue.t 文件级排除
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 use 文件首符号
    assert file_path == "queue/Queue.pm"
    assert line == 7  # 违例 use 行（canary 4 处不算：注释/POD/字符串内）
    data = json.loads(payload)
    assert data["source_layer"] == "app"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "Queue"  # 容器 = package 声明
    assert data["imported"] == "Logging"
    assert data["chain"] == ["Queue", "Logging"]
    assert data["line"] == 7


def test_enforce_perl_e2e_transitive_and_test_file_excluded(tmp_path):
    """三层链 Storage → Queue → Logging + 测试文件 use 不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "storage").mkdir()
    (repo / "storage" / "Storage.pm").write_text(
        "package Storage;\n"
        "use Queue;\n"
        "1;\n",
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
        " VALUES ('g3', 'p', 'repo.storage.Storage', 'Package', 'Storage', 'storage/Storage.pm', 1, 3, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 4
    assert out["packages"] == 3
    assert out["tests_excluded"] == 1
    assert out["violations"] == 3  # Queue→Logging 直连 + Storage→Queue 直连 + Storage 经 Queue 传递
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("Queue", "Logging")]["chain"] == ["Queue", "Logging"]
    assert by_pair[("Storage", "Queue")]["chain"] == ["Storage", "Queue"]
    assert by_pair[("Storage", "Logging")]["chain"] == ["Storage", "Queue", "Logging"]
    # 锚定：Queue 两条违例锚 Queue.pm:7；Storage→Queue 锚 Storage.pm:2
    anchored = {(r[4], r[5]) for r in rows}
    assert anchored == {("queue/Queue.pm", 7), ("storage/Storage.pm", 2)}


def test_enforce_perl_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_perl_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 use（保留词法 canary——修复后 canary 也不得复活违例）
        (repo / "queue" / "Queue.pm").write_text(
            "# use Logging;\n"
            "=pod\n"
            "use Logging;\n"
            "=cut\n"
            'my $s = "use Logging;";\n'
            "package Queue;\n"
            "sub work { 1; }\n"
            "1;\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_perl_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "queue" / "Queue.pm").write_text(
        "package Queue;\n"
        "sub work { 1; }\n"
        "1;\n",
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


def test_enforce_perl_e2e_conditional_require_and_hashkey_collision(tmp_path):
    """条件 require（if 块内+赋值右侧）+哈希键碰撞 canary。

    修复前 `$opt{s}` 的 `s}` 触发 q 算符误判（`}` 作定界符），把到下一个
    `}` 之间的 `require Logging` 整段空白化——违例漏提（0 违例假阴）。
    修复后：闭合括号不作定界符、`{`/`=` 语句边界命中 require；双边同对
    dedup 单违例，锚 (file,line) 字典序首。
    """
    repo = _mk_repo(tmp_path)
    (repo / "queue" / "Queue.pm").write_text(
        "# use Logging;\n"  # 行注释 canary
        "=pod\n"
        "use Logging;\n"  # POD canary
        "=cut\n"
        'my $s = "use Logging;";\n'  # 字符串 canary
        "package Queue;\n"
        "my %opt = (s => 1);\n"  # 修复前：s} q 算符误触起点
        "if ($opt{s}) { require Logging; }\n"  # 修复前：require 被误触吞掉
        "sub work { my $m = require Logging; return 1; }\n"  # 第二边同对 dedup
        "1;\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 修复前 0（哈希键碰撞吞 require 假阴）
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (anchor, file_path, line) == ("g1", "queue/Queue.pm", 8)
    data = json.loads(payload)
    assert data["chain"] == ["Queue", "Logging"]
    assert data["line"] == 8  # 两条 Queue→Logging 边（行 8/行 9）字典序首
