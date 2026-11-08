"""S7 R 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_r.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实包布局——DESCRIPTION 包容器、
词法 canary（行注释/roxygen/字符串/未闭合串内伪调用）、testthat 测试排除、
传递链；真仓 dogfood 另见 work/r-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：app(queue) ← interface(logging)
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
        " VALUES ('g1', 'p', 'repo.queue.queue', 'Module', 'queue', 'queue/R/queue.R', 6, 6, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'repo.logging.logging', 'Module', 'logging', 'logging/R/logging.R', 1, 1, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "queue" / "R").mkdir(parents=True)
    (repo / "logging" / "R").mkdir(parents=True)
    (repo / "tests" / "testthat").mkdir(parents=True)
    (repo / "queue" / "DESCRIPTION").write_text("Package: queue\n", encoding="utf-8")
    (repo / "logging" / "DESCRIPTION").write_text("Package: logging\n", encoding="utf-8")
    # 违例：第 6 行 library(logging)；词法 canary——行注释（1）/roxygen（2）/
    # 双引号串（3）/单引号串（4）/未闭合串截断（5）内伪调用
    # 若泄漏会多 queue→logging 边
    (repo / "queue" / "R" / "queue.R").write_text(
        "# library(logging)\n"
        "#' @import logging\n"
        'x <- "library(logging)"\n'
        "y <- 'library(logging)'\n"
        'z <- "unterminated\n'
        "library(logging)\n",
        encoding="utf-8",
    )
    (repo / "logging" / "R" / "logging.R").write_text(
        "log_msg <- function(x) x\n",
        encoding="utf-8",
    )
    # 测试文件：testthat 约定蓄意调用双模块——排除后不产边（若泄漏会引入
    # tests_excluded 计数漂移与伪边）
    (repo / "tests" / "testthat" / "test-queue.R").write_text(
        "library(queue)\nlibrary(logging)\n",
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


def test_enforce_r_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "r-source-package-parser"  # 语言路由命中 R 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # queue.R + logging.R + test-queue.R（扫描计数诚实）
    assert out["modules"] == 3  # queue/logging/测试散文件容器入图（叶可见）
    assert out["tests_excluded"] == 1  # tests/testthat/test-queue.R 文件级排除
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例调用文件首符号
    assert file_path == "queue/R/queue.R"
    assert line == 6  # 违例调用行（canary 5 处不算：注释/roxygen/双串/单串/未闭合串内）
    data = json.loads(payload)
    assert data["source_layer"] == "app"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "queue"  # 容器 = DESCRIPTION 包目录
    assert data["imported"] == "logging"
    assert data["chain"] == ["queue", "logging"]
    assert data["line"] == 6


def test_enforce_r_e2e_transitive_and_test_file_excluded(tmp_path):
    """三层链 storage → queue → logging + 测试文件调用不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "storage" / "R").mkdir(parents=True)
    (repo / "storage" / "DESCRIPTION").write_text("Package: storage\n", encoding="utf-8")
    (repo / "storage" / "R" / "storage.R").write_text(
        "library(queue)\n",
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
        " VALUES ('g3', 'p', 'repo.storage.storage', 'Module', 'storage', 'storage/R/storage.R', 1, 1, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 4
    assert out["modules"] == 4
    assert out["tests_excluded"] == 1
    assert out["violations"] == 3  # queue→logging 直连 + storage→queue 直连 + storage 经 queue 传递
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("queue", "logging")]["chain"] == ["queue", "logging"]
    assert by_pair[("storage", "queue")]["chain"] == ["storage", "queue"]
    assert by_pair[("storage", "logging")]["chain"] == ["storage", "queue", "logging"]
    # 锚定：queue/storage→logging 锚 queue.R:6（链尾直接边）；storage→queue 锚 storage.R:1
    anchored = {(r[4], r[5]) for r in rows}
    assert anchored == {("queue/R/queue.R", 6), ("storage/R/storage.R", 1)}


def test_enforce_r_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_r_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例调用（保留词法 canary——修复后 canary 也不得复活违例）
        (repo / "queue" / "R" / "queue.R").write_text(
            "# library(logging)\n"
            "#' @import logging\n"
            'x <- "library(logging)"\n'
            "y <- 'library(logging)'\n"
            'z <- "unterminated\n'
            "run <- function() 1\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_r_e2e_desc_imports_and_multiline_edge(tmp_path):
    """声明边+使用边并存：DESCRIPTION Imports 与跨行 library 实参双证据——
    锚定取 (file,line) 字典序首（DESCRIPTION 先于 R/），声明边胜出且无符号
    锚；注释内跨行伪调用 canary 不产边。"""
    repo = _mk_repo(tmp_path)
    (repo / "queue" / "DESCRIPTION").write_text(
        "Package: queue\n"
        "Imports:\n"  # 字段行 2（值在续行——锚字段起始行）
        "    logging (>= 1.0),\n"
        "    utils\n"  # 外部包不产边
        "Suggests: testthat\n",  # 软依赖不产边
        encoding="utf-8",
    )
    (repo / "queue" / "R" / "queue.R").write_text(
        "# library(\n"
        "#   \"logging\")\n"  # 注释内跨行伪调用 canary（1-2）
        'x <- "library(logging)"\n'  # 串内 canary（3）
        "library(\n"
        '  "logging"\n'
        ")\n"  # 跨行真调用（4-6）——使用侧边 line 4
        "run <- function() 1\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 声明边+使用边同 (queue, logging)——dedup 单违例
    assert out["anchored"] == 0  # DESCRIPTION 边字典序首胜出——无符号锚
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor is None
    assert file_path == "queue/DESCRIPTION"  # "D" < "R" 字典序
    assert line == 2  # Imports: 字段行
    data = json.loads(payload)
    assert data["chain"] == ["queue", "logging"]
    assert data["line"] == 2


def test_enforce_r_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "queue" / "R" / "queue.R").write_text(
        "run <- function() 1\n",
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
