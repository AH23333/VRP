"""L3: S5 artifacts (dossier.json, repomix-output.xml) must be written via
same-directory temp file + atomic swap - a mid-run failure leaves the old
artifact intact and no temp residue (H2 pattern)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from vrp import etl, s5_dossier, s5_rawpack

SCHEMA = etl.SCHEMA_PATH


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / ".vrp").mkdir(parents=True)
    return r


@pytest.fixture()
def facts(repo):
    db = repo / ".vrp" / "facts.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    con.close()
    return db


def _tmp_leftovers(artifact: Path):
    return [p.name for p in artifact.parent.iterdir() if p.name.startswith(artifact.name + ".tmp-")]


def test_dossier_atomic_success(repo, facts):
    dossier = s5_dossier.build_dossier(repo, facts, "p", skip_external=True)
    out = Path(dossier["out"])
    assert out.is_file() and json.loads(out.read_text(encoding="utf-8"))["project"] == "p"
    assert _tmp_leftovers(out) == []


def test_dossier_mid_failure_keeps_old_artifact(repo, facts, monkeypatch):
    s5_dossier.build_dossier(repo, facts, "p", skip_external=True)
    out = repo / ".vrp" / "dossier.json"
    good = out.read_bytes()

    def _boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(s5_dossier.os, "replace", _boom)
    with pytest.raises(OSError, match="disk on fire"):
        s5_dossier.build_dossier(repo, facts, "p", skip_external=True)
    assert out.read_bytes() == good, "old dossier.json must survive a mid-write failure"
    assert _tmp_leftovers(out) == []


def test_dossier_ki_cleans_tmp_and_reraises(repo, facts, monkeypatch):
    """R-P：dossier 落盘窗口 Ctrl-C（KeyboardInterrupt 非 Exception 子类）
    同样走 tmp 清理边界后重抛；修前 KI 绕过清理裸逃，
    dossier.json.tmp-<pid> 泄漏（pid 异名后续运行永不清理）。"""
    def _ki(src, dst):
        raise KeyboardInterrupt()

    monkeypatch.setattr(s5_dossier.os, "replace", _ki)
    with pytest.raises(KeyboardInterrupt):
        s5_dossier.build_dossier(repo, facts, "p", skip_external=True)
    out = repo / ".vrp" / "dossier.json"
    assert not out.exists()  # 首写被中断：无残缺成品
    assert _tmp_leftovers(out) == []


def _fake_run_tree(rc, payload=b"<xml>new</xml>", timed_out=False):
    """R-AO 接缝移至 procs.run_tree（dict 形状）：s5_rawpack.build_raw_pack 收编。"""

    def fake(cmd, cwd=None, timeout=None, env=None):
        if timed_out:
            return {"returncode": -1, "timed_out": True, "stdout": "", "stderr": ""}
        out_arg = Path(cmd[5])
        if payload is not None:
            out_arg.write_bytes(payload)
        return {"returncode": rc, "timed_out": False, "stdout": "", "stderr": "repomix blew up"}

    return fake


def test_rawpack_success_swaps_atomically(repo, monkeypatch):
    monkeypatch.setattr(s5_rawpack, "find_bin", lambda: Path("fake-repomix"))
    monkeypatch.setattr(s5_rawpack.procs, "run_tree", _fake_run_tree(0))
    res = s5_rawpack.build_raw_pack(repo)
    out = repo / ".vrp" / s5_rawpack.OUTPUT_NAME
    assert res["status"] == "ok" and out.read_bytes() == b"<xml>new</xml>"
    assert _tmp_leftovers(out) == []


def test_rawpack_failure_keeps_old_artifact(repo, monkeypatch):
    out = repo / ".vrp" / s5_rawpack.OUTPUT_NAME
    out.write_bytes(b"<xml>OLD</xml>")
    monkeypatch.setattr(s5_rawpack, "find_bin", lambda: Path("fake-repomix"))
    monkeypatch.setattr(s5_rawpack.procs, "run_tree", _fake_run_tree(1, payload=b"<xml>half"))
    res = s5_rawpack.build_raw_pack(repo)
    assert res["status"] == "failed"
    assert out.read_bytes() == b"<xml>OLD</xml>", "old artifact must not be truncated"
    assert _tmp_leftovers(out) == []


def test_rawpack_timeout_keeps_old_artifact(repo, monkeypatch):
    out = repo / ".vrp" / s5_rawpack.OUTPUT_NAME
    out.write_bytes(b"<xml>OLD</xml>")
    monkeypatch.setattr(s5_rawpack, "find_bin", lambda: Path("fake-repomix"))
    monkeypatch.setattr(s5_rawpack.procs, "run_tree", _fake_run_tree(0, payload=b"<xml>half", timed_out=True))
    with pytest.raises(subprocess.TimeoutExpired):
        s5_rawpack.build_raw_pack(repo)
    assert out.read_bytes() == b"<xml>OLD</xml>"
    assert _tmp_leftovers(out) == []
