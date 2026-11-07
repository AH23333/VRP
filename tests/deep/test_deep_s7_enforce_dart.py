"""S7 Dart 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_dart.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实布局——lib/src 结构、传递链、
test/ 测试目录排除；真仓 dogfood 另见 work/dart-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：core(lib/src/storage.dart) ← interface(lib/src/api.dart)
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
        " VALUES ('g1', 'p', 'probe.lib.src.storage', 'Module', 'storage', 'lib/src/storage.dart', 3, 7, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'probe.lib.src.api', 'Module', 'api', 'lib/src/api.dart', 1, 4, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "lib" / "src").mkdir(parents=True)
    (repo / "lib" / "src" / "api.dart").write_text(
        "int apiVersion() {\n"
        "  return 1;\n"
        "}\n",
        encoding="utf-8",
    )
    # 违例：第 1 行 import api（import 行号锚定目标）
    (repo / "lib" / "src" / "storage.dart").write_text(
        "import 'api.dart';\n\n"
        "int storagePut(String key, String value) {\n"
        "  if (key.isEmpty) return 1;\n"
        "  return apiVersion() > 0 ? 0 : 1;\n"
        "}\n",
        encoding="utf-8",
    )
    # 测试目录：若泄漏会掺入测试 import 边
    test = repo / "test"
    test.mkdir()
    (test / "storage_test.dart").write_text(
        "import '../lib/src/api.dart';\n\nvoid main() {\n  apiVersion();\n}\n",
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


def test_enforce_dart_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "dart-import-parser"  # 语言路由命中 Dart 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # 2 src + 1 test（扫描计数诚实）
    assert out["modules"] == 3  # 容器=文件：测试文件也是可见叶容器（边排除，叶可见）
    assert out["tests_excluded"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 import 文件首符号
    assert file_path == "lib/src/storage.dart"
    assert line == 1  # 违例 import 行
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "lib/src/storage.dart"  # 容器 = 文件（Dart library）
    assert data["imported"] == "lib/src/api.dart"
    assert data["chain"] == ["lib/src/storage.dart", "lib/src/api.dart"]
    assert data["line"] == 1


def test_enforce_dart_e2e_transitive_and_test_root_excluded(tmp_path):
    """三层链 storage → queue → api + 测试目录不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "lib" / "src" / "queue.dart").write_text(
        "import 'api.dart';\n\n"
        "int queueWork() {\n"
        "  return apiVersion();\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "lib" / "src" / "storage.dart").write_text(
        "import 'queue.dart';\n\n"
        "int storagePut(String key, String value) {\n"
        "  if (key.isEmpty) return 1;\n"
        "  return queueWork() > 0 ? 0 : 1;\n"
        "}\n",
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
        " VALUES ('g4', 'p', 'probe.lib.src.queue', 'Module', 'queue', 'lib/src/queue.dart', 3, 6, 'h')"
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
    assert by_pair[("lib/src/storage.dart", "lib/src/queue.dart")]["chain"] == ["lib/src/storage.dart", "lib/src/queue.dart"]
    assert by_pair[("lib/src/queue.dart", "lib/src/api.dart")]["chain"] == ["lib/src/queue.dart", "lib/src/api.dart"]
    assert by_pair[("lib/src/storage.dart", "lib/src/api.dart")]["chain"] == [
        "lib/src/storage.dart", "lib/src/queue.dart", "lib/src/api.dart"
    ]
    # 测试目录 import 不掺入（若泄漏 storage_test.dart 的 api import 会多一条边）
    assert sum(1 for r in rows if json.loads(r[6])["importer"] == "lib/src/storage.dart") == 2


def test_enforce_dart_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_dart_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 import
        (repo / "lib" / "src" / "storage.dart").write_text(
            "int storagePut(String key, String value) {\n"
            "  if (key.isEmpty) return 1;\n"
            "  return 0;\n"
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


def test_enforce_dart_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "lib" / "src" / "storage.dart").write_text(
        "int storagePut(String key, String value) {\n"
        "  if (key.isEmpty) return 1;\n"
        "  return 0;\n"
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
