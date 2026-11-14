"""S7 Go 执法引擎单元 + mocked E2E（真 go list E2E 见 dogfood work/go-enforce-probe）。"""

import json
import sqlite3
from pathlib import Path

from vrp import cli, s7_enforce, s7_enforce_go

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

MODULE_PATH = "example.com/demo"

# 两层：core(store) ← interface(api)；违例 = store 上向 import api
GO_STRUCTURE = json.dumps(
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


def _mk_go_repo(tmp_path: Path, structure: str = GO_STRUCTURE) -> Path:
    repo = tmp_path / "gorepo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'demo/store.Use', 'Function', 'Use', 'store/repo.go', 1, 6, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'demo/api.Handler', 'Function', 'Handler', 'api/handler.go', 1, 3, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
            (structure,),
        )
    con.commit()
    con.close()
    (repo / "go.mod").write_text(f"module {MODULE_PATH}\n\ngo 1.26\n", encoding="utf-8")
    (repo / "api").mkdir()
    (repo / "api" / "handler.go").write_text(
        "package api\n\nfunc Handler() int { return 1 }\n", encoding="utf-8"
    )
    (repo / "store").mkdir()
    # 违例 import：第 3 行引号字面量（_locate_import 行号锚定目标）
    (repo / "store" / "repo.go").write_text(
        "package store\n\nimport \"example.com/demo/api\"\n\nfunc Use() int { return api.Handler() }\n",
        encoding="utf-8",
    )
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


def _stream(repo: Path, imports: dict[str, list[str]] | None = None) -> str:
    """构造 go list -e -json 输出流（探针实测形态：连续 JSON 对象非数组）。"""
    if imports is None:
        imports = {"api": [], "store": ["api"]}
    objs = []
    for rel, imps in imports.items():
        ip = f"{MODULE_PATH}/{rel}"
        obj = {
            "ImportPath": ip,
            "Dir": str((repo / rel).resolve()),
            "GoFiles": [p.name for p in sorted((repo / rel).glob("*.go"))],
        }
        if imps:
            obj["Imports"] = [f"{MODULE_PATH}/{i}" if i != "." else MODULE_PATH for i in imps]
        objs.append(obj)
    return "".join(json.dumps(o) + "\n" for o in objs)


# ------------------------------------------------------------------ unit


def test_go_dir_variants():
    assert s7_enforce_go._go_dir("api/handler.go") == "api"
    assert s7_enforce_go._go_dir("main.go") == "."
    assert s7_enforce_go._go_dir("cmd\\app\\main.go") == "cmd/app"
    assert s7_enforce_go._go_dir("vendor/x/y.go") is None
    assert s7_enforce_go._go_dir("x/vendor/y.go") == "x/vendor"  # 嵌套 vendor 是真实包（探针 P7）
    assert s7_enforce_go._go_dir("internal/vendor/v.go") == "internal/vendor"
    assert s7_enforce_go._go_dir(".vrp/x.go") is None
    assert s7_enforce_go._go_dir("src/a.ts") is None


def test_derive_go_containers(tmp_path):
    repo = _mk_go_repo(tmp_path)
    con = _con(repo)
    try:
        payload = json.loads(GO_STRUCTURE)
        derived = s7_enforce_go.derive_go_containers(con, payload)
    finally:
        con.close()
    assert derived["containers"] == {"core-mod": ["store"], "iface-mod": ["api"]}


def test_module_path(tmp_path):
    repo = tmp_path
    (repo / "go.mod").write_text("module example.com/demo\n\ngo 1.26\n", encoding="utf-8")
    assert s7_enforce_go._module_path(repo) == "example.com/demo"
    assert s7_enforce_go._module_path(tmp_path / "missing") is None
    (repo / "go.mod").write_text("go 1.26\n", encoding="utf-8")
    assert s7_enforce_go._module_path(repo) is None  # 无 module 行


def test_module_path_review_batch_forms(tmp_path):
    """module 指令四形态全合法（go 1.26 探针实证 P1/P2/P4/P5）：块/引号/缩进/Tab+注释；BOM 拒绝真值对齐（P3）。"""
    repo = tmp_path
    (repo / "go.mod").write_text("module (\n\texample.com/block\n)\n\ngo 1.26\n", encoding="utf-8")
    assert s7_enforce_go._module_path(repo) == "example.com/block"
    (repo / "go.mod").write_text('module "example.com/quoted"\n\ngo 1.26\n', encoding="utf-8")
    assert s7_enforce_go._module_path(repo) == "example.com/quoted"
    (repo / "go.mod").write_text(" module example.com/indent\n\ngo 1.26\n", encoding="utf-8")
    assert s7_enforce_go._module_path(repo) == "example.com/indent"
    (repo / "go.mod").write_text("module\texample.com/tab // trailing\n\ngo 1.26\n", encoding="utf-8")
    assert s7_enforce_go._module_path(repo) == "example.com/tab"
    (repo / "go.mod").write_text("module (\n\texample.com/unbal\n", encoding="utf-8")
    assert s7_enforce_go._module_path(repo) is None  # 未闭合块 go 拒绝（探针 P9）
    (repo / "go.mod").write_bytes(b"\xef\xbb\xbfmodule example.com/bom\n\ngo 1.26\n")
    assert s7_enforce_go._module_path(repo) is None  # BOM go.mod 工具链拒绝（探针 P3 rc=1）


def test_derive_go_containers_nested_vendor(tmp_path):
    """internal/vendor 嵌套包是真实容器（探针 P7 build rc=0）——不因路径段 vendor 被丢。"""
    repo = _mk_go_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f3', 'p', 'demo/vendor.V', 'Function', 'V', 'internal/vendor/v.go', 1, 2, 'h')"
    )
    con.commit()
    con.close()
    payload = json.loads(GO_STRUCTURE)
    payload["modules"].append(
        {"module_id": "ven-mod", "name": "内嵌", "responsibility": "嵌", "symbol_fids": ["f3"], "layer": "core"}
    )
    con = _con(repo)
    try:
        derived = s7_enforce_go.derive_go_containers(con, payload)
    finally:
        con.close()
    assert derived["containers"]["ven-mod"] == ["internal/vendor"]


def test_parse_json_stream():
    text = '{"a": 1}\n{"b": [2, 3]}\n\n {"c": "x"}'
    assert s7_enforce_go._parse_json_stream(text) == [{"a": 1}, {"b": [2, 3]}, {"c": "x"}]
    assert s7_enforce_go._parse_json_stream("  ") == []


def test_import_graph_excludes_errored_and_maps_root():
    packages = [
        {"ImportPath": "m.example", "Imports": ["m.example/sub"], "GoFiles": ["main.go"]},
        {"ImportPath": "m.example/sub", "Imports": ["m.example"], "GoFiles": ["a.go"]},  # 环：构造防御
        {"ImportPath": "m.example/bad", "Imports": [], "Error": {"Err": "boom"}},
        {"ImportPath": "other.com/pkg", "Imports": []},  # 非本模块
    ]
    graph, pkg_by_dir, errored = s7_enforce_go._import_graph(packages, "m.example")
    assert graph == {".": ["sub"], "sub": ["."]}
    assert set(pkg_by_dir) == {".", "sub"}
    assert errored == 1  # 错误包剔除但计数公开


def test_find_violations_bfs_shortest_chain():
    graph = {
        "a": ["b", "mid"],
        "mid": ["c"],
        "b": ["c"],
        "c": [],
        "w": [],
    }
    contracts = [
        {
            "name": "layer: core must not import interface",
            "source_layer": "core",
            "forbidden_layer": "interface",
            "source_modules": ["a"],
            "forbidden_modules": ["c"],
        }
    ]
    violations = s7_enforce_go._find_violations(graph, contracts)
    assert len(violations) == 1
    v = violations[0]
    assert v["importer"] == "a" and v["imported"] == "c"
    assert v["chain"] == ["a", "b", "c"]  # BFS 最短链（a→mid→c 更长不取）


def test_find_violations_transitive_only():
    """无直接边、仅传递可达仍算违例（import-linter forbidden 同义）。"""
    graph = {"a": ["b"], "b": ["c"], "c": []}
    contracts = [
        {
            "name": "layer: core must not import interface",
            "source_modules": ["a"],
            "forbidden_modules": ["c"],
        }
    ]
    violations = s7_enforce_go._find_violations(graph, contracts)
    assert len(violations) == 1
    assert violations[0]["chain"] == ["a", "b", "c"]


def test_find_violations_none_when_unreachable():
    graph = {"a": ["b"], "b": [], "c": []}
    contracts = [
        {"name": "n", "source_modules": ["a"], "forbidden_modules": ["c"]}
    ]
    assert s7_enforce_go._find_violations(graph, contracts) == []


def test_locate_import_line(tmp_path):
    repo = _mk_go_repo(tmp_path)
    pkg = {
        "Dir": str((repo / "store").resolve()),
        "GoFiles": ["repo.go"],
    }
    located = s7_enforce_go._locate_import(repo, pkg, "api", MODULE_PATH)
    assert located == ("store/repo.go", 3)  # 违例 import 行号
    # 行号找不到 → 首文件 + None（诚实）
    (repo / "store" / "repo.go").write_text(
        "package store\n\nfunc Use() int { return 0 }\n", encoding="utf-8"
    )
    located = s7_enforce_go._locate_import(repo, pkg, "api", MODULE_PATH)
    assert located == ("store/repo.go", None)
    assert s7_enforce_go._locate_import(repo, {"GoFiles": []}, "api", MODULE_PATH) is None


def test_locate_import_root_package_no_dot_prefix(tmp_path):
    """根包（rel_pkg_dir="."）锚定文件路径不可带 "./" 前缀——去归一化使锚定查表失败（冒烟 F05 修正回归）。"""
    repo = _mk_go_repo(tmp_path)
    (repo / "main.go").write_text(
        f'package main\n\nimport "{MODULE_PATH}/api"\n\nfunc main() {{ _ = api.Handler }}\n',
        encoding="utf-8",
    )
    pkg = {"Dir": str(repo.resolve()), "GoFiles": ["main.go"]}
    located = s7_enforce_go._locate_import(repo, pkg, "api", MODULE_PATH)
    assert located == ("main.go", 3)  # 非 "./main.go"


# ------------------------------------------------- run_go / run() mocked


def test_run_go_violation_mock(tmp_path, monkeypatch):
    repo = _mk_go_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_go, "_execute", lambda repo, timeout: (0, _stream(repo), ""))
    con = _con(repo)
    try:
        out = s7_enforce_go.run_go(repo, con, json.loads(GO_STRUCTURE), "active", {"core-mod": ["store"], "iface-mod": ["api"]})
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "go-list"
    assert out["contracts"] == 1
    assert out["packages"] == 2
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
    assert row[3] == "f1"  # store/repo.go 首符号
    assert row[4] == "store/repo.go"
    assert row[5] == 3 and row[6] == 3  # 违例 import 行
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "store"
    assert payload["imported"] == "api"
    assert payload["chain"] == ["store", "api"]
    assert payload["line"] == 3


def test_run_go_idempotent_mock(tmp_path, monkeypatch):
    repo = _mk_go_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_go, "_execute", lambda repo, timeout: (0, _stream(repo), ""))
    con = _con(repo)
    try:
        first = s7_enforce_go.run_go(repo, con, json.loads(GO_STRUCTURE), "active", {"core-mod": ["store"], "iface-mod": ["api"]})
        second = s7_enforce_go.run_go(repo, con, json.loads(GO_STRUCTURE), "active", {"core-mod": ["store"], "iface-mod": ["api"]})
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all 不膨胀


def test_run_go_compliant_clears_old_findings(tmp_path, monkeypatch):
    repo = _mk_go_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    # 合规图：store 不再 import api
    monkeypatch.setattr(
        s7_enforce_go, "_execute", lambda repo, timeout: (0, _stream(repo, {"api": [], "store": []}), "")
    )
    con = _con(repo)
    try:
        out = s7_enforce_go.run_go(repo, con, json.loads(GO_STRUCTURE), "active", {"core-mod": ["store"], "iface-mod": ["api"]})
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清旧违例


def test_run_go_errored_packages_counted(tmp_path, monkeypatch):
    repo = _mk_go_repo(tmp_path)
    stream = _stream(repo) + json.dumps(
        {"ImportPath": f"{MODULE_PATH}/broken", "Dir": str(repo / "broken"), "GoFiles": ["b.go"], "Error": {"Err": "no required module provides package x"}}
    )
    monkeypatch.setattr(s7_enforce_go, "_execute", lambda repo, timeout: (0, stream, ""))
    con = _con(repo)
    try:
        out = s7_enforce_go.run_go(repo, con, json.loads(GO_STRUCTURE), "active", {"core-mod": ["store"], "iface-mod": ["api"]})
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["errored_packages"] == 1  # 剔除出图但计数公开


def test_run_go_no_go_mod(tmp_path, monkeypatch):
    repo = _mk_go_repo(tmp_path)
    (repo / "go.mod").unlink()
    monkeypatch.setattr(s7_enforce_go, "find_go", lambda: "fake-go")
    con = _con(repo)
    try:
        out = s7_enforce_go.run_go(repo, con, json.loads(GO_STRUCTURE), "active", {"core-mod": ["store"], "iface-mod": ["api"]})
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no go.mod" in out["reason"]


def test_run_go_malformed_gomod_reason(tmp_path, monkeypatch):
    """go.mod 存在但 module 指令不可解析（BOM——go 工具链同拒）→ reason 与缺失区分（诚实）。"""
    repo = _mk_go_repo(tmp_path)
    (repo / "go.mod").write_bytes(b"\xef\xbb\xbfmodule example.com/demo\n\ngo 1.26\n")
    monkeypatch.setattr(s7_enforce_go, "find_go", lambda: "fake-go")
    con = _con(repo)
    try:
        out = s7_enforce_go.run_go(repo, con, json.loads(GO_STRUCTURE), "active", {"core-mod": ["store"], "iface-mod": ["api"]})
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no parseable module directive" in out["reason"]


def test_run_go_enforcer_not_found(tmp_path, monkeypatch):
    repo = _mk_go_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_go, "find_go", lambda: None)
    con = _con(repo)
    try:
        out = s7_enforce_go.run_go(repo, con, json.loads(GO_STRUCTURE), "active", {"core-mod": ["store"], "iface-mod": ["api"]})
    finally:
        con.close()
    assert out["status"] == "enforcer-not-found"
    assert "VRP_GO_BIN" in out["reason"]
    assert out["contracts"] == 1


def test_run_go_timeout(tmp_path, monkeypatch):
    repo = _mk_go_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_go, "_execute", lambda repo, timeout: (None, None, None))
    con = _con(repo)
    try:
        out = s7_enforce_go.run_go(repo, con, json.loads(GO_STRUCTURE), "active", {"core-mod": ["store"], "iface-mod": ["api"]}, timeout=9)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "9s" in out["reason"]


def test_execute_timeout_tree_killed_returns_none(tmp_path, monkeypatch):
    """R-AN：go list 超时（go 直子被杀后编译工具链孙进程持管道，#84；MCP enforce
    工具直调链活面）——树杀归 (None,None,None)，墙钟即返
    （探针 pre-fix 15.1s vs 2s 预算的 7.5× 超支消除）。"""
    import sys
    import time as time_mod

    slow = tmp_path / "slow_go.py"
    slow.write_text("import time\ntime.sleep(30)\nprint('ok')\n", encoding="utf-8")
    if sys.platform == "win32":
        exe = tmp_path / "fake_go.cmd"
        exe.write_text(f'@"{sys.executable}" "{slow}" %*\n', encoding="utf-8")
    else:
        exe = tmp_path / "fake_go.sh"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{slow}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)

    monkeypatch.setattr(s7_enforce_go, "find_go", lambda: str(exe))
    start = time_mod.monotonic()
    assert s7_enforce_go._execute(tmp_path, 2) == (None, None, None)
    assert time_mod.monotonic() - start < 15, "tree-kill must return promptly; orphan-held pipe would hang"


def test_run_go_enforcer_failed_keeps_findings(tmp_path, monkeypatch):
    repo = _mk_go_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(s7_enforce_go, "_execute", lambda repo, timeout: (1, "", "go: go.mod file not found"))
    con = _con(repo)
    try:
        out = s7_enforce_go.run_go(repo, con, json.loads(GO_STRUCTURE), "active", {"core-mod": ["store"], "iface-mod": ["api"]})
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "rc=1" in out["reason"]
    assert "go.mod" in out["stderr_tail"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 失败不误删


# ------------------------------------------------------ run() 路由


def test_run_routes_go(tmp_path, monkeypatch):
    """纯 Go 结构 → run() 路由至 Go 引擎并落 P9。"""
    repo = _mk_go_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_go, "_execute", lambda repo, timeout: (0, _stream(repo), ""))
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "go-list"
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1


def test_run_mixed_go_and_py_not_enforceable(tmp_path):
    repo = _mk_go_repo(tmp_path)
    con = _con(repo)
    # 混入 Python 符号（同 structure 的第三个模块）
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
    con.close()
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "mixed-language" in out["reason"]
    assert "py + go" in out["reason"]


def test_run_go_single_layer_not_enforceable(tmp_path):
    single = json.dumps(
        {
            "modules": [
                {"module_id": "m1", "name": "n", "responsibility": "r", "symbol_fids": ["f1"], "layer": "only"},
            ],
            "layers": [{"name": "only", "may_depend_on": []}],
        }
    )
    repo = _mk_go_repo(tmp_path, structure=single)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no derivable contracts" in out["reason"]


def test_cli_enforce_go_wiring(tmp_path, monkeypatch, capsys):
    repo = _mk_go_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_go, "_execute", lambda repo, timeout: (0, _stream(repo), ""))
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
