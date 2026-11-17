"""S5 vrp groups：finding 同根因归组守卫（构想v12 甜点）。

守卫重点：指纹无序对称（互环 a→b/b→a 同键、克隆族同键归组——lusiem
实证 token-clone 同无序对 11×）、无 fid 回退 (file_path, start_line) 键、
group_id 稳定（跨 etl 复跑不变）、排序 (-members, group_id) 纯确定、
member_ids cap（summary 记 cap 恒真）、红线归组不删行（findings 行数
前后不变——只读查询面）、limit 截断展示 summary 恒真（findings 同
哲学）、确定性（同库双跑同输出）、facts.db 缺失诚实 SystemExit。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vrp import grouping as grp
from vrp.cli import build_parser, cmd_groups
from vrp.cli import main as cli_main

SCHEMA = Path(grp.__file__).parent / "schema" / "facts_schema.sql"

FID_A = "a" * 64
FID_B = "b" * 64
FID_C = "c" * 64


def _seed(con: sqlite3.Connection, rows: list[tuple]) -> None:
    """rows: (pathology, kind, anchor, related, file_path, start_line)。"""
    con.executemany(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid,"
        " file_path, start_line, payload_json) VALUES (?, ?, 'etl', ?, ?, ?, ?, '{}')",
        rows,
    )
    con.commit()


def _facts_repo(tmp_path: Path, rows: list[tuple]) -> Path:
    repo = tmp_path / "repo"
    facts = repo / ".vrp" / "facts.db"
    facts.parent.mkdir(parents=True)
    con = sqlite3.connect(facts)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    _seed(con, rows)
    con.close()
    return repo


# ---------- 指纹层 ----------


def test_group_key_unordered_symmetry():
    """无序对称：锚定对 (A,B) 与 (B,A) 同键（互环/克隆族语义）；
    异对/异 kind/异 pathology 不同键。
    """
    ab = grp.group_key("P2", "token-clone", FID_A, FID_B, "x.py", 1)
    ba = grp.group_key("P2", "token-clone", FID_B, FID_A, "y.py", 2)
    assert ab == ba  # 文件/行不参与 fid 键——根因是符号对
    assert ab != grp.group_key("P2", "token-clone", FID_A, FID_C, "x.py", 1)
    assert ab != grp.group_key("P2", "similar_to", FID_A, FID_B, "x.py", 1)
    assert ab != grp.group_key("P3", "token-clone", FID_A, FID_B, "x.py", 1)


def test_group_key_path_fallback():
    """无 fid 回退：file_path+start_line 键；同文件异行/异文件不同键；
    行 None 与行 0 键不同（str(start_line or 0) 归一 None→0——同键）。
    """
    k1 = grp.group_key("P4", "unused-file", None, None, "scripts/a.mjs", None)
    k2 = grp.group_key("P4", "unused-file", None, None, "scripts/a.mjs", 0)
    assert k1 == k2  # None 与 0 归一（无行号 finding 不因行号语义裂组）
    assert k1 != grp.group_key("P4", "unused-file", None, None, "scripts/b.mjs", None)
    assert k1 != grp.group_key("P4", "unused-file", None, None, "scripts/a.mjs", 3)
    single = grp.group_key("P1", "debug-print", FID_A, None, "x.py", 1)
    assert single == grp.group_key("P1", "debug-print", None, FID_A, "y.py", 2)


# ---------- 归组层 ----------


def test_group_findings_shape_and_order(facts_db):
    """归组：同根因同组、排序 (-members, group_id)、sample/member_ids 形状。"""
    _seed(facts_db, [
        ("P2", "token-clone", FID_A, FID_B, "a.py", 10),
        ("P2", "token-clone", FID_B, FID_A, "b.py", 20),  # 互向同组
        ("P2", "token-clone", FID_A, FID_B, "c.py", 30),
        ("P1", "debug-print", FID_A, None, "a.py", 5),
        ("P4", "unused-file", None, None, "z.mjs", None),
    ])
    result = grp.group_findings(facts_db)
    s = result["summary"]
    assert s["total_findings"] == 5
    assert s["total_groups"] == 3
    assert s["multi_member_groups"] == 1
    assert s["findings_in_multi"] == 3
    assert s["red_line"] == "grouping never deletes - findings rows untouched"
    top = result["groups"][0]
    assert top["members"] == 3  # members 最大者居首
    assert top["kind"] == "token-clone"
    assert top["member_ids"] == [1, 2, 3]  # 组内 id 升序
    assert top["sample"] == {"file_path": "a.py", "start_line": 10}  # 首见样本


def test_group_rows_untouched(facts_db):
    """红线：归组是只读查询面——findings 行数前后不变。"""
    _seed(facts_db, [("P2", "token-clone", FID_A, FID_B, "a.py", 10)])
    before = facts_db.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    grp.group_findings(facts_db)
    after = facts_db.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    assert before == after == 1


def test_member_ids_cap(facts_db):
    """cap：member_ids 截 10；members/cap 恒真（诚实计数）。"""
    _seed(facts_db, [
        ("P2", "token-clone", FID_A, FID_B, "a.py", i) for i in range(12)
    ])
    result = grp.group_findings(facts_db)
    g = result["groups"][0]
    assert g["members"] == 12
    assert len(g["member_ids"]) == grp.MEMBER_IDS_CAP == 10
    assert result["summary"]["member_ids_cap"] == 10


# ---------- 入口与 CLI 面 ----------


def test_run_groups_e2e_and_missing_facts(tmp_path):
    """全链：文件库只读归组；facts.db 缺失诚实 SystemExit。"""
    repo = _facts_repo(tmp_path, [
        ("P2", "token-clone", FID_A, FID_B, "a.py", 10),
        ("P2", "token-clone", FID_B, FID_A, "b.py", 20),
    ])
    result = grp.run_groups(repo, limit=1)
    assert result["summary"]["total_groups"] == 1
    assert result["shown"] == 1
    assert result["groups"][0]["members"] == 2
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    with pytest.raises(SystemExit, match="run 'vrp run' first"):
        grp.run_groups(empty)


def test_run_groups_deterministic(tmp_path):
    """确定性红线：同库双跑输出全等（无时间戳）。"""
    repo = _facts_repo(tmp_path, [
        ("P1", "debug-print", FID_A, None, "a.py", 1),
        ("P2", "token-clone", FID_A, FID_B, "b.py", 2),
    ])
    assert grp.run_groups(repo) == grp.run_groups(repo)


def test_render_text_shape(tmp_path):
    repo = _facts_repo(tmp_path, [
        ("P2", "token-clone", FID_A, FID_B, "a.py", 10),
        ("P2", "token-clone", FID_B, FID_A, "b.py", 20),
    ])
    text = grp.render_text(grp.run_groups(repo))
    assert text.startswith("vrp groups - repo")
    assert "findings  : 2 total -> 1 groups (1 multi-member, 2 findings share a root cause)" in text
    assert "grouping never deletes" in text
    assert "x2    P2/token-clone  a.py:10  ids: 1, 2" in text


def test_cli_parser_accepts_groups():
    args = build_parser().parse_args(["groups", "somerepo"])
    assert args.func is cmd_groups
    assert args.repo == "somerepo"
    assert args.limit == grp.DEFAULT_LIMIT
    assert args.json is False
    args = build_parser().parse_args(["groups", "somerepo", "--json", "--limit", "3"])
    assert args.json is True
    assert args.limit == 3


def test_cli_groups_end_to_end(tmp_path, capsys):
    """CLI 全链：vrp groups --json → rc 0 + JSON 可解析 + 归组内嵌。"""
    repo = _facts_repo(tmp_path, [
        ("P2", "token-clone", FID_A, FID_B, "a.py", 10),
        ("P2", "token-clone", FID_B, FID_A, "b.py", 20),
        ("P4", "unused-file", None, None, "z.mjs", None),
    ])
    import json

    rc = cli_main(["groups", str(repo), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["total_findings"] == 3
    assert payload["summary"]["total_groups"] == 2
    assert payload["groups"][0]["members"] == 2
