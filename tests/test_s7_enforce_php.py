"""S7 PHP 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/php-enforce-probe）。

引擎无外部执法器（内置源级 namespace/use 解析器，岛模型词法），quick 层直接
跑真解析——mock 只用于超时分支。
"""

import json
import shutil
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_php

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

PHP_CONTAINERS = {"core-mod": ["Probe\\Core"], "iface-mod": ["Probe\\Iface"]}

# 两层：core(Probe\Core) ← interface(Probe\Iface)；违例 = Core 上向 use Iface
PHP_STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    },
    ensure_ascii=False,
)


def _mk_php_repo(tmp_path: Path, structure: str | None = PHP_STRUCTURE) -> Path:
    repo = tmp_path / "prepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'Probe.Core.Store', 'Class', 'Store', 'src/Store.php', 7, 11, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'Probe.Iface.Api', 'Interface', 'Api', 'src/Api.php', 3, 6, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "Api.php").write_text(
        "<?php\n\nnamespace Probe\\Iface;\n\ninterface Api\n{\n    public function get(): string;\n}\n",
        encoding="utf-8",
    )
    # 违例：第 5 行 use Probe\Iface\Api（use 行号锚定目标）
    (repo / "src" / "Store.php").write_text(
        "<?php\n\n"
        "namespace Probe\\Core;\n\n"
        "use Probe\\Iface\\Api;\n\n"
        "final class Store\n"
        "{\n"
        "    public function __construct(private Api $api) {}\n"
        "}\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ 词法 unit


def test_blank_noncode_islands_html():
    src = (
        "<html>use Fake\\A;</html>\n"
        "<?php\n"
        "namespace Probe\\Core;\n"
        "?>\n"
        "<body>use Fake\\B;</body>\n"
        "<?php\n"
        "$x = 1;\n"
        "?>\n"
        "tail html\n"
    )
    out = s7_enforce_php._blank_noncode(src)
    assert "Fake" not in out  # 岛外 HTML 内 use 不可见
    assert "<html>" not in out and "tail html" not in out
    assert "namespace Probe\\Core;" in out  # 岛内代码保留
    assert "$x = 1;" in out
    assert out.count("\n") == src.count("\n")  # 等长空白化保行号


def test_blank_noncode_comments_and_attribute():
    src = (
        "<?php\n"
        "// use One\\Two;\n"
        "# use Three\\Four;\n"
        "#[Attribute(use Five\\Six)]\n"
        "#[Route('/x', ['a' => 1])]\n"
        "/* use Seven\\Eight; /* nested */ still code */\n"
        "$keep = 1; // tail use Nine\\Ten\n"
        "use Real\\Target;\n"
    )
    out = s7_enforce_php._blank_noncode(src)
    assert "One\\Two" not in out and "Three\\Four" not in out  # 行注释（// 与 #）
    assert "Five\\Six" not in out and "Route" not in out  # #[ attribute 非注释：深度扫描整体空白化
    assert "Seven\\Eight" not in out and "nested" not in out
    assert "still code" in out  # PHP 块注释不可嵌套：首个 */ 闭合，其后是代码
    assert "Nine\\Ten" not in out  # 行尾注释
    assert "$keep = 1;" in out
    assert "use Real\\Target;" in out


def test_blank_noncode_string_quote_semantics():
    src = (
        "<?php\n"
        "$a = 'it\\'s';\n"
        "$b = 'C:\\temp';\n"
        "$c = \"a\\\"b\";\n"
        "$d = \"v = {$arr['k']}\";\n"
        "$e = `ls -la`;\n"
        "$g = \"{$arr[\"k\"]}\";\n"
        "use Real\\Keep;\n"
    )
    out = s7_enforce_php._blank_noncode(src)
    assert "it" not in out and "temp" not in out  # 单引号串空白化（\' 转义；\t 字面不吞闭合引号）
    assert "\"b\"" not in out  # 双引号 \" 转义：一整串
    assert "arr" not in out  # 插值内单引号键随串空白化
    assert "ls -la" not in out  # 反引号 shell-exec
    assert "arr" not in out  # 插值内嵌双引号（文档化局限）：提前截断，残余扫描不残留
    assert "use Real\\Keep;" in out
    assert out.count("\n") == src.count("\n")


def test_blank_noncode_heredoc_nowdoc():
    src = (
        "<?php\n"
        "$a = <<<EOT\n"
        "use Heredoc\\Fake;\n"
        "EOT;\n"
        "$b = <<<'NOW'\n"
        "$raw = 'quote'\n"
        "NOW . 'tail';\n"
        "$c = <<<IND\n"
        "body\n"
        "    IND;\n"
        "use Real\\Keep;\n"
    )
    out = s7_enforce_php._blank_noncode(src)
    assert "Heredoc" not in out  # heredoc 体空白化（体内容 use 不可见）
    assert "$raw" not in out  # nowdoc 体空白化
    assert "tail" not in out  # 闭合行残余字符串按代码空白化
    assert "body" not in out  # PHP 7.3+ 缩进闭合容错
    assert "use Real\\Keep;" in out
    assert out.count("\n") == src.count("\n")


# ------------------------------------------------------------------ 解析 unit


def test_parse_namespace_forms():
    def ns(src: str) -> str | None:
        return s7_enforce_php._parse_namespace(s7_enforce_php._blank_noncode(src))

    assert ns("<?php\nnamespace Probe\\Core;\nclass A {}\n") == "Probe\\Core"
    assert ns("<?php\nnamespace Probe\\Braced {\n}\n") == "Probe\\Braced"
    assert ns("<?php\nnamespace App;\n") == "App"  # 单段命名空间（合法 PHP）
    assert ns("<?php\nnamespace {\n}\n") is None  # 全局命名空间块不入图
    assert ns("<?php\n$namespace = 1;\n") is None  # 变量名非声明
    assert ns("<?php\n// namespace Fake\\One;\n") is None  # 注释内不解析
    assert ns("<?php\nclass NoNs {}\n") is None


def test_iter_uses_all_forms():
    src = (
        "<?php\n"
        "namespace Probe\\Core;\n"
        "use Probe\\Iface\\Api;\n"                # 3
        "use Probe\\Iface\\Api as ApiI;\n"        # 4 别名
        "use function Probe\\Util\\helper;\n"     # 5 function 导入
        "use const Probe\\Util\\MAGIC;\n"         # 6 const 导入
        "use Probe\\A\\One, Probe\\B\\Two;\n"     # 7 逗号多目标
        "use Probe\\Grp\\{Alpha, Beta as G};\n"   # 8 分组
        "use \\Probe\\Abs\\Thing;\n"              # 9 前导反斜杠（绝对 FQN）
        "use Exception;\n"                        # 10 顶层名：无命名空间语义
        "use Probe\\Iface\\Sub\\Deep;\n"          # 11
    )
    rows = s7_enforce_php._iter_uses(s7_enforce_php._blank_noncode(src))
    assert rows == [
        ("Probe\\Iface", 3),
        ("Probe\\Iface", 4),  # as 别名剥壳取左侧
        ("Probe\\Util", 5),  # function kind 剥除
        ("Probe\\Util", 6),  # const kind 剥除
        ("Probe\\A", 7),
        ("Probe\\B", 7),  # 逗号多目标各成边
        ("Probe\\Grp", 8),  # 分组展开
        ("Probe\\Grp", 8),
        ("Probe\\Abs", 9),  # 前导 \ 剥除
        # Exception：顶层名 None 不出现
        ("Probe\\Iface\\Sub", 11),
    ]


def test_iter_uses_closure_capture_rejected():
    src = (
        "<?php\n"
        "namespace Probe\\Core;\n"
        "$f = function () use ($db, $log) {\n"
        "    return $db;\n"
        "};\n"
        "use($h);\n"  # 无空格形式：正则不匹配
        "class Store {\n    use BarTrait;\n}\n"  # trait use：顶层 trait 名无命名空间语义
    )
    rows = s7_enforce_php._iter_uses(s7_enforce_php._blank_noncode(src))
    assert rows == []  # 闭包捕获/trait 顶层名/紧凑括号均不产边


def test_iter_uses_namespaced_trait_use_is_dependency():
    src = (
        "<?php\nnamespace Probe\\Core;\n"
        "class Store {\n    use Probe\\Traits\\BarTrait;\n}\n"
    )
    rows = s7_enforce_php._iter_uses(s7_enforce_php._blank_noncode(src))
    assert rows == [("Probe\\Traits", 4)]  # 命名空间 trait use 是真实依赖（deptrac 同口径）


def test_target_ns_cases():
    assert s7_enforce_php._target_ns("Probe\\Api") == "Probe"  # use 导入的是命名项：去尾段
    assert s7_enforce_php._target_ns("Probe\\Api as A") == "Probe"  # 别名剥壳
    assert s7_enforce_php._target_ns("\\Full\\Qualified\\Name") == "Full\\Qualified"  # 前导 \ 剥除
    assert s7_enforce_php._target_ns("Foo\\Bar::CONST") == "Foo"  # :: 成员剥除
    assert s7_enforce_php._target_ns("Exception") is None  # 顶层名无命名空间语义
    assert s7_enforce_php._target_ns("A\\\\B") is None  # 空段防御


def test_is_test_conventions():
    assert s7_enforce_php._is_test("tests/StoreTest.php")  # tests 目录段
    assert s7_enforce_php._is_test("src/test/StoreTest.php")  # test 目录段
    assert s7_enforce_php._is_test("src/StoreTest.php")  # *Test.php（PHPUnit 约定）
    assert s7_enforce_php._is_test("src/Test.php")  # Test.php 本名
    assert not s7_enforce_php._is_test("src/greatest.php")  # 小写 t：非测试（大小写敏感）
    assert not s7_enforce_php._is_test("src/Storage.php")
    assert not s7_enforce_php._is_test("src/testing/Store.php")  # testing 目录段不在约定内
    assert not s7_enforce_php._is_test("src/STORE.PHP")  # 大写后缀仍匹配 .php，但 stem 大小写敏感


# ------------------------------------------------------------------ 扫描/图 unit


def test_scan_php_skip_dirs_tests_and_orphans(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "vendor").mkdir()
    (repo / "src" / "Api.php").write_text(
        "<?php\nnamespace Probe\\Iface;\n", encoding="utf-8"
    )
    (repo / "src" / "Store.php").write_text(
        "<?php\nnamespace Probe\\Core;\nuse Probe\\Iface\\Api;\n", encoding="utf-8"
    )
    (repo / "src" / "global.php").write_text(
        "<?php\nclass NoNs {}\n", encoding="utf-8"  # 无 namespace 声明：orphan
    )
    (repo / "src" / "page.php").write_text(
        "<html>plain</html>\n", encoding="utf-8"  # 纯 HTML：orphan
    )
    (repo / "tests" / "StoreTest.php").write_text(
        "<?php\nnamespace Probe\\Core;\nuse Probe\\Iface\\Api;\n", encoding="utf-8"
    )
    (repo / "vendor" / "pkg.php").write_text(
        "<?php\nnamespace Vendor\\Pkg;\nuse Probe\\Iface\\Api;\n", encoding="utf-8"
    )
    file_ns, use_rows, files, tests_excluded, timed_out = s7_enforce_php.scan_php(repo)
    assert timed_out is False
    assert file_ns == {
        "src/Api.php": "Probe\\Iface",
        "src/Store.php": "Probe\\Core",
        "tests/StoreTest.php": "Probe\\Core",  # 测试文件命名空间仍入表（叶可见）
    }  # orphan（global/page）不入表；vendor 整目录跳过
    assert use_rows == [{"file": "src/Store.php", "line": 3, "target": "Probe\\Iface"}]
    assert tests_excluded == 1
    assert files == 5  # 扫描计数诚实（vendor 除外）


def test_scan_php_timeout(tmp_path):
    repo = _mk_php_repo(tmp_path)
    file_ns, use_rows, files, tests, timed_out = s7_enforce_php.scan_php(repo, timeout=0)
    assert timed_out is True


def test_build_php_graph_self_external_leaf():
    file_ns = {
        "a/A.php": "Probe\\A",
        "b/B.php": "Probe\\B",
        "leaf/L.php": "Probe\\Leaf",
    }
    use_rows = [
        {"file": "a/A.php", "line": 1, "target": "Probe\\A\\Same"},  # 同命名空间：无自环
        {"file": "a/A.php", "line": 2, "target": "Probe\\B\\Klass"},
        {"file": "a/A.php", "line": 3, "target": "Symfony\\Component\\Yaml"},  # 外部依赖：无命中跳过
        {"file": "missing/M.php", "line": 1, "target": "Probe\\B\\Klass"},  # orphan 文件：跳过
    ]
    graph, edges = s7_enforce_php.build_php_graph(file_ns, use_rows)
    assert graph == {"Probe\\A": ["Probe\\B"], "Probe\\B": [], "Probe\\Leaf": []}  # 叶命名空间入图
    assert edges == [{"file": "a/A.php", "line": 2, "src": "Probe\\A", "dst": "Probe\\B"}]


def test_resolve_ns_longest_prefix():
    ns = {"Probe", "Probe\\Api", "Probe\\Store", "Other"}
    assert s7_enforce_php._resolve_ns("Probe\\Api", ns) == "Probe\\Api"
    assert s7_enforce_php._resolve_ns("Probe\\Api\\Handler", ns) == "Probe\\Api"
    assert s7_enforce_php._resolve_ns("Probe\\Util\\Log", ns) == "Probe"  # 未知子空间回退父级
    assert s7_enforce_php._resolve_ns("Other", ns) == "Other"
    assert s7_enforce_php._resolve_ns("Symfony\\Yaml", ns) is None  # 外部依赖
    assert s7_enforce_php._resolve_ns("Probex\\Y", ns) is None  # 前缀污染不匹配


def test_belongs_parent_ns():
    assert s7_enforce_php._belongs("Probe\\Store", ["Probe"])  # 子空间归父容器
    assert s7_enforce_php._belongs("Probe", ["Probe"])
    assert not s7_enforce_php._belongs("Probex", ["Probe"])  # 前缀污染
    assert not s7_enforce_php._belongs("Other", ["Probe"])
    assert not s7_enforce_php._belongs("Probe", ["Probe\\Store"])  # 反向不成立


def test_find_violations_bfs_shortest_chain():
    graph = {"a.x": ["b.x", "mid.x"], "mid.x": ["c.x"], "b.x": ["c.x"], "c.x": [], "w.x": []}
    contracts = [
        {
            "name": "layer: core must not import interface",
            "source_layer": "core",
            "forbidden_layer": "interface",
            "source_modules": ["a.x"],
            "forbidden_modules": ["c.x"],
        }
    ]
    violations = s7_enforce_php._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "a.x" and v["imported"] == "c.x"
    assert v["chain"] == ["a.x", "b.x", "c.x"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶命名空间（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"a.x": ["leaf.x"], "leaf.x": []}
    contracts = [{"name": "n", "source_modules": ["a.x"], "forbidden_modules": ["leaf.x"]}]
    violations = s7_enforce_php._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["a.x", "leaf.x"]


def test_find_violations_none_when_unreachable():
    graph = {"a.x": ["b.x"], "b.x": [], "c.x": []}
    contracts = [{"name": "n", "source_modules": ["a.x"], "forbidden_modules": ["c.x"]}]
    assert s7_enforce_php._find_violations(graph, contracts) == []


# ------------------------------------------------------------------ derive


def test_derive_php_containers(tmp_path):
    repo = _mk_php_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_php.derive_php_containers(repo, con, json.loads(PHP_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == PHP_CONTAINERS
    assert derived["php_files"] == 2
    assert derived["orphan_files"] == 0
    assert derived["timed_out"] is False
    assert derived["extensions"] == []


def test_derive_php_containers_orphans_counted(tmp_path):
    repo = _mk_php_repo(tmp_path)
    (repo / "src" / "global.php").write_text(
        "<?php\n$GLOBALS['x'] = 1;\n", encoding="utf-8"  # 无 namespace：orphan
    )
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'global_fn', 'Function', 'g', 'src/global.php', 2, 2, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps({
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1", "f3"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    }, ensure_ascii=False),))
    con.commit()
    try:
        derived = s7_enforce_php.derive_php_containers(
            repo, con, json.loads(con.execute("SELECT payload_json FROM structure").fetchone()[0])
        )
    finally:
        con.close()
    assert derived["containers"] == PHP_CONTAINERS  # orphan 不进容器
    assert derived["php_files"] == 3
    assert derived["orphan_files"] == 1  # 无 namespace 声明诚实计数


# ------------------------------------------------- run_php / run() E2E


def test_run_php_violation_e2e(tmp_path):
    repo = _mk_php_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_php.run_php(repo, con, json.loads(PHP_STRUCTURE), "active", PHP_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "php-namespace-parser"
    assert out["contracts"] == 1
    assert out["files"] == 2
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
    assert row[3] == "f1"  # Store.php 首符号
    assert row[4] == "src/Store.php"
    assert row[5] == 5 and row[6] == 5  # 违例 use 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "Probe\\Core"
    assert payload["imported"] == "Probe\\Iface"
    assert payload["chain"] == ["Probe\\Core", "Probe\\Iface"]
    assert payload["line"] == 5


def test_run_php_transitive_e2e(tmp_path):
    """Core → App → Iface：core 层内传递链触达 interface（九引擎传递闭包同义）。"""
    repo = _mk_php_repo(tmp_path)
    (repo / "src" / "Queue.php").write_text(
        "<?php\n\n"
        "namespace Probe\\App;\n\n"
        "use Probe\\Iface\\Api;\n\n"
        "final class Queue\n"
        "{\n"
        "    public function __construct(private Api $api) {}\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "src" / "Store.php").write_text(
        "<?php\n\n"
        "namespace Probe\\Core;\n\n"
        "use Probe\\App\\Queue;\n\n"
        "final class Store\n"
        "{\n"
        "    public function __construct(private Queue $queue) {}\n"
        "}\n",
        encoding="utf-8",
    )
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
                {"module_id": "app-mod", "name": "队列", "responsibility": "队", "symbol_fids": [], "layer": "core"},
                {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
            ],
            "layers": [
                {"name": "core", "may_depend_on": []},
                {"name": "interface", "may_depend_on": ["core"]},
            ],
        },
        ensure_ascii=False,
    )
    payload = json.loads(structure)
    containers = {"core-mod": ["Probe\\Core"], "app-mod": ["Probe\\App"], "iface-mod": ["Probe\\Iface"]}
    con = _con(repo)
    try:
        out = s7_enforce_php.run_php(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # App→Iface 直连 + Core 经 App 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["Probe\\App"]["chain"] == ["Probe\\App", "Probe\\Iface"]
    assert by_importer["Probe\\Core"]["chain"] == ["Probe\\Core", "Probe\\App", "Probe\\Iface"]
    assert by_importer["Probe\\Core"]["line"] == 5  # 锚定链尾直接 use 边：Queue.php 第 5 行
    assert all(r[0] == "src/Queue.php" for r in rows)


def test_run_php_idempotent(tmp_path):
    repo = _mk_php_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_php.run_php(repo, con, json.loads(PHP_STRUCTURE), "active", PHP_CONTAINERS)
        second = s7_enforce_php.run_php(repo, con, json.loads(PHP_STRUCTURE), "active", PHP_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_php_compliant_clears_old_findings(tmp_path):
    repo = _mk_php_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 use
    (repo / "src" / "Store.php").write_text(
        "<?php\n\nnamespace Probe\\Core;\n\nfinal class Store\n{\n    public function get(): string { return 's'; }\n}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_php.run_php(repo, con, json.loads(PHP_STRUCTURE), "active", PHP_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_php_no_namespaces(tmp_path):
    repo = _mk_php_repo(tmp_path)
    shutil.rmtree(repo / "src")
    con = _con(repo)
    try:
        out = s7_enforce_php.run_php(repo, con, json.loads(PHP_STRUCTURE), "active", PHP_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no php namespaces" in out["reason"]


def test_run_php_single_layer_not_enforceable(tmp_path):
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
                {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "core"},
            ],
            "layers": [
                {"name": "core", "may_depend_on": []},
            ],
        },
        ensure_ascii=False,
    )
    repo = _mk_php_repo(tmp_path, structure=structure)
    con = _con(repo)
    try:
        out = s7_enforce_php.run_php(
            repo, con, json.loads(structure), "active", PHP_CONTAINERS
        )
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no derivable contracts" in out["reason"]


def test_run_php_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_php_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_php,
        "scan_php",
        lambda repo, timeout=600: ({}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_php.run_php(
            repo, con, json.loads(PHP_STRUCTURE), "active", PHP_CONTAINERS, timeout=9
        )
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "php scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_php_test_file_excluded_e2e(tmp_path):
    """*Test.php 测试文件的 use 不产违例（PHPUnit 约定排除）。"""
    repo = _mk_php_repo(tmp_path)
    (repo / "tests").mkdir()
    # 测试里 use Iface（同生产违例方向）——若泄漏会双计违例
    (repo / "tests" / "StoreTest.php").write_text(
        "<?php\n\nnamespace Probe\\Core;\n\nuse Probe\\Iface\\Api;\n\nfinal class StoreTest {}\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_php.run_php(repo, con, json.loads(PHP_STRUCTURE), "active", PHP_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 仅 Store.php 的生产 use
    assert out["tests_excluded"] == 1
    assert out["files"] == 3  # 测试文件扫描计数诚实


# ------------------------------------------------------ run() 路由


def test_run_routes_php(tmp_path):
    """纯 PHP 结构 → run() 路由至 php 引擎并落 P9。"""
    repo = _mk_php_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "php-namespace-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_routes_php_orphan_surfaced(tmp_path):
    repo = _mk_php_repo(tmp_path)
    (repo / "src" / "global.php").write_text(
        "<?php\n$x = 1;\n", encoding="utf-8"  # 无 namespace：orphan
    )
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'global_fn', 'Function', 'g', 'src/global.php', 2, 2, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps({
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1", "f3"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    }, ensure_ascii=False),))
    con.commit()
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1
    assert out["orphan_php_files"] == 1  # 无 namespace 声明诚实上报


def test_run_mixed_php_and_py_not_enforceable(tmp_path):
    repo = _mk_php_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'pyhelper.h', 'Function', 'h', 'pyhelper/mod.py', 1, 2, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps({
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
            {"module_id": "py-mod", "name": "杂", "responsibility": "混", "symbol_fids": ["f3"], "layer": "core"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    }, ensure_ascii=False),))
    con.commit()
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "mixed-language" in out["reason"]
    assert "py + php" in out["reason"]


def test_run_php_derive_timeout(tmp_path, monkeypatch):
    repo = _mk_php_repo(tmp_path)
    monkeypatch.setattr(
        s7_enforce_php,
        "scan_php",
        lambda repo, timeout=600: ({}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "php scan exceeded" in out["reason"]


def test_cli_enforce_php_wiring(tmp_path, capsys):
    repo = _mk_php_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["engine"] == "php-namespace-parser"
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
