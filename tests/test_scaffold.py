"""D10（构想v10）：strangler scaffold 生成器测试——骨架/shim/迁移序/CLI。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import cli, scaffold

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    con.close()
    return repo


def _add_symbol(repo: Path, fid: str, name: str, file_path: str,
                label: str = "Function") -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES (?, 'p', ?, ?, ?, ?, 1, 2, 'h')",
        (fid, name, label, name, file_path),
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


def _mk_proposal(repo: Path, modules: list[dict]) -> Path:
    p = repo / ".vrp" / "proposals" / "structure-proposal.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"modules": modules, "layers": []}, ensure_ascii=False), encoding="utf-8")
    return p


def _mod(mid: str, fids: list[str]) -> dict:
    return {
        "module_id": mid,
        "name": f"[PROPOSED] {mid}",
        "responsibility": f"[PROPOSED] seed: {mid}",
        "symbol_fids": fids,
        "layer": "proposed",
    }


def test_scaffold_skeleton_shim_migration_md(tmp_path):
    """三件产物：__init__.py 骨架（TODO 带符号清单）+ shim 模板 + MIGRATION.md。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "alpha", "src/a.py")
    _add_symbol(repo, "f2", "Beta", "src/a.py", label="Class")
    _add_symbol(repo, "f3", "gamma", "src/b.py")
    _add_edge(repo, "f3", "f1")  # src -> util 跨模块边
    _mk_proposal(repo, [
        _mod("util", ["f1", "f2"]),
        _mod("src", ["f3"]),
    ])
    result = scaffold.run_scaffold(repo)
    out = Path(result["out"])
    init = (out / "util" / "__init__.py").read_text(encoding="utf-8")
    assert "[PROPOSED] util" in init
    assert "- alpha (Function) <- src/a.py" in init
    assert "- Beta (Class) <- src/a.py" in init
    shim = (out / "shims" / "src_a.py__to__util.py").read_text(encoding="utf-8")
    assert "from scaffold.util import" in shim
    assert "alpha" in shim and "Beta" in shim
    md = (out / "MIGRATION.md").read_text(encoding="utf-8")
    assert "util" in md and "src" in md
    assert result["modules"] == 2 and result["skeletons"] == 2 and result["shims"] == 2
    # util: src/a.py 一对（alpha+Beta 合一）；src: src/b.py 一对（gamma）
    assert (out / "shims" / "src_b.py__to__src.py").exists()


def test_scaffold_migration_order_edge_before_core(tmp_path):
    """Fowler 先边缘后核心：跨界边最少的模块排最前（平局 module_id 字典序）。"""
    repo = _mk_repo(tmp_path)
    for i in range(3):
        _add_symbol(repo, f"c{i}", f"c{i}", f"core/mod{i}.py")
    for i in range(2):
        _add_symbol(repo, f"e{i}", f"e{i}", f"edge/mod{i}.py")
    # core 三模块互相密集跨界；edge 只有 1 条跨界边
    _add_edge(repo, "c0", "c1")
    _add_edge(repo, "c1", "c2")
    _add_edge(repo, "c0", "c2")
    _add_edge(repo, "e0", "e1")
    _add_edge(repo, "c0", "e0")
    _mk_proposal(repo, [
        _mod("core/mod0", ["c0"]),
        _mod("core/mod1", ["c1"]),
        _mod("core/mod2", ["c2"]),
        _mod("edge/mod0", ["e0"]),
        _mod("edge/mod1", ["e1"]),
    ])
    result = scaffold.run_scaffold(repo)
    order = [m["module_id"] for m in result["migration_order"]]
    # edge/mod1: 1 边（e0->e1 计两端）；core/mod1 与 core/mod2: 各 2；edge/mod0: 2；
    # core/mod0: 4。边缘 1 边最先。
    assert order[0] == "edge/mod1"
    assert order[-1] == "core/mod0"


def test_scaffold_method_not_shimmable_but_in_todo(tmp_path):
    """Method 随 Class 走不可独立导出——shim 排除但 TODO 诚实列出。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "m1", "run", "svc/x.py", label="Method")
    _mk_proposal(repo, [_mod("svc", ["m1"])])
    result = scaffold.run_scaffold(repo)
    out = Path(result["out"])
    init = (out / "svc" / "__init__.py").read_text(encoding="utf-8")
    assert "- run (Method) <- svc/x.py" in init
    assert result["shims"] == 0
    assert not (out / "shims" / "svc_x.py__to__svc.py").exists()


def test_scaffold_safe_id_sanitizes(tmp_path):
    """module_id 含 / 与括号 → Python 包名安全形。"""
    assert scaffold._safe_id("src/utils") == "src_utils"
    assert scaffold._safe_id("(root)") == "root"
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "alpha", "src/utils/a.py")
    _mk_proposal(repo, [_mod("(root)", ["f1"])])
    result = scaffold.run_scaffold(repo)
    assert (Path(result["out"]) / "root" / "__init__.py").exists()


def test_scaffold_same_old_file_two_modules_two_shims(tmp_path):
    """同旧文件被两模块认领=每对一 shim（不合并跨模块导入）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "alpha", "lib/shared.py")
    _add_symbol(repo, "f2", "beta", "lib/shared.py")
    _mk_proposal(repo, [_mod("m1", ["f1"]), _mod("m2", ["f2"])])
    result = scaffold.run_scaffold(repo)
    out = Path(result["out"])
    assert (out / "shims" / "lib_shared.py__to__m1.py").exists()
    assert (out / "shims" / "lib_shared.py__to__m2.py").exists()
    assert result["shims"] == 2


def test_scaffold_missing_proposal_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    with pytest.raises(SystemExit, match="run 'vrp propose' first"):
        scaffold.run_scaffold(repo)


def test_scaffold_missing_facts_exit(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    _mk_proposal(repo, [_mod("a", ["f1"])])  # proposal 合法但无 facts.db
    with pytest.raises(SystemExit, match="facts.db missing"):
        scaffold.run_scaffold(repo)


def test_scaffold_bad_proposal_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    p = repo / ".vrp" / "proposals" / "structure-proposal.json"
    p.parent.mkdir(parents=True)
    p.write_text("{broken", encoding="utf-8")
    with pytest.raises(SystemExit, match="cannot load proposal"):
        scaffold.run_scaffold(repo)


def test_scaffold_empty_modules_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    _mk_proposal(repo, [])
    with pytest.raises(SystemExit, match="no modules"):
        scaffold.run_scaffold(repo)


def test_scaffold_deterministic_rerun(tmp_path):
    """同 proposal+facts.db 重跑字节级同产物（无时间戳）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "alpha", "a/x.py")
    _add_symbol(repo, "f2", "beta", "a/y.py")
    _add_edge(repo, "f1", "f2")
    _mk_proposal(repo, [_mod("a", ["f1"]), _mod("b", ["f2"])])
    r1 = scaffold.run_scaffold(repo)
    snap1 = {p.name: p.read_bytes() for p in Path(r1["out"]).rglob("*") if p.is_file()}
    r2 = scaffold.run_scaffold(repo)
    snap2 = {p.name: p.read_bytes() for p in Path(r2["out"]).rglob("*") if p.is_file()}
    assert snap1 == snap2


def test_scaffold_custom_out_and_proposal(tmp_path):
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "alpha", "a/x.py")
    alt = tmp_path / "alt-proposal.json"
    alt.write_text(json.dumps({"modules": [_mod("a", ["f1"])]}), encoding="utf-8")
    out = tmp_path / "stage"
    result = scaffold.run_scaffold(repo, proposal=alt, out=out)
    assert str(out) in result["out"]
    assert (out / "a" / "__init__.py").exists()


def test_render_text_shape(tmp_path):
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "alpha", "a/x.py")
    _mk_proposal(repo, [_mod("a", ["f1"])])
    text = scaffold.render_text(scaffold.run_scaffold(repo))
    assert "migration order (edge -> core):" in text
    assert "a" in text


def test_cli_scaffold_end_to_end(tmp_path, capsys):
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "alpha", "a/x.py")
    _mk_proposal(repo, [_mod("a", ["f1"])])
    rc = cli.main(["scaffold", str(repo), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["modules"] == 1
    assert out["skeletons"] == 1
    assert out["shims"] == 1


def test_cli_parser_accepts_scaffold():
    parser = cli.build_parser()
    args = parser.parse_args(["scaffold", "some/repo", "--json"])
    assert args.repo == "some/repo" and args.json
