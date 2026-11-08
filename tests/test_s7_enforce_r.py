"""S7 R 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/r-enforce-probe）。

引擎无外部执法器（内置源级 source/包引用解析），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_r

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

R_CONTAINERS = {"queue-mod": ["queue"], "logging-mod": ["logging"]}

# 两层：app(queue) ← interface(logging)；违例 = queue 上向 library(logging)
R_STRUCTURE = json.dumps(
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


def _mk_r_repo(tmp_path: Path, structure: str | None = R_STRUCTURE) -> Path:
    repo = tmp_path / "rrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'rrepo.queue.queue', 'Module', 'queue', 'queue/R/queue.R', 1, 1, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'rrepo.logging.logging', 'Module', 'logging', 'logging/R/logging.R', 1, 1, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "queue" / "R").mkdir(parents=True)
    (repo / "logging" / "R").mkdir(parents=True)
    (repo / "queue" / "DESCRIPTION").write_text("Package: queue\n", encoding="utf-8")
    (repo / "logging" / "DESCRIPTION").write_text("Package: logging\n", encoding="utf-8")
    # 违例：第 1 行 library(logging)（library 边锚定目标）
    (repo / "queue" / "R" / "queue.R").write_text(
        "library(logging)\n",
        encoding="utf-8",
    )
    (repo / "logging" / "R" / "logging.R").write_text(
        "log_msg <- function(x) x\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode_canary_forms():
    src = (
        '# library(logging)\n'  # 行注释 canary
        "#' @import logging\n"  # roxygen canary（@import 元数据非依赖）
        'x <- "library(logging)"\n'  # 双引号串 canary
        "y <- 'library(logging)'\n"  # 单引号串 canary
        "library(logging)\n"  # 真 library
        "z <- 1\n"
    )
    blanked = s7_enforce_r._blank_noncode(src)
    assert "library(logging)\n" in blanked  # 代码存活（第 5 行）
    assert blanked.count("library(logging)") == 1  # canary 空白化
    assert "@import" not in blanked
    assert len(blanked) == len(src)  # 等长空白
    assert blanked.count("\n") == src.count("\n")  # 换行保留


def test_blank_unclosed_string_truncates_line():
    """R 串不可裸跨行：未闭合串自愈截断行尾（不吞文件尾）。"""
    src = 'x <- "unclosed\nlibrary(logging)\n'
    blanked = s7_enforce_r._blank_noncode(src)
    assert "unclosed" not in blanked
    assert "library(logging)" in blanked  # 行 2 存活——未吞文件尾
    assert blanked.count("\n") == 2


def test_blank_escaped_quote_closes():
    src = 's <- "a \\" b"\nlibrary(logging)\n'
    blanked = s7_enforce_r._blank_noncode(src)
    rows = s7_enforce_r._iter_refs(blanked)
    assert rows == [{"kind": "package", "target": "logging", "line": 2}]


def test_preserved_source_string_targets():
    """source 目标区整体保留（同 C include 哲学）——三种实参形态。"""
    src = (
        'source("R/a.R")\n'
        "source('R/b.R')\n"
        'source(file = "R/c.R")\n'
    )
    blanked = s7_enforce_r._blank_noncode(src)
    assert 'source("R/a.R")' in blanked
    assert "source('R/b.R')" in blanked
    assert 'source(file = "R/c.R")' in blanked
    rows = s7_enforce_r._iter_refs(blanked)
    assert rows == [
        {"kind": "source", "target": "R/a.R", "line": 1},
        {"kind": "source", "target": "R/b.R", "line": 2},
        {"kind": "source", "target": "R/c.R", "line": 3},
    ]


def test_preserved_library_string_and_named():
    src = (
        'library("logging")\n'
        'requireNamespace(package = "queue")\n'
        'loadNamespace(\n"core"\n)\n'  # 跨行实参
    )
    blanked = s7_enforce_r._blank_noncode(src)
    assert 'library("logging")' in blanked
    assert 'requireNamespace(package = "queue")' in blanked
    rows = s7_enforce_r._iter_refs(blanked)
    assert rows == [
        {"kind": "package", "target": "logging", "line": 1},
        {"kind": "package", "target": "queue", "line": 2},
        {"kind": "package", "target": "core", "line": 3},  # 跨行实参提取（此前漏提）
    ]


def test_iter_refs_multiline_call_args():
    """跨行实参提取（空白类含换行）；关键字与 ( 须同行——R 语句边界不误触。"""
    src = (
        'library(\n  "dplyr")\n'  # 1-2 跨行串实参
        "source(\n  'R/a.R')\n"  # 3-4
        'source(file =\n  "R/b.R")\n'  # 5-6 命名实参跨行
        'requireNamespace(\n  package = "ns"\n)\n'  # 7-9
        'source\n("R/c.R")\n'  # 10-11 关键字-括号跨行=语句边界，不误触
    )
    blanked = s7_enforce_r._blank_noncode(src)
    rows = s7_enforce_r._iter_refs(blanked)
    assert rows == [  # _iter_refs 按 regex 分组：source 组在前
        {"kind": "source", "target": "R/a.R", "line": 3},
        {"kind": "source", "target": "R/b.R", "line": 5},
        {"kind": "package", "target": "dplyr", "line": 1},
        {"kind": "package", "target": "ns", "line": 7},
    ]


def test_preserved_dollar_and_backtick_rejected():
    """`$source(...)` 方法访问拒绝；反引号标识符内容空白化。"""
    src = 'q <- lst$source("no.R")\n`source("bt.R")`\nlibrary(logging)\n'
    blanked = s7_enforce_r._blank_noncode(src)
    assert "no.R" not in blanked
    assert "bt.R" not in blanked
    rows = s7_enforce_r._iter_refs(blanked)
    assert rows == [{"kind": "package", "target": "logging", "line": 3}]


def test_preserved_base_qualified_source():
    """`base::source("x.R")` 语义仍是载入——保留并提取。"""
    src = 'base::source("R/x.R")\n'
    blanked = s7_enforce_r._blank_noncode(src)
    rows = s7_enforce_r._iter_refs(blanked)
    assert rows == [  # :: 限定符按通用规则同时产出 package 引用（base 外部，建图时跳过）
        {"kind": "source", "target": "R/x.R", "line": 1},
        {"kind": "package", "target": "base", "line": 1},
    ]


def test_sys_source_not_extracted():
    """sys.source 动态载入不入图（文档化局限）；base::source 仍提取。"""
    src = 'sys.source("R/x.R")\nbase::source("R/y.R")\n'
    blanked = s7_enforce_r._blank_noncode(src)
    assert "R/x.R" not in blanked  # sys.source 串空白化（前缀守卫拒绝保留）
    rows = s7_enforce_r._iter_refs(blanked)
    assert rows == [
        {"kind": "source", "target": "R/y.R", "line": 2},
        {"kind": "package", "target": "base", "line": 2},
    ]


def test_iter_refs_matrix():
    src = (
        'source("R/a.R")\n'  # source 双引号
        "library(subpkg)\n"  # library 裸词
        'library("quoted")\n'  # library 引号
        "require(bare)\n"  # require 裸词
        'loadNamespace("loaded")\n'  # loadNamespace
        'requireNamespace(package = "rqns")\n'  # requireNamespace package= 命名
        "z <- stats::runif(1)\n"  # :: 引用
        "w <- logging:::internal()\n"  # ::: 引用
        "v <- a:b\n"  # 单冒号非引用
        "library()\n"  # 空参
        "n <- 1\n"
    )
    blanked = s7_enforce_r._blank_noncode(src)
    rows = s7_enforce_r._iter_refs(blanked)
    assert rows == [
        {"kind": "source", "target": "R/a.R", "line": 1},
        {"kind": "package", "target": "subpkg", "line": 2},
        {"kind": "package", "target": "quoted", "line": 3},
        {"kind": "package", "target": "bare", "line": 4},
        {"kind": "package", "target": "loaded", "line": 5},
        {"kind": "package", "target": "rqns", "line": 6},
        {"kind": "package", "target": "stats", "line": 7},
        {"kind": "package", "target": "logging", "line": 8},
    ]


def test_is_test_conventions():
    assert s7_enforce_r._is_test("tests/testthat/test-x.R")  # testthat 标准布局
    assert s7_enforce_r._is_test("testthat/helper.R")  # 独立 testthat/
    assert s7_enforce_r._is_test("tests/x.R")  # tests/ 目录
    assert s7_enforce_r._is_test("src/test-foo.R")  # test-*.R 连字符标准
    assert s7_enforce_r._is_test("src/test_foo.R")
    assert s7_enforce_r._is_test("src/foo_test.R")
    assert s7_enforce_r._is_test("src/foo_tests.R")
    assert s7_enforce_r._is_test("src/test.R")
    assert not s7_enforce_r._is_test("src/testing.R")  # testing 非测试
    assert not s7_enforce_r._is_test("Testing/x.R")  # 目录段精确小写
    assert not s7_enforce_r._is_test("src/latest.R")
    assert not s7_enforce_r._is_test("src/api.R")
    assert not s7_enforce_r._is_test("src/api.py")  # 非 .R


def test_abs_like_forms():
    assert s7_enforce_r._abs_like("/abs/x.R")
    assert s7_enforce_r._abs_like("C:/x.R")
    assert s7_enforce_r._abs_like("C:\\x.R")
    assert s7_enforce_r._abs_like("~/x.R")
    assert s7_enforce_r._abs_like("https://x/y.R")
    assert s7_enforce_r._abs_like("a\\b.R")  # 反斜杠路径不入图
    assert not s7_enforce_r._abs_like("R/api.R")
    assert not s7_enforce_r._abs_like("./api.R")
    assert not s7_enforce_r._abs_like("../x.R")


def test_owner_nearest_ancestor():
    package_dirs = {"pkg": "pkg", "pkg/sub": "sub"}
    assert s7_enforce_r._owner("pkg/sub/R/x.R", package_dirs) == "pkg/sub"  # 最近祖先
    assert s7_enforce_r._owner("pkg/R/y.R", package_dirs) == "pkg"
    assert s7_enforce_r._owner("loose.R", package_dirs) is None
    assert s7_enforce_r._owner("R/x.R", {".": "root"}) == "."  # 根包


def test_desc_imports_matrix():
    """DESCRIPTION Imports:/Depends: 解析：续行合并/版本规格剥离/R 跳过/
    Suggests 不产边/锚定字段起始行。"""
    text = (
        "Package: mypkg\n"  # 1
        "Version: 1.0\n"  # 2
        "Imports: dplyr (>= 1.0.0),\n"  # 3
        "    tidyr, R (>= 4.0)\n"  # 4 续行（DCF 缩进）
        "Suggests: testthat\n"  # 5 测试软依赖不产边
        "Depends: methods, utils\n"  # 6
        "LinkingTo: Rcpp\n"  # 7 编译链接不产边
    )
    rows = s7_enforce_r._desc_imports(text)
    assert rows == [
        {"kind": "package", "target": "dplyr", "line": 3},
        {"kind": "package", "target": "tidyr", "line": 3},
        {"kind": "package", "target": "methods", "line": 6},
        {"kind": "package", "target": "utils", "line": 6},
    ]


def test_desc_imports_empty_and_continuation_only():
    """Imports: 值全在续行（字段行空值）——字段行号仍为锚。"""
    text = "Package: p\nImports:\n    a,\n    b (>= 2.0)\n"
    rows = s7_enforce_r._desc_imports(text)
    assert rows == [
        {"kind": "package", "target": "a", "line": 2},
        {"kind": "package", "target": "b", "line": 2},
    ]


def test_scan_r_layout(tmp_path):
    repo = tmp_path / "proj"
    for d in ("R", "scripts", "tests/testthat", "renv", "packrat", ".git", "subpkg/R"):
        (repo / d).mkdir(parents=True)
    (repo / "subpkg" / "DESCRIPTION").write_text("Package: subpkg\n", encoding="utf-8")
    (repo / "subpkg" / "R" / "c.R").write_text("c <- 1\n", encoding="utf-8")
    (repo / "R" / "a.R").write_text('source("b.R")\nlibrary(subpkg)\n', encoding="utf-8")
    (repo / "R" / "b.R").write_text("b <- 1\n", encoding="utf-8")
    (repo / "scripts" / "run.R").write_text('source("R/a.R")\n', encoding="utf-8")
    (repo / "tests" / "testthat" / "test-a.R").write_text("library(subpkg)\n", encoding="utf-8")
    (repo / "renv" / "x.R").write_text("library(subpkg)\n", encoding="utf-8")
    (repo / "packrat" / "y.R").write_text("library(subpkg)\n", encoding="utf-8")
    (repo / ".git" / "z.R").write_text("library(subpkg)\n", encoding="utf-8")
    (repo / "notes.txt").write_text("library(txt)\n", encoding="utf-8")
    file_set, ref_rows, package_dirs, files_scanned, tests_excluded, timed_out = s7_enforce_r.scan_r(repo)
    assert timed_out is False
    assert files_scanned == 5  # renv/packrat/.git 跳过
    assert tests_excluded == 1  # tests/testthat/test-a.R
    assert file_set == {
        "R/a.R", "R/b.R", "scripts/run.R",
        "tests/testthat/test-a.R", "subpkg/R/c.R",
    }
    assert package_dirs == {"subpkg": "subpkg"}
    assert ref_rows == [  # 测试调用不入列
        {"file": "R/a.R", "kind": "source", "target": "b.R", "line": 1},
        {"file": "R/a.R", "kind": "package", "target": "subpkg", "line": 2},
        {"file": "scripts/run.R", "kind": "source", "target": "R/a.R", "line": 1},
    ]


def test_scan_r_timeout(tmp_path):
    repo = _mk_r_repo(tmp_path)
    _fs, _r, _pd, _files, _e, timed_out = s7_enforce_r.scan_r(repo, timeout=0)
    assert timed_out is True


def test_build_graph_package_and_source_edges():
    file_set = {"R/a.R", "R/b.R", "scripts/run.R", "subpkg/R/c.R", "tests/testthat/test-a.R"}
    ref_rows = [
        {"file": "R/a.R", "kind": "source", "target": "b.R", "line": 1},
        {"file": "R/a.R", "kind": "package", "target": "subpkg", "line": 2},
        {"file": "scripts/run.R", "kind": "source", "target": "R/a.R", "line": 1},
        {"file": "subpkg/R/c.R", "kind": "source", "target": "R/a.R", "line": 1},
        {"file": "R/a.R", "kind": "package", "target": "crangone", "line": 3},  # 外部跳过
    ]
    package_dirs = {"subpkg": "subpkg"}
    graph, edges = s7_enforce_r.build_r_graph(file_set, ref_rows, package_dirs)
    assert graph == {
        "R/a.R": ["R/b.R", "subpkg"],
        "R/b.R": [],
        "scripts/run.R": ["R/a.R"],
        "subpkg": ["R/a.R"],
        "tests/testthat/test-a.R": [],  # 测试容器仍入图（叶可见）
    }
    assert edges == [
        {"file": "R/a.R", "line": 1, "src": "R/a.R", "dst": "R/b.R"},
        {"file": "R/a.R", "line": 2, "src": "R/a.R", "dst": "subpkg"},
        {"file": "scripts/run.R", "line": 1, "src": "scripts/run.R", "dst": "R/a.R"},
        {"file": "subpkg/R/c.R", "line": 1, "src": "subpkg", "dst": "R/a.R"},
    ]


def test_build_graph_root_package_adopts_all():
    """根 DESCRIPTION = 整仓单包（Swift manifest 权威同哲学）——散文件收编。"""
    file_set = {"R/a.R", "scripts/run.R"}
    graph, edges = s7_enforce_r.build_r_graph(
        file_set,
        [{"file": "scripts/run.R", "kind": "source", "target": "R/a.R", "line": 1}],
        {".": "proj"},
    )
    assert graph == {".": []}  # 同包内 source = 自环跳过
    assert edges == []


def test_belongs_exact_match():
    assert s7_enforce_r._belongs("queue", ["queue"])
    assert not s7_enforce_r._belongs("queue2", ["queue"])  # 前缀污染守卫
    assert not s7_enforce_r._belongs("x", ["queue"])


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
    violations = s7_enforce_r._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "app" and v["imported"] == "iface"
    assert v["chain"] == ["app", "mid", "iface"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶容器（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"app": ["leaf"], "leaf": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["leaf"]}]
    violations = s7_enforce_r._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["app", "leaf"]


def test_find_violations_none_when_unreachable():
    graph = {"app": ["mid"], "mid": [], "iface": []}
    contracts = [{"name": "n", "source_modules": ["app"], "forbidden_modules": ["iface"]}]
    assert s7_enforce_r._find_violations(graph, contracts) == []


# ------------------------------------------------- derive / run_r E2E


def test_derive_r_containers(tmp_path):
    repo = _mk_r_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_r.derive_r_containers(repo, con, json.loads(R_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == R_CONTAINERS
    assert derived["r_files"] == 2  # queue/R/queue.R + logging/R/logging.R
    assert derived["packages"] == 2
    assert derived["loose_files"] == 0
    assert derived["extensions"] == []
    assert derived["timed_out"] is False


def test_run_r_violation_e2e(tmp_path):
    repo = _mk_r_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_r.run_r(repo, con, json.loads(R_STRUCTURE), "active", R_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "r-source-package-parser"
    assert out["contracts"] == 1
    assert out["files"] == 2
    assert out["modules"] == 2
    assert out["packages"] == 2
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
    assert row[3] == "f1"  # queue/R/queue.R 首符号
    assert row[4] == "queue/R/queue.R"
    assert row[5] == 1 and row[6] == 1  # 违例 library 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "app"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "queue"  # 容器 = DESCRIPTION 包目录
    assert payload["imported"] == "logging"
    assert payload["chain"] == ["queue", "logging"]
    assert payload["line"] == 1


def test_run_r_desc_imports_violation_e2e(tmp_path):
    """包间依赖仅声明于 DESCRIPTION Imports:（无使用侧 library/::）——
    声明即依赖执法；DESCRIPTION 无符号 → anchor 空、行号=字段行。"""
    repo = _mk_r_repo(tmp_path)
    # 移除使用侧调用——依赖仅存于 DESCRIPTION 声明
    (repo / "queue" / "R" / "queue.R").write_text(
        "run <- function() 1\n", encoding="utf-8"
    )
    (repo / "queue" / "DESCRIPTION").write_text(
        "Package: queue\nImports:\n    logging (>= 1.0)\n", encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_r.run_r(repo, con, json.loads(R_STRUCTURE), "active", R_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 0  # DESCRIPTION 无符号
    row = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT anchor_fid, file_path, start_line, payload_json FROM findings"
    ).fetchone()
    assert row[0] is None
    assert row[1] == "queue/DESCRIPTION"
    assert row[2] == 2  # Imports: 字段行（值在续行——锚字段起始行）
    payload = json.loads(row[3])
    assert payload["importer"] == "queue"
    assert payload["imported"] == "logging"
    assert payload["chain"] == ["queue", "logging"]
    assert payload["line"] == 2


def test_run_r_transitive_e2e(tmp_path):
    """传递链 core storage → queue → interface logging（包目录容器中间层）。"""
    repo = _mk_r_repo(tmp_path)
    (repo / "storage" / "R").mkdir(parents=True)
    (repo / "storage" / "DESCRIPTION").write_text("Package: storage\n", encoding="utf-8")
    (repo / "storage" / "R" / "storage.R").write_text("library(queue)\n", encoding="utf-8")
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
    containers = {"core-mod": ["storage", "queue"], "iface-mod": ["logging"]}
    con = _con(repo)
    try:
        out = s7_enforce_r.run_r(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queue→logging 直连 + storage 经 queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["queue"]["chain"] == ["queue", "logging"]
    assert by_importer["storage"]["chain"] == ["storage", "queue", "logging"]
    assert by_importer["storage"]["line"] == 1  # 锚定链尾直接边：queue/R/queue.R 第 1 行
    assert all(r[0] == "queue/R/queue.R" for r in rows)


def test_run_r_idempotent(tmp_path):
    repo = _mk_r_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_r.run_r(repo, con, json.loads(R_STRUCTURE), "active", R_CONTAINERS)
        second = s7_enforce_r.run_r(repo, con, json.loads(R_STRUCTURE), "active", R_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_r_compliant_clears_old_findings(tmp_path):
    repo = _mk_r_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例调用
    (repo / "queue" / "R" / "queue.R").write_text("run <- function() 1\n", encoding="utf-8")
    con = _con(repo)
    try:
        out = s7_enforce_r.run_r(repo, con, json.loads(R_STRUCTURE), "active", R_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_r_no_files(tmp_path):
    repo = _mk_r_repo(tmp_path)
    (repo / "queue" / "R" / "queue.R").unlink()
    (repo / "logging" / "R" / "logging.R").unlink()
    con = _con(repo)
    try:
        out = s7_enforce_r.run_r(repo, con, json.loads(R_STRUCTURE), "active", R_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no r files found" in out["reason"]


def test_run_r_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_r_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_r,
        "scan_r",
        lambda repo, timeout=600: (set(), [], {}, 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_r.run_r(repo, con, json.loads(R_STRUCTURE), "active", R_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "r scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_r_test_file_excluded_e2e(tmp_path):
    """测试文件（tests/testthat 约定）依赖不产边；排除计数诚实上报。"""
    repo = _mk_r_repo(tmp_path)
    (repo / "tests" / "testthat").mkdir(parents=True)
    (repo / "tests" / "testthat" / "test-queue.R").write_text(
        "library(logging)\nlibrary(queue)\n", encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_r.run_r(repo, con, json.loads(R_STRUCTURE), "active", R_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["tests_excluded"] == 1
    assert out["files"] == 3  # files_scanned 含测试文件
    assert out["modules"] == 3  # 生产 queue/logging + 测试散文件容器入图（叶可见）
    assert out["violations"] == 1  # 仅生产调用产违例


# ------------------------------------------------------ run() 路由


def test_run_routes_r(tmp_path):
    """纯 R 结构（.R）→ run() 路由至 R 引擎。"""
    repo = _mk_r_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "r-source-package-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_r_and_py_not_enforceable(tmp_path):
    repo = _mk_r_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'rrepo.helper.mod', 'Function', 'mod', 'helper/mod.py', 1, 2, 'h')"
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
    assert "py + r" in out["reason"]


def test_cli_enforce_r_wiring(tmp_path, capsys):
    repo = _mk_r_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["engine"] == "r-source-package-parser"
    assert out["violations"] == 1
