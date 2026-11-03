"""L3: S5 artifacts (dossier.json, repomix-output.xml) must be written via
same-directory temp file + atomic swap - a mid-run failure leaves the old
artifact intact and no temp residue (H2 pattern)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import types
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


def _fake_repomix(rc, payload=b"<xml>new</xml>", raise_exc=None):
    def fake(cmd, **kwargs):
        out_arg = Path(cmd[5])
        if payload is not None:
            out_arg.write_bytes(payload)
        if raise_exc is not None:
            raise raise_exc
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="repomix blew up")

    return fake


def test_rawpack_success_swaps_atomically(repo, monkeypatch):
    monkeypatch.setattr(s5_rawpack, "find_bin", lambda: Path("fake-repomix"))
    monkeypatch.setattr(s5_rawpack.subprocess, "run", _fake_repomix(0))
    res = s5_rawpack.build_raw_pack(repo)
    out = repo / ".vrp" / s5_rawpack.OUTPUT_NAME
    assert res["status"] == "ok" and out.read_bytes() == b"<xml>new</xml>"
    assert _tmp_leftovers(out) == []


def test_rawpack_failure_keeps_old_artifact(repo, monkeypatch):
    out = repo / ".vrp" / s5_rawpack.OUTPUT_NAME
    out.write_bytes(b"<xml>OLD</xml>")
    monkeypatch.setattr(s5_rawpack, "find_bin", lambda: Path("fake-repomix"))
    monkeypatch.setattr(s5_rawpack.subprocess, "run", _fake_repomix(1, payload=b"<xml>half"))
    res = s5_rawpack.build_raw_pack(repo)
    assert res["status"] == "failed"
    assert out.read_bytes() == b"<xml>OLD</xml>", "old artifact must not be truncated"
    assert _tmp_leftovers(out) == []


def test_rawpack_timeout_keeps_old_artifact(repo, monkeypatch):
    out = repo / ".vrp" / s5_rawpack.OUTPUT_NAME
    out.write_bytes(b"<xml>OLD</xml>")
    monkeypatch.setattr(s5_rawpack, "find_bin", lambda: Path("fake-repomix"))
    boom = subprocess.TimeoutExpired(cmd=["fake"], timeout=600)
    monkeypatch.setattr(s5_rawpack.subprocess, "run", _fake_repomix(0, payload=b"<xml>half", raise_exc=boom))
    with pytest.raises(subprocess.TimeoutExpired):
        s5_rawpack.build_raw_pack(repo)
    assert out.read_bytes() == b"<xml>OLD</xml>"
    assert _tmp_leftovers(out) == []
