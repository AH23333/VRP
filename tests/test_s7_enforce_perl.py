"""S7 Perl 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/perl-enforce-probe）。

引擎无外部执法器（内置源级 package/use 解析），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_perl

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

PERL_CONTAINERS = {"queue-mod": ["Queue"], "logging-mod": ["Logging"]}

# 两层：app(Queue) ← interface(Logging)；违例 = Queue 上向 use Logging
PERL_STRUCTURE = json.dumps(
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


def _mk_perl_repo(tmp_path: Path, structure: str | None = PERL_STRUCTURE) -> Path:
    repo = tmp_path / "perlrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'perlrepo.queue.Queue', 'Package', 'Queue', 'queue/Queue.pm', 2, 4, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'perlrepo.logging.Logging', 'Package', 'Logging', 'logging/Logging.pm', 2, 3, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "queue").mkdir(parents=True)
    (repo / "logging").mkdir()
    # 违例：第 2 行 use Logging（use 语句行锚定目标）
    (repo / "queue" / "Queue.pm").write_text(
        "package Queue;\n"
        "use Logging;\n"
        "sub work { 1; }\n"
        "1;\n",
        encoding="utf-8",
    )
    (repo / "logging" / "Logging.pm").write_text(
        "package Logging;\n"
        "sub log_msg { 1; }\n"
        "1;\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode_canary_forms():
    src = (
        "# use Evil1;\n"  # 行注释 canary
        "=pod\n"
        "use Evil2;\n"  # POD canary
        "=cut\n"
        'my $s = "use Evil3;";\n'  # 双引号串 canary
        "my $t = 'use Evil4;';\n"  # 单引号串 canary
        "use Real1;\n"  # 真 use
        "my $keep = 1;\n"
    )
    blanked, spans = s7_enforce_perl._blank_noncode(src)
    assert "Real1" in blanked and "keep = 1" in blanked  # 代码存活
    assert "Evil" not in blanked  # canary 空白化
    assert len(blanked) == len(src)  # 等长空白
    assert blanked.count("\n") == src.count("\n")  # 换行保留
    rows = s7_enforce_perl._iter_uses(blanked, spans, src)
    assert rows == [{"kind": "pkg", "target": "Real1", "line": 7}]


def test_blank_sigil_hash_guard():
    """$#array 数组尾下标 sigil 非注释；其后的行尾注释仍生效。"""
    src = "my $n = $#arr + 1; # use Evil5;\nmy $keep = 2;\n"
    blanked, _ = s7_enforce_perl._blank_noncode(src)
    assert "#arr" in blanked  # sigil 存活
    assert "Evil5" not in blanked  # 行尾注释空白化
    assert "keep = 2" in blanked


def test_blank_q_operators():
    src = (
        "my @w = qw(use Evil6 keep);\n"
        "my $r = q(use Evil7);\n"
        "my $x = qq(use Evil8);\n"
        "my $y = qr/use Evil9/;\n"
        "my $z = m{use Evil10};\n"
        "my $a = s/use Evil11/clean/;\n"
        "my $b = tr/a-z/A-Z/;\n"
        "my $c = y/abc/x/;\n"
        "my @k = keys %h;\n"  # 标识符内 q 算符字母不误触
        "my $sq = 2;\n"
        "use RealQ;\n"
    )
    blanked, _ = s7_enforce_perl._blank_noncode(src)
    assert "Evil" not in blanked  # q 算符内容空白化
    assert "keys %h" in blanked  # keys 标识符存活
    assert "$sq = 2" in blanked  # sq 标识符存活
    assert "use RealQ;" in blanked


def test_blank_qop_closing_bracket_misfire():
    """闭合括号 `)]}>` 不作 q 定界符：`$h{s}`/`bar(s)` 裸字形态不空白化真实代码。"""
    src = (
        "package App;\n"
        "my %opt = (s => 1);\n"
        "if ($opt{s}) { require Foo::Bar; }\n"  # 修复前：s} 误触吞 require（假阴）
        "my %h = (q => 1);\n"
        "sub early { $h{q} }\n"  # 修复前：q} 误触两段扫描
        "my $r = bar(s);\n"  # 修复前：s) 误触扫至下个 )
        "my $m = my($y) = 1;\n"
        "1;\n"
    )
    blanked, spans = s7_enforce_perl._blank_noncode(src)
    assert "require Foo::Bar" in blanked  # 哈希键碰撞形态不再吞代码
    assert "package App" in blanked
    assert "$h{q}" in blanked
    assert "my($y)" in blanked
    rows = s7_enforce_perl._iter_uses(blanked, spans, src)
    assert rows == [{"kind": "pkg", "target": "Foo::Bar", "line": 3}]


def test_blank_heredoc_forms():
    src = (
        'my $a = <<"H1";\n'
        "use EvilH1;\n"
        "H1\n"
        "my $b = <<'H2';\n"
        "use EvilH2;\n"
        "H2\n"
        "my $c = <<H3;\n"
        "use EvilH3;\n"
        "H3\n"
        "my $d = <<~H4;\n"  # 缩进 heredoc
        "    use EvilH4;\n"
        "    H4\n"
        "use RealH;\n"
    )
    blanked, spans = s7_enforce_perl._blank_noncode(src)
    assert "Evil" not in blanked  # 四种 heredoc 体全空白化
    assert "use RealH;" in blanked
    assert blanked.count("\n") == src.count("\n")
    rows = s7_enforce_perl._iter_uses(blanked, spans, src)
    assert rows == [{"kind": "pkg", "target": "RealH", "line": 13}]


def test_blank_heredoc_unclosed_swallows_eof():
    src = 'my $h = <<"NOEND";\nuse Swallowed;\n'
    blanked, _ = s7_enforce_perl._blank_noncode(src)
    assert "Swallowed" not in blanked  # 未闭合 heredoc 吞到文件尾
    assert blanked.count("\n") == 2  # 换行保留


def test_blank_unclosed_pod_truncates_eof():
    src = "use KeepP;\n=pod\nuse EvilP;\n"
    blanked, _ = s7_enforce_perl._blank_noncode(src)
    assert "KeepP" in blanked
    assert "EvilP" not in blanked  # 未闭合 POD 截断文件尾
    assert blanked.count("\n") == src.count("\n")


def test_blank_unclosed_string_swallows_eof():
    """Perl 串合法跨行：未闭合串吞到文件尾（保守方向，多报不漏报）。"""
    src = 'my $s = "unclosed\nuse Swallow1;\nuse Swallow2;\n'
    blanked, _ = s7_enforce_perl._blank_noncode(src)
    assert "Swallow" not in blanked
    assert blanked.count("\n") == 3  # 换行保留


def test_blank_end_data_truncates():
    out1 = s7_enforce_perl._blank_noncode("use Keep1;\n__END__\nuse EvilE;\n")[0]
    assert "Keep1" in out1 and "EvilE" not in out1
    out2 = s7_enforce_perl._blank_noncode("use Keep2;\n__DATA__\nuse EvilD2;\n")[0]
    assert "Keep2" in out2 and "EvilD2" not in out2


def test_escaped_quote_closes():
    src = 'my $s = "a \\" b";\nuse After1;\n'
    blanked, spans = s7_enforce_perl._blank_noncode(src)
    rows = s7_enforce_perl._iter_uses(blanked, spans, src)
    assert rows == [{"kind": "pkg", "target": "After1", "line": 2}]


def test_iter_uses_matrix():
    src = (
        "use v5.36;\n"  # 版本守卫
        "require 5.010;\n"  # 版本守卫
        "use strict;\n"  # 裸词
        "use Foo::Bar;\n"
        'require "Util/Helper.pm";\n'  # 引号 .pm → path
        "use parent qw(Base1 Base2);\n"  # parent qw
        "use parent -norequire, 'Base3';\n"  # -norequire 跳过 + 引号目标
        "no Evil::Mod;\n"  # no 反导入不扫
        "require $mod;\n"  # 动态目标不入图
        "use Real::End;\n"
    )
    blanked, spans = s7_enforce_perl._blank_noncode(src)
    rows = s7_enforce_perl._iter_uses(blanked, spans, src)
    assert rows == [
        {"kind": "pkg", "target": "strict", "line": 3},
        {"kind": "pkg", "target": "Foo::Bar", "line": 4},
        {"kind": "path", "target": "Util/Helper.pm", "line": 5},
        {"kind": "pkg", "target": "Base1", "line": 6},
        {"kind": "pkg", "target": "Base2", "line": 6},
        {"kind": "pkg", "target": "Base3", "line": 7},
        {"kind": "pkg", "target": "Real::End", "line": 10},
    ]


def test_iter_uses_statement_positions():
    """语句边界（`{`/`=`/`;`/`(`）后 use/require 命中；守卫形态不误触。"""
    src = (
        "package App;\n"
        "if ($need) { require Foo::Bar; }\n"  # 条件块内 require
        "my $m = require Baz;\n"  # 赋值右侧 require
        "$x = 1; use Qux;\n"  # 分号后续 use
        "return unless (require Cond);\n"  # 圆括号内 require
        "$obj->require(1);\n"  # 方法调用不触发（-> 不在边界集）
        "reuse Foo;\n"  # 标识符前缀不触发（前字符字母）
        "my %h = (use => 1, require => 2);\n"  # 哈希键 => 后非裸词
        "require 5.010;\n"  # 版本守卫
        "require $mod;\n"  # 动态目标不入图
        "use Real;\n"  # 行首（既有路径）不变
    )
    blanked, spans = s7_enforce_perl._blank_noncode(src)
    rows = s7_enforce_perl._iter_uses(blanked, spans, src)
    assert rows == [
        {"kind": "pkg", "target": "Foo::Bar", "line": 2},
        {"kind": "pkg", "target": "Baz", "line": 3},
        {"kind": "pkg", "target": "Qux", "line": 4},
        {"kind": "pkg", "target": "Cond", "line": 5},
        {"kind": "pkg", "target": "Real", "line": 11},
    ]


def test_is_test_conventions():
    assert s7_enforce_perl._is_test("t/00-basic.t")  # t/ 目录约定
    assert s7_enforce_perl._is_test("xt/advanced.t")  # xt/ 扩展测试
    assert s7_enforce_perl._is_test("tests/a.pm")
    assert s7_enforce_perl._is_test("test/a.pm")
    assert s7_enforce_perl._is_test("t/sub/deep.pm")  # 嵌套
    assert s7_enforce_perl._is_test("root.t")  # TAP 后缀
    assert s7_enforce_perl._is_test("script/test.pl")  # 裸名
    assert s7_enforce_perl._is_test("script/test_x.pl")  # test_ 前缀
    assert s7_enforce_perl._is_test("script/x_test.pm")  # _test 后缀
    assert s7_enforce_perl._is_test("script/x_tests.pl")  # _tests 后缀
    assert not s7_enforce_perl._is_test("lib/Foo.pm")
    assert not s7_enforce_perl._is_test("script/foo.pl")
    assert not s7_enforce_perl._is_test("Testing/Foo.pm")  # 目录段精确匹配
    assert not s7_enforce_perl._is_test("tests_x/Foo.pm")
    assert not s7_enforce_perl._is_test("lib/Foo.t3")  # .t3 非 .t


def test_scan_perl_layout(tmp_path):
    repo = tmp_path / "proj"
    (repo / "lib").mkdir(parents=True)
    (repo / "t").mkdir()
    (repo / "blib").mkdir()
    (repo / "vendor").mkdir()
    (repo / ".git").mkdir()
    (repo / "lib" / "Foo.pm").write_text("package Foo;\nuse Bar;\n", encoding="utf-8")
    (repo / "lib" / "Bar.pm").write_text("package Bar;\n1;\n", encoding="utf-8")
    (repo / "t" / "00.t").write_text("use Foo;\n", encoding="utf-8")
    (repo / "blib" / "Junk.pm").write_text("package Junk;\nuse Foo;\n", encoding="utf-8")
    (repo / "vendor" / "V.pm").write_text("package V;\nuse Foo;\n", encoding="utf-8")
    (repo / ".git" / "G.pm").write_text("package G;\nuse Foo;\n", encoding="utf-8")
    (repo / "script.pl").write_text("my $x = 1;\n", encoding="utf-8")
    file_pkg, import_rows, files_scanned, tests_excluded, timed_out = s7_enforce_perl.scan_perl(repo)
    assert timed_out is False
    assert files_scanned == 4  # blib/vendor/.git 跳过
    assert tests_excluded == 1  # t/00.t
    assert file_pkg == {"lib/Foo.pm": "Foo", "lib/Bar.pm": "Bar"}  # 无包 script.pl 不入表
    assert [r["target"] for r in import_rows] == ["Bar"]  # 测试/产物 use 不入列


def test_scan_perl_timeout(tmp_path):
    repo = _mk_perl_repo(tmp_path)
    _f, _r, _files, _e, timed_out = s7_enforce_perl.scan_perl(repo, timeout=0)
    assert timed_out is True


def test_resolve_path():
    file_set = {"lib/Foo.pm", "lib/Bar.pm", "src/x.pl", "src/deep/D.pm"}
    assert s7_enforce_perl._resolve_path("Foo.pm", "lib/Baz.pm", file_set) == "lib/Foo.pm"  # lib/ 惯例首查
    assert s7_enforce_perl._resolve_path("Foo.pm", "", file_set) == "lib/Foo.pm"  # 仓根候选经 lib/ 命中
    assert s7_enforce_perl._resolve_path("deep/D.pm", "src/x.pl", file_set) == "src/deep/D.pm"  # 源目录兜底
    assert s7_enforce_perl._resolve_path("../lib/Foo.pm", "src/x.pl", file_set) == "lib/Foo.pm"  # .. 弹栈
    assert s7_enforce_perl._resolve_path("Missing.pm", "lib/Baz.pm", file_set) is None  # 外部
    assert s7_enforce_perl._resolve_path("/abs/x.pm", "src/x.pl", file_set) is None  # 绝对路径
    assert s7_enforce_perl._resolve_path("C:\\x.pm", "src/x.pl", file_set) is None  # 盘符


def test_build_perl_graph_self_external_leaf():
    file_pkg = {"lib/Self.pm": "Self", "lib/App.pm": "App", "lib/Leaf.pm": "Leaf"}
    import_rows = [
        {"file": "lib/Self.pm", "line": 2, "kind": "pkg", "target": "Self"},  # 自环跳过
        {"file": "lib/App.pm", "line": 2, "kind": "pkg", "target": "Missing"},  # 外部包跳过
        {"file": "lib/App.pm", "line": 3, "kind": "path", "target": "Leaf.pm"},  # 引号路径 → lib/ 候选命中
    ]
    graph, edges = s7_enforce_perl.build_perl_graph(file_pkg, import_rows)
    assert graph == {"App": ["Leaf"], "Leaf": [], "Self": []}  # 叶包全部入图
    assert edges == [{"file": "lib/App.pm", "line": 3, "src": "App", "dst": "Leaf"}]


def test_belongs_exact_match():
    assert s7_enforce_perl._belongs("Queue", ["Queue"])
    assert s7_enforce_perl._belongs("Probe::Store", ["Probe"])  # 父包捕获子包
    assert not s7_enforce_perl._belongs("Queue2", ["Queue"])  # 前缀污染守卫
    assert not s7_enforce_perl._belongs("x", ["Queue"])


def test_find_violations_bfs_shortest_chain():
    graph = {
        "App": ["Mid", "Queue"],
        "Mid": ["Iface"],  # 2 跳
        "Queue": ["Mid"],  # 3 跳路径
        "Iface": [],
        "Other": [],
    }
    contracts = [
        {
            "name": "layer: app must not import interface",
            "source_layer": "app",
            "forbidden_layer": "interface",
            "source_modules": ["App"],
            "forbidden_modules": ["Iface"],
        }
    ]
    violations = s7_enforce_perl._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "App" and v["imported"] == "Iface"
    assert v["chain"] == ["App", "Mid", "Iface"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶包（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"App": ["Leaf"], "Leaf": []}
    contracts = [{"name": "n", "source_modules": ["App"], "forbidden_modules": ["Leaf"]}]
    violations = s7_enforce_perl._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["App", "Leaf"]


def test_find_violations_none_when_unreachable():
    graph = {"App": ["Mid"], "Mid": [], "Iface": []}
    contracts = [{"name": "n", "source_modules": ["App"], "forbidden_modules": ["Iface"]}]
    assert s7_enforce_perl._find_violations(graph, contracts) == []


# ------------------------------------------------- derive / run_perl E2E


def test_derive_perl_containers(tmp_path):
    repo = _mk_perl_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_perl.derive_perl_containers(repo, con, json.loads(PERL_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == PERL_CONTAINERS
    assert derived["perl_files"] == 2  # queue/Queue.pm + logging/Logging.pm
    assert derived["extensions"] == []
    assert derived["orphan_files"] == 0
    assert derived["timed_out"] is False


def test_run_perl_violation_e2e(tmp_path):
    repo = _mk_perl_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_perl.run_perl(repo, con, json.loads(PERL_STRUCTURE), "active", PERL_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "perl-package-parser"
    assert out["contracts"] == 1
    assert out["files"] == 2
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
    assert row[3] == "f1"  # queue/Queue.pm 首符号
    assert row[4] == "queue/Queue.pm"
    assert row[5] == 2 and row[6] == 2  # 违例 use 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "app"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "Queue"  # 容器 = package 声明
    assert payload["imported"] == "Logging"
    assert payload["chain"] == ["Queue", "Logging"]
    assert payload["line"] == 2


def test_run_perl_transitive_e2e(tmp_path):
    """传递链 core Storage → Queue → interface Logging（包容器中间层）。"""
    repo = _mk_perl_repo(tmp_path)
    (repo / "storage").mkdir()
    (repo / "storage" / "Storage.pm").write_text(
        "package Storage;\nuse Queue;\n1;\n", encoding="utf-8"
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
    containers = {"core-mod": ["Storage", "Queue"], "iface-mod": ["Logging"]}
    con = _con(repo)
    try:
        out = s7_enforce_perl.run_perl(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # Queue→Logging 直连 + Storage 经 Queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["Queue"]["chain"] == ["Queue", "Logging"]
    assert by_importer["Storage"]["chain"] == ["Storage", "Queue", "Logging"]
    assert by_importer["Storage"]["line"] == 2  # 锚定链尾直接 use 边：Queue.pm 第 2 行
    assert all(r[0] == "queue/Queue.pm" for r in rows)


def test_run_perl_idempotent(tmp_path):
    repo = _mk_perl_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_perl.run_perl(repo, con, json.loads(PERL_STRUCTURE), "active", PERL_CONTAINERS)
        second = s7_enforce_perl.run_perl(repo, con, json.loads(PERL_STRUCTURE), "active", PERL_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_perl_compliant_clears_old_findings(tmp_path):
    repo = _mk_perl_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 use
    (repo / "queue" / "Queue.pm").write_text(
        "package Queue;\nsub work { 1; }\n1;\n", encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_perl.run_perl(repo, con, json.loads(PERL_STRUCTURE), "active", PERL_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_perl_no_packages(tmp_path):
    repo = _mk_perl_repo(tmp_path)
    (repo / "queue" / "Queue.pm").write_text("my $x = 1;\n", encoding="utf-8")
    (repo / "logging" / "Logging.pm").write_text("my $y = 2;\n", encoding="utf-8")
    con = _con(repo)
    try:
        out = s7_enforce_perl.run_perl(repo, con, json.loads(PERL_STRUCTURE), "active", PERL_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no perl packages found" in out["reason"]


def test_run_perl_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_perl_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_perl,
        "scan_perl",
        lambda repo, timeout=600: ({}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_perl.run_perl(repo, con, json.loads(PERL_STRUCTURE), "active", PERL_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "perl scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_perl_test_file_excluded_e2e(tmp_path):
    """测试文件（t/ 目录 TAP 约定）use 不产边；排除计数诚实上报。"""
    repo = _mk_perl_repo(tmp_path)
    (repo / "t").mkdir()
    (repo / "t" / "00-queue.t").write_text(
        "use Queue;\nuse Logging;\n", encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_perl.run_perl(repo, con, json.loads(PERL_STRUCTURE), "active", PERL_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["tests_excluded"] == 1
    assert out["files"] == 3  # files_scanned 含测试文件
    assert out["packages"] == 2  # 仅生产包入图（.t 无包）
    assert out["violations"] == 1  # 仅生产 use 产违例


def test_run_perl_conditional_require_e2e(tmp_path):
    """条件 require（if 块内+哈希键碰撞 canary）产边入图——修复前整段被 s} 误触空白化。"""
    repo = _mk_perl_repo(tmp_path)
    # 直连 use 换成条件 require：$opt{s} 哈希键形态 + 块内语句位置双重命中
    (repo / "queue" / "Queue.pm").write_text(
        "package Queue;\n"
        "my %opt = (s => 1);\n"
        "if ($opt{s}) { require Logging; }\n"
        "sub work { 1; }\n"
        "1;\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_perl.run_perl(repo, con, json.loads(PERL_STRUCTURE), "active", PERL_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    row = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, start_line, payload_json FROM findings"
    ).fetchone()
    assert row[0] == "queue/Queue.pm"
    assert row[1] == 3  # 条件 require 行
    assert json.loads(row[2])["line"] == 3
    assert json.loads(row[2])["chain"] == ["Queue", "Logging"]


# ------------------------------------------------------ run() 路由


def test_run_routes_perl(tmp_path):
    """纯 Perl 结构（.pm）→ run() 路由至 perl 引擎。"""
    repo = _mk_perl_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "perl-package-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_perl_and_py_not_enforceable(tmp_path):
    repo = _mk_perl_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'perlrepo.helper.mod', 'Function', 'h', 'helper/mod.py', 1, 2, 'h')"
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
    assert "py + perl" in out["reason"]


def test_cli_enforce_perl_wiring(tmp_path, capsys):
    repo = _mk_perl_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["engine"] == "perl-package-parser"
    assert out["violations"] == 1
