"""R3.5' session 测试：快照/计数 diff/verdict 窗口/双图并排报告/复现性/降级。"""

import json
import pathlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vrp import cli
from vrp import render as render_mod
from vrp import session

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', 'abc123')")
    con.commit()
    con.close()
    return repo


def _seed(repo: Path) -> None:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    for i, name in enumerate(("a", "b")):
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?, 'p', ?, 'Function', ?, 'pkg/x.py', ?, ?, 'h')",
            (f"fid-{name}", f"pkg.{name}", name, i + 1, i + 3),
        )
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fid-a', 'fid-b', 'CALLS', 'structural')")
    con.execute(
        "INSERT INTO findings (pathology, kind, source, file_path, start_line, payload_json)"
        " VALUES ('P1', 'k', 's2-astgrep', 'pkg/x.py', 2, '{}')"
    )
    con.commit()
    con.close()


def _mutate(repo: Path) -> None:
    """初末之间发生的"重构"：+1 符号 +1 病理 +1 xcheck。"""
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('fid-c', 'p', 'pkg.c', 'Function', 'c', 'pkg/x.py', 9, 11, 'h')"
    )
    con.execute(
        "INSERT INTO findings (pathology, kind, source, file_path, start_line, payload_json)"
        " VALUES ('P2', 'k', 's2-astgrep', 'pkg/x.py', 10, '{}')"
    )
    con.execute(
        "INSERT INTO discrepancy_ledger (category, detail_json) VALUES ('missing-edge', '{}')"
    )
    con.commit()
    con.close()


def _mk_verdict(repo: Path, tid: str, verdict_at: str) -> None:
    d = repo / ".vrp" / "verdicts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.json").write_text(json.dumps({
        "tid": tid, "type": "T2", "summary": "s", "status": "committed",
        "verdict_at": verdict_at, "gates": [], "confidence": 0.9,
    }), encoding="utf-8")


def _no_archify(monkeypatch):
    monkeypatch.delenv("VRP_ARCHIFY_MJS", raising=False)
    monkeypatch.setattr(render_mod, "find_archify_mjs", lambda: None)


# ----------------------------------------------------------------- begin/end


def test_begin_creates_snapshot_and_metadata(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    result = session.session_begin(repo, "round one")
    sid = result["session_id"]
    sdir = repo / ".vrp" / "sessions" / sid
    assert (sdir / "begin.db").is_file()
    doc = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    assert doc["note"] == "round one"
    assert doc["ended_at"] is None
    assert doc["begin"]["counts"] == {"symbols": 2, "edges": 1, "findings": 1, "discrepancy_ledger": 0}
    assert doc["begin"]["git_head"] == "abc123"
    assert len(doc["begin"]["sha256"]) == 64
    con = sqlite3.connect(f"file:{(sdir / 'begin.db').as_posix()}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 2
        assert con.execute("SELECT value FROM meta WHERE key = 'git_head'").fetchone()[0] == "abc123"
    finally:
        con.close()


def test_begin_missing_facts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(SystemExit, match="facts.db missing"):
        session.session_begin(repo)


def test_begin_twice_active_rejected(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    session.session_begin(repo)
    with pytest.raises(SystemExit, match="already active"):
        session.session_begin(repo)


def test_session_id_collision_suffix(tmp_path):
    """同秒冲突走 -2/-3 后缀（目录已存在即冲突，确定性单测）。"""
    from datetime import datetime as dt

    root = tmp_path / "sessions"
    root.mkdir()
    now = dt(2026, 11, 4, 12, 0, 0)
    (root / "20261104-120000").mkdir()
    (root / "20261104-120000-2").mkdir()
    assert session._next_session_id(root, now) == "20261104-120000-3"
    assert session._next_session_id(root, dt(2026, 11, 4, 12, 0, 1)) == "20261104-120001"


def test_end_writes_end_snapshot_and_diff(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    session.session_begin(repo)
    _mutate(repo)
    result = session.session_end(repo, "done")
    sid = result["session_id"]
    sdir = repo / ".vrp" / "sessions" / sid
    assert (sdir / "end.db").is_file()
    assert result["ended_at"]
    diff = result["diff"]
    assert diff["symbols"] == {"begin": 2, "end": 3, "delta": 1}
    assert diff["findings"] == {"begin": 1, "end": 2, "delta": 1}
    assert diff["discrepancy_ledger"] == {"begin": 0, "end": 1, "delta": 1}
    assert diff["edges"]["delta"] == 0
    doc = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    assert doc["ended_at"] == result["ended_at"]
    assert doc["note"] == "end: done"  # end note 带 end: 前缀区分始末


def test_end_without_active(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    with pytest.raises(SystemExit, match="no active session"):
        session.session_end(repo)


def test_end_missing_facts_after_begin(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    session.session_begin(repo)
    (repo / ".vrp" / "facts.db").unlink()
    with pytest.raises(SystemExit, match="facts.db missing"):
        session.session_end(repo)


# ----------------------------------------------------------------- report


def test_report_diff_window_and_dual_diagrams(tmp_path, monkeypatch):
    _no_archify(monkeypatch)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    session.session_begin(repo)
    _mk_verdict(repo, "tx-before", "2020-01-01T00:00:00+00:00")  # 窗口前：排除
    _mk_verdict(repo, "tx-in", _utc_now_iso())
    _mutate(repo)
    session.session_end(repo)
    result = session.session_report(repo)
    assert result["verdicts_in_window"] == 1
    assert result["diff"]["symbols"]["delta"] == 1
    assert result["archify"] is False
    sdir = repo / ".vrp" / "sessions" / result["session_id"]
    assert (sdir / "report.html").is_file()
    assert (sdir / "begin.mmd").is_file() and (sdir / "end.mmd").is_file()
    assert (sdir / "ir" / "begin-architecture.json").is_file()
    assert (sdir / "ir" / "end-architecture.json").is_file()
    html = (sdir / "report.html").read_text(encoding="utf-8")
    assert "tx-in" in html and "tx-before" not in html  # 窗口过滤
    assert "+1" in html  # diff delta
    assert "archify not found" in html  # 降级诚实记录
    assert html.count("class=\"mermaid\"") == 2  # 双图并排（Mermaid 降级）


def test_report_active_session_rejected(tmp_path, monkeypatch):
    _no_archify(monkeypatch)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    session.session_begin(repo)
    with pytest.raises(SystemExit, match="no ended session"):
        session.session_report(repo)
    with pytest.raises(SystemExit, match="not ended yet"):
        session.session_report(repo, next((repo / ".vrp" / "sessions").iterdir()).name)


def test_report_unknown_session(tmp_path):
    repo = _mk_repo(tmp_path)
    with pytest.raises(SystemExit, match="unknown session"):
        session.session_report(repo, "nope-0000")


def test_report_default_latest_ended(tmp_path, monkeypatch):
    _no_archify(monkeypatch)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    session.session_begin(repo)  # 会话一
    session.session_end(repo)
    _mutate(repo)
    session.session_begin(repo)  # 会话二（含 +1 符号）
    session.session_end(repo)
    result = session.session_report(repo)  # 缺省 = 最近已结束
    assert result["session_id"] != ""
    assert result["diff"]["symbols"] == {"begin": 3, "end": 3, "delta": 0}  # 会话二期间无变化
    explicit = session.session_report(repo, result["session_id"])
    assert explicit["session_id"] == result["session_id"]


def test_report_reproducible_bytes(tmp_path, monkeypatch):
    _no_archify(monkeypatch)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    session.session_begin(repo)
    _mk_verdict(repo, "tx-in", _utc_now_iso())
    session.session_end(repo)
    r1 = session.session_report(repo)
    b1 = Path(r1["report"]).read_bytes()
    r2 = session.session_report(repo)
    b2 = Path(r2["report"]).read_bytes()
    assert b1 == b2  # 同会话字节级同报告（无渲染时刻时间戳）


def test_report_out_dir_override(tmp_path, monkeypatch):
    _no_archify(monkeypatch)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    session.session_begin(repo)
    session.session_end(repo)
    out = tmp_path / "elsewhere"
    result = session.session_report(repo, out_dir=out)
    assert (out / "report.html").is_file()
    assert result["report"] == str(out / "report.html")


def test_report_real_archify(tmp_path):
    """有 archify 沙箱时全链：双 IR validate+render 出并排 html（E2E）。"""
    mjs = render_mod.find_archify_mjs()
    if mjs is None:
        pytest.skip("archify sandbox not available")
    repo = _mk_repo(tmp_path)
    _seed(repo)
    session.session_begin(repo)
    _mutate(repo)
    session.session_end(repo)
    result = session.session_report(repo)
    assert result["archify"] is True
    assert result["degraded"] == []
    sdir = repo / ".vrp" / "sessions" / result["session_id"]
    assert (sdir / "html" / "begin.html").is_file()
    assert (sdir / "html" / "end.html").is_file()
    html = (sdir / "report.html").read_text(encoding="utf-8")
    assert 'iframe src="html/begin.html"' in html
    assert 'iframe src="html/end.html"' in html


# ----------------------------------------------------------------- 原子写


def _flaky_write_text_factory(monkeypatch):
    """对名字以 session.json 开头的目标（含 .tmp）模拟磁盘半写崩溃：
    截断写一半再抛 OSError——真实崩溃的字节级效果（R-M repro C1/C7 同型）。"""
    real = pathlib.Path.write_text

    def flaky(self, data, encoding=None, errors=None, newline=None):
        if self.name.startswith("session.json"):
            with open(self, "w", encoding="utf-8", newline=newline) as f:
                f.write(data[: max(1, len(data) // 2)])
            raise OSError("simulated crash mid-write")
        return real(self, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(pathlib.Path, "write_text", flaky)


def test_write_session_crash_preserves_old(tmp_path, monkeypatch):
    """覆写半写崩溃：既有 session.json 必须原样保留（tmp+os.replace，etl H2 同型）。"""
    sdir = tmp_path / ".vrp" / "sessions" / "20260101-000000"
    sdir.mkdir(parents=True)
    old_doc = {"id": "20260101-000000", "began_at": "2026-01-01T00:00:00+00:00",
               "ended_at": None, "note": "precious begin state"}
    (sdir / "session.json").write_text(
        json.dumps(old_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    old_bytes = (sdir / "session.json").read_bytes()
    _flaky_write_text_factory(monkeypatch)
    with pytest.raises(OSError):
        session._write_session(sdir, dict(old_doc, ended_at="2026-01-02T00:00:00+00:00"))
    assert (sdir / "session.json").read_bytes() == old_bytes
    assert [p.name for p in sdir.iterdir()] == ["session.json"]


def test_write_session_failed_first_write_no_file(tmp_path, monkeypatch):
    """首写半写崩溃：session.json 不存在（不留半写垃圾——无旧档时目录必须干净）。"""
    sdir = tmp_path / ".vrp" / "sessions" / "20260101-000000"
    sdir.mkdir(parents=True)
    _flaky_write_text_factory(monkeypatch)
    with pytest.raises(OSError):
        session._write_session(sdir, {"id": "20260101-000000"})
    assert not (sdir / "session.json").exists()
    assert list(sdir.iterdir()) == []


def test_write_session_success_no_tmp_leftover(tmp_path):
    """成功路径回归：内容正确 + 目录无 .tmp 残留。"""
    sdir = tmp_path / ".vrp" / "sessions" / "20260101-000000"
    sdir.mkdir(parents=True)
    doc = {"id": "20260101-000000", "ended_at": "2026-01-02T00:00:00+00:00"}
    session._write_session(sdir, doc)
    on_disk = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    assert on_disk["ended_at"] == "2026-01-02T00:00:00+00:00"
    assert [p.name for p in sdir.iterdir()] == ["session.json"]


# ----------------------------------------------------------------- CLI


def test_cli_session_wiring(tmp_path, monkeypatch, capsys):
    _no_archify(monkeypatch)
    repo = _mk_repo(tmp_path)
    _seed(repo)
    assert cli.main(["session", str(repo), "begin", "--note", "cli round"]) == 0
    out = capsys.readouterr().out
    assert "session begin" in out and "cli round" in out
    sid = next((repo / ".vrp" / "sessions").iterdir()).name
    _mutate(repo)
    assert cli.main(["session", str(repo), "end"]) == 0
    assert "session end" in capsys.readouterr().out
    assert cli.main(["session", str(repo), "report", "--id", sid]) == 0
    out = capsys.readouterr().out
    assert "session report" in out
    assert (repo / ".vrp" / "sessions" / sid / "report.html").is_file()
