"""S7 Swift 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_swift.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实布局——SwiftPM targets、传递链、
testTarget 排除；真仓 dogfood 另见 work/swift-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

MANIFEST = """// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "probe",
    products: [
        .library(name: "probe", targets: ["Api", "Storage"]),
    ],
    targets: [
        .target(name: "Api"),
        .target(
            name: "Storage",
            dependencies: [.target(name: "Api")]
        ),
        .testTarget(
            name: "StorageTests",
            dependencies: ["Storage"]
        ),
    ]
)
"""

# 两层：core(Storage target) ← interface(Api target)
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
        " VALUES ('g1', 'p', 'probe.Sources.Storage.Storage', 'Function', 'storagePut', 'Sources/Storage/Storage.swift', 3, 7, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'probe.Sources.Api.Api', 'Function', 'apiVersion', 'Sources/Api/Api.swift', 1, 4, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "Package.swift").write_text(MANIFEST, encoding="utf-8")
    (repo / "Sources" / "Api").mkdir(parents=True)
    (repo / "Sources" / "Api" / "Api.swift").write_text(
        "public func apiVersion() -> Int {\n"
        "    return 1\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "Sources" / "Storage").mkdir(parents=True)
    # 违例：第 1 行 import Api（import 行号锚定目标）
    (repo / "Sources" / "Storage" / "Storage.swift").write_text(
        "import Api\n\n"
        "public func storagePut(key: String) -> Int {\n"
        "    if key.isEmpty { return 1 }\n"
        "    return apiVersion()\n"
        "}\n",
        encoding="utf-8",
    )
    # 测试 target：若泄漏会掺入测试 import 边
    (repo / "Tests" / "StorageTests").mkdir(parents=True)
    (repo / "Tests" / "StorageTests" / "StorageTests.swift").write_text(
        "@testable import Storage\n"
        "@testable import Api\n"
        "import XCTest\n\n"
        "final class StorageTests: XCTestCase {\n"
        "    func testPut() { XCTAssertEqual(storagePut(key: \"k\"), 1) }\n"
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


def test_enforce_swift_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "swift-target-parser"  # 语言路由命中 Swift 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # 2 src + 1 test（扫描计数诚实）
    assert out["modules"] == 3  # 容器=target：测试 target 也是可见叶容器（边排除，叶可见）
    assert out["tests_excluded"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 import 文件首符号
    assert file_path == "Sources/Storage/Storage.swift"
    assert line == 1  # 违例 import 行
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "Storage"  # 容器 = SwiftPM target 名
    assert data["imported"] == "Api"
    assert data["chain"] == ["Storage", "Api"]
    assert data["line"] == 1


def test_enforce_swift_e2e_transitive_and_test_target_excluded(tmp_path):
    """三层链 Storage → Queue → Api + 测试 target 不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "Sources" / "Queue").mkdir(parents=True)
    (repo / "Sources" / "Queue" / "Queue.swift").write_text(
        "import Api\n\n"
        "public func queueWork() -> Int {\n"
        "    return apiVersion()\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "Sources" / "Storage" / "Storage.swift").write_text(
        "import Queue\n\n"
        "public func storagePut(key: String) -> Int {\n"
        "    if key.isEmpty { return 1 }\n"
        "    return queueWork()\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "Package.swift").write_text(
        MANIFEST.replace(
            '        .target(name: "Api"),',
            '        .target(name: "Api"),\n        .target(name: "Queue"),',
        ),
        encoding="utf-8",
    )
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["g1"], "layer": "core"},
                {"module_id": "app-mod", "name": "队列", "responsibility": "派", "symbol_fids": ["g4"], "layer": "app"},
                {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["g2"], "layer": "interface"},
            ],
            "layers": [
                {"name": "core", "may_depend_on": []},
                {"name": "app", "may_depend_on": ["core"]},
                {"name": "interface", "may_depend_on": ["app", "core"]},
            ],
        },
        ensure_ascii=False,
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g4', 'p', 'probe.Sources.Queue.Queue', 'Function', 'queueWork', 'Sources/Queue/Queue.swift', 3, 6, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 4  # 3 src + 1 test
    assert out["tests_excluded"] == 1
    assert out["violations"] == 3  # core→app / core→interface / app→interface 各一
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("Storage", "Queue")]["chain"] == ["Storage", "Queue"]
    assert by_pair[("Queue", "Api")]["chain"] == ["Queue", "Api"]
    assert by_pair[("Storage", "Api")]["chain"] == ["Storage", "Queue", "Api"]
    # 测试 target import 不掺入（若泄漏 StorageTests 的 Api import 会多一条边）
    assert sum(1 for r in rows if json.loads(r[6])["importer"] == "Storage") == 2


def test_enforce_swift_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_swift_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 import
        (repo / "Sources" / "Storage" / "Storage.swift").write_text(
            "public func storagePut(key: String) -> Int {\n"
            "    if key.isEmpty { return 1 }\n"
            "    return 0\n"
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


def test_enforce_swift_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "Sources" / "Storage" / "Storage.swift").write_text(
        "public func storagePut(key: String) -> Int {\n"
        "    if key.isEmpty { return 1 }\n"
        "    return 0\n"
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
