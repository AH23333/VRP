"""S7 Clojure 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_clj.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实源布局——命名空间容器、词法
canary（行注释/shebang/字符串/正则字面量/字符字面量/多行串内伪调用）、
test/ 目录测试排除、传递链；真仓 dogfood 另见 work/clj-enforce-probe。
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
        " VALUES ('g1', 'p', 'repo.my-app.queue', 'Module', 'queue', 'src/my_app/queue.clj', 1, 10, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'repo.my-app.logging', 'Module', 'logging', 'src/my_app/logging.clj', 1, 2, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "src" / "my_app").mkdir(parents=True)
    (repo / "test" / "my_app").mkdir(parents=True)
    # 违例：第 9 行 (require 'my-app.logging)；词法 canary——行注释（2）/
    # shebang（3）/双引号串（4）/正则字面量（5）/字符字面量 `\"`（6）/
    # 多行串（7-8）内伪调用；若泄漏会多 queue→logging 边
    (repo / "src" / "my_app" / "queue.clj").write_text(
        "(ns my-app.queue)\n"
        "; (require 'my-app.logging)\n"
        "#! (require 'my-app.logging)\n"
        '(def s "text (require \'my-app.logging)")\n'
        '(def r #"[a-z\\"]+")\n'
        '(def c \\")\n'
        "(def m \"line1\n"
        "(require 'my-app.logging)\")\n"
        "(require 'my-app.logging)\n"
        "(defn run [] 1)\n",
        encoding="utf-8",
    )
    (repo / "src" / "my_app" / "logging.clj").write_text(
        "(ns my-app.logging)\n(defn msg [x] x)\n",
        encoding="utf-8",
    )
    # 测试文件：test/ 目录约定蓄意 require 双模块——排除后不产边（若泄漏
    # 会引入 tests_excluded 计数漂移与伪边）
    (repo / "test" / "my_app" / "queue_test.clj").write_text(
        "(ns my-app.queue-test (:require [my-app.queue] [my-app.logging]))\n",
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


def test_enforce_clj_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "clojure-namespace-parser"  # 语言路由命中 Clojure 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 3  # queue.clj + logging.clj + queue_test.clj（扫描计数诚实）
    assert out["modules"] == 3  # queue/logging/测试命名空间容器入图（叶可见）
    assert out["namespaces"] == 3
    assert out["tests_excluded"] == 1  # test/my_app/queue_test.clj 文件级排除
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 require 文件首符号
    assert file_path == "src/my_app/queue.clj"
    assert line == 9  # 违例 require 行（canary 6 处不算：注释/shebang/串/正则/字符字面量/多行串内）
    data = json.loads(payload)
    assert data["source_layer"] == "app"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "my-app.queue"  # 容器 = 命名空间
    assert data["imported"] == "my-app.logging"
    assert data["chain"] == ["my-app.queue", "my-app.logging"]
    assert data["line"] == 9


def test_enforce_clj_e2e_transitive_and_test_file_excluded(tmp_path):
    """三层链 storage → queue → logging + 测试文件依赖不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "src" / "my_app" / "storage.clj").write_text(
        "(ns my-app.storage)\n(require 'my-app.queue)\n",
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
        " VALUES ('g3', 'p', 'repo.my-app.storage', 'Module', 'storage', 'src/my_app/storage.clj', 1, 2, 'h')"
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
    assert by_pair[("my-app.queue", "my-app.logging")]["chain"] == ["my-app.queue", "my-app.logging"]
    assert by_pair[("my-app.storage", "my-app.queue")]["chain"] == ["my-app.storage", "my-app.queue"]
    assert by_pair[("my-app.storage", "my-app.logging")]["chain"] == ["my-app.storage", "my-app.queue", "my-app.logging"]
    # 锚定：queue/storage→logging 锚 queue.clj:9（链尾直接边）；storage→queue 锚 storage.clj:2
    anchored = {(r[4], r[5]) for r in rows}
    assert anchored == {("src/my_app/queue.clj", 9), ("src/my_app/storage.clj", 2)}


def test_enforce_clj_e2e_reader_conditional_edge(tmp_path):
    """违例藏于 #? 分支（.cljc 双平台形态）+ 注释/串内伪条件 canary——锚分支行。"""
    repo = _mk_repo(tmp_path)
    (repo / "src" / "my_app" / "queue.clj").write_text(
        "(ns my-app.queue)\n"                                                 # 1
        "; (require #?@(:clj [[my-app.logging]]))\n"                          # 2 注释 canary（伪条件）
        "#! (require 'my-app.logging)\n"                                      # 3 shebang canary
        '(def s "text (require \'my-app.logging) #?(:clj [fake.instr])")\n'   # 4 串 canary（伪条件）
        '(def r #"[a-z\\"]+")\n'                                              # 5 正则 canary
        '(def c \\")\n'                                                       # 6 字符字面量 canary
        "(def m \"line1\n"                                                    # 7 多行串起
        "(require 'my-app.logging)\")\n"                                      # 8 多行串讫
        "(require #?(:clj 'my-app.logging :cljs 'my-app.fake))\n"             # 9 #? 分支违例
        "(defn run [] 1)\n",                                                  # 10
        encoding="utf-8",
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    rows = _rows(repo)
    assert len(rows) == 1
    assert rows[0][4] == "src/my_app/queue.clj"
    assert rows[0][5] == 9  # #? 分支内 require 行
    data = json.loads(rows[0][6])
    assert data["chain"] == ["my-app.queue", "my-app.logging"]
    assert data["line"] == 9


def test_enforce_clj_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_clj_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 require（保留词法 canary——修复后 canary 也不得复活违例）
        (repo / "src" / "my_app" / "queue.clj").write_text(
            "(ns my-app.queue)\n"
            "; (require 'my-app.logging)\n"
            "#! (require 'my-app.logging)\n"
            '(def s "text (require \'my-app.logging)")\n'
            '(def r #"[a-z\\"]+")\n'
            '(def c \\")\n'
            "(def m \"line1\n"
            "(require 'my-app.logging)\")\n"
            "(defn run [] 1)\n",
            encoding="utf-8",
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_clj_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "my_app" / "queue.clj").write_text(
        "(ns my-app.queue)\n(defn run [] 1)\n",
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
