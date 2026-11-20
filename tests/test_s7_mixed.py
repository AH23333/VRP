"""S7 混仓分治执法（C7）单元 + mocked E2E（真实双引擎 dogfood 见 tests/deep/test_deep_s7_mixed.py）。

覆盖面（设计稿 §8）：写面并存回归（py+ts findings 共存）、replace=False
语义、status 聚合三态（ok/同态/partial）、跨语言边（确定性排序/cap/
多模块组合/非 structure 过滤/.h 歧义）、timeout_note、分治单次清场。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce, s7_enforce_ts

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "py-core", "name": "py核心", "responsibility": "算", "symbol_fids": ["p1"], "layer": "core"},
            {"module_id": "py-iface", "name": "py接口", "responsibility": "面", "symbol_fids": ["p2"], "layer": "interface"},
            {"module_id": "ts-core", "name": "ts核心", "responsibility": "算", "symbol_fids": ["t1"], "layer": "core"},
            {"module_id": "ts-iface", "name": "ts接口", "responsibility": "面", "symbol_fids": ["t2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    },
    ensure_ascii=False,
)

# py 侧两模块同层 → 无契约可派生（not-enforceable），ts 侧正常两层
STRUCTURE_PY_FLAT = json.dumps(
    {
        "modules": [
            {"module_id": "py-core", "name": "py核心", "responsibility": "算", "symbol_fids": ["p1"], "layer": "core"},
            {"module_id": "py-iface", "name": "py接口", "responsibility": "面", "symbol_fids": ["p2"], "layer": "core"},
            {"module_id": "ts-core", "name": "ts核心", "responsibility": "算", "symbol_fids": ["t1"], "layer": "core"},
            {"module_id": "ts-iface", "name": "ts接口", "responsibility": "面", "symbol_fids": ["t2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    },
    ensure_ascii=False,
)

# 全部模块单层 → 双语言均 not-enforceable（同态聚合分支）
STRUCTURE_SINGLE_LAYER = json.dumps(
    {
        "modules": [
            {"module_id": "py-core", "name": "py核心", "responsibility": "算", "symbol_fids": ["p1"], "layer": "core"},
            {"module_id": "py-iface", "name": "py接口", "responsibility": "面", "symbol_fids": ["p2"], "layer": "core"},
            {"module_id": "ts-core", "name": "ts核心", "responsibility": "算", "symbol_fids": ["t1"], "layer": "core"},
            {"module_id": "ts-iface", "name": "ts接口", "responsibility": "面", "symbol_fids": ["t2"], "layer": "core"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
        ],
    },
    ensure_ascii=False,
)

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

CANNED_TS_JSON = json.dumps(
    {
        "modules": [
            {
                "source": "src/core/a.ts",
                "dependencies": [
                    {
                        "resolved": "src/iface/b.ts",
                        "rules": [{"severity": "error", "name": "layer: core must not import interface"}],
                    }
                ],
            },
            {
                "source": "src/iface/b.ts",
                "dependencies": [
                    {
                        "resolved": "src/core/a.ts",
                        "rules": [{"severity": "warn", "name": "advice-only"}],
                    }
                ],
            },
        ],
        "summary": {"error": 1, "warn": 1, "totalCruised": 2},
    }
)


def _mk_repo(tmp_path: Path, structure: str = STRUCTURE) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('p1', 'p', 'pkgcore.s0.a', 'Function', 'a', 'pkgcore/s0.py', 1, 4, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('p2', 'p', 'pkgiface.cli.c', 'Function', 'c', 'pkgiface/cli.py', 1, 2, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('t1', 'p', 'src.core.a', 'Function', 'a', 'src/core/a.ts', 1, 3, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('t2', 'p', 'src.iface.b', 'Function', 'b', 'src/iface/b.ts', 1, 2, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (structure,),
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
    (repo / "src" / "core").mkdir(parents=True)
    (repo / "src" / "core" / "a.ts").write_text(
        'import { b } from "../iface/b";\nexport const a = b + 1;\n', encoding="utf-8"
    )
    (repo / "src" / "iface").mkdir(parents=True)
    (repo / "src" / "iface" / "b.ts").write_text("export const b = 41;\n", encoding="utf-8")
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


def _mock_py(monkeypatch, stdout=CANNED_BROKEN, rc=1, timeout=False):
    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: "fake-lint-imports")
    if timeout:
        monkeypatch.setattr(s7_enforce, "_execute", lambda bin_path, repo, config, timeout: None)
    else:
        monkeypatch.setattr(s7_enforce, "_execute", lambda bin_path, repo, config, timeout: (rc, stdout))


def _mock_ts(monkeypatch, stdout=CANNED_TS_JSON, rc=0, timeout=False):
    monkeypatch.setattr(s7_enforce_ts, "find_depcruise", lambda: Path("fake-depcruise.cmd"))
    if timeout:
        monkeypatch.setattr(
            s7_enforce_ts, "_execute", lambda bin_path, repo, config, targets, timeout: None
        )
    else:
        monkeypatch.setattr(
            s7_enforce_ts, "_execute", lambda bin_path, repo, config, targets, timeout: (rc, stdout, "")
        )


def _seed_stale(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO findings (pathology, kind, source, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', '\"stale\"')"
    )
    con.commit()


def _rows(con: sqlite3.Connection) -> list[tuple]:
    return con.execute(
        "SELECT pathology, kind, source, anchor_fid, file_path, start_line, payload_json"
        " FROM findings WHERE source = 's7-enforce'"
    ).fetchall()


def _edge(con: sqlite3.Connection, src: str, dst: str, type_: str = "CALLS") -> None:
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?, ?, ?, 'test')",
        (src, dst, type_),
    )
    con.commit()


def _fake_engine(status="ok", violations=None, findings_written=None, anchored=None, calls=None):
    """分治表驱动 dispatch 的直接单元测试用假引擎（签名 = run_X 尾参 replace）。"""

    def run_fn(repo, con, payload, structure_status, containers, timeout, replace):
        if calls is not None:
            calls.append({"replace": replace, "containers": containers, "timeout": timeout})
        out = {"step": "s7-enforce", "structure_status": structure_status, "status": status}
        if violations is not None:
            out["violations"] = violations
            out["findings_written"] = findings_written
            out["anchored"] = anchored
        return out

    return run_fn


def _boom(*args, **kwargs):
    raise AssertionError("timed-out engine must not run")


# ------------------------------------------------------------ mocked E2E


def test_mixed_py_ts_findings_coexist_mock(tmp_path, monkeypatch):
    """写面并存回归（核心交付）：py+ts 双违例同 run 各自落 findings，互不覆盖。"""
    repo = _mk_repo(tmp_path)
    _mock_py(monkeypatch)
    _mock_ts(monkeypatch)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con, timeout=121)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert list(out["languages"]) == ["py", "ts"]  # langs 固定序 = 呈现序
    py = out["languages"]["py"]
    ts = out["languages"]["ts"]
    assert py["status"] == "ok"
    assert (py["violations"], py["findings_written"], py["anchored"]) == (1, 1, 1)
    assert (py["kept"], py["broken"], py["contracts"]) == (0, 1, 1)
    assert py["enforcer"] == "fake-lint-imports"
    assert ts["status"] == "ok"
    assert (ts["violations"], ts["findings_written"], ts["anchored"]) == (1, 1, 1)
    assert ts["engine"] == "dependency-cruiser"
    assert (out["violations"], out["findings_written"], out["anchored"]) == (2, 2, 2)  # 顶层和（全语言报告）
    assert out["cross_language_edges"] == []
    assert out["cross_language_edge_groups_total"] == 0
    assert out["timeout_note"] == "per-engine timeout 121s each; mixed wall clock worst case 242s"
    assert not (repo / ".vrp" / "importlinter.ini").exists()
    assert not (repo / ".vrp" / "dependency-cruiser.cjs").exists()
    con = _con(repo)
    try:
        rows = _rows(con)
    finally:
        con.close()
    assert len(rows) == 2  # 多语言 findings 并存——旧"混仓 not-enforceable"缺口闭合
    assert {r[3] for r in rows} == {"p1", "t1"}
    assert {r[4] for r in rows} == {"pkgcore/s0.py", "src/core/a.ts"}


def test_mixed_single_purge_clears_stale_keeps_foreign(tmp_path, monkeypatch):
    """分治单次清场：陈旧 s7 findings 清除、异源 findings 不动、双语言新件并存。"""
    repo = _mk_repo(tmp_path)
    _mock_py(monkeypatch)
    _mock_ts(monkeypatch)
    con = _con(repo)
    try:
        _seed_stale(con)
        con.execute(
            "INSERT INTO findings (pathology, kind, source, payload_json)"
            " VALUES ('P3', 'other-kind', 's3-xref', '\"keep\"')"
        )
        con.commit()
        out = s7_enforce.run(repo, con, timeout=60)
    finally:
        con.close()
    assert out["status"] == "ok"
    con = _con(repo)
    try:
        s7 = _rows(con)
        foreign = con.execute(
            "SELECT COUNT(*) FROM findings WHERE source = 's3-xref'"
        ).fetchone()[0]
    finally:
        con.close()
    assert len(s7) == 2  # 陈旧件被单次清场清除，双语言新件并存
    assert all(json.loads(r[6]) != "stale" for r in s7)
    assert foreign == 1  # 异源 findings 不在清场范围


def test_mixed_idempotent_rerun(tmp_path, monkeypatch):
    """幂等：分治重跑 findings 不膨胀（单次清场 + 只插不删净效果 = replace-all）。"""
    repo = _mk_repo(tmp_path)
    _mock_py(monkeypatch)
    _mock_ts(monkeypatch)
    con = _con(repo)
    try:
        first = s7_enforce.run(repo, con, timeout=60)
        second = s7_enforce.run(repo, con, timeout=60)
        rows = _rows(con)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 2
    assert len(rows) == 2


def test_mixed_partial_when_py_not_enforceable(tmp_path, monkeypatch):
    """partial 聚合 + 顶层和省略：py 无契约（not-enforceable）时诚实下钻，不盲和。"""
    repo = _mk_repo(tmp_path, STRUCTURE_PY_FLAT)
    _mock_ts(monkeypatch)  # py not-enforceable 路径不触外部调用，无需 mock
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con, timeout=60)
        rows = _rows(con)
    finally:
        con.close()
    assert out["status"] == "partial"
    py = out["languages"]["py"]
    assert py["status"] == "not-enforceable"
    assert py["contracts"] == 0
    assert "no derivable contracts" in py["reason"]
    assert out["languages"]["ts"]["status"] == "ok"
    assert out["languages"]["ts"]["violations"] == 1
    assert "violations" not in out  # 严格 all-int 守卫：py 缺字段 → 顶层和省略
    assert "findings_written" not in out
    assert "anchored" not in out
    assert len(rows) == 1  # 只有 ts 落件


def test_mixed_all_not_enforceable_single_state(tmp_path):
    """同态聚合：双语言均 not-enforceable → status 取该态（非 partial）。"""
    repo = _mk_repo(tmp_path, STRUCTURE_SINGLE_LAYER)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con, timeout=60)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert {r["status"] for r in out["languages"].values()} == {"not-enforceable"}
    assert "violations" not in out


def test_mixed_all_enforcer_not_found_single_state(tmp_path, monkeypatch):
    """同态聚合：双语言均 enforcer-not-found → status 取该态。"""
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s7_enforce, "find_import_linter", lambda: None)
    monkeypatch.setattr(s7_enforce_ts, "find_depcruise", lambda: None)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con, timeout=60)
    finally:
        con.close()
    assert out["status"] == "enforcer-not-found"
    assert "lint-imports not found" in out["languages"]["py"]["reason"]
    assert "depcruise not found" in out["languages"]["ts"]["reason"]


def test_mixed_py_timeout_partial(tmp_path, monkeypatch):
    """per-engine timeout 全额：py 超时 → enforcer-failed，ts 照常 ok → partial。"""
    repo = _mk_repo(tmp_path)
    _mock_py(monkeypatch, timeout=True)
    _mock_ts(monkeypatch)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con, timeout=93)
        rows = _rows(con)
    finally:
        con.close()
    assert out["status"] == "partial"
    assert out["languages"]["py"]["status"] == "enforcer-failed"
    assert out["languages"]["py"]["reason"] == "lint-imports timed out after 93s"
    assert out["languages"]["ts"]["status"] == "ok"
    assert len(rows) == 1  # 只有 ts 落件


def test_mixed_ts_timeout_partial(tmp_path, monkeypatch):
    """ts 超时对称面：ts enforcer-failed，py 照常落件 → partial。"""
    repo = _mk_repo(tmp_path)
    _mock_py(monkeypatch)
    _mock_ts(monkeypatch, timeout=True)
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con, timeout=93)
        rows = _rows(con)
    finally:
        con.close()
    assert out["status"] == "partial"
    assert out["languages"]["ts"]["status"] == "enforcer-failed"
    assert out["languages"]["ts"]["reason"] == "depcruise timed out after 93s"
    assert out["languages"]["py"]["status"] == "ok"
    assert len(rows) == 1


# ------------------------------------------------- _run_mixed 直接单元


def _mk_bare_con(tmp_path: Path) -> sqlite3.Connection:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    return con


def test_run_mixed_dispatch_timeout_reason_guard(tmp_path):
    """dispatch 层超时预检：timed_out 引擎不执行，enforcer-failed 条目带 reason。"""
    con = _mk_bare_con(tmp_path)
    ts_calls: list[dict] = []
    engines = [
        ("rs", {"timed_out": True, "containers": {}}, "rust mod tree build exceeded 60s (partial results discarded)", "orphan_rs_files", _boom),
        ("ts", {"containers": {}}, None, None, _fake_engine(violations=1, findings_written=1, anchored=1, calls=ts_calls)),
    ]
    try:
        out = s7_enforce._run_mixed(tmp_path, con, {}, "active", ["rs", "ts"], {"containers": {}}, engines, 60)
    finally:
        con.close()
    assert out["status"] == "partial"
    assert out["languages"]["rs"] == {
        "step": "s7-enforce",
        "structure_status": "active",
        "status": "enforcer-failed",
        "reason": "rust mod tree build exceeded 60s (partial results discarded)",
    }
    assert out["languages"]["ts"]["status"] == "ok"
    assert ts_calls[0]["replace"] is False  # 分治只插不删直传验证


def test_run_mixed_orphan_merge(tmp_path):
    """orphan 合并：orphan_key 命中才透传（rs），orphan_key=None 不透传（r）。"""
    con = _mk_bare_con(tmp_path)
    engines = [
        ("rs", {"containers": {}, "orphan_files": ["orphan.rs"]}, None, "orphan_rs_files", _fake_engine()),
        ("r", {"containers": {}, "orphan_files": ["x.R"]}, "r scan exceeded 60s (partial results discarded)", None, _fake_engine()),
        ("ts", {"containers": {}}, None, None, _fake_engine()),
    ]
    try:
        out = s7_enforce._run_mixed(tmp_path, con, {}, "active", ["rs", "r", "ts"], {"containers": {}}, engines, 60)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["languages"]["rs"]["orphan_rs_files"] == ["orphan.rs"]
    assert "orphan_r_files" not in out["languages"]["r"]  # orphan_key=None 面不透传


def test_run_mixed_status_aggregation_three_states(tmp_path):
    """status 聚合三态：全 ok→ok；全同态→该态；否则 partial。"""
    con = _mk_bare_con(tmp_path)
    try:
        both_ok = s7_enforce._run_mixed(
            tmp_path, con, {}, "active", ["rs", "ts"], {"containers": {}},
            [
                ("rs", {"containers": {}}, None, None, _fake_engine()),
                ("ts", {"containers": {}}, None, None, _fake_engine()),
            ],
            60,
        )
        both_ne = s7_enforce._run_mixed(
            tmp_path, con, {}, "active", ["rs", "ts"], {"containers": {}},
            [
                ("rs", {"containers": {}}, None, None, _fake_engine(status="not-enforceable")),
                ("ts", {"containers": {}}, None, None, _fake_engine(status="not-enforceable")),
            ],
            60,
        )
        mixed_state = s7_enforce._run_mixed(
            tmp_path, con, {}, "active", ["rs", "ts"], {"containers": {}},
            [
                ("rs", {"containers": {}}, None, None, _fake_engine()),
                ("ts", {"containers": {}}, None, None, _fake_engine(status="not-enforceable")),
            ],
            60,
        )
    finally:
        con.close()
    assert both_ok["status"] == "ok"
    assert both_ne["status"] == "not-enforceable"  # 全同态→该态，非 partial
    assert mixed_state["status"] == "partial"


def test_run_mixed_conditional_top_sums(tmp_path):
    """顶层和守卫：全语言报告 int 才给顶层和；任一缺失 → 省略（诚实下钻）。"""
    con = _mk_bare_con(tmp_path)
    reporting = _fake_engine(violations=1, findings_written=1, anchored=1)
    silent = _fake_engine(status="not-enforceable")
    try:
        full = s7_enforce._run_mixed(
            tmp_path, con, {}, "active", ["rs", "ts"], {"containers": {}},
            [("rs", {"containers": {}}, None, None, reporting), ("ts", {"containers": {}}, None, None, reporting)],
            60,
        )
        partial_report = s7_enforce._run_mixed(
            tmp_path, con, {}, "active", ["rs", "ts"], {"containers": {}},
            [("rs", {"containers": {}}, None, None, reporting), ("ts", {"containers": {}}, None, None, silent)],
            60,
        )
    finally:
        con.close()
    assert (full["violations"], full["findings_written"], full["anchored"]) == (2, 2, 2)
    for key in ("violations", "findings_written", "anchored"):
        assert key not in partial_report


def test_run_mixed_cross_edge_cap_100_of_150(tmp_path):
    """跨语言边 top 100 cap + 诚实总组数 + 确定性排序（键升序）。"""
    con = _mk_bare_con(tmp_path)
    modules = []
    for i in range(150):
        modules.append(
            {"module_id": f"mod-py-{i:03d}", "name": f"p{i}", "responsibility": "r", "symbol_fids": [f"sp{i:03d}"], "layer": "core"}
        )
        modules.append(
            {"module_id": f"mod-ts-{i:03d}", "name": f"s{i}", "responsibility": "r", "symbol_fids": [f"st{i:03d}"], "layer": "interface"}
        )
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?, 'p', ?, 'Function', ?, ?, 1, 2, 'h')",
            (f"sp{i:03d}", f"p{i}.a", "a", f"p{i:03d}/a.py"),
        )
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?, 'p', ?, 'Function', ?, ?, 1, 2, 'h')",
            (f"st{i:03d}", f"s{i}.b", "b", f"s{i:03d}/b.ts"),
        )
        con.execute(
            "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?, ?, 'CALLS', 'test')",
            (f"sp{i:03d}", f"st{i:03d}"),
        )
    con.commit()
    payload = {"modules": modules, "layers": [{"name": "core", "may_depend_on": []}, {"name": "interface", "may_depend_on": ["core"]}]}
    assert s7_enforce._CROSS_EDGE_CAP == 100  # cap 常量锚
    try:
        out = s7_enforce._run_mixed(
            tmp_path, con, payload, "active", ["py", "ts"], {"containers": {}},
            [("ts", {"containers": {}}, None, None, _fake_engine())],
            60,
        )
    finally:
        con.close()
    assert out["cross_language_edge_groups_total"] == 150  # 诚实总组数
    assert len(out["cross_language_edges"]) == 100  # top 100 cap
    assert out["cross_language_edges"][0]["src_module"] == "mod-py-000"  # count 并列 → 键升序


# --------------------------------------------- _cross_language_edges 单元


def test_cross_edges_basic_grouping(tmp_path):
    """基础分组：py→ts 边聚合为 (src_mod, dst_mod, src_lang, dst_lang) 组。"""
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    try:
        _edge(con, "p1", "t1", "CALLS")
        out = s7_enforce._cross_language_edges(con, json.loads(STRUCTURE), ["py", "ts"])
    finally:
        con.close()
    assert out["total"] == 1
    (group,) = out["groups"]
    assert group["src_module"] == "py-core"
    assert group["dst_module"] == "ts-core"
    assert group["src_lang"] == "py"
    assert group["dst_lang"] == "ts"
    assert group["count"] == 1
    assert group["types"] == {"CALLS": 1}
    assert group["sample_edge"] == {"src_fid": "p1", "dst_fid": "t1", "type": "CALLS"}


def test_cross_edges_filter_same_lang_unowned_missing(tmp_path):
    """过滤面：同语言边/未属模块 fid/无 symbols 行 fid 均不收。"""
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    try:
        _edge(con, "p1", "p2", "CALLS")  # 同语言（py→py）
        _edge(con, "t1", "ghost-fid", "CALLS")  # dst 不在 structure 模块
        payload = json.loads(STRUCTURE)
        payload["modules"].append(
            {"module_id": "py-ghost", "name": "幽灵", "responsibility": "r", "symbol_fids": ["g1"], "layer": "core"}
        )
        _edge(con, "g1", "t1", "CALLS")  # src 在模块但无 symbols 行 → 无语言可判
        out = s7_enforce._cross_language_edges(con, payload, ["py", "ts"])
    finally:
        con.close()
    assert out == {"groups": [], "total": 0}


def test_cross_edges_multi_module_fid_combos(tmp_path):
    """一 fid 属多模块 → 模块对全组合上报（两组）。"""
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    try:
        payload = json.loads(STRUCTURE)
        payload["modules"].append(
            {"module_id": "py-extra", "name": "py附加", "responsibility": "r", "symbol_fids": ["p1"], "layer": "core"}
        )
        _edge(con, "p1", "t1", "CALLS")
        out = s7_enforce._cross_language_edges(con, payload, ["py", "ts"])
    finally:
        con.close()
    assert out["total"] == 2
    assert {(g["src_module"], g["dst_module"]) for g in out["groups"]} == {("py-core", "ts-core"), ("py-extra", "ts-core")}


def test_cross_edges_h_ambiguity(tmp_path):
    """.h 歧义：c+objc 并存双命中跳过不猜；仅 objc 在 langs → .h 归 objc。"""
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    try:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES ('h1', 'p', 'util.h', 'Header', 'util', 'util.h', 1, 1, 'h')"
        )
        con.commit()
        _edge(con, "h1", "p9f", "INCLUDE")
        payload = {
            "modules": [
                {"module_id": "mod-objc", "name": "objc", "responsibility": "r", "symbol_fids": ["h1"], "layer": "core"},
                {"module_id": "mod-py", "name": "py", "responsibility": "r", "symbol_fids": ["p9f"], "layer": "interface"},
            ],
            "layers": [{"name": "core", "may_depend_on": []}, {"name": "interface", "may_depend_on": ["core"]}],
        }
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES ('p9f', 'p', 'app.main', 'Function', 'main', 'app/main.py', 1, 2, 'h')"
        )
        con.commit()
        both = s7_enforce._cross_language_edges(con, payload, ["c", "objc", "py"])
        objc_only = s7_enforce._cross_language_edges(con, payload, ["objc", "py"])
    finally:
        con.close()
    assert both == {"groups": [], "total": 0}  # .h 双命中 = 歧义，跳过
    assert objc_only["total"] == 1  # 仅 objc → .h 归 objc，跨语言边入组
    assert objc_only["groups"][0]["src_lang"] == "objc"
    assert objc_only["groups"][0]["dst_lang"] == "py"


def test_cross_edges_empty_structure(tmp_path):
    """structure 无模块 → 早退空面。"""
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    try:
        out = s7_enforce._cross_language_edges(con, {}, ["py", "ts"])
    finally:
        con.close()
    assert out == {"groups": [], "total": 0}


# ------------------------------------------------- replace 参数语义直测


def test_run_ts_replace_false_keeps_stale(tmp_path, monkeypatch):
    """replace=False：只插不删（陈旧件保留）；默认 replace=True：清场重写。"""
    repo = _mk_repo(tmp_path)
    _mock_ts(monkeypatch)
    payload = json.loads(STRUCTURE)
    containers = s7_enforce_ts.derive_ts_containers(_con(repo), payload)["containers"]
    con = _con(repo)
    try:
        _seed_stale(con)
        kept = s7_enforce_ts.run_ts(repo, con, payload, "active", containers, 60, replace=False)
        assert (kept["violations"], kept["findings_written"]) == (1, 1)
        assert len(_rows(con)) == 2  # 陈旧 + 新件
        purged = s7_enforce_ts.run_ts(repo, con, payload, "active", containers, 60)  # 默认 replace=True
        assert (purged["violations"], purged["findings_written"]) == (1, 1)
        assert len(_rows(con)) == 1  # 单语言 replace-all 语义不漂移
    finally:
        con.close()


def test_run_py_replace_false_keeps_stale(tmp_path, monkeypatch):
    """py 路径 replace 直测：_run_py(replace=False) 只插不删。"""
    repo = _mk_repo(tmp_path)
    _mock_py(monkeypatch)
    payload = json.loads(STRUCTURE)
    derived = s7_enforce.derive_containers(_con(repo), payload)
    con = _con(repo)
    try:
        _seed_stale(con)
        out = s7_enforce._run_py(repo, con, payload, "active", derived, 60, replace=False)
        assert (out["violations"], out["findings_written"], out["anchored"]) == (1, 1, 1)
        assert len(_rows(con)) == 2
    finally:
        con.close()
