"""S7 ObjC 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_objc.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实布局——#import/#include 双指令、
词法 canary（注释/块注释/@字符串内伪 import）、XCTest 测试文件排除、传递链；
真仓 dogfood 另见 work/objc-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：app(queue/) ← interface(logging/)
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
        " VALUES ('g1', 'p', 'repo.queue.Queue', 'Class', 'Queue', 'queue/Queue.m', 6, 7, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'repo.logging.Logging', 'Class', 'Logging', 'logging/Logging.m', 2, 3, 'h')"
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
    (repo / "Tests").mkdir()
    (repo / "queue" / "Queue.h").write_text(
        "@interface Queue : NSObject\n@end\n", encoding="utf-8"
    )
    # 违例：第 5 行 #import "Logging.h"（import 指令行锚定目标）；词法 canary——
    # 行注释（1）/块注释（2）/@字符串（3）内伪 import 若泄漏会多 queue→logging 边
    (repo / "queue" / "Queue.m").write_text(
        '// #import "Logging.h"\n'
        '/* #import "Logging.h" */\n'
        'NSString *s = @"#import \\"Logging.h\\"";\n'
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
    # 测试文件：XCTest 形态蓄意 import 双头——排除后不产边（若泄漏会引入
    # Tests 目录出边与 tests_excluded 计数漂移）
    (repo / "Tests" / "QueueTests.m").write_text(
        '#import "Queue.h"\n'
        '#import "Logging.h"\n'
        "@implementation QueueTests\n"
        "@end\n",
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


def test_enforce_objc_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "objc-import-parser"  # 语言路由命中 ObjC 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 5  # 5 个 .h/.m 文件（扫描计数诚实，含测试文件）
    assert out["dirs"] == 3  # queue/logging/Tests 叶目录全部入图
    assert out["tests_excluded"] == 1  # Tests/QueueTests.m 文件级排除
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 import 文件首符号
    assert file_path == "queue/Queue.m"
    assert line == 5  # 违例 import 行（canary 3 行不算：注释/块注释/@字符串内）
    data = json.loads(payload)
    assert data["source_layer"] == "app"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "queue"  # 容器 = repo 相对目录
    assert data["imported"] == "logging"
    assert data["chain"] == ["queue", "logging"]
    assert data["line"] == 5


def test_enforce_objc_e2e_transitive_and_test_file_excluded(tmp_path):
    """三层链 storage → queue → logging + 测试文件 import 不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "storage").mkdir()
    (repo / "storage" / "Storage.m").write_text(
        '#import "Queue.h"\n'
        "@implementation Storage\n"
        "@end\n",
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
        " VALUES ('g3', 'p', 'repo.storage.Storage', 'Class', 'Storage', 'storage/Storage.m', 2, 3, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 6
    assert out["dirs"] == 4
    assert out["tests_excluded"] == 1
    assert out["violations"] == 3  # queue→logging 直连 + storage→queue 直连 + storage 经 queue 传递
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("queue", "logging")]["chain"] == ["queue", "logging"]
    assert by_pair[("storage", "queue")]["chain"] == ["storage", "queue"]
    assert by_pair[("storage", "logging")]["chain"] == ["storage", "queue", "logging"]
    # 锚定：queue 两条违例锚 Queue.m:5；storage→queue 锚 Storage.m:1
    anchored = {(r[4], r[5]) for r in rows}
    assert anchored == {("queue/Queue.m", 5), ("storage/Storage.m", 1)}
    # 测试文件 import 不掺入（若泄漏 Tests 目录会多出边）
    assert sum(1 for r in rows if json.loads(r[6])["importer"] == "Tests") == 0


def test_enforce_objc_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_objc_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 import（保留词法 canary——修复后 canary 也不得复活违例）
        (repo / "queue" / "Queue.m").write_text(
            '// #import "Logging.h"\n'
            '/* #import "Logging.h" */\n'
            'NSString *s = @"#import \\"Logging.h\\"";\n'
            '#import "Queue.h"\n'
            "@implementation Queue\n"
            "@end\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_objc_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "queue" / "Queue.m").write_text(
        '#import "Queue.h"\n'
        "@implementation Queue\n"
        "@end\n",
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
