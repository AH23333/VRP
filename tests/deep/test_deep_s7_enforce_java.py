"""S7 Java 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_java.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实 Maven 布局——src/main/java 包结构、
传递链、src/test/java 测试源根排除；真仓 dogfood 另见 work/java-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：core(com.deep.store) ← interface(com.deep.api)
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
        " VALUES ('g1', 'p', 'com.deep.store.Store', 'Class', 'Store', 'src/main/java/com/deep/store/Store.java', 5, 7, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'com.deep.api.Handler', 'Class', 'Handler', 'src/main/java/com/deep/api/Handler.java', 3, 5, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "pom.xml").write_text(
        "<project>\n  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>com.deep</groupId>\n  <artifactId>deep</artifactId>\n"
        "  <version>0.1.0</version>\n</project>\n",
        encoding="utf-8",
    )
    main = repo / "src" / "main" / "java" / "com" / "deep"
    (main / "store").mkdir(parents=True)
    (main / "api").mkdir(parents=True)
    (main / "api" / "Handler.java").write_text(
        "package com.deep.api;\n\npublic class Handler {\n    public String handle() { return \"h\"; }\n}\n",
        encoding="utf-8",
    )
    # 违例：store 包 import api 包（第 3 行）
    (main / "store" / "Store.java").write_text(
        "package com.deep.store;\n\n"
        "import com.deep.api.Handler;\n\n"
        "public class Store {\n"
        "    public String get() { return new Handler().handle(); }\n"
        "}\n",
        encoding="utf-8",
    )
    # 测试源根：若泄漏会双计违例
    test = repo / "src" / "test" / "java" / "com" / "deep" / "store"
    test.mkdir(parents=True)
    (test / "StoreTest.java").write_text(
        "package com.deep.store;\n\n"
        "import com.deep.api.Handler;\n\n"
        "public class StoreTest {\n"
        "    public void t() { new Handler(); }\n"
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


def test_enforce_java_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "java-import-parser"  # 语言路由命中 Java 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # 2 main + 1 test（扫描计数诚实）
    assert out["packages"] == 2
    assert out["tests_excluded"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 import 文件首符号
    assert file_path == "src/main/java/com/deep/store/Store.java"
    assert line == 3  # 违例 import 行
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "com.deep.store"  # 容器 = Java 包
    assert data["imported"] == "com.deep.api"
    assert data["chain"] == ["com.deep.store", "com.deep.api"]
    assert data["line"] == 3


def test_enforce_java_e2e_transitive_and_test_root_excluded(tmp_path):
    """三层链 com.deep.store → com.deep.queue → com.deep.api + 测试源根不掺入。"""
    repo = _mk_repo(tmp_path)
    main = repo / "src" / "main" / "java" / "com" / "deep"
    (main / "queue").mkdir()
    (main / "queue" / "Queue.java").write_text(
        "package com.deep.queue;\n\n"
        "import com.deep.api.Handler;\n\n"
        "public class Queue {\n"
        "    public String work() { return new Handler().handle(); }\n"
        "}\n",
        encoding="utf-8",
    )
    (main / "store" / "Store.java").write_text(
        "package com.deep.store;\n\n"
        "import com.deep.queue.Queue;\n\n"
        "public class Store {\n"
        "    public String get() { return new Queue().work(); }\n"
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
        " VALUES ('g4', 'p', 'com.deep.queue.Queue', 'Class', 'Queue', 'src/main/java/com/deep/queue/Queue.java', 5, 7, 'h')"
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
    assert by_pair[("com.deep.store", "com.deep.queue")]["chain"] == ["com.deep.store", "com.deep.queue"]
    assert by_pair[("com.deep.queue", "com.deep.api")]["chain"] == ["com.deep.queue", "com.deep.api"]
    assert by_pair[("com.deep.store", "com.deep.api")]["chain"] == ["com.deep.store", "com.deep.queue", "com.deep.api"]
    # 测试源根 import 不掺入（若泄漏 StoreTest 的 api import 会双计 store→api）
    assert sum(1 for r in rows if json.loads(r[6])["importer"] == "com.deep.store") == 2


def test_enforce_java_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_java_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 import（保类自洽）
        (repo / "src" / "main" / "java" / "com" / "deep" / "store" / "Store.java").write_text(
            "package com.deep.store;\n\npublic class Store {\n    public String get() { return \"s\"; }\n}\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_java_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "main" / "java" / "com" / "deep" / "store" / "Store.java").write_text(
        "package com.deep.store;\n\npublic class Store {\n    public String get() { return \"s\"; }\n}\n",
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
