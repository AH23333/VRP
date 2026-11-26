"""E 批：JSON 墙命令缺省英文人读面 + --json 机读面字节一致锁。

锁面（#E）：
- 23 个原"JSON 墙"命令缺省输出 _face_* 英文人读面（ASCII、无墙钟时间戳、
  非机器面不 json.loads 得出）；
- --json = 旧机读面字节不变（cli._dumps 载荷逐字节钉位）；
- 面函数对罐头载荷（真实形状二次验证过）渲染诚实：空结果一行、长列表
  _LIST_CAP 截断+计数。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vrp import cli


# --------------------------------------------------------------- 罐头载荷
# 形状 = 各 result 的真实键（E 批前二次验证），值 = ASCII 罐头。

PRECHECK = {
    "name": "canned-repo",
    "repo": "D:/x/canned-repo",
    "git": {"is_repo": True, "head": "abcdef1234567890", "branch": "main", "dirty": False,
            "dirty_files": [], "dirty_count": 0},
    "commands": {"test": "pytest", "build": "npm run build"},
    "languages": {"tool": "scc", "languages": [
        {"name": "Python", "files": 10, "lines": 1200},
        {"name": "TypeScript", "files": 4, "lines": 800},
    ]},
    "content_repo": {"detected": False},
}

INDEX = {
    "status": "ok", "project": "canned-repo", "db": "D:/x/cbmdb/canned-repo.db",
    "nodes": 1895, "edges": 7379, "not_indexed_files_count": 2,
    "parse_partial_count": 1, "parse_unusable_count": 0,
    "rebuild_reason": "new project",
}

ETL = {
    "mode": "incremental", "symbols": 1895, "edges": 6920, "usage_hints": 449,
    "history_coupling": 0, "test_linkage_cbm": 12, "test_linkage_js_ts": 3,
    "test_linkage_rust": 0, "findings_p2": 2, "facts_db": "D:/x/repo/.vrp/facts.db",
    "t_total_s": 4.2,
}

XCHECK_FULL = {
    "index": "ok", "ca_calls": 120, "cbm_calls": 118, "agreement": 110,
    "agreement_pct": 91, "ca_only": 6, "ca_only_builtin_target": 2,
    "cbm_only": 8, "cbm_only_test_source": 3, "ledger_rows": 14,
}
XCHECK_EARLY = {"index": "skipped", "ledger_rows": 0, "reason": "code-atlas js not found"}

S2 = {
    "total": 7, "anchored": 5, "rejected": 2, "dead_code_candidates": 9,
    "dead_code_by_confidence": {"high": 2, "medium": 4, "low": 3},
    "p4": {"knip": "skipped", "vulture": 4, "ledger_rows": 6},
    "p2": {"jscpd": 3, "ledger_rows": 3},
}

MUTATE_PLAN = {"budget": 20, "selected": 6, "plan": "D:/x/repo/.vrp/mutation-plan.json"}

MUTATE_RUN_OK = {
    "status": "ok", "budget": 2, "targets": 2, "files": 2, "mutants_total": 10,
    "killed": 7, "survived": 2, "timeout": 1, "surviving_targets": 2,
    "survival_rate": 0.2, "engines": {"mutmut": "ok"}, "findings": 2,
    "report": "D:/x/repo/.vrp/mutation-report.json",
}
MUTATE_RUN_NO_TARGETS = {"status": "no-targets", "budget": 2, "targets": 0}

ENFORCE_OK = {
    "status": "ok", "enforcer": "lint-imports", "kept": 5, "broken": 1,
    "violations": 1, "findings_written": 1, "anchored": 1,
}
ENFORCE_MIXED = {
    "status": "partial", "languages": {
        "python": {"status": "ok", "kept": 5, "broken": 1},
        "go": {"status": "skipped", "reason": "no Go modules"},
    },
    "cross_language_edges": 0,
}

VALIDATE = {
    "anchors": {"counts": {"active": 12, "cosmetic": 3, "stale": 2},
                "stale_reasons": {"file-gone": 1, "span-shift": 1}},
    "coverage": {"total": 15, "annotated": 12},
}

GATE = {
    "accepted": 3,
    "rejected": [
        {"index": 1, "reason": "anchor not found"},
        {"index": 4, "reason": "quote mismatch"},
    ],
}

FREEZE = {
    "git": {"commit": "abcdef1234567890abcdef", "branch": "main"},
    "commands": {"test": ["pytest -q (rc=0)"]},
    "graph_baseline": {"status": "captured", "bytes": 1234, "artifact": ".vrp/baseline.json"},
    "test_impact_truncated": False,
}

DOSSIER = {"generated_at": "2026-11-26T00:00:00Z", "project": "canned-repo",
           "budget": 32000, "task": "T1", "out": "D:/x/repo/.vrp/dossier.json"}

VERDICTS = {"count": 2, "verdicts": [
    {"tid": "tx-1", "type": "T2", "status": "committed", "failed_gate": None,
     "confidence": 0.9, "verdict_at": "2026-11-04T00:00:00Z"},
    {"tid": "tx-2", "type": "T4", "status": "rolled_back", "failed_gate": "G3",
     "confidence": None, "verdict_at": "2026-11-04T01:00:00Z"},
]}

CURATE_REFRESH = {
    "refreshed_at": "2026-11-26T00:00:00Z",
    "structure": {"action": "refreshed", "remapped": 2, "dropped": 0},
    "slices": [
        {"slice_id": "s-auth", "status": "ok", "action": "refreshed"},
        {"slice_id": "s-db", "status": "unremappable", "action": "skipped",
         "reason": "ambiguous fid"},
    ],
}

SEAMS = {
    "level": 1, "structure": "active",
    "modules": ["auth", "db", "api"],
    "seams": [
        {"from": "api", "to": "auth", "edges": 8,
         "by_type": {"CALLS": 6, "IMPORTS": 2}, "fid_pairs": 7, "capped": False,
         "top_edges": []},
    ],
    "internal_edges": 40, "unassigned_edges": 3,
    "note": "",
}
SEAMS_MISSING = {"level": 1, "structure": "missing",
                 "note": "no ingested structure - run 'vrp curate' first"}

PROPOSE_OK = {
    "ok": True, "proposal": "D:/x/repo/.vrp/proposals/p-001.json",
    "candidates": 120, "modules": 5, "target_modules": 5,
    "module_sizes": {"auth": 40, "db": 35, "api": 25, "ui": 12, "util": 8},
}

TRACES = {
    "ok": True, "label": "run-1", "trace_items": 50, "distinct_pairs": 20,
    "stored_pairs": 18, "skipped_unresolved": 2,
    "agreement": {"agreed": 15, "observed_only": 3, "static_only": 5,
                  "static_pairs_total": 20, "observed_only_sample": ["a -> b"]},
    "unresolved_sample": ["missing_mod.fn"],
}

TESTCOV = {
    "ok": True, "label": "cov-1", "files_in_run": 10, "files_matched": 8,
    "lines_executed_total": 900,
    "agreement": {"linkage_targets": 6, "linked_and_covered": 4,
                  "linked_not_covered_measured": 1,
                  "linked_not_covered_unmeasured": 1, "linked_undetermined": 0,
                  "covered_not_linked": 2},
    "unresolved_files": ["gen_file.py"], "unresolved_files_total": 2,
}

GOLDEN = {
    "name": "canned-audit", "language": "python",
    "faces": {"violations": {"truth": 3, "actual": 3, "matched": 2, "missing": 1,
                             "spurious": 1, "precision": 0.67, "recall": 0.67,
                             "missing_samples": [], "spurious_samples": []}},
}

COVERAGE = {
    "mode": "summary", "total_rows": 9,
    "kinds": {"not_indexed": {"count": 5, "samples": ["a.py", "b.py"]},
              "parse_partial": {"count": 4, "samples": ["c.py"]}},
}

IMPACT = {
    "base": "HEAD", "direction": "inbound", "depth": 2,
    "changed_files": ["pkg/a.py"], "impacted_total": 12, "saturated": False,
    "suggested_tests": [{"count": 3, "qns": ["tests/test_a.py::test_x"]}],
}

REPORT = {
    "report": "D:/x/repo/.vrp/report/report.html",
    "artifacts": ["D:/x/repo/.vrp/report/report-ir.json", "D:/x/repo/.vrp/report/findings.sarif"],
    "archify": False,
    "degraded": ["archify: node missing - static Mermaid fallback"],
    "counts": {"slices": 3, "verdicts": 2, "sarif_results": 7},
}

STATUS_FULL = {
    "meta": {"git_head": "abcdef1234"},
    "counts": {"symbols": 1895, "edges": 6920, "findings": 13},
    "edges_by_evidence": {"heuristic": 3426, "structural": 3494},
    "freshness": {"state": "stale", "facts_head": "old", "head": "abcdef1234", "behind": 3},
    "cbm_version": {"recorded": "0.12.1", "current": "0.12.2", "stale": True,
                    "note": "soft hint (not a gate)"},
}


# --------------------------------------------------------------- Part A：面渲染（全 23 面）

def test_all_faces_ascii_english():
    """全部面：ASCII 英文（罐头载荷下零 CJK/全角），且不伪装成 JSON。"""
    faces = [
        cli._face_precheck(PRECHECK),
        cli._face_index(INDEX),
        cli._face_etl(ETL),
        cli._face_xcheck(XCHECK_FULL),
        cli._face_s2(S2),
        cli._face_mutate_plan(MUTATE_PLAN),
        cli._face_mutate_run(MUTATE_RUN_OK),
        cli._face_enforce(ENFORCE_OK),
        cli._face_validate(VALIDATE),
        cli._face_gate(GATE),
        cli._face_freeze(FREEZE),
        cli._face_dossier(DOSSIER),
        cli._face_verdicts(VERDICTS),
        cli._face_curate_refresh(CURATE_REFRESH),
        cli._face_seams(SEAMS),
        cli._face_propose(PROPOSE_OK),
        cli._face_traces(TRACES),
        cli._face_testcov(TESTCOV),
        cli._face_golden(GOLDEN),
        cli._face_coverage(COVERAGE),
        cli._face_impact(IMPACT),
        cli._face_report(REPORT),
        cli._face_status(STATUS_FULL, "canned-repo"),
    ]
    assert len(faces) == 23
    for text in faces:
        assert text.isascii(), f"non-ASCII in face: {text!r}"
        assert not text.lstrip().startswith("{"), f"face looks like a JSON wall: {text[:40]!r}"


def test_face_precheck_markers():
    text = cli._face_precheck(PRECHECK)
    assert text.startswith("vrp precheck - canned-repo")
    assert "branch=main" in text and "dirty=0" in text
    assert "test=pytest" in text
    assert "Python 10 files/1,200 lines" in text


def test_face_index_gaps_line():
    text = cli._face_index(INDEX)
    assert "nodes       : 1,895" in text
    assert "2 files not indexed, 1 parse-partial, 0 parse-unusable" in text


def test_face_xcheck_early_vs_full():
    early = cli._face_xcheck(XCHECK_EARLY)
    assert "skipped" in early and "code-atlas js not found" in early
    full = cli._face_xcheck(XCHECK_FULL)
    assert "agreement: 91%" in full
    assert "ca_only 6" in full


def test_face_mutate_run_variants():
    ok = cli._face_mutate_run(MUTATE_RUN_OK)
    assert "killed 7, survived 2, timeout 1" in ok
    assert "survival    : 20.0%" in ok
    no_t = cli._face_mutate_run(MUTATE_RUN_NO_TARGETS)
    assert "no mutation targets" in no_t


def test_face_enforce_mixed_languages():
    mixed = cli._face_enforce(ENFORCE_MIXED)
    assert "python     : ok" in mixed
    assert "go         : skipped - no Go modules" in mixed


def test_face_verdicts_keeps_verdict_at():
    """verdict_at 是数据列（非墙钟），面里保留供人核对。"""
    text = cli._face_verdicts(VERDICTS)
    assert "tx-1  T2  committed" in text
    assert "tx-2  T4  rolled_back (G3)" in text
    assert "2026-11-04T01:00:00Z" in text


def test_face_seams_missing_honest():
    text = cli._face_seams(SEAMS_MISSING)
    assert text == "vrp seams - no ingested structure - run 'vrp curate' first"


def test_face_report_layout():
    text = cli._face_report(REPORT)
    assert text.splitlines()[0] == "Report generated: D:/x/repo/.vrp/report/report.html"
    assert "  - D:/x/repo/.vrp/report/findings.sarif" in text
    assert "archify - node missing - static Mermaid fallback" in text  # ': ' 轻变换为 ' - '
    assert "counts: slices 3, verdicts 2, sarif_results 7" in text
    assert text.endswith("open D:/x/repo/.vrp/report/report.html in a browser")


def test_face_status_stale_behind():
    text = cli._face_status(STATUS_FULL, "canned-repo")
    assert "counts      : symbols 1,895, edges 6,920, findings 13" in text
    assert "freshness   : stale (3 commits behind HEAD) - re-run 'vrp run' to refresh" in text
    assert "cbm         : recorded 0.12.1, current 0.12.2 (stale)" in text


def test_face_gate_rejected_list():
    text = cli._face_gate(GATE)
    assert "claims      : 3 accepted / 2 rejected" in text
    assert "claim #1 rejected - anchor not found" in text


def test_face_list_cap_truncation():
    """长列表截断到 _LIST_CAP + 计数行（诚实：不是静默吞）。"""
    big = dict(GATE, accepted=0,
               rejected=[{"index": i, "reason": "r"} for i in range(cli._LIST_CAP + 5)])
    text = cli._face_gate(big)
    assert f"... 5 more" in text
    assert text.count("rejected - r") == cli._LIST_CAP


# --------------------------------------------------------------- Part B：CLI 走线 + --json 字节钉


class _FakeCon:
    def close(self) -> None:  # cmd_validate finally 面需要
        pass


def _assert_json_bytes(payload, out):
    assert out == cli._dumps(payload) + "\n"


def test_wiring_precheck(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli.s0_precheck, "precheck", lambda r: PRECHECK)
    assert cli.main(["precheck", str(repo)]) == 0
    face = capsys.readouterr().out
    assert face.startswith("vrp precheck - canned-repo")
    assert cli.main(["precheck", str(repo), "--json"]) == 0
    _assert_json_bytes(PRECHECK, capsys.readouterr().out)


def test_wiring_index(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "canned-repo"
    repo.mkdir()
    monkeypatch.setattr(cli.s1_cbm, "index_repository",
                        lambda r, name, reindex=False: INDEX)
    assert cli.main(["index", str(repo)]) == 0
    face = capsys.readouterr().out
    assert face.startswith("vrp index - CBM repository index")
    assert cli.main(["index", str(repo), "--json"]) == 0
    _assert_json_bytes(INDEX, capsys.readouterr().out)


def test_wiring_etl(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli.etl_mod, "run_etl", lambda r, name, full=False: ETL)
    assert cli.main(["etl", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("vrp etl - CBM index -> facts.db")
    assert cli.main(["etl", str(repo), "--json"]) == 0
    _assert_json_bytes(ETL, capsys.readouterr().out)


def test_wiring_xcheck(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    (repo / ".vrp" / "facts.db").write_bytes(b"")
    monkeypatch.setattr(cli.xcheck, "run_xcheck", lambda r, db, reindex=False: XCHECK_FULL)
    assert cli.main(["xcheck", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("vrp xcheck - code-atlas cross-validation")
    assert cli.main(["xcheck", str(repo), "--json"]) == 0
    _assert_json_bytes(XCHECK_FULL, capsys.readouterr().out)


def test_wiring_seams(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli.nav_mod, "nav_seams", lambda r: SEAMS)
    assert cli.main(["seams", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("vrp seams - cross-module edges")
    assert cli.main(["seams", str(repo), "--json"]) == 0
    _assert_json_bytes(SEAMS, capsys.readouterr().out)


def test_wiring_propose(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli.propose_mod, "propose_structure", lambda r, **kw: PROPOSE_OK)
    assert cli.main(["propose", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("vrp propose - deterministic structure")
    assert cli.main(["propose", str(repo), "--json"]) == 0
    _assert_json_bytes(PROPOSE_OK, capsys.readouterr().out)


def test_wiring_golden(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    truth = tmp_path / "truth.json"
    truth.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli.golden_mod, "run_audit", lambda r, f: GOLDEN)
    assert cli.main(["golden", str(repo), "--file", str(truth)]) == 0
    assert capsys.readouterr().out.startswith("vrp golden - canned-audit (python)")
    assert cli.main(["golden", str(repo), "--file", str(truth), "--json"]) == 0
    _assert_json_bytes(GOLDEN, capsys.readouterr().out)


def test_wiring_coverage(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli.coverage_mod, "check_coverage",
                        lambda r, paths=None, kinds=None: COVERAGE)
    assert cli.main(["coverage", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("vrp coverage - index completeness")
    assert cli.main(["coverage", str(repo), "--json"]) == 0
    _assert_json_bytes(COVERAGE, capsys.readouterr().out)


def test_wiring_impact(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli.impact_mod, "impact_report", lambda r, **kw: IMPACT)
    assert cli.main(["impact", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("vrp impact - change blast radius")
    assert cli.main(["impact", str(repo), "--json"]) == 0
    _assert_json_bytes(IMPACT, capsys.readouterr().out)


def test_wiring_report(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    import vrp.render as render_mod

    monkeypatch.setattr(render_mod, "render_report", lambda r, out=None: REPORT)
    assert cli.main(["report", str(repo)]) == 0
    face = capsys.readouterr().out
    assert face.startswith("Report generated: ")
    # report --json = _print_step 型机读面（旧字节：标题 + JSON）
    assert cli.main(["report", str(repo), "--json"]) == 0
    assert capsys.readouterr().out == f"== S8 report ==\n{cli._dumps(REPORT)}\n"


def test_wiring_status_missing_db(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert cli.main(["status", str(repo)]) == 0
    face = capsys.readouterr().out
    assert face.startswith("vrp status - repo")
    assert "not found - run 'vrp run' first" in face
    assert cli.main(["status", str(repo), "--json"]) == 0
    _assert_json_bytes({"facts_db": None}, capsys.readouterr().out)


def test_wiring_validate_rich(tmp_path, monkeypatch, capsys):
    """validate 走真库太重——monkeypatch _open_facts_rw + s3 读函数走罐头。"""
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    (repo / ".vrp" / "facts.db").write_bytes(b"")
    monkeypatch.setattr(cli, "_open_facts_rw",
                        lambda r: (_FakeCon(), r / ".vrp" / "facts.db"))
    monkeypatch.setattr(cli.s3, "validate_anchors", lambda con, r: VALIDATE["anchors"])
    monkeypatch.setattr(cli.s3, "coverage", lambda con: {"total": 15, "annotated": 12})
    assert cli.main(["validate", str(repo)]) == 0
    face = capsys.readouterr().out
    assert face.startswith("vrp validate - anchor revalidation + coverage")
    assert "12 active / 3 cosmetic / 2 stale" in face
    assert cli.main(["validate", str(repo), "--json"]) == 0
    _assert_json_bytes(VALIDATE, capsys.readouterr().out)


def test_wiring_gate_missing_claims_sentence(tmp_path, capsys):
    """E 批 P1：gate 缺 claims 文件报错补"是什么/在哪"句。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(SystemExit, match="S3 evidence-gate claims"):
        cli.main(["gate", str(repo), str(tmp_path / "missing.json")])
