"""I5（构想v11 甜点）：`vrp explain` 错误码人话速查测试。

- 守卫双锁：GATE_EXPLANATIONS 键 == RULE_CODES 值全集；REJECT_EXPLANATIONS
  键 == AST 抽取 curate/auto_curate 源码的 SLICE_/STRUCT_/FILE_* 全串常量
  ——新码不写人话即红（防"码表有了人话没有"）
- 单源锁：P 码描述 == render.PATHOLOGY_LEGEND 值（零复制，直引）
"""

from __future__ import annotations

import json

from vrp import explain, render, s6_transact
from vrp.cli import build_parser, cmd_explain
from vrp.cli import main as cli_main


def test_gate_registry_matches_rule_codes_exactly():
    """gate 人话键 == s6_transact.RULE_CODES 值全集（双向，新闸门即强制文档化）。"""
    assert set(explain.GATE_EXPLANATIONS) == set(s6_transact.RULE_CODES.values())


def test_reject_registry_matches_extraction_exactly():
    """reject 人话键 == AST 抽取集（双向：源码新码未登记→红；登记幽灵码→红）。"""
    assert set(explain.REJECT_EXPLANATIONS) == set(explain._extract_reject_codes())


def test_extraction_finds_all_three_prefixes():
    """三前缀全中：直参（SLICE_SCHEMA）/循环元组（SLICE_EMPTY_NAME）/FILE_ 码。"""
    extracted = set(explain._extract_reject_codes())
    for code in (
        "SLICE_SCHEMA",
        "SLICE_EMPTY_NAME",  # 循环元组形态，非 _err 直参
        "SLICE_MISSING_EDGE",
        "STRUCT_LAYER_CYCLE",
        "STRUCT_RULE_LAYER",
        "FILE_NOT_JSON",
        "FILE_WRONG_SHAPE",
    ):
        assert code in extracted, f"{code} not extracted"
    assert all(
        explain._REJECT_RE.fullmatch(c) for c in extracted
    ), "extracted set must only contain fullmatch codes"


def test_extraction_excludes_prose_and_non_codes():
    """prompt 散文里的码（长串子串）不收；非码常量（CALLS 等）不收。"""
    extracted = set(explain._extract_reject_codes())
    assert "" not in extracted
    assert all(len(c) > 4 for c in extracted)
    for prose_marker in ("Rejection codes you may receive", "CALLS", "entry", "process"):
        assert prose_marker not in extracted


def test_explain_exact_gate_full_form():
    result = explain.run_explain("gate/G4/edge-mismatch")
    assert len(result["matches"]) == 1
    assert result["matches"][0]["code"] == "G4/edge-mismatch"
    assert result["matches"][0]["family"] == "gate"
    assert explain.explain_rc(result) == 0


def test_explain_gate_prefix_family():
    result = explain.run_explain("G4")
    codes = [m["code"] for m in result["matches"]]
    assert codes == [
        "G4/dangling-reference",
        "G4/edge-mismatch",
        "G4/structural-change",
        "G4/undeclared-change",
        "G4/verify-broken",
    ]
    assert explain.explain_rc(result) == 0


def test_explain_reject_exact_and_prefix():
    result = explain.run_explain("SLICE_MISSING_EDGE")
    assert len(result["matches"]) == 1
    assert "facts.db" in result["matches"][0]["description"]
    prefix = explain.run_explain("slice_")
    assert len(prefix["matches"]) >= 6
    assert {m["family"] for m in prefix["matches"]} == {"reject"}


def test_explain_pathology_single_sourced():
    result = explain.run_explain("P9")
    assert len(result["matches"]) == 1
    assert result["matches"][0]["family"] == "pathology"
    assert result["matches"][0]["description"] == render.PATHOLOGY_LEGEND["P9"]
    all_p = explain.run_explain("P")
    assert {m["code"] for m in all_p["matches"]} == set(render.PATHOLOGY_LEGEND)


def test_explain_case_insensitive():
    low = explain.run_explain("g4/edge-mismatch")
    assert len(low["matches"]) == 1
    mixed = explain.run_explain("Struct_Layer_Cycle")
    assert len(mixed["matches"]) == 1


def test_explain_unknown_rc1_with_hint():
    result = explain.run_explain("XYZ123")
    assert result["matches"] == []
    assert explain.explain_rc(result) == 1
    text = explain.render_text(result)
    assert "no code matches" in text
    assert "families:" in text


def test_overview_no_query():
    result = explain.run_explain(None)
    assert result["query"] is None
    assert explain.explain_rc(result) == 0
    assert set(result["families"]) == {"gate", "reject", "pathology"}
    assert result["counts"]["gate"] == len(s6_transact.RULE_CODES)
    assert result["counts"]["reject"] == len(explain.REJECT_EXPLANATIONS)
    assert result["counts"]["pathology"] == len(render.PATHOLOGY_LEGEND)
    text = explain.render_text(result)
    assert "gate" in text and "reject" in text and "pathology" in text


def test_render_match_lines():
    result = explain.run_explain("G0")
    text = explain.render_text(result)
    assert "G0/apply-edits [gate]" in text


def test_parser_optional_code():
    args = build_parser().parse_args(["explain"])
    assert args.code is None
    assert args.func is cmd_explain
    args2 = build_parser().parse_args(["explain", "P9", "--json"])
    assert args2.code == "P9" and args2.json is True
    assert not hasattr(args, "repo")


def test_cli_end_to_end_json_and_rc():
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main(["explain", "P9", "--json"])
    out = json.loads(buf.getvalue())
    assert rc == 0
    assert out["matches"][0]["code"] == "P9"

    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = cli_main(["explain", "NO_SUCH_CODE"])
    assert rc2 == 1
    assert "no code matches" in buf2.getvalue()
