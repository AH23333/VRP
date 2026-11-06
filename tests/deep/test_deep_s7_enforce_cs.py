"""S7 C# 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_cs.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实 .NET 工程布局——namespace 结构、
传递链、tests/ 测试目录排除；真仓 dogfood 另见 work/cs-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：core(Deep.Store) ← interface(Deep.Api)
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
        " VALUES ('g1', 'p', 'deep.store.Store', 'Class', 'Store', 'src/Deep/Store/Store.cs', 5, 9, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'deep.api.Handler', 'Class', 'Handler', 'src/Deep/Api/Handler.cs', 3, 7, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "DeepProbe.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk\">\n  <PropertyGroup>\n"
        "    <TargetFramework>net9.0</TargetFramework>\n  </PropertyGroup>\n</Project>\n",
        encoding="utf-8",
    )
    main = repo / "src" / "Deep"
    (main / "Store").mkdir(parents=True)
    (main / "Api").mkdir(parents=True)
    (main / "Api" / "Handler.cs").write_text(
        "namespace Deep.Api\n{\n    public class Handler\n    {\n"
        "        public string Handle() => \"h\";\n    }\n}\n",
        encoding="utf-8",
    )
    # 违例：Store 命名空间 using Api 命名空间（第 3 行）
    (main / "Store" / "Store.cs").write_text(
        "namespace Deep.Store\n"
        "{\n"
        "    using Deep.Api;\n"
        "\n"
        "    public class Store\n"
        "    {\n"
        "        public string Get() => new Handler().Handle();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    # 测试目录：若泄漏会双计违例
    test = repo / "tests"
    test.mkdir()
    (test / "StoreTests.cs").write_text(
        "namespace Deep.Store\n"
        "{\n"
        "    using Deep.Api;\n"
        "\n"
        "    public class StoreTests\n"
        "    {\n"
        "        public void T() { new Handler(); }\n"
        "    }\n"
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


def test_enforce_cs_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "cs-namespace-parser"  # 语言路由命中 C# 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # 2 main + 1 test（扫描计数诚实）
    assert out["namespaces"] == 2
    assert out["tests_excluded"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 using 文件首符号
    assert file_path == "src/Deep/Store/Store.cs"
    assert line == 3  # 违例 using 行
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "Deep.Store"  # 容器 = C# 命名空间
    assert data["imported"] == "Deep.Api"
    assert data["chain"] == ["Deep.Store", "Deep.Api"]
    assert data["line"] == 3


def test_enforce_cs_e2e_transitive_and_test_dir_excluded(tmp_path):
    """三层链 Deep.Store → Deep.Queue → Deep.Api + tests/ 不掺入。"""
    repo = _mk_repo(tmp_path)
    main = repo / "src" / "Deep"
    (main / "Queue").mkdir()
    (main / "Queue" / "Queue.cs").write_text(
        "namespace Deep.Queue\n"
        "{\n"
        "    using Deep.Api;\n"
        "\n"
        "    public class Queue\n"
        "    {\n"
        "        public string Work() => new Handler().Handle();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (main / "Store" / "Store.cs").write_text(
        "namespace Deep.Store\n"
        "{\n"
        "    using Deep.Queue;\n"
        "\n"
        "    public class Store\n"
        "    {\n"
        "        public string Get() => new Queue().Work();\n"
        "    }\n"
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
        " VALUES ('g4', 'p', 'deep.queue.Queue', 'Class', 'Queue', 'src/Deep/Queue/Queue.cs', 5, 9, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 4  # 3 main + 1 test
    assert out["tests_excluded"] == 1
    assert out["violations"] == 3  # core→app / core→interface / app→interface 各一
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("Deep.Store", "Deep.Queue")]["chain"] == ["Deep.Store", "Deep.Queue"]
    assert by_pair[("Deep.Queue", "Deep.Api")]["chain"] == ["Deep.Queue", "Deep.Api"]
    assert by_pair[("Deep.Store", "Deep.Api")]["chain"] == ["Deep.Store", "Deep.Queue", "Deep.Api"]
    # 测试目录 using 不掺入（若泄漏 StoreTests 的 Api using 会双计 Store→Api）
    assert sum(1 for r in rows if json.loads(r[6])["importer"] == "Deep.Store") == 2


def test_enforce_cs_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_cs_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 using（保类自洽）
        (repo / "src" / "Deep" / "Store" / "Store.cs").write_text(
            "namespace Deep.Store\n{\n    public class Store\n    {\n        public string Get() => \"s\";\n    }\n}\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_cs_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "Deep" / "Store" / "Store.cs").write_text(
        "namespace Deep.Store\n{\n    public class Store\n    {\n        public string Get() => \"s\";\n    }\n}\n",
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
