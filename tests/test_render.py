"""R3' render 测试：IR 生成 + Mermaid 源 + 报告复现性 + 降级路径。

R6' 增：staleness 横幅（M4 不对称修复）+ 渲染输出清理（孤儿修复）。
R7' 增：L1/L2 多级下钻；R7'(2) 增：模块交互图链（chain/ 三段式原子提交，
成功替代 L1/L2，失败降级记录诚实标注）。
"""

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from vrp import render
from vrp import render_chain
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


def _mk_verdict(repo: Path, tid: str = "tx-1", status: str = "needs_confirmation",
                verdict_at: str = "2026-11-04T10:00:00+00:00") -> None:
    d = repo / ".vrp" / "verdicts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.json").write_text(json.dumps({
        "tid": tid, "type": "T2-rename", "summary": "s", "status": status,
        "verdict_at": verdict_at, "gates": [], "confidence": 0.9,
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
    assert "properties" not in run  # 未超 cap 无截断标注


def test_sarif_truncation_annotated(tmp_path):
    """R-C F-2：SARIF 超过 SARIF_FINDINGS_CAP 时截断，run properties 诚实标注总数。"""
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    cap = render.SARIF_FINDINGS_CAP
    con.executemany(
        "INSERT INTO findings (pathology, kind, source, file_path, start_line, payload_json)"
        " VALUES ('P9', 'k', 's2-astgrep', ?, ?, '{}')",
        [(f"pkg/f{i}.py", i + 1) for i in range(cap + 5)],
    )
    con.commit()
    con.row_factory = sqlite3.Row
    total = con.execute(
        "SELECT COUNT(*) FROM findings WHERE file_path IS NOT NULL AND file_path != ''"
    ).fetchone()[0]
    assert total > cap  # 前置：确已超限
    sarif = render.build_sarif(repo, con, "test")
    con.close()
    run = sarif["runs"][0]
    assert len(run["results"]) == cap
    props = run["properties"]
    assert props["findings_total"] == total and props["results_cap"] == cap


def test_timeline_ir_follows_verdict_at_order(tmp_path):
    """R-C F-3：时间线事件按 load_verdicts 顺序（verdict_at 升序），与 docstring 一致。"""
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _mk_verdict(repo, "tx-3", verdict_at="2026-11-04T11:00:00+00:00")
    _mk_verdict(repo, "tx-1", verdict_at="2026-11-04T09:00:00+00:00")
    _mk_verdict(repo, "tx-2", verdict_at="2026-11-04T10:00:00+00:00")
    verdicts = s6_transact.load_verdicts(repo)
    ir = render.build_timeline_ir(verdicts)
    assert [e["id"] for e in ir["events"]] == ["tx-1", "tx-2", "tx-3"]


def test_timeline_ir_skips_unreadable_placeholders(tmp_path):
    """R-O：损坏 verdict 文件的 unreadable 占位（verdict_at=""）不进时间线。

    at 空串违反 archify timeline schema 的 ISO 8601 pattern（必须带 Z 或
    ±HH:MM 偏移），一个半写/损坏 verdict 文件曾使整段 timeline validate
    失败、html/timeline.html 降级消失（timeline 无 mermaid 兜底）；机制
    不变量：timeline 事件必须是时间事实，损坏占位不是。诚实面不动：
    load_verdicts 清单仍完整含 unreadable 条目。
    """
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _mk_verdict(repo, "tx-1")  # 默认 needs_confirmation + 有效 verdict_at
    vdir = repo / ".vrp" / "verdicts"
    (vdir / "tx-bad.json").write_text("{not json", encoding="utf-8")  # 半写/损坏
    verdicts = s6_transact.load_verdicts(repo)
    assert [v["status"] for v in verdicts] == ["unreadable", "needs_confirmation"]  # 诚实面完整
    ir = render.build_timeline_ir(verdicts)
    assert [e["id"] for e in ir["events"]] == ["tx-1"]  # 仅时间事实进时间线
    at_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?(Z|[+-]\d{2}:\d{2})$")
    assert all(at_re.match(e["at"]) for e in ir["events"])  # 全 pattern 合规


def test_timeline_ir_all_unreadable_returns_none(tmp_path):
    """R-O：全部占位无时间戳 → None（空 events 被 archify minItems 拒，不出 timeline）。"""
    repo = _mk_repo(tmp_path)
    _seed(repo)
    vdir = repo / ".vrp" / "verdicts"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "tx-bad.json").write_text("{not json", encoding="utf-8")
    verdicts = s6_transact.load_verdicts(repo)
    assert verdicts and verdicts[0]["status"] == "unreadable"  # 前置：确实全是占位
    assert render.build_timeline_ir(verdicts) is None


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


def test_render_report_skips_corrupt_slice_payload(tmp_path, monkeypatch):
    """R-C F-1：坏 payload_json 的 active slice 被跳过（SystemExit 语义），不崩整报告。"""
    monkeypatch.delenv("VRP_ARCHIFY_MJS", raising=False)
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO slices (slice_id, name, summary, payload_json, status)"
        " VALUES ('zz-bad', 'bad', 's', 'NOT-JSON', 'active')"
    )
    con.commit()
    con.close()
    result = render.render_report(repo)  # 修复前此处抛 json.JSONDecodeError
    out = repo / ".vrp" / "report"
    assert (out / "report.html").is_file()
    assert (out / "slice-flow.mmd").is_file()  # 好 slice 正常渲染
    assert not (out / "slice-zz-bad.mmd").exists()  # 坏 slice 跳过
    assert result["counts"]["slices"] == 1  # 只有好 slice 计数


def test_build_chain_missing_structure_table(tmp_path):
    """R-C F-4：pre-R2' 旧库无 structure 表——build_chain 诚实降级，不裸抛 OperationalError。"""
    if shutil.which("node") is None:
        pytest.skip("node not in PATH")
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute("DROP TABLE structure")
    con.commit()
    con.close()
    res = render_chain.build_chain(repo, tmp_path / "chain", tmp_path / "no.mjs")
    assert res["ok"] is False
    assert any("structure" in n for n in res["notes"])


def test_render_report_real_archify(tmp_path):
    """有 archify 沙箱时全链：validate+render 出 html（E2E，浅 fixture）。"""
    mjs = render.find_archify_mjs()
    if mjs is None:
        pytest.skip("archify sandbox not available")
    if shutil.which("node") is None:
        pytest.skip("node not available")
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
    # R7'(2)：模块交互图链成功 → chain/ 出、L1/L2 不出
    assert result["chain"]["ok"] is True
    assert result["counts"]["chain_modules"] == 2
    assert result["counts"]["chain_pages"] == 4  # index + 2 网格 + 1 代码页
    assert (out / "chain" / "index.html").is_file()
    assert not (out / "modules").exists()
    assert not (out / "symbols").exists()
    assert (out / "ir" / "chain-index.json").is_file()
    assert not (out / "ir" / "modules-index.json").exists()
    # R6' 产物完整性不变量：渲出的每个 html 必被 report.html 链接（无孤儿）
    report_text = (out / "report.html").read_text(encoding="utf-8")
    for html_file in (out / "html").glob("*.html"):
        assert f"html/{html_file.name}" in report_text, f"孤儿 html：{html_file.name}"
    assert 'href="chain/index.html"' in report_text  # L0 → 链入口


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


def test_staleness_git_calls_pin_utf8_replace(tmp_path, monkeypatch):
    """R-L：两处 git 调用必须显式 encoding/errors——text=True 落 locale
    严格解码，读线程吞解码异常静默丢输出（stdout=None → .strip() 崩）。"""
    repo = _mk_git_repo(tmp_path)
    captured: list[dict] = []
    real_run = subprocess.run

    def spy_run(cmd, **kwargs):
        captured.append(kwargs)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(render.subprocess, "run", spy_run)
    st = render._staleness(repo, "0" * 40)
    assert st is not None  # "0"*40 != HEAD → 两条 git 调用都触发
    assert len(captured) == 2
    assert all(
        k.get("encoding") == "utf-8" and k.get("errors") == "replace" for k in captured
    )


def test_report_carries_stale_banner(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_git_repo(tmp_path)
    _seed(repo)
    _set_git_head(repo, _head(repo))
    (repo / "x.txt").write_text("2", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "c2")
    result = render.render_report(repo)
    text = Path(result["report"]).read_text(encoding="utf-8")
    assert "Stale report" in text
    assert "about 1 commit(s) behind current HEAD" in text


def test_report_no_banner_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_git_repo(tmp_path)
    _seed(repo)
    _set_git_head(repo, _head(repo))
    result = render.render_report(repo)
    text = Path(result["report"]).read_text(encoding="utf-8")
    assert "Stale report" not in text


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


# ------------------------------------------- R7'：L1 模块页 / L2 符号页（多级下钻）


def _seed_files(repo: Path) -> None:
    (repo / "pkg").mkdir(exist_ok=True)
    (repo / "pkg" / "x.py").write_text(
        "def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\ndef c():\n    return 3\n",
        encoding="utf-8",
    )


def test_module_and_symbol_pages_chain(tmp_path, monkeypatch):
    """主链优先：L0 report → L1 modules → L2 symbols（含源码片段）互链贯通。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _seed_files(repo)
    result = render.render_report(repo)
    out = repo / ".vrp" / "report"
    assert result["counts"]["module_pages"] == 2
    assert result["counts"]["symbol_pages"] == 3
    assert (out / "modules" / "m0-m1.html").is_file()
    assert (out / "modules" / "m1-m2.html").is_file()
    for fid in ("fid-a", "fid-b", "fid-c"):
        assert (out / "symbols" / f"{fid}.html").is_file()
    report = (out / "report.html").read_text(encoding="utf-8")
    assert 'href="modules/m0-m1.html"' in report  # L0 → L1
    assert 'href="modules/m1-m2.html"' in report
    mod = (out / "modules" / "m0-m1.html").read_text(encoding="utf-8")
    assert 'href="../report.html"' in mod  # L1 → L0
    assert 'href="../symbols/fid-b.html"' in mod  # L1 → L2
    sym = (out / "symbols" / "fid-a.html").read_text(encoding="utf-8")
    assert 'href="../report.html"' in sym  # L2 → L0
    assert 'href="../modules/m0-m1.html"' in sym  # L2 → L1
    assert "def a():" in sym  # 源码片段（真实文件）
    assert "flow" in sym  # 所属业务线
    assert "graph LR" in sym  # 邻域图


def test_interlink_integrity_all_targets_exist(tmp_path, monkeypatch):
    """互链完整性不变量：所有页面上每个 href 目标必须存在（静态多页无断链）。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _seed_files(repo)
    render.render_report(repo)
    out = repo / ".vrp" / "report"
    pages = [out / "report.html"]
    pages += sorted((out / "modules").glob("*.html"))
    pages += sorted((out / "symbols").glob("*.html"))
    assert len(pages) == 6  # 1 report + 2 模块页 + 3 符号页（确定性）
    for page in pages:
        text = page.read_text(encoding="utf-8")
        for target in re.findall(r'href="([^"]+)"', text):
            if target.startswith(("http://", "https://")):
                continue
            assert (page.parent / target).exists(), f"{page.name}: 断链 {target}"


def test_module_page_dir_fallback(tmp_path, monkeypatch):
    """structure rejected → 目录聚合兜底出模块页（与 L0 IR 同源语义）。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute("UPDATE structure SET status = 'rejected'")
    con.commit()
    con.close()
    result = render.render_report(repo)
    out = repo / ".vrp" / "report"
    assert result["counts"]["module_pages"] == 1  # 目录 pkg 聚合
    page = out / "modules" / "m0-pkg.html"
    assert page.is_file()
    assert "directory fallback" in page.read_text(encoding="utf-8")


def test_symbol_pages_global_cap_honest(tmp_path, monkeypatch):
    """符号页全局 cap：截断须诚实标注，不出页的符号不落文件。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    monkeypatch.setattr(render, "SYMBOL_PAGES_GLOBAL_CAP", 1)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    result = render.render_report(repo)
    out = repo / ".vrp" / "report"
    assert result["counts"]["symbol_pages"] == 1
    assert (out / "symbols" / "fid-a.html").is_file()  # m1 候选字典序首个
    assert not (out / "symbols" / "fid-b.html").exists()
    report = (out / "report.html").read_text(encoding="utf-8")
    assert "truncated" in report  # 诚实标注
    idx = json.loads((out / "ir" / "modules-index.json").read_text(encoding="utf-8"))
    assert idx["symbol_pages"] == ["fid-a"] and idx["symbol_pages_truncated"] is True


def test_code_snippet_variants(tmp_path):
    repo = tmp_path / "r"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "x.py").write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\n", encoding="utf-8")
    sn = render._code_snippet(repo, "pkg/x.py", 4, 4)
    assert sn is not None and sn["lo"] == 1 and sn["hi"] == 7 and not sn["truncated"]  # ±3 上下文
    assert sn["text"].splitlines()[0] == "l1"
    sn2 = render._code_snippet(repo, "pkg/x.py", 1, 1)
    assert sn2 is not None and sn2["lo"] == 1 and sn2["hi"] == 4 and not sn2["truncated"]
    short = tmp_path / "s"
    short.mkdir()
    (short / "f.py").write_text("only\n", encoding="utf-8")
    sn3 = render._code_snippet(short, "f.py", 1, 1)
    assert sn3 is not None and sn3["truncated"]  # EOF 截断（want_hi=4 > 1 行）
    assert render._code_snippet(repo, "missing.py", 1, 1) is None  # 文件缺失
    assert render._code_snippet(repo, "<synthetic>", 1, 1) is None  # 尖括号 synthetic 守卫
    assert render._code_snippet(repo, "../escape.py", 1, 1) is None  # .. 逃逸守卫
    assert render._code_snippet(repo, "C:/abs.py", 1, 1) is None  # 绝对路径守卫


def test_module_page_pathology_badges_and_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute("UPDATE findings SET anchor_fid = 'fid-a'")
    con.commit()
    con.close()
    render.render_report(repo)
    out = repo / ".vrp" / "report"
    mod = (out / "modules" / "m0-m1.html").read_text(encoding="utf-8")
    assert ">P1<" in mod  # 符号行病理徽章
    assert "pkg/x.py:2" in mod  # findings 表（按文件命中）
    sym = (out / "symbols" / "fid-a.html").read_text(encoding="utf-8")
    assert ">P1<" in sym
    assert "class s0 p1" in sym  # 邻域图焦点病理着色
    assert "class s1 p1" in mod  # 模块图成员病理着色（fid-a=s1）


def test_mermaid_module_graph(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = _ro_con(repo)
    md = render._module_page_data(con, 0, render._iter_modules(con)[0])
    con.close()
    src = render.mermaid_module_graph(md)
    assert src.startswith("graph LR")
    assert "classDef p1 fill:#fdf0f0,stroke:#b3261e" in src  # 病理着色样式恒备
    assert 's0["b"]' in src and 's1["a"]' in src  # 度序：b(2) > a(1)
    assert "s1 --> s0" in src  # a→b 实测边


def test_multilevel_pages_reproducible(tmp_path, monkeypatch):
    """复现契约扩展到 L1/L2：两次渲染全产物字节级全等（无时间戳/确定性排序）。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _seed_files(repo)
    r1 = render.render_report(repo)
    out = repo / ".vrp" / "report"

    def _snapshot(root: Path) -> dict:
        return {
            str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix in (".html", ".json", ".mmd", ".sarif")
        }

    snap1 = _snapshot(out)
    r2 = render.render_report(repo)
    snap2 = _snapshot(out)
    assert r1["ir_sha256"] == r2["ir_sha256"]
    assert len(snap1) >= 10  # report + 2 模块页 + 3 符号页 + ir 若干 + sarif + mmd
    assert snap1 == snap2


# ------------------------------------- R7'(2)：模块交互图链（archify 三段式）


def _need_archify():
    if render.find_archify_mjs() is None:
        pytest.skip("archify not available")
    if shutil.which("node") is None:
        pytest.skip("node not available")


def test_chain_happy_path(tmp_path):
    """链成功：chain/ 出、L1/L2 不出、report 链接贯通、chain-index 进 ir。"""
    _need_archify()
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _seed_files(repo)
    result = render.render_report(repo)
    out = repo / ".vrp" / "report"
    assert result["chain"]["ok"] is True
    assert result["degraded"] == []
    assert result["counts"]["chain_modules"] == 2  # m1 + m2
    assert result["counts"]["module_pages"] == 0  # L1 不出
    assert result["counts"]["symbol_pages"] == 0  # L2 不出
    chain = out / "chain"
    assert (chain / "index.html").is_file()
    assert (chain / "s3-m1-x.html").is_file()  # m1 网格（a、b）
    assert (chain / "s3-m2-x.html").is_file()  # m2 网格（c）
    assert (chain / "s4f-pkg-x.html").is_file()  # 唯一代码页（共享文件单次写）
    assert not (out / "modules").exists()
    assert not (out / "symbols").exists()
    idx = json.loads((out / "ir" / "chain-index.json").read_text(encoding="utf-8"))
    assert idx["kind"] == "chain-index"
    assert [m["module_id"] for m in idx["modules"]] == ["m1", "m2"]
    assert idx["file_pages"] == ["pkg/x.py"]
    report = (out / "report.html").read_text(encoding="utf-8")
    assert 'href="chain/index.html"' in report  # L0 → 链入口
    assert 'href="chain/s4f-pkg-x.html"' in report  # findings → 文件页


def test_chain_link_integrity(tmp_path):
    """链完整性不变量：href + M（JS 点击图）双源——每个目标文件与锚点必须存在。"""
    _need_archify()
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _seed_files(repo)
    render.render_report(repo)
    chain = repo / ".vrp" / "report" / "chain"
    pages = sorted(chain.glob("*.html"))
    assert [p.name for p in pages] == [
        "index.html", "s3-m1-x.html", "s3-m2-x.html", "s4f-pkg-x.html",
    ]
    texts = {p.name: p.read_text(encoding="utf-8") for p in pages}
    # 代码页含全部符号锚点（全局索引：m1 的 a/b + m2 的 c 共存一页）
    for anchor in ("sym-a", "sym-b", "sym-c"):
        assert f'id="{anchor}"' in texts["s4f-pkg-x.html"], anchor
    ids_by_page = {name: set(re.findall(r'id="([^"]+)"', t)) for name, t in texts.items()}
    for p in pages:
        hrefs = re.findall(r'href="([^"]+)"', texts[p.name])
        m = re.search(r"var M=(\{.*?\});var PG", texts[p.name], re.S)
        if m:  # 注入的点击图（href 之外的第二链接源）
            hrefs += list(json.loads(m.group(1)).values())
        assert hrefs, f"{p.name}: 无链接可验"
        for h in hrefs:
            if h.startswith(("http://", "https://")):
                continue
            file_part, _, frag = h.partition("#")
            target = p.parent / file_part if file_part else p
            assert target.exists(), f"{p.name}: 断链 {h}"
            if frag:
                assert frag in ids_by_page[target.name], f"{p.name}: 缺锚点 {h}"


def test_chain_dataflow_fid_clicks_direction(tmp_path):
    """S1 dataflow 节点下钻方向：M 键 = IR 节点 id（nid），值 = 文件页锚点。

    回归锁定（fa6213f 起存量 bug）：fid_clicks 若按 {fid: nid} 存，
    build_chain 消费按 {nid: fid} 解包得 clicks 空 → s1 页 JS 不注入、
    节点不可点；且链链接完整性测试对 M 缺失静默跳过（if m:），不设
    本测试则方向反转无红灯。
    """
    _need_archify()
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    for fid, name, fpath, start in (
            ("fid-a", "a", "pkg/x.py", 1), ("fid-c", "c", "pkg/y.py", 1)):
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?, 'p', ?, 'Function', ?, ?, ?, ?, 'h')",
            (fid, f"pkg.{name}", name, fpath, start, start + 2),
        )
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fid-a', 'fid-c', 'CALLS', 'structural')")
    con.execute(
        "INSERT INTO structure (payload_json, status) VALUES (?, 'active')",
        (json.dumps({
            "modules": [{"module_id": "m1", "name": "甲", "responsibility": "负责一",
                          "symbol_fids": ["fid-a", "fid-c"], "layer": "domain"}],
            "layers": [{"name": "domain", "may_depend_on": []}],
        }),),
    )
    con.commit()
    con.close()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "x.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (repo / "pkg" / "y.py").write_text("def c():\n    return 3\n", encoding="utf-8")
    render.render_report(repo)
    chain = repo / ".vrp" / "report" / "chain"
    page = chain / "s1-m1-dataflow.html"
    assert page.is_file()  # 2 文件真实符号 → s1 dataflow 入口
    text = page.read_text(encoding="utf-8")
    m = re.search(r"var M=(\{.*?\});var PG", text, re.S)
    assert m, "s1 dataflow 页缺节点点击 JS（fid_clicks 方向反转回归）"
    clicks = json.loads(m.group(1))
    ir = json.loads((chain / "ir" / "s1-m1-dataflow.json").read_text(encoding="utf-8"))
    node_ids = {n["id"] for n in ir["nodes"]}
    assert clicks, "M 为空：节点下钻全部丢失"
    assert set(clicks) <= node_ids, f"M 键须为 IR 节点 id 而非 fid: {sorted(clicks)}"
    for href in clicks.values():
        file_part, _, frag = href.partition("#")
        target = chain / file_part
        assert target.is_file(), f"断链 {href}"
        assert f'id="{frag}"' in target.read_text(encoding="utf-8"), f"缺锚点 {href}"


def test_chain_no_archify(tmp_path, monkeypatch):
    """无 archify：不出 chain/，counts 归零，L1/L2 兜底不受影响。"""
    monkeypatch.delenv("VRP_ARCHIFY_MJS", raising=False)
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    result = render.render_report(repo)
    out = repo / ".vrp" / "report"
    assert result["chain"]["ok"] is False
    assert result["counts"]["chain_modules"] == 0
    assert not (out / "chain").exists()
    assert result["counts"]["module_pages"] == 2  # L1/L2 照常


def test_chain_fake_mjs_degrades_to_l1(tmp_path, monkeypatch):
    """假 archify（存在但崩）：链失败整体清理 → L1/L2 兜底 + 降级记录。"""
    fake = tmp_path / "fake.mjs"
    fake.write_text('throw new Error("boom");', encoding="utf-8")
    monkeypatch.setenv("VRP_ARCHIFY_MJS", str(fake))
    repo = _mk_repo(tmp_path)
    _seed(repo)
    result = render.render_report(repo)
    out = repo / ".vrp" / "report"
    assert result["chain"]["ok"] is False
    assert not (out / "chain").exists()  # 失败链整体清理（无半成品）
    assert result["counts"]["chain_modules"] == 0
    assert result["counts"]["module_pages"] == 2  # L1 兜底
    assert (out / "modules" / "m0-m1.html").is_file()
    assert (out / "ir" / "modules-index.json").is_file()
    assert not (out / "ir" / "chain-index.json").exists()
    assert any("module chain degraded" in d for d in result["degraded"])


def test_chain_path_guard():
    """路径守卫：绝对/含冒号/.. 逃逸/synthetic 尖括号全拒。"""
    assert render_chain._path_ok("pkg/x.py")
    assert render_chain._path_ok("a/b/c.js")
    assert not render_chain._path_ok("")
    assert not render_chain._path_ok("C:/abs.py")  # 绝对 + 冒号
    assert not render_chain._path_ok("/abs.py")
    assert not render_chain._path_ok("../esc.py")  # .. 逃逸
    assert not render_chain._path_ok("a/../../b.py")
    assert not render_chain._path_ok("<synthetic>")  # synthetic 守卫
    assert not render_chain._path_ok("pkg/x:y.py")  # 冒号（Windows 盘符/流语法）


def test_chain_reproducible(tmp_path):
    """复现契约扩展到链：两次渲染全产物（含 chain/ 与 archify 图）字节级全等。"""
    _need_archify()
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _seed_files(repo)

    def _snapshot(root: Path) -> dict:
        return {
            str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix in (".html", ".json", ".mmd", ".sarif")
        }

    r1 = render.render_report(repo)
    out = repo / ".vrp" / "report"
    snap1 = _snapshot(out)
    assert any(k.startswith("chain/") for k in snap1)  # 链产物确实在快照内
    r2 = render.render_report(repo)
    snap2 = _snapshot(out)
    assert r1["ir_sha256"] == r2["ir_sha256"]
    assert snap1 == snap2


def test_report_user_sections(tmp_path, monkeypatch):
    """report.html 用户信息架构：引导/总览/体检/深层数据 + P 码图例 + findings 折叠。

    用户产物定位锁定：AI 梳理产出（模块职责/层规则）必须进用户主视觉，
    深层材料（SARIF/Datasette）归拢折叠节。
    """
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _seed_files(repo)
    render.render_report(repo)
    t = (repo / ".vrp" / "report" / "report.html").read_text(encoding="utf-8")
    assert "How to read this report" in t
    assert "Project overview" in t
    assert "Responsibility (AI-curated)" in t
    assert "负责一" in t and "负责二" in t  # AI 梳理职责透传（数据原样）
    assert "Layer rules" in t
    assert "Pathology checkup" in t
    assert "debug residue" in t  # P 码图例人话
    assert "Full findings index - click to expand" in t  # 裸清单降权折叠
    assert "Deep data (for AI agents & auditing)" in t
    assert "findings.sarif" in t  # SARIF 归入深层数据节
    assert "datasette .vrp/facts.db" in t
    # 体检节：_seed 的 P1 finding 计入（人话标题 + 来源）
    assert "P1 - debug residue" in t
    assert "s2-astgrep" in t


def test_chain_index_cards_show_curation(tmp_path):
    """index 卡片呈现 AI 梳理产出：职责一句话 + 层徽标 + 模块依赖 + 层序排序。"""
    _need_archify()
    repo = _mk_repo(tmp_path)
    _seed(repo)
    _seed_files(repo)
    render.render_report(repo)
    chain = repo / ".vrp" / "report" / "chain"
    idx = (chain / "index.html").read_text(encoding="utf-8")
    assert "负责一" in idx and "负责二" in idx  # 卡片职责（AI 梳理）
    assert 'class="lay">domain<' in idx and 'class="lay">service<' in idx
    assert "depends on: m2" in idx  # fid-b→fid-c 跨模块边聚合
    # 层序：domain 层的 m1 卡先于 service 层的 m2 卡
    assert idx.index("<h2>m1</h2>") < idx.index("<h2>m2</h2>")
    irj = json.loads((repo / ".vrp" / "report" / "ir" / "chain-index.json").read_text(encoding="utf-8"))
    m1 = next(m for m in irj["modules"] if m["module_id"] == "m1")
    assert m1["responsibility"] == "负责一"
    assert m1["layer"] == "domain"
    assert m1["depends_on"] == ["m2"]


# ------------------------------------------- archify validate 错误通道（R7' tsa 空括号病根）

def test_archify_error_text_json_stdout():
    """validate --json 失败：错误在 stdout JSON 的 error 字段（stderr 恒空）。"""
    stdout = json.dumps({"ok": False, "error": "sequence schema validation failed:\n  / must have required property 'meta'"})
    got = render._archify_error_text(stdout, "")
    assert "must have required property" in got
    assert got.startswith("sequence schema")


def test_archify_error_text_plain_stderr_fallback():
    got = render._archify_error_text("", "plain failure text")
    assert got == "plain failure text"


def test_archify_error_text_non_json_stdout_passthrough():
    got = render._archify_error_text("not json at all", "ignored")
    assert got == "not json at all"


def test_archify_error_text_limit():
    got = render._archify_error_text("x" * 500, "")
    assert len(got) == 120


def test_archify_error_text_empty_both():
    assert render._archify_error_text("", "") == ""


def test_render_chain_error_text_uses_stdout_json():
    """render_chain._archify_error_text 与 render 同语义（proc 对象输入）。"""
    proc = subprocess.CompletedProcess(["node"], 1, stdout=json.dumps({"ok": False, "error": "boom detail"}),
                                       stderr="")
    assert render_chain._archify_error_text(proc) == "boom detail"
    proc2 = subprocess.CompletedProcess(["node"], 1, stdout="", stderr="plain err")
    assert render_chain._archify_error_text(proc2) == "plain err"


# ------------------------------------------- _arch_id Unicode 守卫（R-H #79）
# 病根：str.isalnum() 放过 Unicode 字母——中文目录/模块名（如 AI_Animation 的
# 01_动效库）曾生成含中文 id，违反 archify $defs/id ^[a-zA-Z][a-zA-Z0-9_-]*$
# （schema38，纯 ASCII）→ validate 拒 → L0 模块图降级。

_ARCH_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


@pytest.mark.parametrize("raw", [
    "01_动效库",       # 实测项目目录（数字开头 + 中文）
    "动画模块",         # 纯中文模块 id
    "src/组件",        # 路径型目录
    "模块-Ⅻ",          # Unicode 数字/字母变体
    "«quoted»",        # 全非法字符（空清洗兜底）
])
def test_arch_id_unicode_inputs_match_archify_pattern(raw):
    assert _ARCH_ID_PATTERN.match(render._arch_id(raw))


@pytest.mark.parametrize("raw, expected", [
    ("api-gateway", "api-gateway"),
    ("core", "core"),
    ("01_tools", "n01_tools"),
    ("src/comp", "src-comp"),
])
def test_arch_id_ascii_inputs_unchanged(raw, expected):
    """ASCII 输入行为零变化（回归保护）。"""
    assert render._arch_id(raw) == expected


def test_architecture_ir_dir_fallback_unicode_dir(tmp_path):
    """AI_Animation 场景：中文目录名 → L0 目录聚合兜底 IR 组件 id 仍须过 archify 约束。"""
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'q', 'Function', 'n', '01_动效库/x.js', 1, 3, 'h')"
    )
    con.commit()
    con.close()
    con = _ro_con(repo)
    ir = render.build_architecture_ir(repo, con)
    con.close()
    assert ir["meta"]["title"].startswith("L0 directory")
    for comp in ir["components"]:
        assert _ARCH_ID_PATTERN.match(comp["id"]), comp["id"]


def test_build_sarif_information_uri_points_to_real_repo(tmp_path):
    """SARIF driver.informationUri 须指向真实仓库（曾编造 DeusData URL，R-H #79）。"""
    repo = _mk_repo(tmp_path)
    _seed(repo)
    con = _ro_con(repo)
    sarif = render.build_sarif(repo, con, "0.0.0-test")
    con.close()
    assert sarif["runs"][0]["tool"]["driver"]["informationUri"] == "https://github.com/AH23333/VRP"
