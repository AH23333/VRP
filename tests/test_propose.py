"""#143 B 件测试：重组切片提议（学 unwind slices propose）。"""

import json
import sqlite3
from pathlib import Path

from vrp import cli, curate, propose

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    con.close()
    return repo


def _add_symbol(repo: Path, fid: str, qn: str, name: str, file_path: str, line: int,
                label: str = "Function") -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES (?, 'p', ?, ?, ?, ?, ?, ?, 'h')",
        (fid, qn, label, name, file_path, line, line + 2),
    )
    con.commit()
    con.close()


def _add_edge(repo: Path, src: str, dst: str, etype: str = "CALLS") -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?, ?, ?, 'structural')",
        (src, dst, etype),
    )
    con.commit()
    con.close()


def test_propose_seeds_top_dirs_and_merges_by_weight(tmp_path):
    """顶层目录播种 + 边权贪心凝聚到目标数（同权字典序）。"""
    repo = _mk_repo(tmp_path)
    for d, n in (("alpha", 2), ("beta", 2), ("gamma", 2)):
        for i in range(n):
            _add_symbol(repo, f"{d}{i}", f"{d}.m{i}", f"m{i}", f"{d}/x.py", i)
    _add_edge(repo, "alpha0", "beta0", "CALLS")
    _add_edge(repo, "beta0", "alpha1", "CALL_REFERENCE")  # alpha<->beta 权 2
    _add_edge(repo, "gamma0", "alpha0", "CALLS")          # alpha<->gamma 权 1
    out = propose.propose_structure(repo, target_modules=2, balance_cap=1.0)
    assert out["ok"] and out["candidates"] == 6
    assert out["reached_target"] is True
    assert out["module_sizes"] == {"alpha": 4, "gamma": 2}  # beta 并入 alpha（权 2 优先）
    assert out["merges"] == [{"kept": "alpha", "merged": "beta", "weight": 2}]
    payload = json.loads(Path(out["proposal"]).read_text(encoding="utf-8"))
    assert [m["module_id"] for m in payload["modules"]] == ["alpha", "gamma"]
    assert payload["modules"][0]["symbol_fids"] == ["alpha0", "alpha1", "beta0", "beta1"]
    assert payload["layers"] == [{"name": "proposed", "may_depend_on": []}]


def test_propose_balance_cap_blocks_oversized_merge(tmp_path):
    """平衡帽：合并后超帽禁并——诚实少于目标数。"""
    repo = _mk_repo(tmp_path)
    for d, n in (("big", 7), ("small", 3)):
        for i in range(n):
            _add_symbol(repo, f"{d}{i}", f"{d}.m{i}", f"m{i}", f"{d}/x.py", i)
    for i in range(7):
        _add_edge(repo, f"big{i}", "small0")  # 高亲和
    out = propose.propose_structure(repo, target_modules=1, balance_cap=0.6)
    # 7+3=10 > 6（0.6*10）——禁并；单模块目标不可达，诚实两模块
    assert out["ok"] and out["reached_target"] is False
    assert out["module_sizes"] == {"big": 7, "small": 3}
    assert out["merges"] == []


def test_propose_zero_weight_no_merge(tmp_path):
    """零耦合种子不合并（合并无亲和无意义——诚实保留目录粒度）。"""
    repo = _mk_repo(tmp_path)
    for d in ("d1", "d2", "d3"):
        _add_symbol(repo, f"{d}0", f"{d}.m", "m", f"{d}/x.py", 1)
    out = propose.propose_structure(repo, target_modules=1)
    assert out["reached_target"] is False
    assert out["module_sizes"] == {"d1": 1, "d2": 1, "d3": 1}


def test_propose_pseudo_symbols_excluded(tmp_path):
    """File/Module 伪符号不入候选（auto_curate grounding 同口径）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "real", "pkg.m", "m", "pkg/x.py", 1)
    _add_symbol(repo, "pf", "pkg.file", "file", "pkg/y.py", 1, label="File")
    _add_symbol(repo, "pm", "pkg.mod", "mod", "pkg/z.py", 1, label="Module")
    _add_symbol(repo, "synth", "builtins.s", "s", "<python-builtins>", 1)  # 尖括号排除
    out = propose.propose_structure(repo)
    assert out["candidates"] == 1
    payload = json.loads(Path(out["proposal"]).read_text(encoding="utf-8"))
    assert payload["modules"][0]["symbol_fids"] == ["real"]


def test_propose_oversized_seed_subdir_split(tmp_path):
    """超帽单目录按子目录切分（一层）再参与合并。"""
    repo = _mk_repo(tmp_path)
    for i in range(8):
        _add_symbol(repo, f"a{i}", f"pkg.a{i}", f"a{i}", "big/sub/x.py", i)
    for i in range(2):
        _add_symbol(repo, f"b{i}", f"pkg.b{i}", f"b{i}", "big/direct.py", i)
    # big 10 个 > cap 6（0.6*10）→ 切 big/sub(8) + big/(direct)(2)
    # big/sub 8 仍超帽——如实保留（一层切分），direct 侧独立成模块
    out = propose.propose_structure(repo, target_modules=8)
    assert out["module_sizes"] == {"big/(direct)": 2, "big/sub": 8}


def test_propose_root_files_seed_root(tmp_path):
    """根级文件归 (root) 种子。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "r1", "top.a", "a", "top.py", 1)
    _add_symbol(repo, "p1", "pkg.b", "b", "pkg/x.py", 1)
    out = propose.propose_structure(repo)
    assert out["module_sizes"] == {"(root)": 1, "pkg": 1}


def test_propose_output_passes_curate_structure_validation(tmp_path):
    """提议草案能过 curate 结构验证器（dry-run 零 errors——采纳路径畅通）。"""
    repo = _mk_repo(tmp_path)
    for d in ("alpha", "beta"):
        for i in range(2):
            _add_symbol(repo, f"{d}{i}", f"{d}.m{i}", f"m{i}", f"{d}/x.py", i)
    _add_edge(repo, "alpha0", "beta0")
    out = propose.propose_structure(repo, target_modules=1)
    payload = json.loads(Path(out["proposal"]).read_text(encoding="utf-8"))
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        errors = curate._validate_structure(con, payload)
    finally:
        con.close()
    assert errors == []


def test_propose_deterministic_bytes(tmp_path):
    """同 facts.db 两提议字节级全等（无时间戳，全排序）。"""
    repo = _mk_repo(tmp_path)
    for d in ("alpha", "beta", "gamma"):
        for i in range(3):
            _add_symbol(repo, f"{d}{i}", f"{d}.m{i}", f"m{i}", f"{d}/x.py", i)
    _add_edge(repo, "alpha0", "beta0")
    _add_edge(repo, "beta1", "gamma2")
    out1 = propose.propose_structure(repo, target_modules=2)
    p1 = Path(out1["proposal"]).read_bytes()
    out2 = propose.propose_structure(repo, target_modules=2)
    assert Path(out2["proposal"]).read_bytes() == p1
    assert out1["merges"] == out2["merges"]


def test_propose_out_override_and_writes_outside_ai(tmp_path):
    """--out 覆盖；默认写 .vrp/proposals/ 不写 .vrp/ai/（curate 全收面隔离）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a", "a", "pkg/x.py", 1)
    out = propose.propose_structure(repo)
    p = Path(out["proposal"])
    assert p == repo / ".vrp" / "proposals" / "structure-proposal.json"
    assert not (repo / ".vrp" / "ai").exists()
    custom = tmp_path / "custom" / "s.json"
    out2 = propose.propose_structure(repo, out=custom)
    assert Path(out2["proposal"]) == custom


def test_propose_empty_candidates_honest(tmp_path):
    """零候选诚实 ok=False（CLI rc=1 消费）。"""
    repo = _mk_repo(tmp_path)
    out = propose.propose_structure(repo)
    assert out["ok"] is False
    assert "no candidate symbols" in out["reason"]


def test_propose_invalid_params_rejected(tmp_path):
    """target<1 / balance_cap 越界 → SystemExit。"""
    repo = _mk_repo(tmp_path)
    for kwargs in ({"target_modules": 0}, {"balance_cap": 0.0}, {"balance_cap": 1.5}):
        try:
            propose.propose_structure(repo, **kwargs)
            raise AssertionError("invalid params must raise")
        except SystemExit:
            pass


def test_propose_cli_wiring(tmp_path, capsys):
    """CLI vrp propose 接线：默认参数 + rc 语义（ok=False → rc=1）。"""
    repo = _mk_repo(tmp_path)
    argv = cli.build_parser().parse_args(["propose", str(repo)])
    assert argv.func is cli.cmd_propose
    assert argv.func(argv) == 1  # 空仓零候选诚实 rc=1
    assert "no candidate symbols" in capsys.readouterr().out
    _add_symbol(repo, "f1", "pkg.a", "a", "pkg/x.py", 1)
    argv = cli.build_parser().parse_args(["propose", str(repo)])
    assert argv.func(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
