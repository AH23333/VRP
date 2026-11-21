"""T11（构想v12）：代码导览报告测试——阅读序/锚点/CLI/dogfood。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import cli, tour

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    con.close()
    return repo


def _add_symbol(repo: Path, fid: str, name: str, file_path: str) -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES (?, 'p', ?, 'Function', ?, ?, 1, 2, 'h')",
        (fid, name, name, file_path),
    )
    con.commit()
    con.close()


def _set_structure(repo: Path, payload: dict, status: str = "active") -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, ?, 't')",
        (json.dumps(payload, ensure_ascii=False), status),
    )
    con.commit()
    con.close()


def _mod(mid: str, name: str, resp: str, layer: str, fids: list[str]) -> dict:
    return {
        "module_id": mid, "name": name, "responsibility": resp,
        "symbol_fids": fids, "layer": layer,
    }


_PAYLOAD = {
    "modules": [
        _mod("cli", "接口层", "用户入口", "interface", ["f1", "f2"]),
        _mod("core", "核心", "领域逻辑", "core", ["f3", "f4", "f5"]),
        _mod("util", "工具", "小工具集", "core", ["f6"]),
    ],
    "layers": [
        {"name": "core", "may_depend_on": []},
        {"name": "interface", "may_depend_on": ["core"]},
    ],
}


def _seed(repo: Path) -> None:
    _add_symbol(repo, "f1", "main", "src/cli.py")
    _add_symbol(repo, "f2", "parse_args", "src/cli.py")
    _add_symbol(repo, "f3", "engine_a", "src/core.py")
    _add_symbol(repo, "f4", "engine_b", "src/core.py")
    _add_symbol(repo, "f5", "engine_c", "src/extra.py")
    _add_symbol(repo, "f6", "tiny", "src/util.py")
    _set_structure(repo, _PAYLOAD)


def test_tour_reading_order_layer_topo_then_size(tmp_path):
    """阅读序：core 层（被依赖地基）先；层内小模块先（教程由浅入深）。"""
    repo = _mk_repo(tmp_path)
    _seed(repo)
    result = tour.run_tour(repo)
    steps = [(t["module_id"], t["reading_step"]) for t in result["tour"]]
    # util (core, 1 符号) → core (core, 3 符号) → cli (interface)
    assert steps == [("util", 1), ("core", 2), ("cli", 3)]


def test_tour_module_fields_and_anchors(tmp_path):
    """每模块一句话职责（curated 单源直引）+符号数+主要文件+代表符号。"""
    repo = _mk_repo(tmp_path)
    _seed(repo)
    result = tour.run_tour(repo)
    by_id = {t["module_id"]: t for t in result["tour"]}
    cli = by_id["cli"]
    assert cli["name"] == "接口层" and cli["responsibility"] == "用户入口"
    assert cli["symbols"] == 2
    assert cli["top_files"] == [{"file": "src/cli.py", "count": 2}]
    core = by_id["core"]
    assert core["symbols"] == 3
    # 主要文件按符号数降序：core.py (2) 先于 extra.py (1)
    assert core["top_files"][0] == {"file": "src/core.py", "count": 2}
    assert set(core["representatives"]) == {"engine_a", "engine_b", "engine_c"}


def test_tour_layer_order_cycle_tail_keeps_rest(tmp_path):
    """层环：无环部分照常拓扑，环残余字典序尾附（防御呈现）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a", "a.py")
    _set_structure(repo, {
        "modules": [_mod("m1", "M1", "r", "l1", ["f1"])],
        "layers": [
            {"name": "l1", "may_depend_on": ["l2"]},
            {"name": "l2", "may_depend_on": ["l1"]},
        ],
    })
    result = tour.run_tour(repo)
    assert result["layers"] == ["l1", "l2"]  # 环残余按字典序
    assert result["tour"][0]["module_id"] == "m1"


def test_tour_active_preferred_over_latest(tmp_path):
    """active 载荷优先于最新非 active（curate 状态语义）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a", "a.py")
    _set_structure(repo, {
        "modules": [_mod("stale", "旧", "旧语义", "core", ["f1"])],
        "layers": [{"name": "core", "may_depend_on": []}],
    }, status="superseded")
    _set_structure(repo, {
        "modules": [_mod("fresh", "新", "新语义", "core", ["f1"])],
        "layers": [{"name": "core", "may_depend_on": []}],
    }, status="active")
    result = tour.run_tour(repo)
    assert result["tour"][0]["module_id"] == "fresh"


def test_tour_no_structure_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    with pytest.raises(SystemExit, match="run curate first"):
        tour.run_tour(repo)


def test_tour_empty_modules_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    _set_structure(repo, {"modules": [], "layers": []})
    with pytest.raises(SystemExit, match="no modules"):
        tour.run_tour(repo)


def test_tour_missing_facts_exit(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    with pytest.raises(SystemExit, match="facts.db missing"):
        tour.run_tour(repo)


def test_tour_deterministic_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    r1 = tour.run_tour(repo)
    r2 = tour.run_tour(repo)
    assert r1 == r2


def test_render_text_steps_and_responsibility(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    text = tour.render_text(tour.run_tour(repo))
    assert "## Step 1: 工具 (util) [core]" in text
    assert "小工具集" in text
    assert "src/util.py (1)" in text
    assert "representatives: tiny" in text


def test_cli_tour_end_to_end(tmp_path, capsys):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    rc = cli.main(["tour", str(repo), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["modules"] == 3
    assert [t["module_id"] for t in out["tour"]] == ["util", "core", "cli"]


def test_cli_parser_accepts_tour():
    parser = cli.build_parser()
    args = parser.parse_args(["tour", "some/repo", "--json"])
    assert args.repo == "some/repo" and args.json


ROOT = Path(__file__).resolve().parents[1]


def test_dogfood_vrp_repo_tour():
    """dogfood：自仓 curated structure 真跑——5 模块导览活样例。"""
    result = tour.run_tour(ROOT)
    assert result["modules"] >= 4
    steps = [t["reading_step"] for t in result["tour"]]
    assert steps == sorted(steps)
    for t in result["tour"]:
        assert t["responsibility"], f"module {t['module_id']} lacks curated responsibility"
        assert t["symbols"] >= 0


# ---------- #215 dogfood 考古演练修复：stale 回退披露+空骨架警告+标题 ----------


def test_tour_stale_fallback_disclosed(tmp_path):
    """stale 回退披露：非 active structure 被使用时 status 原样上浮，
    render 输出响亮 WARNING（rc 仍 0 报告面语义——谎言变披露的 caveat）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "a", "a.py")
    _set_structure(repo, {
        "modules": [_mod("m1", "M1", "r", "core", ["f1"])],
        "layers": [{"name": "core", "may_depend_on": []}],
    }, status="stale")
    result = tour.run_tour(repo)
    assert result["structure"]["status"] == "stale"
    assert result["structure"]["fids_total"] == 1
    assert result["structure"]["fids_resolved"] == 1
    text = tour.render_text(result)
    assert "WARNING: structure status=stale" in text
    assert "curate-auto" in text


def test_tour_all_dead_fids_skeleton_warning(tmp_path):
    """全空 fid 解析警告：structure fids 在当前符号表零存活（100% 陈旧
    dogfood 实测形态）→ render 输出 skeleton-only WARNING——"空曲线是
    说谎"红线修复（此前静默全 0 步骤 rc=0 零警告）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "live", "real", "a.py")
    _set_structure(repo, {
        "modules": [_mod("m1", "M1", "r", "core", ["dead1", "dead2", "dead3"])],
        "layers": [{"name": "core", "may_depend_on": []}],
    })
    result = tour.run_tour(repo)
    assert result["structure"]["fids_total"] == 3
    assert result["structure"]["fids_resolved"] == 0
    assert result["tour"][0]["symbols"] == 0
    text = tour.render_text(result)
    assert "WARNING: 0/3 structure symbol fids resolve" in text
    assert "skeleton only" in text


def test_tour_repo_path_resolved_title(tmp_path, monkeypatch):
    """repo 路径 resolve：`vrp tour .` 场景（相对路径）标题不再空置
    （Path(".").name="" 修复）——repo 字段上浮绝对路径。"""
    repo = _mk_repo(tmp_path)
    _seed(repo)
    monkeypatch.chdir(repo)
    result = tour.run_tour(Path("."))
    assert result["repo"] == str(repo)
    text = tour.render_text(result)
    assert text.startswith("# Code tour - repo")
