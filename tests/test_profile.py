"""R4 vrp profile：仓库数字画像守卫（构想v12 甜点）。

守卫重点：Gini 手算锚定（全等 0/两值经典例 0.5/全零诚实 0.0）、
集中度 top1%/top10% 手算（幂律拟合的诚实替代——CSN 拟合不做的边界
记档）、边密度公式（n<=1 诚实 0.0）、枢纽排序 (count DESC, fid) 纯
确定、只读红线（symbols/edges 行数前后不变）、确定性（同库双跑同
输出）、facts.db 缺失诚实 SystemExit、空库诚实零面不崩。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vrp import profile as prof
from vrp.cli import build_parser, cmd_profile
from vrp.cli import main as cli_main

SCHEMA = Path(prof.__file__).parent / "schema" / "facts_schema.sql"


def _facts_repo(tmp_path: Path, symbols: list[tuple], edges: list[tuple]) -> Path:
    repo = tmp_path / "repo"
    facts = repo / ".vrp" / "facts.db"
    facts.parent.mkdir(parents=True)
    con = sqlite3.connect(facts)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.executemany(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path)"
        " VALUES (?, 'p', ?, 'function', ?, ?)",
        symbols,
    )
    con.executemany(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?, ?, 'CALLS', 'etl')",
        edges,
    )
    con.commit()
    con.close()
    return repo


# ---------- 纯函数层：Gini 与集中度手算锚定 ----------


def test_gini_hand_computed():
    """Gini 手算锚定：全等→0；[0,0,0,10]→0.75（经典例）；全零→0.0。"""
    assert prof.gini([3, 3, 3]) == 0.0  # 全等：无差异
    assert prof.gini([]) == 0.0  # 空诚实
    assert prof.gini([5]) == 0.0  # 单元素诚实
    assert prof.gini([0, 0, 0, 0]) == 0.0  # 全零诚实
    # [0,0,0,10]：n=4 total=10 ordered cum=[0,0,0,10] weighted=0+0+0+40=40
    # gini = 2*40/(4*10) - 5/4 = 2 - 1.25 = 0.75
    assert prof.gini([0, 0, 0, 10]) == 0.75
    # [1,1,1,1] vs [0,1,1,2]：更不均 → gini 更大
    assert prof.gini([0, 1, 1, 2]) > prof.gini([1, 1, 1, 1])


def test_concentration_hand_computed():
    """集中度手算：100 值 top1=1 值 top10=10 值累计占比。"""
    values = [100] + [1] * 99  # sum=199
    c = prof.concentration(values)
    assert c["count"] == 100
    assert c["sum"] == 199
    assert c["top1pct_share"] == round(100 / 199, 4)
    assert c["top10pct_share"] == round((100 + 9) / 199, 4)
    empty = prof.concentration([])
    assert empty == {"count": 0, "sum": 0, "top1pct_share": 0.0, "top10pct_share": 0.0}
    zeros = prof.concentration([0, 0, 0])
    assert zeros["top1pct_share"] == 0.0  # 全零诚实 0 非除零


# ---------- 画像层 ----------


def test_profile_shape_and_math(tmp_path):
    """三面分布+密度+枢纽：手算核（入度 gini/密度/top 扇入）。"""
    # 4 符号 3 边：a->b, a->c, b->c
    repo = _facts_repo(
        tmp_path,
        symbols=[
            ("fa", "m.a", "a", "a.py"),
            ("fb", "m.b", "b", "a.py"),
            ("fc", "m.c", "c", "b.py"),
            ("fd", "m.d", "d", "b.py"),
        ],
        edges=[("fa", "fb"), ("fa", "fc"), ("fb", "fc")],
    )
    result = prof.run_profile(repo)
    s = result["summary"]
    assert s["symbols"] == 4
    assert s["edges"] == 3
    assert s["files_with_symbols"] == 2
    assert s["edge_density"] == round(3 / (4 * 3), 6)
    assert "power_law_note" in s
    dists = result["distributions"]
    # 入度 {b:1, c:2}；出度 {a:2, b:1}
    assert dists["in_degree"]["count"] == 2
    assert dists["in_degree"]["sum"] == 3
    assert dists["in_degree"]["max"] == 2
    # 文件符号 [2, 2] 全等 → gini 0
    assert dists["file_symbols"]["gini"] == 0.0
    hubs = result["hubs"]
    assert hubs["top_fan_in"][0] == {"fid": "fc", "qualified_name": "m.c", "in_edges": 2}
    assert hubs["top_fan_out"][0] == {"fid": "fa", "qualified_name": "m.a", "out_edges": 2}


def test_profile_hubs_tiebreak_deterministic(tmp_path):
    """枢纽平局：(count DESC, fid) 纯确定——同 count 按 fid 字典序。"""
    repo = _facts_repo(
        tmp_path,
        symbols=[("fz", "m.z", "z", "z.py"), ("fa", "m.a", "a", "a.py")],
        edges=[("fa", "fz"), ("fz", "fa")],
    )
    result = prof.run_profile(repo)
    assert [h["fid"] for h in result["hubs"]["top_fan_in"]] == ["fa", "fz"]
    assert [h["fid"] for h in result["hubs"]["top_fan_out"]] == ["fa", "fz"]


def test_profile_rows_untouched(tmp_path):
    """只读红线：symbols/edges 行数前后不变。"""
    repo = _facts_repo(
        tmp_path,
        symbols=[("fa", "m.a", "a", "a.py")],
        edges=[("fa", "fa")],
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    before = (
        con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
    )
    con.close()
    prof.run_profile(repo)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    after = (
        con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
    )
    con.close()
    assert before == after == (1, 1)


def test_profile_empty_and_single(tmp_path):
    """空库诚实零面不崩；单符号密度 0.0（n<=1 诚实除零护栏）。"""
    repo = _facts_repo(tmp_path, [], [])
    result = prof.run_profile(repo)
    assert result["summary"]["symbols"] == 0
    assert result["summary"]["edge_density"] == 0.0
    assert result["distributions"]["in_degree"]["count"] == 0
    assert result["hubs"]["top_fan_in"] == []
    repo1 = _facts_repo(tmp_path / "one", [("fa", "m.a", "a", "a.py")], [])
    single = prof.run_profile(repo1)
    assert single["summary"]["edge_density"] == 0.0


def test_profile_deterministic(tmp_path):
    """确定性红线：同库双跑全等（无时间戳）。"""
    repo = _facts_repo(
        tmp_path,
        symbols=[("fa", "m.a", "a", "a.py"), ("fb", "m.b", "b", "b.py")],
        edges=[("fa", "fb")],
    )
    assert prof.run_profile(repo) == prof.run_profile(repo)


def test_profile_missing_facts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(SystemExit, match="run 'vrp run' first"):
        prof.run_profile(repo)


# ---------- CLI 面 ----------


def test_render_text_shape(tmp_path):
    repo = _facts_repo(
        tmp_path,
        symbols=[("fa", "m.a", "a", "a.py"), ("fb", "m.b", "b", "b.py")],
        edges=[("fa", "fb")],
    )
    text = prof.render_text(prof.run_profile(repo))
    assert text.startswith("vrp profile - repo")
    assert "scale      : 2 symbols, 1 edges, 2 files" in text
    assert "in_degree   : gini" in text
    assert "no CSN fit" in text
    assert "fan-in  x1     m.b" in text
    assert "fan-out x1     m.a" in text


def test_cli_parser_accepts_profile():
    args = build_parser().parse_args(["profile", "somerepo"])
    assert args.func is cmd_profile
    assert args.repo == "somerepo"
    assert args.json is False
    args = build_parser().parse_args(["profile", "somerepo", "--json"])
    assert args.json is True


def test_cli_profile_end_to_end(tmp_path, capsys):
    """CLI 全链：vrp profile --json → rc 0 + JSON 可解析。"""
    import json

    repo = _facts_repo(
        tmp_path,
        symbols=[("fa", "m.a", "a", "a.py")],
        edges=[("fa", "fa")],
    )
    rc = cli_main(["profile", str(repo), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["symbols"] == 1
    assert payload["hubs"]["top_fan_in"][0]["qualified_name"] == "m.a"


def test_cli_help_renders_with_percent_literals():
    """回归锁（#165 门禁抓红）：help 渲染遇字面 % 不得 ValueError——
    top1%/top10% 原先裸躺在 profile 的 help=（%-展开直接炸）；D 批改造后
    下沉为 description（不经 % 展开），主 help + profile --help 双渲染
    专项锁死（描述回迁 help= 不转义时此测试必红）。
    """
    import contextlib
    import io

    parser = build_parser()
    main_buf = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        with contextlib.redirect_stdout(main_buf):
            parser.parse_args(["--help"])
    assert exc.value.code == 0

    sub_buf = io.StringIO()
    with pytest.raises(SystemExit) as sub_exc:
        with contextlib.redirect_stdout(sub_buf):
            parser.parse_args(["profile", "--help"])
    assert sub_exc.value.code == 0
    assert "top1%/top10%" in sub_buf.getvalue()
