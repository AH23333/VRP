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
