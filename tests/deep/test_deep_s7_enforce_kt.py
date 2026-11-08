"""S7 Kotlin 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_kt.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实 Maven Kotlin 工程布局——package
结构、传递链、src/test/kotlin 测试源根排除；真仓 dogfood 另见 work/kt-enforce-probe。
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
        " VALUES ('g1', 'p', 'deep.store.Store', 'Class', 'Store', 'src/main/kotlin/com/deep/store/Store.kt', 5, 9, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'deep.api.Handler', 'Class', 'Handler', 'src/main/kotlin/com/deep/api/Handler.kt', 3, 7, 'h')"
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
        "  <groupId>com.deep</groupId>\n  <artifactId>deep-probe</artifactId>\n</project>\n",
        encoding="utf-8",
    )
    main = repo / "src" / "main" / "kotlin" / "com" / "deep"
    (main / "store").mkdir(parents=True)
    (main / "api").mkdir(parents=True)
    (main / "api" / "Handler.kt").write_text(
        "package com.deep.api\n\nclass Handler {\n    fun handle(): String = \"h\"\n}\n",
        encoding="utf-8",
    )
    # 违例：Store 包 import Api 包（第 3 行）
    (main / "store" / "Store.kt").write_text(
        "package com.deep.store\n\n"
        "import com.deep.api.Handler\n\n"
        "class Store {\n"
        "    fun get(): String = Handler().handle()\n"
        "}\n",
        encoding="utf-8",
    )
    # 测试源根：若泄漏会双计违例
    test = repo / "src" / "test" / "kotlin" / "com" / "deep" / "store"
    test.mkdir(parents=True)
    (test / "StoreTest.kt").write_text(
        "package com.deep.store\n\n"
        "import com.deep.api.Handler\n\n"
        "class StoreTest {\n"
        "    fun t() { Handler() }\n"
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


def test_enforce_kt_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "kt-package-parser"  # 语言路由命中 Kotlin 引擎
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
    assert file_path == "src/main/kotlin/com/deep/store/Store.kt"
    assert line == 3  # 违例 import 行
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "com.deep.store"  # 容器 = Kotlin 包
    assert data["imported"] == "com.deep.api"
    assert data["chain"] == ["com.deep.store", "com.deep.api"]
    assert data["line"] == 3


def test_enforce_kt_e2e_transitive_and_test_root_excluded(tmp_path):
    """三层链 com.deep.store → com.deep.queue → com.deep.api + 测试源根不掺入。"""
    repo = _mk_repo(tmp_path)
    main = repo / "src" / "main" / "kotlin" / "com" / "deep"
    (main / "queue").mkdir()
    (main / "queue" / "Queue.kt").write_text(
        "package com.deep.queue\n\n"
        "import com.deep.api.Handler\n\n"
        "class Queue {\n"
        "    fun work(): String = Handler().handle()\n"
        "}\n",
        encoding="utf-8",
    )
    (main / "store" / "Store.kt").write_text(
        "package com.deep.store\n\n"
        "import com.deep.queue.Queue\n\n"
        "class Store {\n"
        "    fun get(): String = Queue().work()\n"
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
        " VALUES ('g4', 'p', 'deep.queue.Queue', 'Class', 'Queue', 'src/main/kotlin/com/deep/queue/Queue.kt', 5, 9, 'h')"
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
    # 测试源根 import 不掺入（若泄漏 StoreTest 的 Api import 会双计 Store→Api）
    assert sum(1 for r in rows if json.loads(r[6])["importer"] == "com.deep.store") == 2


def test_enforce_kt_e2e_review_batch_regressions(tmp_path):
    """review 修正回归三合一：BOM 前缀 + 未闭合串换行截断 + 末行 import 无换行。

    修复前该形态 0 违例假阴（串吞 import 或 EOF 终结符不命中）。
    """
    repo = _mk_repo(tmp_path)
    # WIP 文件形态：BOM 开头；第 3 行未闭合串（换行截断）；末行 import 无换行（EOF 终止）
    (repo / "src" / "main" / "kotlin" / "com" / "deep" / "store" / "Store.kt").write_text(
        '\ufeffpackage com.deep.store\nval s = "wip\nimport com.deep.api.Handler',
        encoding="utf-8",
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 修复前 0（假阴）
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"
    assert file_path == "src/main/kotlin/com/deep/store/Store.kt"
    assert line == 3  # 末行 import 真行号
    data = json.loads(payload)
    assert data["chain"] == ["com.deep.store", "com.deep.api"]
    assert data["line"] == 3


def test_enforce_kt_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_kt_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 import（保类自洽）
        (repo / "src" / "main" / "kotlin" / "com" / "deep" / "store" / "Store.kt").write_text(
            "package com.deep.store\n\nclass Store {\n    fun get(): String = \"s\"\n}\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_kt_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "main" / "kotlin" / "com" / "deep" / "store" / "Store.kt").write_text(
        "package com.deep.store\n\nclass Store {\n    fun get(): String = \"s\"\n}\n",
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
