"""D11 迁移进度测试面：快照单源/history 追加/收缩%/零基线/缺结构诚实/CLI。"""

import json
import sqlite3
from pathlib import Path

from vrp import migprog
from vrp.cli import main as cli_main

REPO_ROOT = Path(__file__).parent.parent
SCHEMA = REPO_ROOT / "vrp" / "schema" / "facts_schema.sql"

F1, F2 = "f1", "f2"


def _repo_with_edges(tmp_path, edges, structure=True):
    """scratch 仓：structure active+跨界边集。"""
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    for fid in (F1, F2):
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path)"
            " VALUES (?,'p','p.'||?,'Function',?,'a.py')",
            (fid, fid, fid),
        )
    for _ in range(edges):
        con.execute(
            "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,'CALLS','import')",
            (F1, F2),
        )
    if structure:
        payload = {
            "modules": [
                {"module_id": "a", "name": "a", "symbol_fids": [F1], "layer": "l"},
                {"module_id": "b", "name": "b", "symbol_fids": [F2], "layer": "l"},
            ],
            "layers": [{"name": "l", "may_depend_on": []}],
        }
        con.execute(
            "INSERT INTO structure (payload_json, status, created_at) VALUES (?,'active','t')",
            (json.dumps(payload, ensure_ascii=False),),
        )
    con.commit()
    con.close()
    return repo


def test_snapshot_cross_edges_single_source(tmp_path):
    repo = _repo_with_edges(tmp_path, 3)
    snap = migprog.snapshot(repo)
    assert snap["structure"] == "active"
    assert snap["cross_edges"] == 3
    assert snap["pairs"] == [{"from": "a", "to": "b", "edges": 3}]


def test_snapshot_no_structure_honest(tmp_path):
    repo = _repo_with_edges(tmp_path, 2, structure=False)
    snap = migprog.snapshot(repo)
    assert snap["cross_edges"] is None
    assert snap["structure"] == "missing"


def test_save_and_progress_shrink_pct(tmp_path):
    repo = _repo_with_edges(tmp_path, 8)  # 基线：8 条跨界边
    saved = migprog.save_snapshot(repo)
    assert saved["saved"] is True
    assert saved["cross_edges"] == 8
    assert (repo / ".vrp" / "migprog-history.jsonl").exists()
    # 迁移推进：跨界边收缩到 2
    repo2 = _repo_with_edges(tmp_path / "s2", 2)
    history_src = repo / ".vrp" / "migprog-history.jsonl"
    dst = repo2 / ".vrp" / "migprog-history.jsonl"
    dst.write_text(history_src.read_text(encoding="utf-8"), encoding="utf-8")
    result = migprog.progress(repo2)
    assert result["snapshots"] == 1
    assert result["baseline"]["cross_edges"] == 8
    assert result["progress_pct"] == 75.0  # (8-2)/8 收缩 75%


def test_progress_coupling_grew_negative(tmp_path):
    repo = _repo_with_edges(tmp_path, 2)
    migprog.save_snapshot(repo)
    repo2 = _repo_with_edges(tmp_path / "s2", 4)
    dst = repo2 / ".vrp" / "migprog-history.jsonl"
    dst.write_text((repo / ".vrp" / "migprog-history.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    result = migprog.progress(repo2)
    assert result["progress_pct"] == -100.0  # 耦合翻倍——负值诚实


def test_progress_zero_baseline_undefined(tmp_path):
    repo = _repo_with_edges(tmp_path, 0)  # 基线 0 跨界边
    migprog.save_snapshot(repo)
    result = migprog.progress(repo)
    assert result["progress_pct"] is None  # 零基线未定义诚实


def test_progress_no_snapshot_honest(tmp_path):
    repo = _repo_with_edges(tmp_path, 5)
    result = migprog.progress(repo)
    assert result["snapshots"] == 0
    assert "--snapshot" in result["note"]


def test_history_append_and_vs_last(tmp_path):
    repo = _repo_with_edges(tmp_path, 6)
    migprog.save_snapshot(repo)
    repo2 = _repo_with_edges(tmp_path / "s2", 6)
    dst = repo2 / ".vrp" / "migprog-history.jsonl"
    dst.write_text((repo / ".vrp" / "migprog-history.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    migprog.save_snapshot(repo2)  # 第二条快照（同值）
    repo3 = _repo_with_edges(tmp_path / "s3", 3)
    dst3 = repo3 / ".vrp" / "migprog-history.jsonl"
    dst3.write_text(dst.read_text(encoding="utf-8"), encoding="utf-8")
    result = migprog.progress(repo3)
    lines = dst3.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # 追加审计语义——不清理
    assert result["snapshots"] == 2
    assert result["vs_last"]["cross_edges"] == 6
    assert result["vs_last"]["delta_pct"] == 50.0  # (6-3)/6 vs 上一条收缩 50%——正号=收缩
    assert result["progress_pct"] == 50.0


def test_render_text_faces(tmp_path):
    repo = _repo_with_edges(tmp_path, 6)
    snap_text = migprog.render_text(migprog.snapshot(repo))
    assert "cross_edges: 6 over 1 module pair(s)" in snap_text
    empty = migprog.render_text(migprog.progress(repo))
    assert "--snapshot" in empty
    migprog.save_snapshot(repo)
    repo2 = _repo_with_edges(tmp_path / "s2", 3)
    dst = repo2 / ".vrp" / "migprog-history.jsonl"
    dst.write_text((repo / ".vrp" / "migprog-history.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    text = migprog.render_text(migprog.progress(repo2))
    assert "progress: +50.00% seam shrink since baseline" in text


def test_cli_migprog_snapshot_and_progress(tmp_path, capsys):
    repo = _repo_with_edges(tmp_path, 4)
    rc = cli_main(["migprog", str(repo), "--snapshot", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["saved"] is True and data["cross_edges"] == 4
    rc = cli_main(["migprog", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "snapshots: 1" in out
    assert "progress: +0.00% seam shrink since baseline" in out
