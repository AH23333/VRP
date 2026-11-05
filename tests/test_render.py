"""R3' render 测试：IR 生成 + Mermaid 源 + 报告复现性 + 降级路径。

R6' 增：staleness 横幅（M4 不对称修复）+ 渲染输出清理（孤儿修复）。
"""

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from vrp import render
from vrp import s6_transact

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.commit()
    con.close()
    return repo


def _seed(repo: Path) -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    for i, name in enumerate(("a", "b", "c")):
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?, 'p', ?, 'Function', ?, ?, ?, ?, 'h')",
            (f"fid-{name}", f"pkg.{name}", name, "pkg/x.py", i + 1, i + 3),
        )
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fid-a', 'fid-b', 'CALLS', 'structural')")
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fid-b', 'fid-c', 'CALLS', 'structural')")
    con.execute(
        "INSERT INTO findings (pathology, kind, source, file_path, start_line, payload_json)"
        " VALUES ('P1', 'k', 's2-astgrep', 'pkg/x.py', 2, '{}')"
    )
    con.execute(
        "INSERT INTO slices (slice_id, name, summary, payload_json, status) VALUES ('flow', '线', 's', ?, 'active')",
        (json.dumps({
            "steps": [{"fid": "fid-a", "role": "entry"}, {"fid": "fid-b"}, {"fid": "fid-c", "role": "persist"}],
            "acts": [{"name": "幕一", "from_step": 0, "to_step": 1}],
        }),),
    )
    con.execute(
        "INSERT INTO structure (payload_json, status) VALUES (?, 'active')",
        (json.dumps({
            "modules": [
                {"module_id": "m1", "name": "甲", "responsibility": "负责一", "symbol_fids": ["fid-a", "fid-b"], "layer": "domain"},
                {"module_id": "m2", "name": "乙", "responsibility": "负责二", "symbol_fids": ["fid-c"], "layer": "service"},
            ],
            "layers": [
                {"name": "domain", "may_depend_on": []},
                {"name": "service", "may_depend_on": ["domain"]},
            ],
        }),),
    )
    con.commit()
    con.close()


def _mk_verdict(repo: Path, tid: str = "tx-1", status: str = "needs_confirmation") -> None:
    d = repo / ".vrp" / "verdicts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.json").write_text(json.dumps({
        "tid": tid, "type": "T2-rename", "summary": "s", "status": status,
        "verdict_at": "2026-11-04T10:00:00+00:00", "gates": [], "confidence": 0.9,
    }), encoding="utf-8")


def _ro_con(repo: Path) -> sqlite3.Connection:
    db = repo / ".vrp" / "facts.db"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ----------------------------------------------------------------- IR 层


def test_architecture_ir_from_structure(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = _ro_con(repo)
    ir = render.build_architecture_ir(repo, con)
    con.close()
    assert ir["diagram_type"] == "architecture"
    ids = [c["id"] for c in ir["components"]]
    assert ids == ["m1", "m2"]  # 层序优先（domain < service）
    assert all(c["pos"] and c["size"] for c in ir["components"])  # 渲染必需
    labels = [b["label"] for b in ir["boundaries"]]
    assert labels == ["domain", "service"]
    conns = {(c["from"], c["to"]) for c in ir["connections"]}
    assert ("m1", "m2") in conns  # 跨模块实测边聚合


def test_architecture_ir_dir_fallback(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'q', 'Function', 'n', 'pkg/x.py', 1, 3, 'h')"
    )
    con.commit()
    con.close()
    con = _ro_con(repo)
    ir = render.build_architecture_ir(repo, con)
    con.close()
    assert ir["meta"]["title"].startswith("L0 directory")
    assert ir["connections"] == [] and ir["boundaries"] == []
    assert any(c["id"] == "pkg" for c in ir["components"])


def test_architecture_ir_rejected_structure_falls_back(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute("UPDATE structure SET status = 'rejected'")
    con.commit()
    con.close()
    con = _ro_con(repo)
    ir = render.build_architecture_ir(repo, con)
    con.close()
    assert ir["meta"]["title"].startswith("L0 directory")  # 非 active 不用


def test_sequence_ir(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = _ro_con(repo)
    ir = render.build_sequence_ir(repo, con, "flow")
    con.close()
    assert [p["id"] for p in ir["participants"]] == ["a", "b", "c"]
    assert len(ir["messages"]) == 2
    assert ir["messages"][0]["y"] == 160 and ir["messages"][1]["y"] == 210  # 等距
    assert ir["segments"][0]["label"] == "幕一"


def test_sequence_ir_unknown_slice(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = _ro_con(repo)
    with pytest.raises(SystemExit, match="unknown or inactive"):
        render.build_sequence_ir(repo, con, "nope")
    con.close()


def test_timeline_ir(tmp_path):
    verdicts = [
        {"tid": "tx-1", "type": "T2", "status": "committed", "verdict_at": "2026-11-04T10:00:00+00:00"},
        {"tid": "tx-2", "type": "T4", "status": "needs_confirmation", "verdict_at": "2026-11-04T11:00:00+00:00"},
    ]
    ir = render.build_timeline_ir(verdicts)
    assert ir["diagram_type"] == "timeline"
    assert ir["meta"]["evidence"] == "observed"  # archify 枚举
    kinds = [e["kind"] for e in ir["events"]]
    assert kinds == ["recovery", "alert"]


def test_sarif(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = _ro_con(repo)
    sarif = render.build_sarif(repo, con, "test")
    con.close()
    assert sarif["version"] == "2.1.0"
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "vrp" and run["tool"]["driver"]["version"] == "test"
    assert run["results"][0]["ruleId"] == "P1"
    loc = run["results"][0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "pkg/x.py" and loc["region"]["startLine"] == 2


# ----------------------------------------------------------------- Mermaid


def test_mermaid_sequence_acts_to_rect(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = _ro_con(repo)
    ir = render.build_sequence_ir(repo, con, "flow")
    con.close()
    src = render.mermaid_sequence(ir)
    assert src.startswith("sequenceDiagram")
    assert "participant a as a" in src
    assert "rect rgb(235, 235, 245)" in src and "Note over a,b: 幕一" in src
    assert "a->>b:" in src and "b->>c:" in src


def test_mermaid_architecture(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = _ro_con(repo)
    ir = render.build_architecture_ir(repo, con)
    con.close()
    src = render.mermaid_architecture(ir)
    assert src.startswith("graph TD")
    assert "m1" in src and "m2" in src
    assert 'm1 -->|"1"| m2' in src  # 跨模块实测边计数（fid-b->fid-c；同模块 a->b 不计）


# ----------------------------------------------------------------- report


def test_render_report_degraded_no_archify(tmp_path, monkeypatch):
    monkeypatch.delenv("VRP_ARCHIFY_MJS", raising=False)
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _mk_verdict(repo)
    result = render.render_report(repo)
    out = repo / ".vrp" / "report"
    assert result["archify"] is False
    assert any("archify not found" in r for r in result["degraded"])
    assert (out / "report.html").is_file()
    assert (out / "findings.sarif").is_file()
    assert (out / "ir" / "architecture.json").is_file()
    assert (out / "architecture.mmd").is_file()
    assert not (out / "html").exists() or not any((out / "html").iterdir())
    report = (out / "report.html").read_text(encoding="utf-8")
    assert "facts.git_head" in report
    assert "tx-1" in report  # verdict 清单进报告
    assert "needs_confirmation" in report


def test_render_report_reproducible_no_timestamp(tmp_path, monkeypatch):
    monkeypatch.delenv("VRP_ARCHIFY_MJS", raising=False)
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _mk_verdict(repo)
    r1 = render.render_report(repo)
    report1 = Path(r1["report"]).read_bytes()
    r2 = render.render_report(repo)
    report2 = Path(r2["report"]).read_bytes()
    assert report1 == report2  # 同 facts.db 字节级同报告（无时间戳）
    assert r1["ir_sha256"] == r2["ir_sha256"]


def test_render_report_git_head_missing_graceful(tmp_path, monkeypatch):
    """render 只读无 M4（决策点 tx_run 自有 M4 兜底）：meta 空不崩，git_head=''。"""
    monkeypatch.delenv("VRP_ARCHIFY_MJS", raising=False)
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute("DELETE FROM meta")
    con.commit()
    con.close()
    result = render.render_report(repo)
    report = Path(result["report"]).read_text(encoding="utf-8")
    assert "(none)" in report  # git_head 缺失显式标注，不崩


def test_render_report_real_archify(tmp_path):
    """有 archify 沙箱时全链：validate+render 出 html（E2E，浅 fixture）。"""
    mjs = render.find_archify_mjs()
    if mjs is None:
        pytest.skip("archify sandbox not available")
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _mk_verdict(repo)
    result = render.render_report(repo)
    assert result["archify"] is True
    assert result["degraded"] == []
    out = repo / ".vrp" / "report"
    assert (out / "html" / "architecture.html").is_file()
    assert (out / "html" / "slice-flow.html").is_file()
    assert (out / "html" / "timeline.html").is_file()
    # R6' 产物完整性不变量：渲出的每个 html 必被 report.html 链接（无孤儿）
    report_text = (out / "report.html").read_text(encoding="utf-8")
    for html_file in (out / "html").glob("*.html"):
        assert f"html/{html_file.name}" in report_text, f"孤儿 html：{html_file.name}"


# ------------------------------------------- R6'：staleness 横幅 + 输出清理


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _mk_git_repo(base: Path) -> Path:
    repo = _mk_repo(base)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "x.txt").write_text("1", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    return repo


def _set_git_head(repo: Path, head: str) -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute("UPDATE meta SET value = ? WHERE key = 'git_head'", (head,))
    con.commit()
    con.close()


def _head(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return proc.stdout.strip()


def test_staleness_none_when_fresh_or_nongit(tmp_path):
    repo = _mk_git_repo(tmp_path)
    assert render._staleness(repo, _head(repo)) is None
    assert render._staleness(repo, "") is None  # 无 git_head 不判定
    nongit = _mk_repo(tmp_path / "sub")
    assert render._staleness(nongit, "0" * 40) is None


def test_staleness_detected_when_behind(tmp_path):
    repo = _mk_git_repo(tmp_path)
    old = _head(repo)
    (repo / "x.txt").write_text("2", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "c2")
    st = render._staleness(repo, old)
    assert st is not None
    assert st["behind"] == 1 and st["head"] == _head(repo) and st["facts_head"] == old


def test_report_carries_stale_banner(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_git_repo(tmp_path)
    _seed(repo)
    _set_git_head(repo, _head(repo))
    (repo / "x.txt").write_text("2", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "c2")
    result = render.render_report(repo)
    text = Path(result["report"]).read_text(encoding="utf-8")
    assert "陈旧报告" in text
    assert "落后当前 HEAD 约 1 个提交" in text


def test_report_no_banner_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_git_repo(tmp_path)
    _seed(repo)
    _set_git_head(repo, _head(repo))
    result = render.render_report(repo)
    text = Path(result["report"]).read_text(encoding="utf-8")
    assert "陈旧报告" not in text


def test_render_report_cleans_orphans_keeps_external(tmp_path, monkeypatch):
    """R6' 病根四：渲染前清自有产物——上轮孤儿（timeline 孤儿教训）不得混入本轮。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    out = repo / ".vrp" / "report"
    (out / "html").mkdir(parents=True)
    (out / "ir").mkdir(parents=True)
    (out / "html" / "timeline.html").write_text("orphan", encoding="utf-8")
    (out / "ir" / "timeline.json").write_text("{}", encoding="utf-8")
    (out / "slice-dead.mmd").write_text("orphan", encoding="utf-8")
    (out / "old-report.html").write_text("x", encoding="utf-8")
    (out / "keep.txt").write_text("external", encoding="utf-8")
    render.render_report(repo)
    assert not (out / "html" / "timeline.html").exists()
    assert not (out / "ir" / "timeline.json").exists()
    assert not (out / "slice-dead.mmd").exists()
    assert (out / "old-report.html").exists()  # 非 render 自有名（只写 report.html），不动
    assert (out / "keep.txt").exists()  # 外部文件不动
    assert (out / "architecture.mmd").is_file()
    assert (out / "report.html").is_file()


# ------------------------------------------------------- 快材料（MCP）


def test_render_slice_material(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    material = render.render_slice(repo, "flow")
    assert material["slice_id"] == "flow"
    assert material["participants"] == 3 and material["messages"] == 2 and material["acts"] == 1
    assert material["mermaid"].startswith("sequenceDiagram")


def test_render_verdicts_material(tmp_path):
    repo = _mk_repo(tmp_path)
    _mk_verdict(repo, "tx-1", "needs_confirmation")
    _mk_verdict(repo, "tx-2", "committed")
    material = render.render_verdicts(repo)
    assert material["count"] == 2
    assert [o["tid"] for o in material["open"]] == ["tx-1"]
    assert [t["tid"] for t in material["t4_pending"]] == ["tx-1"]


# ----------------------------------------------------------------- CLI


def test_cli_report_wiring(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("VRP_ARCHIFY_MJS", raising=False)
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    from vrp import cli

    repo = _mk_repo(tmp_path)
    _seed(repo)
    rc = cli.main(["report", str(repo)])
    assert rc == 0
    assert (repo / ".vrp" / "report" / "report.html").is_file()
    assert "S8 report" in capsys.readouterr().out


def test_cli_report_out_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("VRP_ARCHIFY_MJS", raising=False)
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    from vrp import cli

    repo = _mk_repo(tmp_path)
    _seed(repo)
    out = tmp_path / "reports"
    rc = cli.main(["report", str(repo), "--out", str(out)])
    assert rc == 0
    assert (out / "report.html").is_file()
