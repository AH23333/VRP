"""S7 C/C++ 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_c.py）。

引擎内置 #include 解析器无外部工具链：deep 层覆盖真实 C 工程目录布局——
include 依赖、传递链、测试文件排除（目录段与文件名约定）；真仓 dogfood
（gcc 编译验证 + CBM 事实）另见 work/c-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：core(src/deep/store) ← interface(src/deep/api)
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
        " VALUES ('g1', 'p', 'deep.store.store_put', 'Function', 'store_put', 'src/deep/store/storage.c', 3, 7, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'deep.api.api_handle', 'Function', 'api_handle', 'src/deep/api/api.c', 3, 6, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(deep_probe C)\n",
        encoding="utf-8",
    )
    base = repo / "src" / "deep"
    (base / "store").mkdir(parents=True)
    (base / "api").mkdir(parents=True)
    (base / "api" / "api.h").write_text(
        "#ifndef DEEP_API_H\n#define DEEP_API_H\n\nconst char *api_handle(void);\n\n#endif\n",
        encoding="utf-8",
    )
    (base / "api" / "api.c").write_text(
        '#include "api.h"\n\nconst char *api_handle(void)\n{\n    return "ok";\n}\n',
        encoding="utf-8",
    )
    # 违例：storage.c 第 1 行 include ../api/api.h（跨目录上向依赖）
    (base / "store" / "storage.c").write_text(
        '#include "../api/api.h"\n\n'
        "int store_put(const char *key)\n"
        "{\n"
        "    (void)key;\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    # 测试 canary（目录段约定）：若泄漏会双计 store→api 违例
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_storage.c").write_text(
        '#include "../src/deep/api/api.h"\n\n'
        "void test_store(void)\n"
        "{\n"
        "    (void)api_handle();\n"
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


def test_enforce_c_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "c-include-parser"  # 语言路由命中 C 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 4  # 3 生产 + 1 测试（扫描计数诚实）
    assert out["dirs"] == 3  # src/deep/store、src/deep/api、tests
    assert out["tests_excluded"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 include 文件首符号
    assert file_path == "src/deep/store/storage.c"
    assert line == 1  # 违例 include 行
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "src/deep/store"  # 容器 = 目录
    assert data["imported"] == "src/deep/api"
    assert data["chain"] == ["src/deep/store", "src/deep/api"]
    assert data["line"] == 1


def test_enforce_c_e2e_transitive_and_filename_test_excluded(tmp_path):
    """三层链 store → queue → api + 文件名约定测试不掺入（泄漏则链变形为直连）。"""
    repo = _mk_repo(tmp_path)
    base = repo / "src" / "deep"
    (base / "queue").mkdir()
    (base / "queue" / "queue.h").write_text(
        "#ifndef DEEP_QUEUE_H\n#define DEEP_QUEUE_H\n\nint task_enqueue(const char *task);\n\n#endif\n",
        encoding="utf-8",
    )
    (base / "queue" / "queue.c").write_text(
        '#include "../api/api.h"\n\n'
        "int task_enqueue(const char *task)\n"
        "{\n"
        "    (void)task;\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    (base / "store" / "storage.c").write_text(
        '#include "../queue/queue.h"\n\n'
        "int store_put(const char *key)\n"
        "{\n"
        "    (void)key;\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    # 文件名约定 canary：store 目录内 test_storage.c 直连 include api——
    # 若泄漏，BFS 最短链变直连 [store, api] 且锚 test_storage.c，断言链形即可捕获
    (base / "store" / "test_storage.c").write_text(
        '#include "../api/api.h"\n\n'
        "void test_store(void)\n"
        "{\n"
        "    (void)api_handle();\n"
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
        " VALUES ('g4', 'p', 'deep.queue.task_enqueue', 'Function', 'task_enqueue', 'src/deep/queue/queue.c', 3, 7, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 7  # 5 生产（api.h/api.c/storage.c/queue.h/queue.c）+ 2 测试
    assert out["tests_excluded"] == 2  # 目录段 tests/ + 文件名 test_storage.c
    assert out["violations"] == 3  # core→app / core→interface / app→interface 各一
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("src/deep/store", "src/deep/queue")]["chain"] == ["src/deep/store", "src/deep/queue"]
    assert by_pair[("src/deep/queue", "src/deep/api")]["chain"] == ["src/deep/queue", "src/deep/api"]
    # canary 排除生效：store→api 走 queue 传递链而非直连（直连即泄漏）
    assert by_pair[("src/deep/store", "src/deep/api")]["chain"] == [
        "src/deep/store",
        "src/deep/queue",
        "src/deep/api",
    ]
    assert all(r[4] != "src/deep/store/test_storage.c" for r in rows)  # canary 文件不锚定


def test_enforce_c_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_c_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 include（保函数自洽）
        (repo / "src" / "deep" / "store" / "storage.c").write_text(
            "int store_put(const char *key)\n"
            "{\n"
            "    (void)key;\n"
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


def test_enforce_c_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "deep" / "store" / "storage.c").write_text(
        "int store_put(const char *key)\n"
        "{\n"
        "    (void)key;\n"
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
