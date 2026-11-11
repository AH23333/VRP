"""M3: subprocess timeouts inside pipeline steps must exit 1, not traceback through cli.main."""

from __future__ import annotations

import subprocess

import pytest

from vrp import cli, s0_precheck


def _raise_timeout(*args, **kwargs):
    raise subprocess.TimeoutExpired(cmd=["cbm", "cli", "index_repository"], timeout=300)


def test_main_catches_timeout_expired(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(s0_precheck, "precheck", _raise_timeout)
    rc = cli.main(["precheck", str(repo)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err


def test_main_still_catches_runtime_error(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(s0_precheck, "precheck", _raise_runtime)
    rc = cli.main(["precheck", str(repo)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def _raise_runtime(*args, **kwargs):
    raise RuntimeError("boom")


def test_main_catches_value_error_malformed_json(tmp_path, capsys):
    """R-G C2: 坏 JSON 的 ValueError（JSONDecodeError）必须转 error rc=1，非裸 traceback 出 main。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    claims = tmp_path / "claims.json"
    claims.write_text("{not json", encoding="utf-8")
    rc = cli.main(["gate", str(repo), str(claims)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_main_lets_guidance_system_exit_propagate(tmp_path):
    """R-G R3 守卫：引导性 SystemExit（argparse 同型）保持直抛，不得被 main 边界吞掉。"""
    import pytest

    with pytest.raises(SystemExit, match="not a directory"):
        cli.main(["status", str(tmp_path / "missing-repo")])


def test_open_facts_rw_ki_closes_connection(tmp_path, monkeypatch):
    """R-P：cli._open_facts_rw 窗口 Ctrl-C（KeyboardInterrupt 非 Exception
    子类）同样走 con.close() 后重抛；修前 KI 绕过清理泄漏句柄，Windows 锁
    facts.db。unlink 探测仅 Windows 有守卫力，其他平台恒绿无害。"""
    import sqlite3

    from vrp import etl

    from pathlib import Path

    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    facts = repo / ".vrp" / "facts.db"
    schema = Path(cli.__file__).parent / "schema" / "facts_schema.sql"
    con = sqlite3.connect(facts)
    con.executescript(schema.read_text(encoding="utf-8"))
    con.commit()
    con.close()

    def _ki(con_, repo_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(etl, "check_facts_freshness", _ki)
    try:
        cli._open_facts_rw(repo)
        raise AssertionError("expected KeyboardInterrupt")
    except KeyboardInterrupt:
        pass
    facts.unlink()  # leaked handle would lock this on Windows
