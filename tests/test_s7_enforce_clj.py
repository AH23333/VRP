"""S7 Clojure 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/clj-enforce-probe）。

引擎无外部执法器（内置源级 ns/require 解析），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_clj

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

CLJ_CONTAINERS = {"queue-mod": ["my-app.queue"], "logging-mod": ["my-app.logging"]}

# 两层：app(queue) ← interface(logging)；违例 = queue 上向 logging
CLJ_STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "queue-mod", "name": "队列", "responsibility": "队", "symbol_fids": ["f1"], "layer": "app"},
            {"module_id": "logging-mod", "name": "日志", "responsibility": "志", "symbol_fids": ["f2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "interface", "may_depend_on": ["app"]},
            {"name": "app", "may_depend_on": []},
        ],
    },
    ensure_ascii=False,
)


def _mk_clj_repo(tmp_path: Path, structure: str | None = CLJ_STRUCTURE) -> Path:
    repo = tmp_path / "cljrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'cljrepo.my-app.queue', 'Module', 'queue', 'src/my_app/queue.clj', 1, 3, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'cljrepo.my-app.logging', 'Module', 'logging', 'src/my_app/logging.clj', 1, 2, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "src" / "my_app").mkdir(parents=True)
    # 违例：第 2 行 (:require [my-app.logging :as log])（ns 边锚定目标）
    (repo / "src" / "my_app" / "queue.clj").write_text(
        "(ns my-app.queue\n"
        "  (:require [my-app.logging :as log]))\n"
        "(defn run [] (log/msg 1))\n",
        encoding="utf-8",
    )
    (repo / "src" / "my_app" / "logging.clj").write_text(
        "(ns my-app.logging)\n(defn msg [x] x)\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


def _extract(src: str) -> tuple[list[dict], dict]:
    """词法 → 形态树 → 依赖行 + ns 名。"""
    blanked, strings = s7_enforce_clj._blank_noncode(src)
    rows: list[dict] = []
    ns_name: dict = {"name": None}
    s7_enforce_clj._walk(
        s7_enforce_clj._parse_forms(s7_enforce_clj._tokenize(blanked, strings)),
        rows,
        ns_name,
    )
    return rows, ns_name


# ------------------------------------------------------------------ unit


def test_blank_noncode_canary_forms():
    src = (
        "; (require 'evil.ns)\n"  # 行注释 canary
        "#! (require 'shebang.ns)\n"  # shebang 注释 canary
        '(def s "text (require in.str)")\n'  # 串内容 canary
        "#_(require 'discarded.ns)\n"  # #_ 两字符空白化（形态保留——诚实局限）
        "(require 'kept.ns)\n"  # 真 require
    )
    blanked, strings = s7_enforce_clj._blank_noncode(src)
    assert "kept.ns" in blanked  # 代码存活
    assert "evil.ns" not in blanked  # 注释空白化
    assert "shebang.ns" not in blanked
    assert "in.str" not in blanked  # 串内容空白化
    assert "text (require in.str)" in list(strings.values())  # 串内容记录
    assert "#_" not in blanked  # #_ 两字符空白化
    assert "discarded.ns" in blanked  # 其后形态不丢弃（文档化局限）
    assert len(blanked) == len(src)  # 等长空白
    assert blanked.count("\n") == src.count("\n")  # 换行保留


def test_blank_unclosed_string_swallows_eof():
    """Clojure 串合法跨行：未闭合吞 EOF（保守）——同 Perl，异 R 截断。"""
    src = '(def a "unclosed\n(require (quote y.ns))\n'
    blanked, _ = s7_enforce_clj._blank_noncode(src)
    assert "y.ns" not in blanked  # 吞 EOF
    assert blanked.count("\n") == src.count("\n")  # 换行仍保留（行号不漂移）


def test_blank_escaped_quote_closes():
    src = '(def s "a \\" b")\n(require (quote after.ns))\n'
    blanked, strings = s7_enforce_clj._blank_noncode(src)
    assert "after.ns" in blanked  # 转义闭合后代码存活
    assert 'a \\" b' in list(strings.values())  # 原文记录（含转义）


def test_regex_literal_embedded_quote():
    src = '(def r #"[a-z\\"]+")\n(require (quote after.ns))\n'
    blanked, _ = s7_enforce_clj._blank_noncode(src)
    assert "a-z" not in blanked  # 正则字面量整体空白化
    assert "#" not in blanked
    assert "after.ns" in blanked  # 内嵌引号不误闭


def test_char_literals_no_false_string():
    """`\\c` 字符字面量全形态：`\\"` 防误开串（同 Erlang `$"` 哲学）。"""
    src = (
        '(def c \\")\n'
        "(def n \\newline)\n"
        "(def u \\u0041)\n"
        "(def o \\o101)\n"
        "(require 'after.ns)\n"
    )
    blanked, _ = s7_enforce_clj._blank_noncode(src)
    assert "after.ns" in blanked  # 无假串开启
    assert "newline" not in blanked  # 命名字符字面量消费
    assert "0041" not in blanked  # unicode 消费
    assert "101" not in blanked  # 八进制消费


def test_tokenizer_form_reader_load_string():
    """tokenizer 串 token 内容取自 strings 记录——load 实参可提取。"""
    src = '(load "my_loads")\n'
    rows, ns_name = _extract(src)
    assert ns_name["name"] is None  # 无 ns 声明
    assert rows == [{"kind": "load", "target": "my_loads", "line": 1}]


def test_extract_ns_clauses_matrix():
    """ns 子句提取矩阵：向量/裸 symbol/前缀列表/:refer 不误取/:use/:require-macros/:import 不产边。"""
    src = (
        "(ns my.app\n"                                                        # 1
        "  (:require [my.lib :as lib]\n"                                      # 2
        "             [my.other :refer [a b]]\n"                              # 3
        "             my.bare\n"                                              # 4
        "             [my.prefix [sub.one :as s1] [sub.two]]\n"               # 5
        "             [my.reload :reload-all])\n"                             # 6
        "  (:use [my.legacy])\n"                                              # 7
        "  (:import (java.util Date))\n"                                      # 8
        "  (:require-macros [my.macros :as m]))\n"                            # 9
        "(defn f [] (lib/g 1))\n"                                             # 10
    )
    rows, ns_name = _extract(src)
    assert ns_name["name"] == "my.app"
    ns_targets = sorted(r["target"] for r in rows if r["kind"] == "ns")
    assert ns_targets == sorted([
        "my.lib", "my.other", "my.bare",
        "my.prefix.sub.one", "my.prefix.sub.two",
        "my.reload", "my.legacy", "my.macros",
    ])
    assert "a" not in ns_targets and "b" not in ns_targets  # :refer 参数不误取
    assert "lib" not in ns_targets and "s1" not in ns_targets  # :as 别名不误取
    assert not any("java" in t for t in ns_targets)  # :import Java 类不产边
    line_of = {(r["kind"], r["target"]): r["line"] for r in rows}
    assert line_of[("ns", "my.lib")] == 2
    assert line_of[("ns", "my.prefix.sub.one")] == 5
    assert line_of[("ns", "my.legacy")] == 7
    assert line_of[("ns", "my.macros")] == 9


def test_extract_require_function_forms():
    """require/use 函数形态：'sym / '[vec] / "str" 实参；:reload 族 flag 跳过。"""
    src = (
        "(require 'my.func1)\n"
        "(require '[my.func2 :as f2])\n"
        '(require "my.func3")\n'
        "(require 'my.func4 :reload-all)\n"
        "(use 'my.use1)\n"
        "(use '[my.use2 :as u2])\n"
    )
    rows, _ = _extract(src)
    assert rows == [
        {"kind": "ns", "target": "my.func1", "line": 1},
        {"kind": "ns", "target": "my.func2", "line": 2},
        {"kind": "ns", "target": "my.func3", "line": 3},
        {"kind": "ns", "target": "my.func4", "line": 4},
        {"kind": "ns", "target": "my.use1", "line": 5},
        {"kind": "ns", "target": "my.use2", "line": 6},
    ]


def test_extract_guards_and_nested():
    """`/` 成员访问守卫 + 关键字头非调用；嵌套 require 递归提取（条件 require）。"""
    src = (
        "(alias/require 'slash.guarded)\n"  # `/` 守卫——不触
        "(:require kw.fake)\n"  # 关键字头非调用
        "(when debug (require 'cond.ns))\n"  # 嵌套条件 require（递归）
        "(external/call (require 'not.touched))\n"  # 外层限定+内层 require
    )
    rows, _ = _extract(src)
    assert rows == [
        {"kind": "ns", "target": "cond.ns", "line": 3},
        {"kind": "ns", "target": "not.touched", "line": 4},
    ]


def test_is_test_conventions():
    assert s7_enforce_clj._is_test("test/my_app/core_test.clj")  # test/ 标准布局
    assert s7_enforce_clj._is_test("test/my_app/core_test.cljc")
    assert s7_enforce_clj._is_test("tests/my_app/x.clj")  # tests/ 目录
    assert s7_enforce_clj._is_test("src/my_app/core_test.clj")  # *_test.clj 惯例
    assert s7_enforce_clj._is_test("src/my_app/core_test.cljc")
    assert s7_enforce_clj._is_test("src/test.clj")  # 裸 test.clj
    assert not s7_enforce_clj._is_test("src/my_app/core.clj")
    assert not s7_enforce_clj._is_test("src/Testing/x.clj")  # 目录段精确小写
    assert not s7_enforce_clj._is_test("src/testing.clj")
    assert not s7_enforce_clj._is_test("src/latest.clj")
    assert not s7_enforce_clj._is_test("src/api.py")  # 非 .clj/.cljc


def test_abs_like_forms():
    assert s7_enforce_clj._abs_like("/abs/x.clj")
    assert s7_enforce_clj._abs_like("C:/x.clj")
    assert s7_enforce_clj._abs_like("~/x.clj")
    assert s7_enforce_clj._abs_like("https://x/y.clj")
    assert not s7_enforce_clj._abs_like("src/x.clj")
    assert not s7_enforce_clj._abs_like("scripts/helper.clj")


def test_scan_clj_layout(tmp_path):
    repo = tmp_path / "proj"
    for d in ("src/my_app", "test/my_app", "target", ".cpcache", ".git", "node_modules", "vendor"):
        (repo / d).mkdir(parents=True)
    (repo / "src" / "my_app" / "a.clj").write_text(
        "(ns proj.a\n  (:require [proj.b :as b]\n            [crangone.core :as ext]))\n",
        encoding="utf-8",
    )
    (repo / "src" / "my_app" / "b.clj").write_text("(ns proj.b)\n", encoding="utf-8")
    (repo / "test" / "my_app" / "a_test.clj").write_text(
        "(ns proj.a-test (:require [proj.b]))\n", encoding="utf-8"
    )
    for rel in ("target/x.clj", ".cpcache/y.clj", ".git/z.clj", "node_modules/n.clj", "vendor/v.clj"):
        p = repo / rel
        p.write_text("(require 'noise.ns)\n", encoding="utf-8")
    (repo / "notes.txt").write_text("(require 'txt.ns)\n", encoding="utf-8")
    file_set, ref_rows, ns_by_file, files_scanned, tests_excluded, timed_out = s7_enforce_clj.scan_clj(repo)
    assert timed_out is False
    assert files_scanned == 3  # target/.cpcache/.git/node_modules/vendor 跳过
    assert tests_excluded == 1  # test/my_app/a_test.clj
    assert file_set == {"src/my_app/a.clj", "src/my_app/b.clj", "test/my_app/a_test.clj"}
    assert ns_by_file == {
        "src/my_app/a.clj": "proj.a",
        "src/my_app/b.clj": "proj.b",
        "test/my_app/a_test.clj": "proj.a-test",
    }
    assert ref_rows == [  # 测试 require 不入列
        {"file": "src/my_app/a.clj", "kind": "ns", "target": "proj.b", "line": 2},
        {"file": "src/my_app/a.clj", "kind": "ns", "target": "crangone.core", "line": 3},
    ]


def test_scan_clj_timeout(tmp_path):
    repo = _mk_clj_repo(tmp_path)
    _fs, _r, _n, _files, _e, timed_out = s7_enforce_clj.scan_clj(repo, timeout=0)
    assert timed_out is True


def test_build_clj_graph_ns_edges():
    """ns 边经命名空间索引命中；散文件容器可出边；外部命名空间跳过。"""
    file_set = {"src/my_app/a.clj", "src/my_app/b.clj", "src/loose.clj", "test/my_app/a_test.clj"}
    ns_by_file = {
        "src/my_app/a.clj": "proj.a",
        "src/my_app/b.clj": "proj.b",
        "test/my_app/a_test.clj": "proj.a-test",
    }
    ref_rows = [
        {"file": "src/my_app/a.clj", "kind": "ns", "target": "proj.b", "line": 2},
        {"file": "src/my_app/a.clj", "kind": "ns", "target": "crangone.core", "line": 3},  # 外部跳过
        {"file": "src/loose.clj", "kind": "ns", "target": "proj.a", "line": 1},  # 散文件出边
    ]
    graph, edges = s7_enforce_clj.build_clj_graph(file_set, ref_rows, ns_by_file)
    assert graph == {
        "proj.a": ["proj.b"],
        "proj.b": [],
        "proj.a-test": [],  # 测试容器仍入图（叶可见）
        "src/loose.clj": ["proj.a"],
    }
    assert edges == [
        {"file": "src/my_app/a.clj", "line": 2, "src": "proj.a", "dst": "proj.b"},
        {"file": "src/loose.clj", "line": 1, "src": "src/loose.clj", "dst": "proj.a"},
    ]


def test_build_clj_graph_load_resolution():
    """load 按 classpath 根候选；load-file 按仓根+文件目录候选；绝对路径跳过。"""
    file_set = {"scripts/helper.clj", "src/dep.clj"}
    ns_by_file = {"scripts/helper.clj": "proj.helper", "src/dep.clj": "proj.dep"}
    ref_rows = [
        {"file": "scripts/helper.clj", "kind": "load", "target": "dep", "line": 2},
        {"file": "scripts/helper.clj", "kind": "load", "target": "src/dep", "line": 3},
        {"file": "scripts/helper.clj", "kind": "load-file", "target": "src/dep.clj", "line": 4},
        {"file": "scripts/helper.clj", "kind": "load", "target": "/abs/dep", "line": 5},  # abs 跳过
    ]
    graph, edges = s7_enforce_clj.build_clj_graph(file_set, ref_rows, ns_by_file)
    assert graph == {"proj.helper": ["proj.dep"], "proj.dep": []}
    assert edges == [
        {"file": "scripts/helper.clj", "line": 2, "src": "proj.helper", "dst": "proj.dep"},
        {"file": "scripts/helper.clj", "line": 3, "src": "proj.helper", "dst": "proj.dep"},
        {"file": "scripts/helper.clj", "line": 4, "src": "proj.helper", "dst": "proj.dep"},
    ]


def test_belongs_exact_match():
    assert s7_enforce_clj._belongs("my-app.queue", ["my-app.queue"])
    assert not s7_enforce_clj._belongs("my-app.queue2", ["my-app.queue"])  # 前缀污染守卫
    assert not s7_enforce_clj._belongs("x", ["my-app.queue"])


def test_find_violations_bfs_shortest_chain():
    graph = {
        "app": ["mid", "queue"],
        "mid": ["iface"],  # 2 跳
        "queue": ["mid"],  # 3 跳路径
        "iface": [],
        "other": [],
    }
    contracts = [
        {
            "name": "layer: app must not import interface",
            "source_layer": "app",
            "forbidden_layer": "interface",
            "source_modules": ["app"],
            "forbidden_modules": ["iface"],
        }
    ]
    violations = s7_enforce_clj._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "app" and v["imported"] == "iface"
    assert v["chain"] == ["app", "mid", "iface"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶容器（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"app": ["leaf"], "leaf": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["leaf"]}]
    violations = s7_enforce_clj._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["app", "leaf"]


def test_find_violations_none_when_unreachable():
    graph = {"app": ["mid"], "mid": [], "iface": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["iface"]}]
    assert s7_enforce_clj._find_violations(graph, contracts) == []


# ------------------------------------------------- derive / run_clj E2E


def test_derive_clj_containers(tmp_path):
    repo = _mk_clj_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_clj.derive_clj_containers(repo, con, json.loads(CLJ_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == CLJ_CONTAINERS
    assert derived["clj_files"] == 2  # src/my_app/queue.clj + logging.clj
    assert derived["namespaces"] == 2
    assert derived["loose_files"] == 0
    assert derived["extensions"] == []
    assert derived["timed_out"] is False


def test_run_clj_violation_e2e(tmp_path):
    repo = _mk_clj_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_clj.run_clj(repo, con, json.loads(CLJ_STRUCTURE), "active", CLJ_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "clojure-namespace-parser"
    assert out["contracts"] == 1
    assert out["files"] == 2
    assert out["modules"] == 2
    assert out["namespaces"] == 2
    assert out["tests_excluded"] == 0
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    row = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT pathology, kind, source, anchor_fid, file_path, start_line, end_line, payload_json"
        " FROM findings"
    ).fetchone()
    assert row[0] == "P9"
    assert row[1] == "layer-violation"
    assert row[2] == "s7-enforce"
    assert row[3] == "f1"  # src/my_app/queue.clj 首符号
    assert row[4] == "src/my_app/queue.clj"
    assert row[5] == 2 and row[6] == 2  # 违例 require 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "app"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "my-app.queue"  # 容器 = 命名空间
    assert payload["imported"] == "my-app.logging"
    assert payload["chain"] == ["my-app.queue", "my-app.logging"]
    assert payload["line"] == 2


def test_run_clj_transitive_e2e(tmp_path):
    """传递链 core storage → queue → interface logging（命名空间容器中间层）。"""
    repo = _mk_clj_repo(tmp_path)
    (repo / "src" / "my_app" / "storage.clj").write_text(
        "(ns my-app.storage\n  (:require [my-app.queue :as q]))\n",
        encoding="utf-8",
    )
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
                {"module_id": "iface-mod", "name": "日志", "responsibility": "志", "symbol_fids": ["f2"], "layer": "interface"},
            ],
            "layers": [
                {"name": "core", "may_depend_on": []},
                {"name": "interface", "may_depend_on": ["core"]},
            ],
        },
        ensure_ascii=False,
    )
    con = _con(repo)
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    con.close()
    payload = json.loads(structure)
    containers = {"core-mod": ["my-app.storage", "my-app.queue"], "iface-mod": ["my-app.logging"]}
    con = _con(repo)
    try:
        out = s7_enforce_clj.run_clj(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queue→logging 直连 + storage 经 queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["my-app.queue"]["chain"] == ["my-app.queue", "my-app.logging"]
    assert by_importer["my-app.storage"]["chain"] == ["my-app.storage", "my-app.queue", "my-app.logging"]
    assert by_importer["my-app.storage"]["line"] == 2  # 锚定链尾直接边：queue.clj 第 2 行
    assert all(r[0] == "src/my_app/queue.clj" for r in rows)


def test_run_clj_idempotent(tmp_path):
    repo = _mk_clj_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_clj.run_clj(repo, con, json.loads(CLJ_STRUCTURE), "active", CLJ_CONTAINERS)
        second = s7_enforce_clj.run_clj(repo, con, json.loads(CLJ_STRUCTURE), "active", CLJ_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_clj_compliant_clears_old_findings(tmp_path):
    repo = _mk_clj_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例依赖
    (repo / "src" / "my_app" / "queue.clj").write_text(
        "(ns my-app.queue)\n(defn run [] 1)\n", encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_clj.run_clj(repo, con, json.loads(CLJ_STRUCTURE), "active", CLJ_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_clj_no_files(tmp_path):
    repo = _mk_clj_repo(tmp_path)
    (repo / "src" / "my_app" / "queue.clj").unlink()
    (repo / "src" / "my_app" / "logging.clj").unlink()
    con = _con(repo)
    try:
        out = s7_enforce_clj.run_clj(repo, con, json.loads(CLJ_STRUCTURE), "active", CLJ_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no clojure files found" in out["reason"]


def test_run_clj_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_clj_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_clj,
        "scan_clj",
        lambda repo, timeout=600: (set(), [], {}, 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_clj.run_clj(repo, con, json.loads(CLJ_STRUCTURE), "active", CLJ_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "clojure scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_clj_test_file_excluded_e2e(tmp_path):
    """测试文件（test/ 目录 + *_test.clj 约定）依赖不产边；排除计数诚实上报。"""
    repo = _mk_clj_repo(tmp_path)
    (repo / "test" / "my_app").mkdir(parents=True)
    (repo / "test" / "my_app" / "queue_test.clj").write_text(
        "(ns my-app.queue-test (:require [my-app.queue] [my-app.logging]))\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_clj.run_clj(repo, con, json.loads(CLJ_STRUCTURE), "active", CLJ_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["tests_excluded"] == 1
    assert out["files"] == 3  # files_scanned 含测试文件
    assert out["modules"] == 3  # 生产 queue/logging + 测试命名空间容器入图（叶可见）
    assert out["violations"] == 1  # 仅生产 require 产违例


# ------------------------------------------------------ run() 路由


def test_run_routes_clj(tmp_path):
    """纯 Clojure 结构（.clj）→ run() 路由至 Clojure 引擎。"""
    repo = _mk_clj_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "clojure-namespace-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_clj_and_py_not_enforceable(tmp_path):
    repo = _mk_clj_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'cljrepo.helper.mod', 'Function', 'mod', 'helper/mod.py', 1, 2, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps({
        "modules": [
            {"module_id": "queue-mod", "name": "队列", "responsibility": "队", "symbol_fids": ["f1"], "layer": "app"},
            {"module_id": "logging-mod", "name": "日志", "responsibility": "志", "symbol_fids": ["f2"], "layer": "interface"},
            {"module_id": "py-mod", "name": "杂", "responsibility": "混", "symbol_fids": ["f3"], "layer": "app"},
        ],
        "layers": [
            {"name": "interface", "may_depend_on": ["app"]},
            {"name": "app", "may_depend_on": []},
        ],
    }, ensure_ascii=False),))
    con.commit()
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "mixed-language" in out["reason"]
    assert "py + clj" in out["reason"]


def test_cli_enforce_clj_wiring(tmp_path, capsys):
    repo = _mk_clj_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["engine"] == "clojure-namespace-parser"
    assert out["violations"] == 1
