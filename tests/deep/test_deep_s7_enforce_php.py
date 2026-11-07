"""S7 PHP 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_php.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实 Composer/PSR-4 工程布局——
src/ 命名空间结构、传递链、tests/ PHPUnit 排除、vendor/ 跳过；真仓 dogfood
（含沙箱 php -l 语法验证）另见 work/php-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：core(Deep\Core) ← interface(Deep\Iface)
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
        " VALUES ('g1', 'p', 'Deep.Core.Store', 'Class', 'Store', 'src/Store.php', 7, 11, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'Deep.Iface.Api', 'Interface', 'Api', 'src/Api.php', 3, 6, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "composer.json").write_text(
        json.dumps({"autoload": {"psr-4": {"Deep\\": "src/"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "Api.php").write_text(
        "<?php\n\nnamespace Deep\\Iface;\n\ninterface Api\n{\n    public function get(): string;\n}\n",
        encoding="utf-8",
    )
    # 违例：Store 命名空间 use Api 命名空间（第 5 行）
    (repo / "src" / "Store.php").write_text(
        "<?php\n\n"
        "namespace Deep\\Core;\n\n"
        "use Deep\\Iface\\Api;\n\n"
        "final class Store\n"
        "{\n"
        "    public function __construct(private Api $api) {}\n"
        "}\n",
        encoding="utf-8",
    )
    # 测试目录：若泄漏会双计违例
    (repo / "tests").mkdir()
    (repo / "tests" / "StoreTest.php").write_text(
        "<?php\n\n"
        "namespace Deep\\Core;\n\n"
        "use Deep\\Iface\\Api;\n\n"
        "final class StoreTest extends \\PHPUnit\\Framework\\TestCase\n"
        "{\n"
        "    public function testStore(): void {}\n"
        "}\n",
        encoding="utf-8",
    )
    # vendor 第三方：整体跳过（若扫描会多出命名空间与文件计数）
    vendor = repo / "vendor" / "monolog" / "monolog" / "src"
    vendor.mkdir(parents=True)
    (vendor / "Logger.php").write_text(
        "<?php\n\nnamespace Deep\\Hidden;\n\nuse Deep\\Iface\\Api;\n", encoding="utf-8"
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


def test_enforce_php_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "php-namespace-parser"  # 语言路由命中 PHP 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # 2 src + 1 test（vendor 跳过，扫描计数诚实）
    assert out["namespaces"] == 2  # vendor 的 Deep\Hidden 未入图
    assert out["tests_excluded"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 use 文件首符号
    assert file_path == "src/Store.php"
    assert line == 5  # 违例 use 行
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "Deep\\Core"  # 容器 = PHP 命名空间
    assert data["imported"] == "Deep\\Iface"
    assert data["chain"] == ["Deep\\Core", "Deep\\Iface"]
    assert data["line"] == 5


def test_enforce_php_e2e_transitive_and_test_root_excluded(tmp_path):
    r"""三层链 Deep\Core → Deep\App → Deep\Iface + 测试目录/vendor 不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "src" / "Queue.php").write_text(
        "<?php\n\n"
        "namespace Deep\\App;\n\n"
        "use Deep\\Iface\\Api;\n\n"
        "final class Queue\n"
        "{\n"
        "    public function __construct(private Api $api) {}\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "src" / "Store.php").write_text(
        "<?php\n\n"
        "namespace Deep\\Core;\n\n"
        "use Deep\\App\\Queue;\n\n"
        "final class Store\n"
        "{\n"
        "    public function __construct(private Queue $queue) {}\n"
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
        " VALUES ('g4', 'p', 'Deep.App.Queue', 'Class', 'Queue', 'src/Queue.php', 7, 11, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 4  # 3 src + 1 test（vendor 跳过）
    assert out["tests_excluded"] == 1
    assert out["violations"] == 3  # core→app / core→interface / app→interface 各一
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("Deep\\Core", "Deep\\App")]["chain"] == ["Deep\\Core", "Deep\\App"]
    assert by_pair[("Deep\\App", "Deep\\Iface")]["chain"] == ["Deep\\App", "Deep\\Iface"]
    assert by_pair[("Deep\\Core", "Deep\\Iface")]["chain"] == ["Deep\\Core", "Deep\\App", "Deep\\Iface"]
    # 测试目录 canary：tests/StoreTest.php 若泄漏会多出 (Deep\Core → Deep\Iface) 直连计数
    assert sum(1 for r in rows if json.loads(r[6])["importer"] == "Deep\\Core") == 2
    # vendor canary：Deep\Hidden 未入图（若泄漏 importer Deep\Hidden 会出现）
    assert all(json.loads(r[6])["importer"] != "Deep\\Hidden" for r in rows)
    assert out["namespaces"] == 3  # Core/App/Iface；Hidden 不在


def test_enforce_php_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_php_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 use（保类自洽）
        (repo / "src" / "Store.php").write_text(
            "<?php\n\nnamespace Deep\\Core;\n\nfinal class Store\n{\n    public function get(): string { return 's'; }\n}\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_php_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "Store.php").write_text(
        "<?php\n\nnamespace Deep\\Core;\n\nfinal class Store\n{\n    public function get(): string { return 's'; }\n}\n",
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
