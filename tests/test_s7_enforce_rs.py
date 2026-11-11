"""S7 Rust 执法引擎单元 + 内置解析器 E2E（真仓 dogfood 见 work/rs-enforce-probe）。

引擎无外部执法器（内置源级解析器），quick 层直接跑真解析——mock 只用于超时分支。
"""

import json
import shutil
import sqlite3
import time
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_rs

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

RS_CONTAINERS = {"core-mod": ["store"], "iface-mod": ["api"]}

# 两层：core(store) ← interface(api)；违例 = store 上向 use crate::api
RS_STRUCTURE = json.dumps(
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


def _mk_rs_repo(tmp_path: Path, structure: str | None = RS_STRUCTURE) -> Path:
    repo = tmp_path / "rsrepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'demo::store::get', 'Function', 'get', 'src/store.rs', 3, 4, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'demo::api::handle', 'Function', 'handle', 'src/api.rs', 1, 1, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text("mod api;\nmod store;\n", encoding="utf-8")
    (repo / "src" / "api.rs").write_text("pub fn handle() {}\n", encoding="utf-8")
    # 违例：第 1 行 use crate::api（use 行号锚定目标）
    (repo / "src" / "store.rs").write_text(
        "use crate::api::handle;\n\npub fn get() { handle(); }\n", encoding="utf-8"
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_blank_noncode():
    src = 'let a = 1; // line\nlet b = "x/*y*/z"; /* block /* nested */ still */ let c = 3;\n'
    out = s7_enforce_rs._blank_noncode(src)
    assert out.splitlines()[0].startswith("let a = 1;")  # 行注释空白化保行号
    assert '"x/*y*/z"' not in out  # 字符串整体空白化
    assert "nested" not in out and "still" not in out  # 嵌套块注释
    assert "let c = 3;" in out  # 注释后代码保留
    raw = s7_enforce_rs._blank_noncode("let r = r#\"raw \"q\" /* no */\"#; let s = 2;\n")
    assert "raw" not in raw and "let s = 2;" in raw  # 原始字符串
    life = s7_enforce_rs._blank_noncode("fn f<'a>(x: &'a str) -> char { '\\n' }")
    assert "'a" in life and "'\\n'" not in life  # 寿命标注保留、字符字面量空白化


def test_iter_uses_expansion():
    text = s7_enforce_rs._blank_noncode(
        "use crate::api::{client::{get, post}, handler};\n"
        "use crate::api::*;\n"
        "use crate::logging as log;\n"
        "use crate::{api, logging};\n"
        "use crate::api::client as c;\n"
        "use crate::api::{\n    client,\n};\n"
        "use mycrate::foo;\n"  # extern：跳过
        "use crate;\n"  # 裸 crate：跳过
    )
    paths = [p for _pos, ps in s7_enforce_rs._iter_uses(text) for p in ps]
    assert ["crate", "api", "client", "get"] in paths  # 嵌套组展开
    assert ["crate", "api", "client", "post"] in paths
    assert ["crate", "api", "handler"] in paths
    assert ["crate", "api"] in paths  # 路径后 glob use crate::api::*
    assert ["crate", "logging"] in paths  # as 重命名
    assert ["crate", "api", "client"] in paths  # 多行组
    assert not any(p[0] == "mycrate" for p in paths)  # extern crate 不属层执法
    assert not any(p == ["crate"] for p in paths)  # 裸 use crate; 无意义跳过


def test_absolutize_variants():
    # 绝对段表示：crate 根=[]；crate:: 前缀脱落；super 链弹栈越根=None
    assert s7_enforce_rs._absolutize(["crate", "api"], []) == ["api"]
    assert s7_enforce_rs._absolutize(["crate", "api", "client"], ["store", "db"]) == ["api", "client"]
    assert s7_enforce_rs._absolutize(["self", "foo"], []) == ["foo"]
    assert s7_enforce_rs._absolutize(["self", "foo"], ["store", "db"]) == ["store", "db", "foo"]
    assert s7_enforce_rs._absolutize(["super", "foo"], ["store", "db"]) == ["store", "foo"]
    assert s7_enforce_rs._absolutize(["super", "foo"], ["store"]) == ["foo"]  # 顶层 super → crate 根
    assert s7_enforce_rs._absolutize(["super", "super", "foo"], ["store", "db"]) == ["foo"]
    assert s7_enforce_rs._absolutize(["super", "foo"], []) is None  # 根文件 super 越界
    assert s7_enforce_rs._absolutize(["super", "super", "x"], ["store"]) is None
    assert s7_enforce_rs._absolutize(["crate"], []) == ["crate"]  # 根 glob
    assert s7_enforce_rs._absolutize(["self"], ["store", "db"]) == ["store", "db"]
    assert s7_enforce_rs._absolutize(["super"], ["store", "db"]) == ["store"]
    assert s7_enforce_rs._absolutize([], ["store"]) is None


def test_resolve_target_longest_prefix():
    mods = {"crate", "api", "api::client", "store", "store::db"}
    assert s7_enforce_rs._resolve_target(["api", "client"], mods) == "api::client"
    assert s7_enforce_rs._resolve_target(["api", "client", "get"], mods) == "api::client"  # 非模块条目
    assert s7_enforce_rs._resolve_target(["api", "handler"], mods) == "api"
    assert s7_enforce_rs._resolve_target(["CONST"], mods) == "crate"  # 根条目 → crate


def test_is_test_cfg_variants():
    assert s7_enforce_rs._is_test_cfg("cfg(test)")
    assert s7_enforce_rs._is_test_cfg("cfg( test )")
    assert s7_enforce_rs._is_test_cfg("cfg(all(test, unix))")
    assert s7_enforce_rs._is_test_cfg("cfg(all(unix, test))")
    assert not s7_enforce_rs._is_test_cfg("cfg(not(test))")  # 非测试门控
    assert not s7_enforce_rs._is_test_cfg("cfg(all(not(test), unix))")
    assert not s7_enforce_rs._is_test_cfg("cfg(unix)")
    assert not s7_enforce_rs._is_test_cfg("cfg_attr(test, derive(Debug))")


def test_blank_test_mods_file_and_inline():
    text = s7_enforce_rs._blank_noncode(
        "#[cfg(test)]\nmod tests {\n    use super::*;\n}\n"
        "#[cfg(test)] mod fixture;\n"
        "#[cfg(not(test))] mod prod;\n"
        "mod real;\n"
    )
    blanked = s7_enforce_rs._blank_test_mods(s7_enforce_rs._blank_inline_mods(text))
    decls = s7_enforce_rs._iter_mod_decls(blanked)
    assert decls == [("prod", False), ("real", False)]  # cfg(test) 声明整体不可见
    assert "use super" not in blanked


def test_build_mod_tree_2018_and_modrs_styles(tmp_path):
    repo = tmp_path / "crate"
    (repo / "src" / "api").mkdir(parents=True)
    (repo / "src" / "store").mkdir(parents=True)
    (repo / "src" / "lib.rs").write_text("mod api;\nmod store;\n", encoding="utf-8")
    (repo / "src" / "api" / "mod.rs").write_text("pub mod client;\n", encoding="utf-8")  # mod.rs 风格
    (repo / "src" / "api" / "client.rs").write_text("", encoding="utf-8")
    (repo / "src" / "store.rs").write_text("mod db;\n", encoding="utf-8")  # 2018 风格 → src/store/db.rs
    (repo / "src" / "store" / "db.rs").write_text("", encoding="utf-8")
    file_mod, timed_out = s7_enforce_rs.build_mod_tree(repo)
    assert timed_out is False
    assert file_mod == {
        "src/lib.rs": "crate",
        "src/api/mod.rs": "api",
        "src/api/client.rs": "api::client",
        "src/store.rs": "store",
        "src/store/db.rs": "store::db",
    }


def test_build_mod_tree_prefers_lib_over_main(tmp_path):
    repo = tmp_path / "crate"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lib.rs").write_text("mod api;\n", encoding="utf-8")
    (repo / "src" / "api.rs").write_text("", encoding="utf-8")
    (repo / "src" / "main.rs").write_text("mod binonly;\n", encoding="utf-8")  # 另一 crate 根：不入树
    (repo / "src" / "binonly.rs").write_text("", encoding="utf-8")
    file_mod, _timed_out = s7_enforce_rs.build_mod_tree(repo)
    assert file_mod == {"src/lib.rs": "crate", "src/api.rs": "api"}


def test_build_mod_tree_no_root(tmp_path):
    file_mod, timed_out = s7_enforce_rs.build_mod_tree(tmp_path / "empty")
    assert file_mod == {}
    assert timed_out is False


def test_scan_use_edges_excludes_tests_and_extern(tmp_path):
    repo = tmp_path / "crate"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lib.rs").write_text("mod api;\nmod store;\n", encoding="utf-8")
    (repo / "src" / "api.rs").write_text("pub fn handle() {}\n", encoding="utf-8")
    (repo / "src" / "store.rs").write_text(
        "use mycrate::foo;\n"  # extern：跳过
        "#[cfg(test)]\nmod tests {\n    use super::*;\n}\n"  # 行内测试块：排除
        "#[cfg(test)]\nmod tfile;\n"  # 文件式测试 mod：声明排除
        "use crate::api::handle;\n",
        encoding="utf-8",
    )
    (repo / "src" / "tfile.rs").write_text("use crate::api::handle;\n", encoding="utf-8")
    file_mod, _timed_out = s7_enforce_rs.build_mod_tree(repo)
    assert "src/tfile.rs" not in file_mod  # cfg(test) 文件不进树（成 orphan）
    graph, edges, files, timed_out = s7_enforce_rs.scan_use_edges(repo, file_mod)
    assert timed_out is False
    assert graph["api"] == []  # 叶模块也入图（空出边）
    assert graph["store"] == ["api"]  # extern/测试 use 不产边
    assert "crate" not in graph["store"]  # mod tests 内 use super::* 若泄漏会误指 crate
    assert files == 3
    assert [(e["file"], e["line"]) for e in edges] == [("src/store.rs", 8)]


def test_scan_use_edges_counts_production_inline_mod_use(tmp_path):
    """生产行内 mod 体 use 是真实编译期依赖边（rustc 探针 P2）——计入且行号保真。"""
    repo = tmp_path / "crate"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lib.rs").write_text("mod api;\nmod store;\n", encoding="utf-8")
    (repo / "src" / "api.rs").write_text("pub fn handle() {}\n", encoding="utf-8")
    (repo / "src" / "store.rs").write_text(
        "mod helper {\n    use crate::api::{handle};\n    pub(crate) fn go() { handle(); }\n}\n"
        "pub fn get() { helper::go(); }\n",
        encoding="utf-8",
    )
    file_mod, _timed_out = s7_enforce_rs.build_mod_tree(repo)
    graph, edges, _files, timed_out = s7_enforce_rs.scan_use_edges(repo, file_mod)
    assert timed_out is False
    assert graph["store"] == ["api"]  # 行内 mod use 边不再丢弃
    assert [(e["file"], e["line"]) for e in edges] == [("src/store.rs", 2)]


def test_scan_use_edges_inline_mod_super_nested(tmp_path):
    """行内 mod 内 super=enclosing（探针 P3）：边 store→store::logger；嵌套行内下钻。"""
    repo = tmp_path / "crate"
    (repo / "src" / "store").mkdir(parents=True)
    (repo / "src" / "lib.rs").write_text("mod api;\nmod store;\n", encoding="utf-8")
    (repo / "src" / "api.rs").write_text("pub fn handle() {}\n", encoding="utf-8")
    (repo / "src" / "store.rs").write_text(
        "mod logger;\n"
        "mod outer {\n"
        "    mod inner {\n"
        "        use crate::api::handle;\n"
        "    }\n"
        "}\n"
        "mod helper {\n"
        "    use super::logger::log_it;\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "src" / "store" / "logger.rs").write_text("pub fn log_it() {}\n", encoding="utf-8")
    file_mod, _timed_out = s7_enforce_rs.build_mod_tree(repo)
    assert file_mod["src/store/logger.rs"] == "store::logger"
    graph, edges, _files, timed_out = s7_enforce_rs.scan_use_edges(repo, file_mod)
    assert timed_out is False
    assert graph["store"] == ["api", "store::logger"]  # crate 绝对 + super 解析
    edge_set = {(e["file"], e["line"], e["dst"]) for e in edges}
    assert ("src/store.rs", 4, "api") in edge_set
    assert ("src/store.rs", 8, "store::logger") in edge_set


def test_scan_use_edges_skips_macro_rules_def_bodies(tmp_path):
    """macro_rules 定义体是模板非真实边（探针 P5）——体内 use 不计、体内 mod 不入树。"""
    repo = tmp_path / "crate"
    (repo / "src" / "store").mkdir(parents=True)
    (repo / "src" / "lib.rs").write_text("mod api;\nmod store;\n", encoding="utf-8")
    (repo / "src" / "api.rs").write_text("pub fn handle() {}\n", encoding="utf-8")
    (repo / "src" / "store.rs").write_text(
        "macro_rules! fake {\n"
        "    () => { mod fake_mod; };\n"
        "}\n"
        "macro_rules! fake_use {\n"
        "    () => { use crate::api::handle; };\n"
        "}\n"
        "mod logger;\n",
        encoding="utf-8",
    )
    (repo / "src" / "store" / "fake_mod.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    (repo / "src" / "store" / "logger.rs").write_text("pub fn log_it() {}\n", encoding="utf-8")
    file_mod, _timed_out = s7_enforce_rs.build_mod_tree(repo)
    assert "src/store/fake_mod.rs" not in file_mod  # 体内模板 mod 不产幻影树条目
    assert file_mod["src/store/logger.rs"] == "store::logger"  # 真 mod 声明保留
    graph, _edges, _files, timed_out = s7_enforce_rs.scan_use_edges(repo, file_mod)
    assert timed_out is False
    assert graph.get("store", []) == []  # 模板 use 不误计


def test_scan_use_edges_multiline_string_no_phantom(tmp_path):
    """普通串跨行合法（探针 P1）——串内伪 use 不产边（kt/cs/java 行截断修法不适用）。"""
    repo = tmp_path / "crate"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lib.rs").write_text("mod api;\nmod store;\n", encoding="utf-8")
    (repo / "src" / "api.rs").write_text("pub fn handle() {}\n", encoding="utf-8")
    (repo / "src" / "store.rs").write_text(
        'static S: &str = "line1\nuse crate::api::handle;\nline3";\npub fn get() -> usize { S.len() }\n',
        encoding="utf-8",
    )
    file_mod, _timed_out = s7_enforce_rs.build_mod_tree(repo)
    graph, _edges, _files, timed_out = s7_enforce_rs.scan_use_edges(repo, file_mod)
    assert timed_out is False
    assert graph.get("store", []) == []


def test_iter_uses_ctx_cur_tracking():
    """上下文 cur 跟踪：嵌套行内 mod 内 use 按行内路径解析（探针 P3/P7）。"""
    text = s7_enforce_rs._blank_test_mods(
        s7_enforce_rs._blank_macro_defs(
            s7_enforce_rs._blank_noncode(
                "mod outer {\n"
                "    use crate::api::handle;\n"
                "    mod inner {\n"
                "        use super::x;\n"
                "        use self::y;\n"
                "    }\n"
                "}\n"
                "use crate::top;\n"
            )
        )
    )
    got = s7_enforce_rs._iter_uses_ctx(text, ["store"])
    assert [(list(cur), [list(s) for s in paths]) for _pos, paths, cur in got] == [
        (["store", "outer"], [["crate", "api", "handle"]]),
        (["store", "outer", "inner"], [["super", "x"]]),
        (["store", "outer", "inner"], [["self", "y"]]),
        (["store"], [["crate", "top"]]),
    ]


def test_find_violations_bfs_shortest_chain():
    graph = {"a": ["b", "mid"], "mid": ["c"], "b": ["c"], "c": [], "w": []}
    contracts = [
        {
            "name": "layer: core must not import interface",
            "source_layer": "core",
            "forbidden_layer": "interface",
            "source_modules": ["a"],
            "forbidden_modules": ["c"],
        }
    ]
    violations = s7_enforce_rs._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "a" and v["imported"] == "c"
    assert v["chain"] == ["a", "b", "c"]  # BFS 最短链


def test_find_violations_forbidden_leaf_visible():
    """forbidden 叶模块（无出边）必须在图可见——漏叶即假阴。"""
    graph = {"a": ["leaf"], "leaf": []}
    contracts = [
        {"name": "n", "source_modules": ["a"], "forbidden_modules": ["leaf"]}
    ]
    violations = s7_enforce_rs._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["a", "leaf"]


def test_find_violations_transitive_only():
    graph = {"a": ["b"], "b": ["c"], "c": []}
    contracts = [
        {
            "name": "layer: core must not import interface",
            "source_modules": ["a"],
            "forbidden_modules": ["c"],
        }
    ]
    violations = s7_enforce_rs._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["a", "b", "c"]


def test_find_violations_none_when_unreachable():
    graph = {"a": ["b"], "b": [], "c": []}
    contracts = [
        {"name": "n", "source_modules": ["a"], "forbidden_modules": ["c"]}
    ]
    assert s7_enforce_rs._find_violations(graph, contracts) == []


def test_derive_rs_containers(tmp_path):
    repo = _mk_rs_repo(tmp_path)
    con = _con(repo)
    try:
        derived = s7_enforce_rs.derive_rs_containers(repo, con, json.loads(RS_STRUCTURE))
    finally:
        con.close()
    assert derived["containers"] == {"core-mod": ["store"], "iface-mod": ["api"]}
    assert derived["rs_files"] == 2
    assert derived["orphan_files"] == 0
    assert derived["timed_out"] is False
    assert derived["extensions"] == []


def test_derive_rs_containers_orphans_counted(tmp_path):
    repo = _mk_rs_repo(tmp_path)
    (repo / "src" / "bin").mkdir()
    (repo / "src" / "bin" / "tool.rs").write_text("fn main() {}\n", encoding="utf-8")  # mod 树不可达
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'demo::bin::main', 'Function', 'main', 'src/bin/tool.rs', 1, 1, 'h')"
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
        derived = s7_enforce_rs.derive_rs_containers(repo, con, json.loads(con.execute("SELECT payload_json FROM structure").fetchone()[0]))
    finally:
        con.close()
    assert derived["containers"] == {"core-mod": ["store"], "iface-mod": ["api"]}  # orphan 不进容器
    assert derived["rs_files"] == 3
    assert derived["orphan_files"] == 1  # src/bin/tool.rs 诚实计数

# ------------------------------------------------- run_rs / run() E2E


def test_run_rs_violation_e2e(tmp_path):
    repo = _mk_rs_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce_rs.run_rs(repo, con, json.loads(RS_STRUCTURE), "active", RS_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "rs-use-parser"
    assert out["contracts"] == 1
    assert out["files"] == 3
    assert out["modules"] == 3
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
    assert row[3] == "f1"  # src/store.rs 首符号
    assert row[4] == "src/store.rs"
    assert row[5] == 1 and row[6] == 1  # 违例 use 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "store"
    assert payload["imported"] == "api"
    assert payload["chain"] == ["store", "api"]
    assert payload["line"] == 1


def test_run_rs_transitive_e2e(tmp_path):
    """store → queue → api：core 层内传递链触达 interface（import-linter forbidden 同义）。"""
    repo = _mk_rs_repo(tmp_path)
    (repo / "src" / "lib.rs").write_text("mod api;\nmod queue;\nmod store;\n", encoding="utf-8")
    (repo / "src" / "queue.rs").write_text(
        "use crate::api::handle;\n\npub fn work() { handle(); }\n", encoding="utf-8"
    )
    (repo / "src" / "store.rs").write_text(
        "use crate::queue::work;\n\npub fn get() { work(); }\n", encoding="utf-8"
    )
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["f1", "f3"], "layer": "core"},
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
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'demo::queue::work', 'Function', 'work', 'src/queue.rs', 3, 4, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    payload = json.loads(structure)
    containers = {"core-mod": ["store", "queue"], "iface-mod": ["api"]}
    try:
        out = s7_enforce_rs.run_rs(repo, con, payload, "active", containers)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 2  # queue→api 直连 + store 经 queue 传递
    rows = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, payload_json FROM findings ORDER BY rowid"
    ).fetchall()
    by_importer = {json.loads(r[1])["importer"]: json.loads(r[1]) for r in rows}
    assert by_importer["queue"]["chain"] == ["queue", "api"]
    assert by_importer["store"]["chain"] == ["store", "queue", "api"]  # BFS 最短传递链
    assert by_importer["store"]["line"] == 1  # 锚定链尾直接 use 边：src/queue.rs 第 1 行
    assert all(r[0] == "src/queue.rs" for r in rows)  # 链尾边所在文件


def test_run_rs_idempotent(tmp_path):
    repo = _mk_rs_repo(tmp_path)
    con = _con(repo)
    try:
        first = s7_enforce_rs.run_rs(repo, con, json.loads(RS_STRUCTURE), "active", RS_CONTAINERS)
        second = s7_enforce_rs.run_rs(repo, con, json.loads(RS_STRUCTURE), "active", RS_CONTAINERS)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_rs_compliant_clears_old_findings(tmp_path):
    repo = _mk_rs_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 修掉违例 use
    (repo / "src" / "store.rs").write_text("pub fn get() {}\n", encoding="utf-8")
    con = _con(repo)
    try:
        out = s7_enforce_rs.run_rs(repo, con, json.loads(RS_STRUCTURE), "active", RS_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_rs_no_crate_root(tmp_path):
    repo = _mk_rs_repo(tmp_path)
    shutil.rmtree(repo / "src")
    con = _con(repo)
    try:
        out = s7_enforce_rs.run_rs(repo, con, json.loads(RS_STRUCTURE), "active", RS_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no crate root" in out["reason"]


def test_run_rs_tree_timeout(tmp_path, monkeypatch):
    repo = _mk_rs_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_rs, "build_mod_tree", lambda repo, timeout=600: ({}, True))
    con = _con(repo)
    try:
        out = s7_enforce_rs.run_rs(repo, con, json.loads(RS_STRUCTURE), "active", RS_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "mod tree build exceeded" in out["reason"]
    assert "9s" in out["reason"]


def test_run_rs_scan_timeout_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_rs_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        s7_enforce_rs, "scan_use_edges", lambda repo, file_mod, timeout=600: ({}, [], 0, True)
    )
    con = _con(repo)
    try:
        out = s7_enforce_rs.run_rs(repo, con, json.loads(RS_STRUCTURE), "active", RS_CONTAINERS, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "use scan exceeded" in out["reason"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


def test_run_rs_cfg_test_file_module_excluded(tmp_path):
    """#[cfg(test)] mod tests; 文件式测试模块的 use 不产违例（go TestImports 对齐）。"""
    repo = _mk_rs_repo(tmp_path)
    (repo / "src" / "lib.rs").write_text(
        "mod api;\nmod store;\n#[cfg(test)]\nmod tests;\n", encoding="utf-8"
    )
    (repo / "src" / "tests.rs").write_text(
        "use super::*;\nuse crate::api::handle;\n", encoding="utf-8"
    )
    con = _con(repo)
    try:
        out = s7_enforce_rs.run_rs(repo, con, json.loads(RS_STRUCTURE), "active", RS_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 仅 store 的生产 use
    assert out["files"] == 3  # tests.rs 不在树


def test_run_rs_inline_mod_violation_e2e(tmp_path):
    """行内 mod use 依赖边全程可见（探针 P2）：run_rs 级违例锚定真 use 行。"""
    repo = _mk_rs_repo(tmp_path)
    (repo / "src" / "store.rs").write_text(
        "mod helper {\n    use crate::api::handle;\n    pub(crate) fn go() { handle(); }\n}\n"
        "pub fn get() { helper::go(); }\n",
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        out = s7_enforce_rs.run_rs(repo, con, json.loads(RS_STRUCTURE), "active", RS_CONTAINERS)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 1  # 旧版行内 use 被丢弃 → 假阴 0
    row = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT file_path, start_line, payload_json FROM findings"
    ).fetchone()
    assert row[0] == "src/store.rs"
    assert row[1] == 2  # 行内 use 真行号
    payload = json.loads(row[2])
    assert payload["importer"] == "store"
    assert payload["imported"] == "api"
    assert payload["chain"] == ["store", "api"]


# ------------------------------------------------------ run() 路由


def test_run_routes_rs(tmp_path):
    """纯 Rust 结构 → run() 路由至 rs 引擎并落 P9。"""
    repo = _mk_rs_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "rs-use-parser"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_routes_rs_orphan_surfaced(tmp_path):
    repo = _mk_rs_repo(tmp_path)
    (repo / "src" / "bin").mkdir()
    (repo / "src" / "bin" / "tool.rs").write_text("fn main() {}\n", encoding="utf-8")
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'demo::bin::main', 'Function', 'main', 'src/bin/tool.rs', 1, 1, 'h')"
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
    assert out["orphan_rs_files"] == 1  # mod 树不可达文件诚实上报


def test_run_mixed_rs_and_py_not_enforceable(tmp_path):
    repo = _mk_rs_repo(tmp_path)
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
    assert "py + rs" in out["reason"]


def test_run_rs_single_layer_not_enforceable(tmp_path):
    single = json.dumps(
        {
            "modules": [
                {"module_id": "m1", "name": "n", "responsibility": "r", "symbol_fids": ["f1"], "layer": "only"},
            ],
            "layers": [{"name": "only", "may_depend_on": []}],
        }
    )
    repo = _mk_rs_repo(tmp_path, structure=single)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no derivable contracts" in out["reason"]


def test_run_rs_derive_timeout(tmp_path, monkeypatch):
    repo = _mk_rs_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_rs, "build_mod_tree", lambda repo, timeout=600: ({}, True))
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "rust mod tree build exceeded" in out["reason"]


def test_cli_enforce_rs_wiring(tmp_path, capsys):
    repo = _mk_rs_repo(tmp_path)
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["engine"] == "rs-use-parser"
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


# ---------------------------------------------------------------- R-Q ReDoS 回归


def test_is_test_cfg_redos_near_miss_linear():
    """R-Q 回归：多 ')' 后缀垃圾近失配必须线性（旧惰性组+\\s* O(K·M²) 空间串重扫）。

    旧单正则实测（探针 C2a）：K=8、M=3200 → 266ms、4x 尺寸 25.9x 时间；
    本用例 K=4、M=40000 外推 ~21s 挂起。线性实现 ~ms；10s 上限给旧二次行为
    留足红余量。语义由既有 test_is_test_cfg_variants 钉住。
    """
    inner = "cfg(" + ("a" * 4 + " " * 40000 + ")" + "b") * 4
    t0 = time.perf_counter()
    assert s7_enforce_rs._is_test_cfg(inner) is False
    assert time.perf_counter() - t0 < 10.0
