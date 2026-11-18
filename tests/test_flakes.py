"""L15（构想v11 甜点）：flake 登记表测试。

- schema 守卫：category/status 在册、test_id 形状、count>=1、日期形状、
  无重复、isolation_candidate == (count>=2) 强一致
- 漂移守卫：active 条目文件+函数在树（真实树核验）；改名/删除=红；
  resolved/renamed 豁免
- 判例锁：种子条目（#120 观察记实）在册；首观测不进表纪律（fd0d7db
  testsig/rawpack 判例不进表——定案缺陷修了、环境瞬态记档）
- 抖王名单：count>=2 过滤+隔离候选
- render/CLI E2E
"""

from __future__ import annotations

import json

from vrp import flakes
from vrp.cli import main as cli_main


def _entry(**overrides) -> dict:
    base = {
        "test_id": "tests/test_flakes.py::test_placeholder",
        "category": "timing",
        "status": "active",
        "count": 1,
        "first_seen": "2026-11-18",
        "last_seen": "2026-11-18",
        "isolation_candidate": False,
        "note": "",
    }
    base.update(overrides)
    return base


def test_seed_registry_valid_against_real_tree():
    """种子条目 schema+漂移双绿（真树核验：s7 测试在树）。"""
    assert flakes.validate() == []
    assert flakes.FLAKES[0]["test_id"].endswith("test_execute_timeout_tree_killed_returns_none")


def test_seed_first_observation_not_registered():
    """首观测不进表纪律：fd0d7db 两判例（rawpack 定案缺陷已修/testsig 环境瞬态）
    与 #170 push 负载红不进表——表内只留可核验 active 条目。"""
    blob = json.dumps(flakes.FLAKES, ensure_ascii=False)
    assert "rawpack" not in blob and "testsig" not in blob
    assert len(flakes.FLAKES) == 1


def test_validate_reports_bad_schema():
    entries = [
        _entry(category="bogus"),
        _entry(test_id="no-double-colon", status="weird", count=0, first_seen="11-18", last_seen="x"),
    ]
    problems = flakes.validate(entries)
    assert any("unknown category" in p for p in problems)
    assert any("not in 'path::name' form" in p for p in problems)
    assert any("unknown status" in p for p in problems)
    assert any("count must be int >= 1" in p for p in problems)
    assert any("not YYYY-MM-DD" in p for p in problems)


def test_validate_reports_duplicate_and_candidate_mismatch():
    dup = _entry(test_id="tests/test_a.py::test_x", count=2, isolation_candidate=False)
    entries = [dup, _entry(test_id="tests/test_a.py::test_x")]
    problems = flakes.validate(entries)
    assert any("duplicate test_id" in p for p in problems)
    assert any("isolation_candidate must equal" in p for p in problems)


def test_validate_active_missing_file_and_renamed_function():
    missing = _entry(test_id="tests/test_gone.py::test_x")
    renamed = _entry(test_id="tests/test_flakes.py::test_no_such_function_here")
    problems = flakes.validate([missing, renamed])
    assert any("test file missing" in p for p in problems)
    assert any("test function not found" in p for p in problems)
    # resolved/renamed 豁免：同形状非 active 不报
    assert flakes.validate([_entry(test_id="tests/test_gone.py::test_x", status="resolved")]) == []
    assert flakes.validate([_entry(test_id="tests/test_gone.py::test_x", status="renamed")]) == []


def test_shake_kings_filter():
    entries = [
        _entry(test_id="tests/test_a.py::test_once", count=1),
        _entry(test_id="tests/test_a.py::test_twice", count=2, isolation_candidate=True),
        _entry(test_id="tests/test_a.py::test_thrice", count=3, isolation_candidate=True),
    ]
    kings = flakes.shake_kings(entries)
    assert [k["count"] for k in kings] == [3, 2]


def test_render_text_shape(capsys):
    text = flakes.render_text(flakes.registry())
    assert "[观测]" in text  # 种子 count=1 非抖王
    assert "test_execute_timeout_tree_killed_returns_none" in text
    assert "timing/active" in text
    assert "summary: 1 条目  抖王 0  隔离候选 0" in text
    assert "[drift]" not in text  # 种子零漂移
    kings_text = flakes.render_text(
        [_entry(test_id="tests/test_a.py::test_twice", count=2, isolation_candidate=True)]
    )
    assert "[抖王]" in kings_text
    assert "summary: 1 条目  抖王 1  隔离候选 1" in kings_text
    empty_text = flakes.render_text([])
    assert "登记表空" in empty_text


def test_flakes_rc_gate_semantics():
    assert flakes.flakes_rc(flakes.registry()) == 0
    assert flakes.flakes_rc([_entry(category="bogus")]) == 1  # 漂移是门
    assert flakes.flakes_rc([]) == 0


def test_cli_end_to_end(capsys):
    rc = cli_main(["flakes", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["entries"][0]["test_id"].endswith("test_execute_timeout_tree_killed_returns_none")
    rc = cli_main(["flakes"])
    text = capsys.readouterr().out
    assert rc == 0
    assert "vrp flakes" in text and "[观测]" in text
