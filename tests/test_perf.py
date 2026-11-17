"""J10（构想v11 甜点）：管道性能回归追踪测试。

- record_run：JSONL 追加形态（ts/git_head/stages round3/meta）
- run_perf：last vs previous 逐阶段 delta；回归双门（+25% 且 +1.0s）——
  小基数噪声免疫；坏行诚实计数不裸崩；单条目 note 不比较
- cmd_run 集成：四阶段计时自动落档（monkeypatch 四阶段替身，不跑真管道）
"""

from __future__ import annotations

import json
from pathlib import Path

from vrp import perf
from vrp.cli import build_parser, cmd_run


def _mk_repo(tmp_path: Path, name: str = "perfrepo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".vrp").mkdir()
    return repo


def test_record_run_appends_jsonl(tmp_path):
    repo = _mk_repo(tmp_path)
    e1 = perf.record_run(repo, {"etl": 1.23456, "total": 2.0}, git_head="abc123")
    e2 = perf.record_run(repo, {"etl": 2.0, "total": 3.0}, git_head="def456", meta={"reindex": True})
    lines = perf.perf_path(repo).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["git_head"] == "abc123"
    assert first["stages"]["etl"] == 1.235  # round 3
    assert "ts" in first and "meta" not in first
    second = json.loads(lines[1])
    assert second["meta"] == {"reindex": True}
    assert e1["stages"]["etl"] == 1.235 and e2["git_head"] == "def456"


def test_load_entries_bad_lines_counted(tmp_path):
    repo = _mk_repo(tmp_path)
    path = perf.perf_path(repo)
    good = json.dumps({"ts": "t", "git_head": None, "stages": {"etl": 1.0}})
    path.write_text(
        good + "\nnot json\n\n" + json.dumps({"no_stages": True}) + "\n" + good + "\n",
        encoding="utf-8",
    )
    entries, bad = perf.load_entries(repo)
    assert len(entries) == 2
    assert bad == 2  # 坏 JSON + 无 stages 形状


def test_run_perf_missing_history_exits(tmp_path):
    import pytest

    repo = _mk_repo(tmp_path)
    with pytest.raises(SystemExit, match="no perf history"):
        perf.run_perf(repo)


def test_run_perf_single_entry_no_compare(tmp_path):
    repo = _mk_repo(tmp_path)
    perf.record_run(repo, {"etl": 1.0, "total": 2.0})
    result = perf.run_perf(repo)
    assert result["entries"] == 1
    assert result["previous"] is None
    assert "no previous run" in result["note"]
    assert result["stages"] == []
    assert result["regressions"] == []


def test_run_perf_delta_pct_computed(tmp_path):
    repo = _mk_repo(tmp_path)
    perf.record_run(repo, {"etl": 4.0, "total": 8.0})
    perf.record_run(repo, {"etl": 5.0, "total": 9.0})
    result = perf.run_perf(repo)
    by_stage = {s["stage"]: s for s in result["stages"]}
    assert by_stage["etl"]["latest_s"] == 5.0
    assert by_stage["etl"]["previous_s"] == 4.0
    assert by_stage["etl"]["delta_pct"] == 25.0
    assert by_stage["etl"]["regression"] is False  # 25.0 不逾 >25.0 严格门
    assert result["regressions"] == []


def test_regression_double_gate(tmp_path):
    """相对门+绝对门双卡：大基数 +30% 过双门=True；小基数 +50% 绝对门卡=False。"""
    assert perf._is_regression(13.0, 10.0) is True  # +30%, +3.0s
    assert perf._is_regression(0.3, 0.2) is False  # +50% 但只 +0.1s
    assert perf._is_regression(1.2, 1.0) is False  # +20% 不逾相对门
    assert perf._is_regression(8.0, 10.0) is False  # 改善不旗标
    assert perf._is_regression(5.0, 0.0) is False  # 零基线 delta None


def test_run_perf_regression_flagged(tmp_path):
    repo = _mk_repo(tmp_path)
    perf.record_run(repo, {"etl": 10.0, "cbm_index": 2.0, "total": 12.0})
    perf.record_run(repo, {"etl": 20.0, "cbm_index": 2.1, "total": 22.1})
    result = perf.run_perf(repo)
    assert result["regressions"] == ["etl", "total"]  # total +84%/+10.1s 同过双门
    by_stage = {s["stage"]: s for s in result["stages"]}
    assert by_stage["etl"]["regression"] is True
    assert by_stage["cbm_index"]["regression"] is False


def test_run_perf_stage_union_missing_stage(tmp_path):
    """前次有 xcheck 本次无：并集出现，缺失侧计 0.0 不 KeyError。"""
    repo = _mk_repo(tmp_path)
    perf.record_run(repo, {"etl": 1.0, "xcheck": 5.0})
    perf.record_run(repo, {"etl": 1.1})
    result = perf.run_perf(repo)
    by_stage = {s["stage"]: s for s in result["stages"]}
    assert by_stage["xcheck"]["latest_s"] == 0.0
    assert by_stage["xcheck"]["previous_s"] == 5.0
    assert by_stage["xcheck"]["delta_pct"] == -100.0


def test_run_perf_deterministic(tmp_path):
    repo = _mk_repo(tmp_path)
    perf.record_run(repo, {"etl": 1.0})
    perf.record_run(repo, {"etl": 1.5})
    assert perf.run_perf(repo) == perf.run_perf(repo)


def test_render_text_compare_and_regression(tmp_path):
    repo = _mk_repo(tmp_path)
    perf.record_run(repo, {"etl": 10.0, "total": 12.0}, git_head="a" * 40)
    perf.record_run(repo, {"etl": 20.0, "total": 22.0}, git_head="b" * 40)
    text = perf.render_text(perf.run_perf(repo))
    assert "管道性能批次对账" in text
    assert "+100.0%" in text
    assert "[regression]" in text
    assert "regressions: etl" in text
    assert "single-sample" in text  # noise note 明示


def test_render_text_single_entry(tmp_path):
    repo = _mk_repo(tmp_path)
    perf.record_run(repo, {"etl": 1.0, "total": 1.5})
    text = perf.render_text(perf.run_perf(repo))
    assert "no previous run" in text
    assert "etl" in text and "1.000" in text


def test_cmd_run_records_all_stages(tmp_path, monkeypatch, capsys):
    """cmd_run 集成：四阶段计时+total 自动落档 perf.jsonl（四阶段全替身）。"""
    repo = _mk_repo(tmp_path, "runrepo")
    (repo / "file.py").write_text("x = 1\n", encoding="utf-8")
    from vrp import cli
    from vrp import s0_precheck, s1_cbm, xcheck

    monkeypatch.setattr(
        s0_precheck, "precheck", lambda r: {"repo": str(r), "name": r.name, "git": {"head": "deadbeef"}, "commands": {}, "content_repo": {"detected": False}}
    )
    monkeypatch.setattr(s1_cbm, "index_repository", lambda r, name, reindex=False: {"status": "ok"})
    monkeypatch.setattr(cli.etl_mod, "run_etl", lambda r, name: {"status": "ok"})
    monkeypatch.setattr(xcheck, "run_xcheck", lambda r, db, reindex=False: {"status": "ok"})
    args = build_parser().parse_args(["run", str(repo)])
    rc = cmd_run(args)
    capsys.readouterr()
    assert rc == 0
    entries, bad = perf.load_entries(repo)
    assert bad == 0
    assert len(entries) == 1
    stages = entries[0]["stages"]
    assert set(stages) == {"precheck", "cbm_index", "etl", "xcheck", "total"}
    assert entries[0]["git_head"] == "deadbeef"
    assert entries[0]["meta"] == {"reindex": False}
    assert abs(stages["total"] - sum(stages[k] for k in ("precheck", "cbm_index", "etl", "xcheck"))) < 0.01


def test_cli_perf_end_to_end(tmp_path, capsys):
    repo = _mk_repo(tmp_path, "clirepo")
    perf.record_run(repo, {"etl": 1.0, "total": 1.5})
    perf.record_run(repo, {"etl": 2.0, "total": 2.5})
    from vrp.cli import main as cli_main

    rc = cli_main(["perf", str(repo), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["entries"] == 2
    assert out["latest"]["stages"]["etl"] == 2.0
