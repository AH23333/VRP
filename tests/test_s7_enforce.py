"""S7 架构执法单元 + mocked E2E（真 lint-imports E2E 见 tests/deep/test_deep_s7_enforce.py）。"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import cli, s7_enforce

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 探针实测输出格式（work/probe_r4_forbidden.py，2026-11-05）
CANNED_BROKEN = """
---------
Contracts
---------

Analyzed 5 files, 1 dependencies.
---------------------------------

layer: core must not import interface BROKEN

Contracts: 0 kept, 1 broken.


----------------
Broken contracts
----------------

layer: core must not import interface
-------------------------------

pkgcore.s0 is not allowed to import pkgiface.cli:

-   pkgcore.s0 -> pkgiface.cli (l.2)

"""

STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "core-mod", "name": "核心", "responsibility": "算", "symbol_fids": ["f1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    },
    ensure_ascii=False,
)


def _mk_repo(tmp_path: Path, structure: str | None = STRUCTURE, status: str = "active") -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    # a() 覆盖 1-4 行：违例 import 落在函数内（line 2）→ 锚定 f1
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'pkgcore.s0.a', 'Function', 'a', 'pkgcore/s0.py', 1, 4, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'pkgiface.cli.c', 'Function', 'c', 'pkgiface/cli.py', 1, 2, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    if structure is not None:
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?, ?, 't')",
            (structure, status),
        )
    con.commit()
    con.close()
    (repo / "pkgcore").mkdir()
    (repo / "pkgcore" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkgcore" / "s0.py").write_text(
        "def a():\n    import pkgiface.cli as _cli\n\n    return 1\n", encoding="utf-8"
    )
    (repo / "pkgiface").mkdir()
    (repo / "pkgiface" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkgiface" / "cli.py").write_text("def c():\n    return 3\n", encoding="utf-8")
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


# ------------------------------------------------------------------ unit


def test_dotted_variants():
    assert s7_enforce._dotted("vrp/s1_cbm.py") == "vrp.s1_cbm"
    assert s7_enforce._dotted("a\\b\\c.py") == "a.b.c"
    assert s7_enforce._dotted("pkg/__init__.py") == "pkg"
    assert s7_enforce._dotted("pkg/sub/__init__.py") == "pkg.sub"
    assert s7_enforce._dotted("src/a.ts") is None
    assert s7_enforce._dotted("__init__.py") is None
    assert s7_enforce._dotted(".vrp/x.py") is None


def test_dotted_to_paths_candidates():
    assert s7_enforce._dotted_to_paths("pkgcore.s0") == ["pkgcore/s0/__init__.py", "pkgcore/s0.py"]
    assert s7_enforce._dotted_to_paths("pkgiface") == ["pkgiface/__init__.py", "pkgiface.py"]


def test_forbidden_pairs_linear_stack():
    payload = {
        "layers": [
            {"name": "top", "may_depend_on": ["middle"]},
            {"name": "middle", "may_depend_on": ["bottom"]},
            {"name": "bottom", "may_depend_on": []},
        ]
    }
    assert s7_enforce._forbidden_pairs(payload) == [
        ("bottom", "middle"),
        ("bottom", "top"),
        ("middle", "top"),
        ("top", "bottom"),  # 传递违例：may_depend_on 只豁免直连，top 直 import bottom 同样违规
    ]


def test_forbidden_pairs_dag():
    payload = {
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ]
    }
    assert s7_enforce._forbidden_pairs(payload) == [("core", "interface")]


def test_rule_pair_fields_and_text():
    layers = {"core", "interface"}
    assert s7_enforce._rule_pair({"rule": "r", "from_layer": "core", "to_layer": "interface"}, layers) == ("core", "interface")
    assert s7_enforce._rule_pair({"rule": "core 不许 import interface"}, layers) == ("core", "interface")
    assert s7_enforce._rule_pair({"rule": "core must not import interface"}, layers) == ("core", "interface")
    assert s7_enforce._rule_pair({"rule": "文档要写清楚"}, layers) is None
    assert s7_enforce._rule_pair({"rule": "core 不许 import ghost"}, layers) is None


def test_build_contracts_dedup_and_unenforced():
    payload = {
        "modules": [
            {"module_id": "m1", "name": "n", "responsibility": "r", "symbol_fids": ["f1"], "layer": "core"},
            {"module_id": "m2", "name": "n", "responsibility": "r", "symbol_fids": ["f2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
        "rules": [
            {"rule": "core 不许 import interface"},  # 与 DAG 对去重
            {"rule": "core must not import interface"},
            {"rule": "接口层不许有人乱 import 第三方"},  # 映射不出 → unenforced_rules
        ],
    }
    built = s7_enforce.build_contracts(payload, {"m1": ["pkgcore.s0"], "m2": ["pkgiface.cli"]})
    assert len(built["contracts"]) == 1
    contract = built["contracts"][0]
    assert contract["source_layer"] == "core"
    assert contract["forbidden_layer"] == "interface"
    assert contract["source_modules"] == ["pkgcore.s0"]
    assert contract["forbidden_modules"] == ["pkgiface.cli"]
    assert built["unenforced_rules"] == ["接口层不许有人乱 import 第三方"]
    assert built["unenforced_layers"] == []


def test_build_config_shape():
    contracts = [
        {
            "name": "layer: core must not import interface",
            "source_modules": ["pkgcore.s0"],
            "forbidden_modules": ["pkgiface.cli"],
        }
    ]
    text = s7_enforce.build_config(contracts, ["pkgcore", "pkgiface"])
    assert "[importlinter]" in text
    assert "root_packages =" in text
    assert "    pkgcore" in text and "    pkgiface" in text
    assert "[importlinter:contract:contract-0]" in text
    assert "name = layer: core must not import interface" in text
    assert "type = forbidden" in text
    assert "source_modules =" in text and "    pkgcore.s0" in text
    assert "forbidden_modules =" in text and "    pkgiface.cli" in text


def test_enforceable_container_shapes(tmp_path):
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "noinit").mkdir()  # 顶层无 __init__.py：命名空间包，import-linter 可执法
    (repo / "root_helper.py").write_text("", encoding="utf-8")  # 根级 .py：模块非包
    (repo / "bench-agent").mkdir()  # 连字符目录：非法标识符组件
    assert s7_enforce._enforceable_container(repo, "pkg.sub.mod")
    assert s7_enforce._enforceable_container(repo, "pkg")  # __init__.py 本身
    assert s7_enforce._enforceable_container(repo, "noinit.mod")  # 命名空间包
    assert not s7_enforce._enforceable_container(repo, "root_helper")
    assert not s7_enforce._enforceable_container(repo, "bench-agent.run")
    assert not s7_enforce._enforceable_container(repo, "missing_top.mod")  # 顶层不存在


def test_filter_contracts_unenforceable_containers(tmp_path):
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    contracts = [
        {
            "id": "c1",
            "name": "layer: core must not import interface",
            "source_modules": ["pkg.s0", "root_helper"],
            "forbidden_modules": ["pkg.other", "noinit.mod"],
        },
        {
            "id": "c2",
            "name": "layer: core must not import outer",
            "source_modules": ["bench-agent.run"],
            "forbidden_modules": ["pkg.other"],
        },
    ]
    kept, dropped = s7_enforce._filter_contracts(repo, contracts)
    assert [c["id"] for c in kept] == ["c1"]
    assert kept[0]["source_modules"] == ["pkg.s0"]
    assert kept[0]["forbidden_modules"] == ["pkg.other"]
    assert dropped == ["bench-agent.run", "noinit.mod", "root_helper"]


def test_filter_contracts_all_dropped(tmp_path):
    repo = tmp_path
    contracts = [
        {
            "id": "c1",
            "name": "layer: a must not import b",
            "source_modules": ["root_helper"],
            "forbidden_modules": ["bench-agent.run"],
        }
    ]
    kept, dropped = s7_enforce._filter_contracts(repo, contracts)
    assert kept == []
    assert dropped == ["bench-agent.run", "root_helper"]


def test_parse_broken_canned_probe_output():
    violations = s7_enforce.parse_broken(CANNED_BROKEN)
    assert violations == [
        {
            "contract": "layer: core must not import interface",
            "importer": "pkgcore.s0",
            "imported": "pkgiface.cli",
            "line": 2,
        }
    ]


# tsa dogfood 实测输出（work/probe_r4_tsa_raw.py，2026-11-05）：
# 聚合组与明细组同链双列 + 描述行 ~70 字符折行（折行片段无冒号尾）
CANNED_TSA_BROKEN = """
---------
Contracts
---------

Analyzed 1012 files, 3112 dependencies.
---------------------------------------

layer: core must not import interface BROKEN

Contracts: 0 kept, 1 broken.


----------------
Broken contracts
----------------

layer: core must not import interface
-------------------------------

tree_sitter_analyzer.core is not allowed to import tree_sitter_analyzer.api:

-   tree_sitter_analyzer.core.query_service -> tree_sitter_analyzer.api (l.320)


tree_sitter_analyzer.core.query_service is not allowed to import
tree_sitter_analyzer.api:

-   tree_sitter_analyzer.core.query_service -> tree_sitter_analyzer.api (l.320)

"""


def test_parse_broken_dedups_aggregate_and_detail_groups():
    violations = s7_enforce.parse_broken(CANNED_TSA_BROKEN)
    assert violations == [
        {
            "contract": "layer: core must not import interface",
            "importer": "tree_sitter_analyzer.core.query_service",
            "imported": "tree_sitter_analyzer.api",
            "line": 320,
        }
    ]


# tsa 冒烟真实输出形态（2026-11-06，COLUMNS 钉宽后不折行）：多链/多头行号多值/
# 链尾边行/同边跨契约
CANNED_MULTI_CHAIN_BROKEN = """
---------
Contracts
---------

Analyzed 1139 files, 3325 dependencies.
---------------------------------------

layer: analysis must not import infrastructure BROKEN
layer: benchmark must not import analysis BROKEN

Contracts: 0 kept, 2 broken.


----------------
Broken contracts
----------------

layer: analysis must not import infrastructure
----------------------------------------------

t1.a is not allowed to import t1.w:

-   t1.a -> t1.b (l.13)
    t1.b -> t1.c (l.245)
    t1.c -> t1.w (l.154, l.203)

-   t1.a -> t1.d (l.14, l.224)
    t1.d -> t1.w (l.234)


layer: benchmark must not import analysis
-----------------------------------------

t2.h is not allowed to import t1.a:

-   t2.h -> t1.n (l.51)
    t1.n -> t1.a (l.330)


layer: benchmark must not import infrastructure
-----------------------------------------------

t2.h is not allowed to import t1.w:

-   t2.h -> t1.n (l.51)
    t1.n -> t1.w (l.203)

-   t2.h -> t1.s (l.52)
    t1.s -> t1.w (l.234)

"""


def test_parse_broken_multi_chain_tsa_style():
    """多链真实输出：契约归属不污染、多头行号取首、同边跨契约不去重。"""
    violations = s7_enforce.parse_broken(CANNED_MULTI_CHAIN_BROKEN)
    assert violations == [
        {"contract": "layer: analysis must not import infrastructure", "importer": "t1.a", "imported": "t1.b", "line": 13},
        {"contract": "layer: analysis must not import infrastructure", "importer": "t1.a", "imported": "t1.d", "line": 14},
        {"contract": "layer: benchmark must not import analysis", "importer": "t2.h", "imported": "t1.n", "line": 51},
        {"contract": "layer: benchmark must not import infrastructure", "importer": "t2.h", "imported": "t1.n", "line": 51},
        {"contract": "layer: benchmark must not import infrastructure", "importer": "t2.h", "imported": "t1.s", "line": 52},
    ]


# ------------------------------------------------------------ run() mocked


def _mock_exec(rc, stdout, calls=None):
    def fake(bin_path, repo, config, timeout):
        if calls is not None:
            calls.append({"config": Path(config).name, "cwd": str(repo)})
        return rc, stdout

    return fake


def test_run_skipped_no_structure(tmp_path):
    repo = _mk_repo(tmp_path, structure=None)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "skipped"
    assert "no ingested structure" in out["reason"]


def test_run_skipped_not_active(tmp_path):
    repo = _mk_repo(tmp_path, status="stale")
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "skipped"
    assert "status=stale" in out["reason"]


def test_run_language_unsupported(tmp_path):
    repo = _mk_repo(tmp_path, structure=None)
    con = _con(repo)
    # .zig 仍不在支持清单（.py/.ts/.go/.rs 均已路由对应引擎）
    con.execute("UPDATE symbols SET file_path = 'src/main.zig'")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "language-unsupported"
    assert ".zig" in out["reason"]
    assert "dependency-cruiser" in out["reason"]  # 支持清单列四引擎（.rs 已路由 rs 引擎，此处用 .zig）


def test_run_enforcer_not_found(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: None)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "enforcer-not-found"
    assert "VRP_IMPORT_LINTER_BIN" in out["reason"]
    assert out["contracts"] == 1
    assert not (repo / ".vrp" / "importlinter.ini").exists()


def test_run_not_enforceable_single_layer(tmp_path):
    single = json.dumps(
        {
            "modules": [
                {"module_id": "m1", "name": "n", "responsibility": "r", "symbol_fids": ["f1"], "layer": "only"},
            ],
            "layers": [{"name": "only", "may_depend_on": []}],
        }
    )
    repo = _mk_repo(tmp_path, structure=single)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no derivable contracts" in out["reason"]


# 结构含不可执法容器（tsa 冒烟实证形态）：core 层合法包 + interface 层混入根级 .py
MIXED_STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "core-mod", "name": "核心", "responsibility": "算", "symbol_fids": ["f1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
            {"module_id": "helper-mod", "name": "助手", "responsibility": "辅", "symbol_fids": ["f3"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    },
    ensure_ascii=False,
)

# 全部容器不可执法：两层符号全落根级 .py / 连字符目录
ROOT_ONLY_STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "core-mod", "name": "核心", "responsibility": "算", "symbol_fids": ["f1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    },
    ensure_ascii=False,
)


def _add_symbol(repo: Path, fid: str, qualified: str, name: str, file_path: str):
    con = _con(repo)
    try:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            f" VALUES ('{fid}', 'p', '{qualified}', 'Function', '{name}', '{file_path}', 1, 2, 'h')"
        )
        con.commit()
    finally:
        con.close()


def test_run_partial_filter_mock(tmp_path, monkeypatch):
    """混入不可执法容器：契约保留、非法容器剔除并诚实上报。"""
    repo = _mk_repo(tmp_path, structure=MIXED_STRUCTURE)
    _add_symbol(repo, "f3", "root_helper.h", "h", "root_helper.py")
    (repo / "root_helper.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: "fake-lint-imports")
    monkeypatch.setattr(s7_enforce, "_execute", _mock_exec(0, ""))
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["contracts"] == 1
    assert out["unenforced_containers"] == ["root_helper"]


def test_run_not_enforceable_all_containers_unenforceable(tmp_path, monkeypatch):
    """全部容器不可执法：不再误报 enforcer-failed，诚实分类 not-enforceable。"""
    repo = _mk_repo(tmp_path, structure=ROOT_ONLY_STRUCTURE)
    # 覆盖 _mk_repo 默认符号路径：两层全部落根级 .py / 连字符目录
    con = _con(repo)
    try:
        con.execute("UPDATE symbols SET file_path = 'root_helper.py', qualified_name = 'root_helper.a' WHERE fid = 'f1'")
        con.execute("UPDATE symbols SET file_path = 'bench-agent/run.py', qualified_name = 'bench_agent.b' WHERE fid = 'f2'")
        con.commit()
    finally:
        con.close()
    (repo / "root_helper.py").write_text("", encoding="utf-8")
    (repo / "bench-agent").mkdir()
    (repo / "bench-agent" / "run.py").write_text("", encoding="utf-8")
    # 即使执法器存在也不应被调用——过滤后已无契约
    monkeypatch.setattr(
        s7_enforce, "find_import_linter", lambda: pytest.fail("must not reach enforcer")
    )
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "all containers unenforceable" in out["reason"]
    assert out["contracts"] == 0
    assert out["unenforced_containers"] == ["bench-agent.run", "root_helper"]


def test_run_violation_mock(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: "fake-lint-imports")
    monkeypatch.setattr(s7_enforce, "_execute", _mock_exec(1, CANNED_BROKEN, calls))
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["contracts"] == 1
    assert out["kept"] == 0
    assert out["broken"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert calls and calls[0]["config"] == "importlinter.ini"
    assert calls[0]["cwd"] == str(repo)
    assert not (repo / ".vrp" / "importlinter.ini").exists()  # 运行后清理
    row = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT pathology, kind, source, anchor_fid, file_path, start_line, end_line, payload_json"
        " FROM findings"
    ).fetchone()
    assert row[0] == "P9"
    assert row[1] == "layer-violation"
    assert row[2] == "s7-enforce"
    assert row[3] == "f1"  # a() 覆盖违例 import 行
    assert row[4] == "pkgcore/s0.py"
    assert row[5] == 2 and row[6] == 2
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "pkgcore.s0"
    assert payload["imported"] == "pkgiface.cli"
    assert payload["line"] == 2
    assert payload["structure_status"] == "active"


def test_run_idempotent_mock(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: "fake-lint-imports")
    monkeypatch.setattr(s7_enforce, "_execute", _mock_exec(1, CANNED_BROKEN))
    con = _con(repo)
    try:
        first = s7_enforce.run(repo, con)
        second = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all：不膨胀


def test_run_compliant_mock_clears_old_findings(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: "fake-lint-imports")
    monkeypatch.setattr(s7_enforce, "_execute", _mock_exec(0, "Contracts: 1 kept, 0 broken.\n"))
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    assert out["kept"] == 1 and out["broken"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清掉旧违例


def test_run_enforcer_failed_keeps_old_findings(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 'f1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: "fake-lint-imports")
    monkeypatch.setattr(s7_enforce, "_execute", _mock_exec(2, "usage error boom"))
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "rc=2" in out["reason"]
    assert "boom" in out["stdout_tail"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 执法器失败不误删既有记录


def test_run_timeout(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: "fake-lint-imports")
    monkeypatch.setattr(s7_enforce, "_execute", lambda bin_path, repo, config, timeout: None)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con, timeout=7)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "timed out after 7s" in out["reason"]
    assert not (repo / ".vrp" / "importlinter.ini").exists()


def test_execute_pins_pythonutf8_for_child(tmp_path, monkeypatch):
    """R6' agent 长跑实证（vrp 自仓）：中文 Windows 下 lint-imports 以 locale
    编码（GBK）读 UTF-8 的 importlinter.ini（契约名含中文）→ UnicodeDecodeError
    rc=1 且无 broken 块。child env 必须钉 PYTHONUTF8=1。"""
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "Contracts: 1 kept, 0 broken.\n"
        stderr = ""

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(s7_enforce.subprocess, "run", fake_run)
    rc, out = s7_enforce._execute("lint-imports", tmp_path, tmp_path / "importlinter.ini", 60)
    assert rc == 0
    assert "kept" in out
    assert captured["env"]["PYTHONUTF8"] == "1"
    # tsa 冒烟实证：click 非 tty 下 80 列折行 → parse_broken 漏解析；必须钉大 COLUMNS
    assert captured["env"]["COLUMNS"] == "100000"


def test_cli_enforce_wiring(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: "fake-lint-imports")
    monkeypatch.setattr(s7_enforce, "_execute", _mock_exec(1, CANNED_BROKEN))
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["findings_written"] == 1


def test_cli_enforce_timeout_flag_accepted(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    seen: list[int] = []

    def fake_exec(bin_path, repo, config, timeout):
        seen.append(timeout)
        return 0, "Contracts: 1 kept, 0 broken.\n"

    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: "fake-lint-imports")
    monkeypatch.setattr(s7_enforce, "_execute", fake_exec)
    rc = cli.main(["enforce", str(repo), "--timeout", "33"])
    assert rc == 0
    assert seen == [33]
