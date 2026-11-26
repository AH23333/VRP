"""E 批 P1 横切件测试：run 完成总结 / F3 编码兜底 / mcp stderr 提示 / _count_findings。

锁面（#E）：
- run 结束 `== done ==` 总结：阶段数+总耗时+findings 计数+下一步引导；
  findings=0 与 >0 两条 next 分支；content-repo 早退短 done 行；
- F3：非 TTY stdout 尝试 reconfigure utf-8；reconfigure 失败不阻塞；
- mcp stdio：交互终端（stdin TTY） stderr 一句提示；真客户端（管道）静默；
- _count_findings：缺库/坏库诚实 0，不阻塞总结。
"""

from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vrp import cli, mcp_server

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path, findings: int = 0) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    for i in range(findings):
        con.execute(
            "INSERT INTO findings (pathology, kind, source, file_path, start_line, payload_json)"
            " VALUES ('P1', 'k', 's2-astgrep', 'pkg/x.py', ?, '{}')",
            (i + 1,),
        )
    con.commit()
    con.close()
    return repo


class _FakeStdout:
    """F3 测试替身：isatty 可控，reconfigure 调用留痕，write 转 StringIO。"""

    def __init__(self, isatty: bool, fail_reconfigure: bool = False):
        self._isatty = isatty
        self._fail = fail_reconfigure
        self.buf = io.StringIO()
        self.reconfigure_calls: list[dict] = []

    def isatty(self) -> bool:
        return self._isatty

    def reconfigure(self, **kw):
        self.reconfigure_calls.append(kw)
        if self._fail:
            raise OSError("reconfigure not supported")

    def write(self, s):
        return self.buf.write(s)

    def flush(self):
        pass

    def fileno(self):
        raise OSError("no fileno")


# --------------------------------------------------------------- run 完成总结


def _patch_pipeline(monkeypatch, canned_precheck=None):
    monkeypatch.setattr(
        cli.s0_precheck,
        "precheck",
        lambda repo: canned_precheck
        or {"name": repo.name, "repo": str(repo), "commands": {},
            "git": {"is_repo": True, "head": "h" * 40, "branch": "main"},
            "content_repo": {"detected": False}},
    )
    monkeypatch.setattr(cli.s1_cbm, "index_repository",
                        lambda repo, name, reindex=False: {"status": "ok"})
    monkeypatch.setattr(cli.etl_mod, "run_etl", lambda repo, name: {"mode": "incremental"})
    monkeypatch.setattr(cli.xcheck, "run_xcheck", lambda repo, db, reindex=False: {"index": "ok"})
    monkeypatch.setattr(cli.perf_mod, "record_run", lambda *a, **kw: None)


def test_run_done_summary_with_findings(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path, findings=2)
    _patch_pipeline(monkeypatch)
    assert cli.main(["run", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "== done == 4 stages in " in out
    assert "2 findings in facts.db" in out
    assert f"next: 'vrp findings {repo}' to triage" in out
    assert "vrp report" in out


def test_run_done_summary_zero_findings(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path, findings=0)
    _patch_pipeline(monkeypatch)
    assert cli.main(["run", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "0 findings in facts.db" in out
    assert f"next: 'vrp s2 {repo}' to run pathology detection" in out
    assert "vrp analyze" in out


def test_run_content_repo_short_done(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    _patch_pipeline(
        monkeypatch,
        canned_precheck={
            "name": repo.name, "repo": str(repo), "commands": {},
            "git": {"is_repo": True, "head": "h" * 40, "branch": "main"},
            "content_repo": {"detected": True, "content_share_pct": 92,
                             "content_files": 46, "total_files": 50,
                             "source_files": 4},
        },
    )
    assert cli.main(["run", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith(
        "== done == content repository - code analysis not applicable (rc=0)"
    )


# --------------------------------------------------------------- _count_findings


def test_count_findings_missing_db(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert cli._count_findings(repo) == 0


def test_count_findings_real_db(tmp_path):
    repo = _mk_repo(tmp_path, findings=3)
    assert cli._count_findings(repo) == 3


def test_count_findings_corrupt_db_honest_zero(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    (repo / ".vrp" / "facts.db").write_bytes(b"garbage not a db")
    assert cli._count_findings(repo) == 0


# --------------------------------------------------------------- F3 编码兜底


def test_f3_non_tty_reconfigure_utf8(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path, findings=1)
    fake = _FakeStdout(isatty=False)
    monkeypatch.setattr(sys, "stdout", fake)
    assert cli.main(["status", str(repo)]) == 0
    assert fake.reconfigure_calls == [{"encoding": "utf-8"}]
    assert "counts" in fake.buf.getvalue()


def test_f3_tty_skips_reconfigure(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    fake = _FakeStdout(isatty=True)
    monkeypatch.setattr(sys, "stdout", fake)
    assert cli.main(["status", str(repo)]) == 0
    assert fake.reconfigure_calls == []


def test_f3_reconfigure_failure_not_fatal(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    fake = _FakeStdout(isatty=False, fail_reconfigure=True)
    monkeypatch.setattr(sys, "stdout", fake)
    assert cli.main(["status", str(repo)]) == 0
    assert "vrp status - repo" in fake.buf.getvalue()


# --------------------------------------------------------------- mcp stdio stderr 提示


class _FakeServer:
    def __init__(self):
        self.ran = False

    def run(self, transport):
        self.ran = True
        assert transport == "stdio"


def test_mcp_stdio_tty_hint(monkeypatch, capsys):
    monkeypatch.setattr(mcp_server, "build_server", lambda repo: _FakeServer())
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": lambda self: True})())
    mcp_server.run_stdio(Path("some/repo"))
    err = capsys.readouterr().err
    assert "waiting for MCP protocol on stdin" in err


def test_mcp_stdio_pipe_silent(monkeypatch, capsys):
    monkeypatch.setattr(mcp_server, "build_server", lambda repo: _FakeServer())
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": lambda self: False})())
    mcp_server.run_stdio(Path("some/repo"))
    assert capsys.readouterr().err == ""
