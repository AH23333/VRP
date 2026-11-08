"""S7 Ruby 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_ruby.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实布局——src 包结构、传递链、
test/ 测试目录排除；真仓 dogfood 另见 work/ruby-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：core(Probe::Storage) ← interface(Probe::Api)
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
        " VALUES ('g1', 'p', 'Probe::Storage', 'Module', 'Storage', 'src/store/storage.rb', 3, 9, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'Probe::Api', 'Module', 'Api', 'src/api/api.rb', 1, 7, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "src" / "store").mkdir(parents=True)
    (repo / "src" / "api").mkdir(parents=True)
    (repo / "src" / "api" / "api.rb").write_text(
        "module Probe\n"
        "  module Api\n"
        "    def self.handle(value)\n"
        "      value\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    # 违例：第 1 行 require_relative api（require 行号锚定目标）
    (repo / "src" / "store" / "storage.rb").write_text(
        'require_relative "../api/api"\n\n'
        "module Probe\n"
        "  module Storage\n"
        "    def self.put(value)\n"
        "      Probe::Api.handle(value)\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    # 测试目录：若泄漏会掺入测试 require 边
    test = repo / "test"
    test.mkdir()
    (test / "store_test.rb").write_text(
        'require_relative "../src/api/api"\n\n'
        "module Probe\n"
        "  module Storage\n"
        "    def self.spec(value)\n"
        "      Probe::Api.handle(value)\n"
        "    end\n"
        "  end\n"
        "end\n",
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


def test_enforce_ruby_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "ruby-module-parser"  # 语言路由命中 Ruby 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # 2 src + 1 test（扫描计数诚实）
    assert out["modules"] == 2
    assert out["tests_excluded"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 require 文件首符号
    assert file_path == "src/store/storage.rb"
    assert line == 1  # 违例 require_relative 行
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "Probe::Storage"  # 容器 = Ruby module 全路径
    assert data["imported"] == "Probe::Api"
    assert data["chain"] == ["Probe::Storage", "Probe::Api"]
    assert data["line"] == 1


def test_enforce_ruby_e2e_transitive_and_test_root_excluded(tmp_path):
    """三层链 Probe::Storage → Probe::Queue → Probe::Api + 测试目录不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "src" / "queue").mkdir()
    (repo / "src" / "queue" / "queue.rb").write_text(
        'require_relative "../api/api"\n\n'
        "module Probe\n"
        "  module Queue\n"
        "    def self.work(value)\n"
        "      Probe::Api.handle(value)\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    (repo / "src" / "store" / "storage.rb").write_text(
        'require_relative "../queue/queue"\n\n'
        "module Probe\n"
        "  module Storage\n"
        "    def self.put(value)\n"
        "      Probe::Queue.work(value)\n"
        "    end\n"
        "  end\n"
        "end\n",
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
        " VALUES ('g4', 'p', 'Probe::Queue', 'Module', 'Queue', 'src/queue/queue.rb', 3, 9, 'h')"
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
    assert by_pair[("Probe::Storage", "Probe::Queue")]["chain"] == ["Probe::Storage", "Probe::Queue"]
    assert by_pair[("Probe::Queue", "Probe::Api")]["chain"] == ["Probe::Queue", "Probe::Api"]
    assert by_pair[("Probe::Storage", "Probe::Api")]["chain"] == ["Probe::Storage", "Probe::Queue", "Probe::Api"]
    # 测试目录 require 不掺入（若泄漏 store_test.rb 的 api require 会多一条 Storage→Api 边）
    assert sum(1 for r in rows if json.loads(r[6])["importer"] == "Probe::Storage") == 2


def test_enforce_ruby_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_ruby_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 require（保 module 自洽）
        (repo / "src" / "store" / "storage.rb").write_text(
            "module Probe\n"
            "  module Storage\n"
            "    def self.put(value)\n"
            "      value\n"
            "    end\n"
            "  end\n"
            "end\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_ruby_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "store" / "storage.rb").write_text(
        "module Probe\n"
        "  module Storage\n"
        "    def self.put(value)\n"
        "      value\n"
        "    end\n"
        "  end\n"
        "end\n",
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


def test_enforce_ruby_e2e_review_batch_regressions(tmp_path):
    """review 批五修叠加 E2E：缺任一修复本测失败。

    (a) util.require('../util/util.rb') 成员调用——修复前伪边 → 假违例；
    (b) 跨行括号 require_relative——修复前真违例边丢失 → violations=0；
    (c) __END__ 数据节伪 require/伪 module；
    (d) endless def 块深度漂移；
    (e) =~ 正则体内 module 文本泄漏。
    """
    repo = _mk_repo(tmp_path)
    (repo / "src" / "util").mkdir()
    (repo / "src" / "util" / "util.rb").write_text(
        "module Probe\n"
        "  module Util\n"
        "    def self.wrap(v)\n"
        "      v\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    # 五修陷阱全叠在违例文件上
    (repo / "src" / "store" / "storage.rb").write_text(
        "util.require('../util/util.rb')\n"  # (a) 成员调用非声明边
        'require_relative(\n'  # (b) 跨行括号——真违例边锚定第 2 行
        '  "../api/api"\n'
        ")\n"
        "module Probe\n"
        "  module Storage\n"
        "    def self.put(value)\n"
        "      Probe::Api.handle(value)\n"
        "    end\n"
        "    def helper = 42\n"  # (d) endless def 无 end 不漂移
        "  end\n"
        "end\n"
        "x = y =~ /module Fake/\n"  # (e) =~ 正则体不泄漏
        "__END__\n"  # (c) 数据节起点
        "require 'data_fake.rb'\n"
        "module Fake\n",
        encoding="utf-8",
    )
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["g1"], "layer": "core"},
                {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["g2", "g5"], "layer": "interface"},
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
        " VALUES ('g5', 'p', 'Probe::Util', 'Module', 'Util', 'src/util/util.rb', 2, 6, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 4  # storage/api/util/test
    assert out["modules"] == 3
    assert out["tests_excluded"] == 1
    # (b) 真违例边在（修复前 0）；(a) 伪边不在（修复前假违例 → 2）
    assert out["violations"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    data = json.loads(rows[0][6])
    assert data["importer"] == "Probe::Storage"
    assert data["imported"] == "Probe::Api"  # 修复前若 (a) 泄漏会是 Probe::Util
    assert data["chain"] == ["Probe::Storage", "Probe::Api"]
    assert data["line"] == 2  # 跨行 require_relative 关键字行锚定
    assert rows[0][4] == "src/store/storage.rb"
    assert rows[0][5] == 2
