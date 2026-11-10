"""S7 Ruby 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/ruby-enforce-probe）。

引擎无外部执法器（内置源级 module/require 解析器），quick 层直接跑真解析——
mock 只用于超时分支。
"""

import json
import shutil
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_ruby

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

RUBY_CONTAINERS = {"core-mod": ["Probe::Storage"], "iface-mod": ["Probe::Api"]}

# 两层：core(Probe::Storage) ← interface(Probe::Api)；违例 = Storage 上向 require Api
RUBY_STRUCTURE = json.dumps(
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


def _mk_ruby_repo(tmp_path: Path, structure: str | None = RUBY_STRUCTURE) -> Path:
    repo = tmp_path / "rbrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'Probe::Storage', 'Module', 'Storage', 'src/store/storage.rb', 3, 9, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'Probe::Api', 'Module', 'Api', 'src/api/api.rb', 1, 7, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "src" / "store").mkdir(parents=True)
    (repo / "src" / "api").mkdir(parents=True)
    (repo / "src" / "api" / "api.rb").write_text(
        "module Probe\n"
        "  module Api\n"
        "    def self.handle(value)\n"
        "      value\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    # 违例：第 1 行 require_relative api（require 行号锚定目标）
    (repo / "src" / "store" / "storage.rb").write_text(
        'require_relative "../api/api"\n\n'
        "module Probe\n"
        "  module Storage\n"
        "    def self.put(value)\n"
        "      Probe::Api.handle(value)\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode():
    src = (
        "x = 1 # line comment\n"
        "=begin\n"
        "module Dead\n"
        'require_relative "nope"\n'
        "=end\n"
        'require_relative "api"\n'
        'require "json"\n'
        "s = 'sq #{NOT_TOKEN} str'\n"
        'd = "dq #{INTERP_TOKEN} str"\n'
        "w = %w[alpha beta]\n"
        "q = %q(outer (INNER_TOKEN) tail)\n"
        "h = <<~HEREDOC\n"
        "  module InHeredoc\n"
        '  require_relative "fake"\n'
        "HEREDOC\n"
        "r = /re#gex \\d/\n"
        "div = a / b\n"
        "mod = a % b\n"
    )
    out = s7_enforce_ruby._blank_noncode(src)
    lines = out.split("\n")
    assert lines[0].startswith("x = 1")  # 行注释空白化保行号
    assert "Dead" not in out and "nope" not in out  # =begin/=end 块注释整段空白化
    assert 'require_relative "api"' in out  # require 目标区整体保留
    assert 'require "json"' in out
    assert "NOT_TOKEN" not in out  # 单引号串整体空白化（插值记号不穿透）
    assert "INTERP_TOKEN" not in out  # 双引号插值串整体空白化
    assert "alpha" not in out  # %w 字面量空白化
    assert "INNER_TOKEN" not in out  # %q 嵌套括号定界整体空白化
    assert "InHeredoc" not in out and "fake" not in out  # heredoc 体空白化
    assert "re#gex" not in out  # 正则字面量空白化
    assert "div = a / b" in out  # 除法不误判
    assert "mod = a % b" in out  # 模运算不误判
    assert len(out) == len(src)  # 等长空白


def test_heredoc_squiggly_and_dash_terminators():
    src = (
        "a = <<-TEXT\n"
        "  module DashBody\n"
        "TEXT\n"
        "b = <<~TEXT\n"
        "    module SquigglyBody\n"
        "  TEXT\n"
        "after = 1\n"
    )
    out = s7_enforce_ruby._blank_noncode(src)
    assert "DashBody" not in out  # <<- 前导空白终止行生效
    assert "SquigglyBody" not in out  # <<~ 缩进终止行生效
    assert "after = 1" in out  # heredoc 闭合后代码保留


def test_heredoc_left_shift_not_literal():
    src = (
        "x = 1 << 2\n"
        "y = flags << FLAG\n"
        "z = value\n"
    )
    out = s7_enforce_ruby._blank_noncode(src)
    assert "x = 1 << 2" in out  # 位移运算不进 heredoc 分支
    assert "y = flags << FLAG" in out
    assert "z = value" in out


def test_regex_vs_division_position_heuristic():
    src = (
        "a = /strict # regex/\n"
        "b = list.map { |x| x / 2 }\n"
        "c = arr[0] / 3\n"
        "d = value if flag\n"
    )
    out = s7_enforce_ruby._blank_noncode(src)
    assert "strict" not in out  # = 后正则空白化
    assert "map { |x| x / 2 }" in out  # |x| 后斜杠是除法
    assert "arr[0] / 3" in out  # ] 后斜杠是除法
    assert "d = value if flag" in out  # 行中 modifier if 不吞代码


def test_parse_containers_nested_and_compact():
    blanked = s7_enforce_ruby._blank_noncode(
        "module Probe\n"
        "  module Api\n"
        "    def self.version\n"
        "      1\n"
        "    end\n"
        "  end\n"
        "end\n"
        "module Probe::Storage\n"
        "  def self.put; end\n"
        "end\n"
    )
    assert s7_enforce_ruby._parse_containers(blanked) == ["Probe::Api", "Probe::Storage"]


def test_parse_containers_semicolon_chain_one_line():
    blanked = s7_enforce_ruby._blank_noncode(
        "module A\n"
        "  module B; end\n"
        "end\n"
        "class C; end\n"
    )
    assert s7_enforce_ruby._parse_containers(blanked) == ["A::B", "C"]


def test_parse_containers_root_not_collapsed():
    blanked = s7_enforce_ruby._blank_noncode(
        "module Probe\n"
        "  module Api\n"
        "    def self.x; end\n"
        "  end\n"
        "end\n"
    )
    assert s7_enforce_ruby._parse_containers(blanked) == ["Probe::Api"]  # 共享根 Probe 被更深声明覆盖


def test_parse_containers_class_superclass_ignored():
    blanked = s7_enforce_ruby._blank_noncode(
        "class Foo < Bar\n"
        "  def x; end\n"
        "end\n"
    )
    assert s7_enforce_ruby._parse_containers(blanked) == ["Foo"]  # 超类 Bar 不是容器


def test_parse_containers_pseudo_modules_lexer_guarded():
    blanked = s7_enforce_ruby._blank_noncode(
        "=begin\n"
        "module Dead\n"
        "=end\n"
        "s = 'module Str'\n"
        "h = <<~TEXT\n"
        "module InHeredoc\n"
        "TEXT\n"
        "module Live\n"
        "  def x; end\n"
        "end\n"
    )
    assert s7_enforce_ruby._parse_containers(blanked) == ["Live"]


def test_iter_requires_interpolation_skipped():
    blanked = s7_enforce_ruby._blank_noncode(
        'require_relative "../lib/#{name}"\n'
        'require_relative "../lib/api"\n'
        'require "json"\n'
        'require File.join("lib", "x")\n'
    )
    rows = s7_enforce_ruby._iter_requires(blanked)
    assert rows == [
        {"kind": "require_relative", "arg": "../lib/api", "line": 2},
        {"kind": "require", "arg": "json", "line": 3},
    ]  # 插值目标（动态 require）与表达式实参不入列


def test_iter_requires_pseudo_forms_lexer_guarded():
    blanked = s7_enforce_ruby._blank_noncode(
        's = "require_relative \\"inner\\""\n'
        "# require 'commented'\n"
        'x = \'require "sq"\'\n'
    )
    assert s7_enforce_ruby._iter_requires(blanked) == []  # 伪 require（注释/字符串）不出现


def test_iter_requires_member_access_rejected():
    """obj.require(...) 是成员调用非声明边——不入列（review 批修正回归）。"""
    blanked = s7_enforce_ruby._blank_noncode(
        "util.require('util.rb')\n"
        "x = obj . require('other.rb')\n"
        "require 'real.rb'\n"
    )
    rows = s7_enforce_ruby._iter_requires(blanked)
    assert rows == [{"kind": "require", "arg": "real.rb", "line": 3}]


def test_iter_requires_multiline_paren_form():
    """括号实参可跨行（合法 Ruby）；裸跨行 = 两条语句非实参（Ruby 语义）。"""
    blanked = s7_enforce_ruby._blank_noncode(
        "require(\n"
        "  'foo.rb'\n"
        ")\n"
        "require_relative(\n"
        "  'bar.rb'\n"
        ")\n"
        "require\n"
        "'two_stmt.rb'\n"
    )
    rows = s7_enforce_ruby._iter_requires(blanked)
    assert rows == [
        {"kind": "require", "arg": "foo.rb", "line": 1},
        {"kind": "require_relative", "arg": "bar.rb", "line": 4},
    ]  # 裸 require\n'x' 是两条语句——首行完整即终止，不入列


def test_blank_end_marker_data_section():
    """__END__ 行首独立行：其后 DATA 数据节非代码（伪 require/module 不泄漏）。"""
    src = (
        "require 'genuine.rb'\n"
        "module Real\n"
        "  def self.x; end\n"
        "end\n"
        "__END__\n"
        "require 'fake.rb'\n"
        "module Fake\n"
    )
    out = s7_enforce_ruby._blank_noncode(src)
    assert "genuine.rb" in out  # __END__ 前代码保留
    assert "fake" not in out and "Fake" not in out  # 数据节整段空白化
    assert len(out) == len(src)  # 等长空白
    # 非独立行 __END__（行中标识符）不触发
    mid = "x = flag__END__ing\nmodule Live\n  def y; end\nend\n"
    out2 = s7_enforce_ruby._blank_noncode(mid)
    assert "flag__END__ing" in out2
    assert s7_enforce_ruby._parse_containers(out2) == ["Live"]


def test_parse_containers_endless_def_no_drift():
    """Ruby 3 endless def（无 end）不开启块——后续 module 嵌套不错挂。"""
    blanked = s7_enforce_ruby._blank_noncode(
        "module A\n"
        "  def helper = 42\n"
        "end\n"
        "module B\n"
        "  def m() = 1\n"
        "end\n"
        "module C\n"
        "  def sq(x) = x * x\n"
        "end\n"
        "module D\n"
        "  def foo=(v)\n"
        "    @v = v\n"
        "  end\n"
        "end\n"
    )
    assert s7_enforce_ruby._parse_containers(blanked) == ["A", "B", "C", "D"]
    # 修复前：endless def 开块永不闭合 → A::B::C::D 错误嵌套


def test_parse_containers_endless_def_cross_module():
    """endless def 后续跨 module 声明漂移（修复前 B 挂 A 下）。"""
    blanked = s7_enforce_ruby._blank_noncode(
        "module A\n"
        "  def helper = 42\n"
        "end\n"
        "module B\n"
        "  module C\n"
        "    def self.x; end\n"
        "  end\n"
        "end\n"
    )
    # B 被 B::C 极大元覆盖（共享根坍缩噪声防护）——嵌套归属正确
    assert s7_enforce_ruby._parse_containers(blanked) == ["A", "B::C"]


def test_blank_regex_tilde_and_keyword_tail():
    """=~ 与行首条件关键字尾是正则最高频位置——体内文本不泄漏。"""
    src = (
        "x = y =~ /module Fake/\n"
        "if /require 'fake2.rb'/\n"
        "  z = 1\n"
        "end\n"
        "module Real\n"
        "end\n"
        "a = if x / 2 > 1 then 0 else 1 end\n"
    )
    out = s7_enforce_ruby._blank_noncode(src)
    assert "Fake" not in out and "fake2" not in out  # 正则体空白化
    assert s7_enforce_ruby._parse_containers(out) == ["Real"]
    assert "if x / 2 > 1" in out  # 关键字尾后除法仍安全（if 后有操作数非直接斜杠）


def test_is_test_conventions():
    assert s7_enforce_ruby._is_test("test/store_test.rb") is True
    assert s7_enforce_ruby._is_test("tests/helper.rb") is True
    assert s7_enforce_ruby._is_test("spec/models/user_spec.rb") is True
    assert s7_enforce_ruby._is_test("src/store_test.rb") is True
    assert s7_enforce_ruby._is_test("src/user_spec.rb") is True
    assert s7_enforce_ruby._is_test("src/store.rb") is False
    assert s7_enforce_ruby._is_test("src/testing.rb") is False  # 目录段精确匹配


def test_scan_ruby_tests_and_skip_dirs(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "store.rb").write_text(
        'require_relative "api"\n\n'
        "module Probe\n"
        "  module Storage\n"
        "    def self.put(v)\n"
        "      Probe::Api.handle(v)\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    (repo / "src" / "api.rb").write_text(
        "module Probe\n"
        "  module Api\n"
        "    def self.handle(v)\n"
        "      v\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    # 测试文件：容器仍入表（叶可见），require 不入图
    (repo / "test").mkdir()
    (repo / "test" / "store_test.rb").write_text(
        'require_relative "../src/store"\n\n'
        "module Probe\n"
        "  module Storage\n"
        "    def self.spec(v)\n"
        "      v\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    # vendor 产物：整体跳过
    (repo / "vendor").mkdir()
    (repo / "vendor" / "v.rb").write_text(
        "module Vendor::Thing\n  def self.x; end\nend\n", encoding="utf-8"
    )
    file_containers, require_rows, files, tests_excluded, timed_out = s7_enforce_ruby.scan_ruby(repo)
    assert timed_out is False
    assert file_containers == {
        "src/store.rb": ["Probe::Storage"],
        "src/api.rb": ["Probe::Api"],
        "test/store_test.rb": ["Probe::Storage"],
    }  # vendor 跳过；测试文件容器仍入表
    assert tests_excluded == 1
    assert files == 3
    assert require_rows == [
        {"file": "src/store.rb", "line": 1, "kind": "require_relative", "arg": "api"}
    ]  # 测试 require 不入列
    graph, edges = s7_enforce_ruby.build_ruby_graph(file_containers, require_rows)
    assert graph == {"Probe::Api": [], "Probe::Storage": ["Probe::Api"]}
    assert edges == [
        {"file": "src/store.rb", "line": 1, "src": "Probe::Storage", "dst": "Probe::Api"}
    ]


def test_scan_ruby_no_module_orphan(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "run.rb").write_text(
        'puts "hi"\n', encoding="utf-8"  # 无 module/class 声明（脚本文件）
    )
    file_containers, require_rows, files, tests_excluded, timed_out = s7_enforce_ruby.scan_ruby(repo)
    assert timed_out is False
    assert file_containers == {}
    assert require_rows == []
    assert files == 1  # 扫描计数仍诚实


def test_scan_ruby_bom_line1_constructs(tmp_path):
    """行 1 构造 + 头部 UTF-8 BOM：ruby parser_prepare 跳过 BOM（合法源码），
    =begin/__END__/嵌套 opener 不得失明（修复前：块注释漏检→假边、
    DATA 节污染图、嵌套容器裂为平级）。"""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "b1.rb").write_text(
        '\ufeff=begin\nrequire "ghost"\n=end\nmodule B1\nend\n', encoding="utf-8"
    )
    (repo / "b2.rb").write_text(
        "\ufeffmodule A\nmodule B\nend\nend\n", encoding="utf-8"
    )
    (repo / "b3.rb").write_text(
        '\ufeff__END__\nmodule Ghost\nend\nrequire "ghost"\n', encoding="utf-8"
    )
    file_containers, require_rows, files, tests_excluded, timed_out = s7_enforce_ruby.scan_ruby(repo)
    assert timed_out is False
    assert files == 3
    assert file_containers == {"b1.rb": ["B1"], "b2.rb": ["A::B"]}  # b3 DATA 节不污染
    assert require_rows == []  # 块注释/DATA 内 require 不入图


def test_scan_ruby_timeout(tmp_path):
    repo = _mk_ruby_repo(tmp_path)
    file_containers, require_rows, files, tests, timed_out = s7_enforce_ruby.scan_ruby(repo, timeout=0)
    assert timed_out is True


def test_resolve_require_relative():
    file_set = {"src/store/api.rb", "src/store/x.rb", "src/api/api.rb"}
    row = {"kind": "require_relative", "arg": "../api/api"}
    assert s7_enforce_ruby._resolve_require("src/store/storage.rb", row, file_set) == "src/api/api.rb"
    row = {"kind": "require", "arg": "./api"}  # ./ 前缀按当前文件目录
    assert s7_enforce_ruby._resolve_require("src/store/x.rb", row, file_set) == "src/store/api.rb"
    row = {"kind": "require_relative", "arg": "api.rb"}  # 自带 .rb 后缀原样候选
    assert s7_enforce_ruby._resolve_require("src/store/x.rb", row, file_set) == "src/store/api.rb"
    row = {"kind": "require_relative", "arg": "../../outside"}  # 逃逸仓外拒绝
    assert s7_enforce_ruby._resolve_require("src/store/x.rb", row, file_set) is None
    row = {"kind": "require", "arg": "json"}  # 外部 stdlib/gem
    assert s7_enforce_ruby._resolve_require("src/store/x.rb", row, file_set) is None


def test_resolve_require_load_path():
    file_set = {
        "lib/probe/api.rb",
        "src/probe/queue.rb",
        "app/probe/svc.rb",
        "probe/root.rb",
    }
    row = {"kind": "require", "arg": "probe/api"}
    assert s7_enforce_ruby._resolve_require("x/y.rb", row, file_set) == "lib/probe/api.rb"
    row = {"kind": "require", "arg": "probe/queue"}
    assert s7_enforce_ruby._resolve_require("x/y.rb", row, file_set) == "src/probe/queue.rb"
    row = {"kind": "require", "arg": "probe/svc"}
    assert s7_enforce_ruby._resolve_require("x/y.rb", row, file_set) == "app/probe/svc.rb"
    row = {"kind": "require", "arg": "probe/root"}
    assert s7_enforce_ruby._resolve_require("x/y.rb", row, file_set) == "probe/root.rb"  # 仓根候选


def test_build_ruby_graph_self_and_external_skipped():
    file_containers = {"a/a.rb": ["A"], "b/b.rb": ["B"]}
    require_rows = [
        {"file": "a/a.rb", "line": 1, "kind": "require_relative", "arg": "a"},  # 同容器：无自环
        {"file": "a/a.rb", "line": 2, "kind": "require_relative", "arg": "../b/b"},  # A→B
        {"file": "a/a.rb", "line": 3, "kind": "require", "arg": "json"},  # 外部 gem：跳过
        {"file": "a/a.rb", "line": 4, "kind": "require_relative", "arg": "../missing"},  # 未命中：跳过
    ]
    graph, edges = s7_enforce_ruby.build_ruby_graph(file_containers, require_rows)
    assert graph == {"A": ["B"], "B": []}  # 叶容器入图
    assert edges == [{"file": "a/a.rb", "line": 2, "src": "A", "dst": "B"}]


def test_belongs_parent_container():
    assert s7_enforce_ruby._belongs("Probe::Storage", ["Probe"])
    assert s7_enforce_ruby._belongs("Probe", ["Probe"])
    assert not s7_enforce_ruby._belongs("ProbeX", ["Probe"])  # 前缀污染
    assert not s7_enforce_ruby._belongs("Other::Probe", ["Probe"])


def test_find_violations_bfs_shortest_chain():
    graph = {
        "A::S": ["B::M", "A::Q"],
        "B::M": ["I::F"],  # 2 跳
        "A::Q": ["B::M"],  # 3 跳路径
        "I::F": [],
        "W::X": [],
    }
    contracts = [
        {
            "name": "layer: core must not import interface",
            "source_layer": "core",
            "forbidden_layer": "interface",
            "source_modules": ["A::S"],
            "forbidden_modules": ["I::F"],
        }
    ]
    violations = s7_enforce_ruby._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "A::S" and v["imported"] == "I::F"
    assert v["chain"] == ["A::S", "B::M", "I::F"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶容器（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"A::X": ["L::F"], "L::F": []}
    contracts = [{"name": "n", "source_modules": ["A::X"], "forbidden_modules": ["L::F"]}]
    violations = s7_enforce_ruby._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["A::X", "L::F"]


def test_find_violations_none_when_unreachable():
    graph = {"A::X": ["B::X"], "B::X": [], "C::X": []}
    contracts = [{"name": "n", "source_modules": ["A::X"], "forbidden_modules": ["C::X"]}]
    assert s7_enforce_ruby._find_violations(graph, contracts) == []


# ------------------------------------------------- derive / run_ruby E2E


def test_derive_ruby_containers(tmp_path):
    repo = _mk_ruby_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_ruby.derive_ruby_containers(repo, con, json.loads(RUBY_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == RUBY_CONTAINERS
    assert derived["ruby_files"] == 2
    assert derived["orphan_files"] == 0
    assert derived["timed_out"] is False
    assert derived["extensions"] == []


def test_derive_ruby_containers_orphans_counted(tmp_path):
    repo = _mk_ruby_repo(tmp_path)
    (repo / "src" / "loose.rb").write_text(
        'puts "loose"\n', encoding="utf-8"  # 无 module/class 声明：orphan
    )
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'loose', 'Function', 'loose', 'src/loose.rb', 1, 1, 'h')"
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
        derived = s7_enforce_ruby.derive_ruby_containers(
            repo, con, json.loads(con.execute("SELECT payload_json FROM structure").fetchone()[0])
        )
    finally:
        con.close()
    assert derived["containers"] == RUBY_CONTAINERS  # orphan 不进容器
    assert derived["ruby_files"] == 3
    assert derived["orphan_files"] == 1  # 无声明诚实计数


def test_run_ruby_violation_e2e(tmp_path):
    repo = _mk_ruby_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_ruby.run_ruby(repo, con, json.loads(RUBY_STRUCTURE), "active", RUBY_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "ruby-module-parser"
    assert out["contracts"] == 1
    assert out["files"] == 2
    assert out["modules"] == 2
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
    assert row[3] == "f1"  # storage.rb 首符号
    assert row[4] == "src/store/storage.rb"
    assert row[5] == 1 and row[6] == 1  # 违例 require_relative 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "Probe::Storage"
    assert payload["imported"] == "Probe::Api"
    assert payload["chain"] == ["Probe::Storage", "Probe::Api"]
    assert payload["line"] == 1


def test_run_ruby_bom_line1_module_e2e(tmp_path):
    """行 1 module + BOM E2E：修复前 BOM 吞行 1 opener → 嵌套容器裂为
    平级（Probe/Api）→ Probe::Api 叶失明 → 违例链断裂 0 违例。"""
    repo = _mk_ruby_repo(tmp_path)
    api = repo / "src" / "api" / "api.rb"
    api.write_text("\ufeff" + api.read_text(encoding="utf-8"), encoding="utf-8")
    con = _con(repo)
    try:
        out = s7_enforce_ruby.run_ruby(repo, con, json.loads(RUBY_STRUCTURE), "active", RUBY_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 修复前 0（BOM 吞行 1 opener 致 Probe::Api 叶失明）
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_ruby_transitive_e2e(tmp_path):
    """Storage → Queue → Api：core 层内传递链触达 interface（forbidden 传递闭包同义）。"""
    repo = _mk_ruby_repo(tmp_path)
    (repo / "src" / "queue").mkdir()
    (repo / "src" / "queue" / "queue.rb").write_text(
        'require_relative "../api/api"\n\n'
        "module Probe\n"
        "  module Queue\n"
        "    def self.work(v)\n"
        "      Probe::Api.handle(v)\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    (repo / "src" / "store" / "storage.rb").write_text(
        'require_relative "../queue/queue"\n\n'
        "module Probe\n"
        "  module Storage\n"
        "    def self.put(v)\n"
        "      Probe::Queue.work(v)\n"
        "    end\n"
        "  end\n"
        "end\n",
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
    con = _con(repo)
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    payload = json.loads(structure)
    containers = {"core-mod": ["Probe::Storage", "Probe::Queue"], "iface-mod": ["Probe::Api"]}
    try:
        out = s7_enforce_ruby.run_ruby(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queue→api 直连 + storage 经 queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["Probe::Queue"]["chain"] == ["Probe::Queue", "Probe::Api"]
    assert by_importer["Probe::Storage"]["chain"] == ["Probe::Storage", "Probe::Queue", "Probe::Api"]
    assert by_importer["Probe::Storage"]["line"] == 1  # 锚定链尾直接 require 边：queue.rb 第 1 行
    assert all(r[0] == "src/queue/queue.rb" for r in rows)


def test_run_ruby_idempotent(tmp_path):
    repo = _mk_ruby_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_ruby.run_ruby(repo, con, json.loads(RUBY_STRUCTURE), "active", RUBY_CONTAINERS)
        second = s7_enforce_ruby.run_ruby(repo, con, json.loads(RUBY_STRUCTURE), "active", RUBY_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_ruby_compliant_clears_old_findings(tmp_path):
    repo = _mk_ruby_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 require
    (repo / "src" / "store" / "storage.rb").write_text(
        "module Probe\n"
        "  module Storage\n"
        "    def self.put(v)\n"
        "      v\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_ruby.run_ruby(repo, con, json.loads(RUBY_STRUCTURE), "active", RUBY_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_ruby_no_modules(tmp_path):
    repo = _mk_ruby_repo(tmp_path)
    shutil.rmtree(repo / "src")
    con = _con(repo)
    try:
        out = s7_enforce_ruby.run_ruby(repo, con, json.loads(RUBY_STRUCTURE), "active", RUBY_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no ruby modules" in out["reason"]


def test_run_ruby_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_ruby_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_ruby,
        "scan_ruby",
        lambda repo, timeout=600: ({}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce_ruby.run_ruby(repo, con, json.loads(RUBY_STRUCTURE), "active", RUBY_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "ruby scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_ruby_test_root_excluded_e2e(tmp_path):
    """test/ 目录与 *_test.rb 命名约定的 require 不产违例（minitest/rspec 对齐）。"""
    repo = _mk_ruby_repo(tmp_path)
    test = repo / "test"
    test.mkdir()
    # 测试里 require api（同生产违例方向）——若泄漏会掺入测试 require 边
    (test / "store_test.rb").write_text(
        'require_relative "../src/api/api"\n\n'
        "module Probe\n"
        "  module Storage\n"
        "    def self.spec(v)\n"
        "      Probe::Api.handle(v)\n"
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_ruby.run_ruby(repo, con, json.loads(RUBY_STRUCTURE), "active", RUBY_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 仅 storage.rb 的生产 require
    assert out["tests_excluded"] == 1
    assert out["files"] == 3  # 测试文件扫描计数诚实


# ------------------------------------------------------ run() 路由


def test_run_routes_ruby(tmp_path):
    """纯 Ruby 结构 → run() 路由至 ruby 引擎并落 P9。"""
    repo = _mk_ruby_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "ruby-module-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_routes_ruby_orphan_surfaced(tmp_path):
    repo = _mk_ruby_repo(tmp_path)
    (repo / "src" / "loose.rb").write_text(
        'puts "loose"\n', encoding="utf-8"  # 无 module/class 声明：orphan
    )
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'loose', 'Function', 'loose', 'src/loose.rb', 1, 1, 'h')"
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
    assert out["orphan_ruby_files"] == 1  # 无声明诚实上报


def test_run_mixed_ruby_and_py_not_enforceable(tmp_path):
    repo = _mk_ruby_repo(tmp_path)
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
    assert "py + ruby" in out["reason"]


def test_run_ruby_derive_timeout(tmp_path, monkeypatch):
    repo = _mk_ruby_repo(tmp_path)
    monkeypatch.setattr(
        s7_enforce_ruby,
        "scan_ruby",
        lambda repo, timeout=600: ({}, [], 0, 0, True),
    )
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "ruby scan exceeded" in out["reason"]


def test_cli_enforce_ruby_wiring(tmp_path, capsys):
    repo = _mk_ruby_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
