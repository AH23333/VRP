"""A1：架构腐化时间线——采样回放/链式增量/度量/清理/错误契约。

核心对拍（设计稿 §7，J8 D6 同构）：链式推进 vs 逐样本独立全量 ETL
度量全等。CBM 以假库模拟（test_etl_incremental._mk_cbm 同构）：
per-sample 扫描 worktree 真实树重建 CBM 形态 db（含 CALLS 边），
run_etl 走真实全量/增量代码路径——假的是 CBM 子进程，链与度量是真的。
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
from pathlib import Path

import pytest

from vrp import cli, s1_cbm, timeline
from vrp import etl as etl_mod
from vrp import procs

# ---------- git fixture（test_cochange 先例） ----------


def _git(repo: Path, *args: str, env: dict | None = None) -> dict:
    res = procs.run_tree(["git", *args], cwd=repo, timeout=60, env=env)
    assert res["returncode"] == 0, f"git {' '.join(args)}: {res['stderr']}"
    return res


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    return path


def _commit(repo: Path, files: dict[str, str], msg: str, date: str) -> None:
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    _git(repo, "commit", "-q", "-m", msg, env=env)


# ---------- 语料：4 commit 演化史（c2 使 a/b 成环；c3/c4 引入死代码） ----------

A1 = "def fa():\n    return 1\n"
B1 = "def fb():\n    return fa()\n"
A2 = "def fa():\n    return fb()\n"
C1 = "def fc():\n    return 99\n"
D1 = "def fd():\n    return fa()\n"

DATES = [f"2026-01-0{i}T10:00:00+08:00" for i in range(1, 5)]


# ---------- fake CBM（test_etl_incremental._mk_cbm 同构 + 树扫描重建） ----------


def _mk_cbm(db: Path, nodes: list, edges: list, hashes: list, coverage: list, uid="uid-tl", gen="1") -> None:
    db = Path(db)
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO store_meta VALUES (?,?)", [("db_uid", uid), ("mutation_gen", gen)])
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, properties TEXT)"
    )
    con.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)", nodes)
    con.execute("CREATE TABLE edges (source_id INT, target_id INT, type TEXT, properties TEXT, project TEXT)")
    con.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", edges)
    con.execute("CREATE TABLE index_coverage (project TEXT, rel_path TEXT, kind TEXT, detail TEXT)")
    con.executemany("INSERT INTO index_coverage VALUES (?,?,?,?)", coverage)
    con.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT)")
    con.executemany("INSERT INTO file_hashes VALUES (?,?,?)", hashes)
    con.commit()
    con.close()


def _mk_cbm_from_tree(worktree: Path, name: str, cbm_path: Path) -> None:
    """扫描 worktree 当前树 → CBM 形态 db（Function 节点 + CALLS 边）。

    名字注册表全域唯一（fixture 函数名不撞）；确定性排序落行。
    """
    nodes: list = []
    hashes: list = []
    coverage: list = []
    registry: dict[str, tuple[int, object]] = {}
    nid = 0
    for py in sorted(worktree.rglob("*.py")):
        rel = py.relative_to(worktree).as_posix()
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            coverage.append((name, rel, "gap", "syntax error"))
            continue
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nid += 1
                nodes.append((nid, name, "Function", fn.name, f"{rel}::{fn.name}", rel, fn.lineno, fn.end_lineno, "{}"))
                registry[fn.name] = (nid, fn)
        hashes.append((name, rel, "h"))
        coverage.append((name, rel, "ok", ""))
    raw_edges: list = []
    for fname in sorted(registry):
        fid, fn = registry[fname]
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                tgt = registry.get(call.func.id)
                if tgt is not None and tgt[0] != fid:
                    raw_edges.append((fid, tgt[0], "CALLS", "{}", name))
    _mk_cbm(cbm_path, nodes, sorted(set(raw_edges)), hashes, coverage)


def _patch_fake_cbm(monkeypatch, tmp_path: Path, repo: Path) -> dict:
    tl_name = f"vrp-timeline-{repo.name}"
    cbm_path = tmp_path / f"{tl_name}.db"

    def _fake_index(worktree, name, reindex=False, timeout=3600):
        _mk_cbm_from_tree(Path(worktree), name, cbm_path)
        return {"project": name, "db": str(cbm_path), "status": "indexed"}

    monkeypatch.setattr(s1_cbm, "index_repository", _fake_index)
    monkeypatch.setattr(s1_cbm, "cbm_db_path", lambda name, repo=None: cbm_path)
    monkeypatch.setattr(s1_cbm, "cbm_version", lambda timeout=30: "t-1")
    return {"tl_name": tl_name, "cbm_path": cbm_path}


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, {"a.py": A1, "b.py": B1}, "c1", DATES[0])
    _commit(repo, {"a.py": A2}, "c2", DATES[1])
    _commit(repo, {"c.py": C1}, "c3", DATES[2])
    _commit(repo, {"d.py": D1}, "c4", DATES[3])
    fake = _patch_fake_cbm(monkeypatch, tmp_path, repo)
    return {"repo": repo, **fake}


def _worktree_entries(repo: Path) -> list[str]:
    res = _git(repo, "worktree", "list", "--porcelain")
    return [ln for ln in (res["stdout"] or "").splitlines() if ln.startswith("worktree ")]


# ---------- 纯函数层 ----------


def test_parse_history_keeps_sha():
    raw = (
        "\x1fh2\x1f2026-02-01T10:00:00+08:00\n"
        "\n"
        "b.py\n"
        "src\\x.py\n"
        "b.py\n"
        "\n"
        "\x1fh1\x1f2026-01-01T10:00:00+08:00\n"
        "\n"
        "a.py\n"
    )
    history = timeline.parse_history(raw)
    assert len(history) == 2
    assert history[0] == {"sha": "h2", "date": "2026-02-01T10:00:00+08:00", "files": ["b.py", "src/x.py"]}
    assert history[1] == {"sha": "h1", "date": "2026-01-01T10:00:00+08:00", "files": ["a.py"]}


def test_pick_samples_stride_ends_cap():
    assert timeline.pick_samples(0, 20) == []
    assert timeline.pick_samples(1, 20) == [0]
    assert timeline.pick_samples(5, 20) == [0, 1, 2, 3, 4]
    idxs = timeline.pick_samples(100, 20)
    assert len(idxs) == 20
    assert idxs[0] == 0 and idxs[-1] == 99
    assert timeline.pick_samples(10, 3) == [0, 4, 9]


def test_count_file_cycles_small_graphs():
    assert timeline.count_file_cycles({}) == 0
    assert timeline.count_file_cycles({"a": {"b"}, "b": set()}) == 0
    assert timeline.count_file_cycles({"a": {"b"}, "b": {"a"}}) == 1
    assert timeline.count_file_cycles({"a": {"b"}, "b": {"a"}, "c": {"d"}, "d": {"c"}}) == 2
    assert timeline.count_file_cycles({"a": {"b"}, "b": {"c"}, "c": set()}) == 0
    assert timeline.count_file_cycles({"a": {"a"}}) == 0  # 自环不算


def test_turning_points_logic():
    pts = [
        {"sha": "s1", "date": "d1", "dead_code_candidates": 1, "scc_count": 0, "edge_density": 0.5},
        {"sha": "s2", "date": "d2", "dead_code_candidates": 3, "scc_count": 0, "edge_density": 0.4},
        {"sha": "s3", "date": "d3", "dead_code_candidates": 4, "scc_count": 0, "edge_density": 0.6},
    ]
    tp = timeline._turning_points(pts)
    by_metric = {t["metric"]: t for t in tp}
    assert set(by_metric) == {"dead_code_candidates", "edge_density"}  # scc 无正向
    d = by_metric["dead_code_candidates"]
    assert d["delta"] == 2 and d["from_sha"] == "s1" and d["to_sha"] == "s2"
    e = by_metric["edge_density"]
    assert e["delta"] == pytest.approx(0.2) and e["to_sha"] == "s3"
    assert timeline._turning_points([]) == []
    single = [pts[0]]
    assert timeline._turning_points(single) == []


def test_spark_flat_and_ramp():
    assert timeline._spark([3, 3, 3]) == timeline._SPARK_BLOCKS[3] * 3
    ramp = timeline._spark([0, 1, 2, 3])
    assert ramp[0] == timeline._SPARK_BLOCKS[0]
    assert ramp[-1] == timeline._SPARK_BLOCKS[7]


# ---------- 端到端（真实 run_etl 全量/增量链） ----------


def test_run_timeline_metrics_end_to_end(world):
    result = timeline.run_timeline(world["repo"], samples=4)
    pts = result["points"]
    assert len(pts) == 4
    assert [p["date"] for p in pts] == DATES  # 回放序：最旧→最新
    p1, p2, p3, p4 = pts
    # 逐点度量锚定（c2 改 a.py 成环；c3/c4 加死代码；fa 内容变更 fid 轮换）
    assert (p1["files"], p1["loc"], p1["symbols"], p1["edges"]) == (2, 4, 2, 1)
    assert (p2["files"], p2["loc"], p2["symbols"], p2["edges"]) == (2, 4, 2, 2)
    assert (p3["files"], p3["loc"], p3["symbols"], p3["edges"]) == (3, 6, 3, 2)
    assert (p4["files"], p4["loc"], p4["symbols"], p4["edges"]) == (4, 8, 4, 3)
    assert p1["edge_density"] == 0.5 and p2["edge_density"] == 1.0
    assert p3["edge_density"] == 0.666667 and p4["edge_density"] == 0.75
    assert [p["scc_count"] for p in pts] == [0, 1, 1, 1]
    assert p1["births"] == 2 and p1["deaths"] == 0  # 样本1 births=全部
    assert p2["births"] == 1 and p2["deaths"] == 1  # fa fid 轮换
    assert p3["births"] == 1 and p4["births"] == 1
    assert p1["hotspot_top10"] == [
        {"file": "a.py", "chg": 1, "cc": 1, "hotspot": 1},
        {"file": "b.py", "chg": 1, "cc": 1, "hotspot": 1},
    ]
    assert p2["hotspot_top10"] == [{"file": "a.py", "chg": 1, "cc": 1, "hotspot": 1}]
    # 产物已落盘
    artifact = world["repo"] / ".vrp" / "timeline.json"
    assert artifact.exists()
    obj = json.loads(artifact.read_text(encoding="utf-8"))
    assert obj["commits_total"] == 4 and len(obj["points"]) == 4
    tp = {t["metric"]: t for t in result["turning_points"]}
    assert "scc_count" in tp and tp["scc_count"]["delta"] == 1


def test_chain_equals_independent_full_etl(world, tmp_path):
    """链式 vs 逐样本独立全量：七项计数度量全等（设计稿 §7 核心件）。"""
    result = timeline.run_timeline(world["repo"], samples=4)
    keys = ("files", "loc", "symbols", "edges", "edge_density", "dead_code_candidates", "scc_count")
    for k, point in enumerate(result["points"]):
        wt2 = tmp_path / f"wt2-{k}"
        _git(world["repo"], "worktree", "add", "--detach", str(wt2), point["sha"])
        try:
            _mk_cbm_from_tree(wt2, world["tl_name"], world["cbm_path"])  # 模拟该样本 CBM 索引
            fresh = tmp_path / f"indep-{k}.db"
            etl_mod.run_etl(wt2, world["tl_name"], out_db=fresh, full=True)
            con = sqlite3.connect(fresh)
            try:
                metrics, _fids = timeline._collect_sample(con, wt2, [], None, {})
            finally:
                con.close()
        finally:
            _git(world["repo"], "worktree", "remove", "--force", str(wt2))
        for key in keys:
            assert metrics[key] == point[key], f"sample {k} metric {key}: chain={point[key]} indep={metrics[key]}"


def test_cleanup_user_tree_untouched(world):
    def _tree_files():
        return {
            p.relative_to(world["repo"]).as_posix(): p.read_bytes()
            for p in world["repo"].rglob("*")
            if p.is_file() and ".git" not in p.parts and ".vrp" not in p.parts
        }

    before = _tree_files()
    timeline.run_timeline(world["repo"], samples=4)
    after = _tree_files()
    assert before == after  # 用户源文件零接触（.vrp/timeline.json 是设计内产物）
    assert set(before) == {"a.py", "b.py", "c.py", "d.py"}
    assert (world["repo"] / ".vrp" / "timeline.json").exists()
    assert len(_worktree_entries(world["repo"])) == 1
    assert not world["cbm_path"].exists()  # 时间线 CBM 缓存库用后即删


def test_failure_aborts_and_cleans(world, monkeypatch):
    """单样本失败中止全回放（不跳样本）+ finally 全清理不遮在飞异常。"""
    calls = {"n": 0}
    real = etl_mod.run_etl

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("boom-sample-2")
        return real(*args, **kwargs)

    monkeypatch.setattr(etl_mod, "run_etl", flaky)
    with pytest.raises(RuntimeError, match="boom-sample-2"):
        timeline.run_timeline(world["repo"], samples=4)
    assert calls["n"] == 2
    assert len(_worktree_entries(world["repo"])) == 1
    assert not world["cbm_path"].exists()


def test_not_a_git_repo(tmp_path):
    with pytest.raises(RuntimeError, match="git log"):
        timeline.run_timeline(tmp_path, samples=5)


def test_empty_repo_no_commits(tmp_path):
    repo = _init_repo(tmp_path / "empty")
    with pytest.raises(RuntimeError, match="git log"):
        timeline.run_timeline(repo, samples=5)


def test_single_commit_single_point(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "one")
    _commit(repo, {"a.py": A1}, "c1", DATES[0])
    fake = _patch_fake_cbm(monkeypatch, tmp_path, repo)
    result = timeline.run_timeline(repo, samples=20)
    assert len(result["points"]) == 1
    p = result["points"][0]
    assert p["files"] == 1 and p["symbols"] == 1
    assert p["births"] == 1 and p["deaths"] == 0
    assert result["turning_points"] == []
    text = timeline.render_text(result)
    assert "样本不足 2 点" in text
    assert (repo / ".vrp" / "timeline.json").exists()
    assert not fake["cbm_path"].exists()


def test_artifact_deterministic_double_run(world):
    timeline.run_timeline(world["repo"], samples=4)
    b1 = (world["repo"] / ".vrp" / "timeline.json").read_bytes()
    timeline.run_timeline(world["repo"], samples=4)
    b2 = (world["repo"] / ".vrp" / "timeline.json").read_bytes()
    assert b1 == b2  # git 历史+参数的确定函数——字节级复现
    obj = json.loads(b2.decode("utf-8"))
    assert list(obj) == ["samples_requested", "commits_total", "points", "turning_points"]
    assert list(obj["points"][0]) == [
        "sha", "date", "files", "loc", "symbols", "edges", "edge_density",
        "dead_code_candidates", "births", "deaths", "scc_count", "hotspot_top10",
    ]


def test_cli_timeline_runs(world, capsys):
    rc = cli.main(["timeline", str(world["repo"]), "--samples", "4"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "A1 架构腐化时间线" in out
    assert "转折点" in out
    assert (world["repo"] / ".vrp" / "timeline.json").exists()
