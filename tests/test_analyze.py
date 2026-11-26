"""`vrp analyze` 测试（终端原生 AI 分析）。

单元：facts 状态判定/材料 cap 截断计数/材料字节确定性/prompt 模板。
E2E（chat_fn + run_pipeline_fn 注入）：陈旧自动重建 / 重建失败诚实退出 /
退避重试计数 / 审计留痕形状 / analysis.md 覆盖写 / key 零落盘。
红线：材料面确定（同 facts.db 同字节）；LLM 面只经 chat_fn 注入，零真 API。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import analyze, etl

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

API_KEY = "sk-test-secret-123"


@pytest.fixture(autouse=True)
def _fast_git_probe(monkeypatch):
    """git rev-parse 探针桩掉（test_fixpropose 同型）：fixture meta git_head=''
    == 桩值 '' → fresh；'deadbeef' 失配 → stale 照常真实判定。"""
    monkeypatch.setattr(etl, "current_git_head", lambda repo: "")


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'pkg.calc.total', 'Function', 'total', 'pkg/calc.py', 1, 3, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'pkg.calc.double', 'Function', 'double', 'pkg/calc.py', 5, 7, 'h')"
    )
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('f1', 'f2', 'CALLS', 'structural')")
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid,"
        " file_path, start_line, end_line, payload_json)"
        " VALUES ('P1', 'dead-import', 's2-deadcode', 'f1', NULL, 'pkg/calc.py', 1, 1, '{\"reason\": \"x\"}')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.commit()
    con.close()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "calc.py").write_text("def total(a, b):\n    return a + b\n", encoding="utf-8")
    _write_config(repo)
    return repo


def _write_config(repo: Path) -> None:
    payload = {"llm": {"base_url": "http://llm.example/v1", "api_key": API_KEY, "model": "test-model"}}
    (repo / ".vrp" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _fake_chat(responses):
    prompts = []

    def call(config, messages):
        prompts.append((config, messages))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return dict(item)
        return {"content": item, "finish_reason": "stop", "usage": {}}

    call.prompts = prompts
    return call


def _mk_stale(repo: Path) -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        con.execute("UPDATE meta SET value = 'deadbeef' WHERE key = 'git_head'")
        con.commit()
    finally:
        con.close()


def _mk_fresh(repo: Path) -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        con.execute("UPDATE meta SET value = '' WHERE key = 'git_head'")
        con.commit()
    finally:
        con.close()


# ---------- config ----------

def test_missing_config_human_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / ".vrp" / "config.json").unlink()
    with pytest.raises(SystemExit, match="run 'vrp config' first"):
        analyze.run_analyze(repo, chat_fn=_fake_chat(["report"]))


def test_bad_config_human_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / ".vrp" / "config.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(SystemExit, match="run 'vrp config' first"):
        analyze.run_analyze(repo, chat_fn=_fake_chat(["report"]))


# ---------- facts 状态与自动重建 ----------

def test_facts_missing_auto_rebuild(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / ".vrp" / "facts.db").unlink()
    said = []

    def fake_pipeline(r, name, print_fn=None):
        said.append(f"pipeline:{name}")
        _mk_repo_facts_only(r)

    summary = analyze.run_analyze(
        repo, chat_fn=_fake_chat(["报告"]), run_pipeline_fn=fake_pipeline, print_fn=said.append
    )
    assert summary["rc"] == 0
    assert said[0].startswith("facts missing - rebuilding automatically (S0->S1->ETL->xcheck)")
    assert "pipeline:repo" in said


def _mk_repo_facts_only(repo: Path) -> None:
    """fake 管道产物：补回一个 fresh 的最小 facts.db。"""
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.commit()
    con.close()


def test_facts_stale_auto_rebuild(tmp_path):
    repo = _mk_repo(tmp_path)
    _mk_stale(repo)
    said = []

    def fake_pipeline(r, name, print_fn=None):
        said.append("pipeline")
        _mk_fresh(r)

    summary = analyze.run_analyze(
        repo, chat_fn=_fake_chat(["报告"]), run_pipeline_fn=fake_pipeline, print_fn=said.append
    )
    assert summary["rc"] == 0
    assert said[0].startswith("facts stale - rebuilding automatically (S0->S1->ETL->xcheck)")
    assert "pipeline" in said


def test_fresh_facts_skip_rebuild(tmp_path):
    repo = _mk_repo(tmp_path)
    said = []

    def fake_pipeline(r, name, print_fn=None):
        said.append("MUST-NOT-RUN")

    summary = analyze.run_analyze(
        repo, chat_fn=_fake_chat(["报告"]), run_pipeline_fn=fake_pipeline, print_fn=said.append
    )
    assert summary["rc"] == 0
    assert said == []  # 新鲜 facts：零重建、零进度行


def test_rebuild_failure_honest_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / ".vrp" / "facts.db").unlink()

    def fake_pipeline(r, name, print_fn=None):
        raise RuntimeError("cbm not found")

    with pytest.raises(SystemExit, match="facts rebuild failed.*cbm not found"):
        analyze.run_analyze(repo, chat_fn=_fake_chat(["报告"]), run_pipeline_fn=fake_pipeline)


def test_rebuild_still_stale_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    _mk_stale(repo)

    def noop_pipeline(r, name, print_fn=None):
        pass  # 撒谎的管道：什么都没修

    with pytest.raises(SystemExit, match="facts still stale after rebuild"):
        analyze.run_analyze(repo, chat_fn=_fake_chat(["报告"]), run_pipeline_fn=noop_pipeline)


# ---------- 材料面 ----------

def test_material_cap_truncation_counts():
    parts = {"dossier_t1": {"x": "a" * 300}, "findings_top": {"y": "b" * 50}}
    text, stats = analyze.assemble_material_text(parts, cap=200)
    assert len(text) == 200
    assert stats["truncated"] is True
    assert stats["truncated_chars"] == stats["full_chars"] - 200
    assert stats["material_bytes"] == len(text.encode("utf-8"))


def test_material_within_cap_not_truncated():
    parts = {"findings_top": {"y": "b" * 50}}
    text, stats = analyze.assemble_material_text(parts, cap=200)
    assert stats["truncated"] is False
    assert stats["truncated_chars"] == 0
    assert "b" * 50 in text


def test_material_byte_deterministic(tmp_path):
    """诚实边界 pin：材料面同 facts.db 同字节（剔 generated_at 后两次装配全等）。"""
    repo = _mk_repo(tmp_path)
    parts1 = analyze.build_material_parts(repo)
    parts2 = analyze.build_material_parts(repo)
    assert json.dumps(parts1, ensure_ascii=False, sort_keys=True) == json.dumps(
        parts2, ensure_ascii=False, sort_keys=True
    )
    text1, _ = analyze.assemble_material_text(parts1)
    text2, _ = analyze.assemble_material_text(parts2)
    assert text1 == text2
    assert "generated_at" not in text1


def test_prompt_contains_template_and_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat(["报告"])
    summary = analyze.run_analyze(repo, chat_fn=fake)
    assert summary["rc"] == 0
    prompt = fake.prompts[0][1][0]["content"]
    for section in ("## Architecture overview", "## Main pathologies and evidence", "## Risk points", "## Recommended actions"):
        assert section in prompt
    assert "fid / P-code" in prompt
    assert '"P1"' in prompt  # findings P 码进材料
    assert "f1" in prompt  # dossier 符号进材料
    assert summary["findings_total"] == 1
    assert summary["findings_shown"] == 1


def test_findings_top_capped(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        for i in range(analyze.FINDINGS_TOP + 10):
            con.execute(
                "INSERT INTO findings (pathology, kind, source, file_path, start_line, end_line, payload_json)"
                f" VALUES ('P2', 'k{i}', 's2', 'pkg/calc.py', 1, 1, '{{}}')"
            )
        con.commit()
        result = analyze.top_findings(con)
    finally:
        con.close()
    assert result["shown"] == analyze.FINDINGS_TOP
    assert result["total"] == analyze.FINDINGS_TOP + 11


# ---------- timeline ----------

def test_with_timeline_non_git_skipped(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(analyze.timeline_mod, "run_timeline", lambda *a, **k: pytest.fail("must not run"))
    fake = _fake_chat(["报告"])
    summary = analyze.run_analyze(repo, with_timeline=True, chat_fn=fake)
    assert summary["rc"] == 0
    assert '"status": "skipped"' in fake.prompts[0][1][0]["content"] or "skipped" in fake.prompts[0][1][0]["content"]
    assert "not a git checkout" in fake.prompts[0][1][0]["content"]


def test_with_timeline_git_attached(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(etl, "current_git_head", lambda repo: "abc123")
    monkeypatch.setattr(
        analyze.timeline_mod,
        "run_timeline",
        lambda *a, **k: {"samples_requested": 20, "commits_total": 5, "points": [{"sha": "a"}], "turning_points": []},
    )
    fake = _fake_chat(["报告"])
    summary = analyze.run_analyze(repo, with_timeline=True, chat_fn=fake)
    assert summary["rc"] == 0
    assert "timeline" in fake.prompts[0][1][0]["content"]
    assert "turning_points" in fake.prompts[0][1][0]["content"]


# ---------- LLM 重试 ----------

def test_retry_backoff_then_ok(tmp_path):
    repo = _mk_repo(tmp_path)
    sleeps = []
    fake = _fake_chat([ValueError("HTTP 500: boom"), ValueError("HTTP 500: boom"), "报告"])
    summary = analyze.run_analyze(repo, chat_fn=fake, sleep_fn=sleeps.append)
    assert summary["rc"] == 0
    assert len(summary["attempts"]) == 3
    assert summary["attempts"][0]["outcome"] == "request_error"
    assert summary["attempts"][-1]["outcome"] == "ok"
    assert sleeps == [2.0, 4.0]  # 指数退避


def test_retry_exhausted_honest_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    sleeps = []
    fake = _fake_chat([ValueError("HTTP 500: a"), ValueError("HTTP 500: b"), ValueError("HTTP 500: c")])
    with pytest.raises(SystemExit, match="3 attempt"):
        analyze.run_analyze(repo, chat_fn=fake, sleep_fn=sleeps.append)
    assert sleeps == [2.0, 4.0]
    # 失败留痕仍完整（summary.json 落盘可审计）
    audits = list((repo / ".vrp" / "analyze").iterdir())
    assert len(audits) == 1
    failed = json.loads((audits[0] / "summary.json").read_text(encoding="utf-8"))
    assert failed["rc"] == 1
    assert len(failed["attempts"]) == 3


def test_empty_response_length_fast_fail(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat([{"content": "", "finish_reason": "length", "usage": {}}])
    with pytest.raises(SystemExit, match="max_output_tokens"):
        analyze.run_analyze(repo, chat_fn=fake)
    assert len(fake.prompts) == 1  # 快速失败：只烧一次


# ---------- 输出与审计 ----------

def test_analysis_md_overwrite(tmp_path):
    repo = _mk_repo(tmp_path)
    analyze.run_analyze(repo, chat_fn=_fake_chat(["第一版报告"]))
    analyze.run_analyze(repo, chat_fn=_fake_chat(["第二版报告"]))
    report = (repo / ".vrp" / "analysis.md").read_text(encoding="utf-8")
    assert report == "第二版报告"
    assert "第一版" not in report


def test_audit_shape_and_no_key_leak(tmp_path):
    repo = _mk_repo(tmp_path)
    summary = analyze.run_analyze(repo, chat_fn=_fake_chat(["报告"]))
    audit = Path(summary["audit_dir"])
    for name in (
        "config.echo.json",
        "material.json",
        "prompt.txt",
        "response.md",
        "usage.json",
        "summary.json",
    ):
        assert (audit / name).exists(), f"missing audit file: {name}"
    material = json.loads((audit / "material.json").read_text(encoding="utf-8"))
    assert material["stats"]["material_bytes"] == summary["material_bytes"]
    assert "dossier_t1" in material["parts"]
    assert "findings_top" in material["parts"]
    # 红线：key 零落盘（扫描整个留痕树）
    for path in (repo / ".vrp" / "analyze").rglob("*"):
        if path.is_file():
            assert API_KEY not in path.read_text(encoding="utf-8", errors="replace"), f"key leaked: {path}"


def test_summary_metadata_fields(tmp_path):
    repo = _mk_repo(tmp_path)
    summary = analyze.run_analyze(repo, chat_fn=_fake_chat(["报告"]))
    for key in ("run_id", "model", "material_bytes", "material_chars", "truncated_chars",
                "findings_total", "report_path", "audit_dir", "rc"):
        assert key in summary
    assert summary["model"] == "test-model"
    assert summary["report"] == "报告"
    assert summary["truncated_chars"] == 0
    assert summary["rc"] == 0


def test_same_second_audit_collision_suffix(tmp_path, monkeypatch):
    import time as _time

    repo = _mk_repo(tmp_path)
    frozen = _time.gmtime()
    monkeypatch.setattr(analyze.time, "gmtime", lambda: frozen)
    s1 = analyze.run_analyze(repo, chat_fn=_fake_chat(["一"]))
    s2 = analyze.run_analyze(repo, chat_fn=_fake_chat(["二"]))
    assert s1["run_id"] != s2["run_id"]
    assert s2["run_id"].endswith("-2")
    assert Path(s1["audit_dir"]) != Path(s2["audit_dir"])
