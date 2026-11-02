from pathlib import Path

import pytest

from vrp import s5_rawpack


def test_find_bin_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "repomix.cmd"
    fake.write_text("@echo off", encoding="utf-8")
    monkeypatch.setenv("VRP_REPOMIX_BIN", str(fake))
    assert s5_rawpack.find_bin() == fake


def test_find_bin_missing(monkeypatch):
    monkeypatch.delenv("VRP_REPOMIX_BIN", raising=False)
    assert s5_rawpack.find_bin() is None or s5_rawpack.find_bin().is_file()


def test_build_raw_pack_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr("vrp.s5_rawpack.find_bin", lambda: None)
    result = s5_rawpack.build_raw_pack(tmp_path)
    assert result["status"] == "skipped"


@pytest.mark.skipif(s5_rawpack.find_bin() is None, reason="repomix sandbox not installed")
def test_build_raw_pack_real(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (tmp_path / ".vrp").mkdir()
    (tmp_path / ".vrp" / "facts.db").write_bytes(b"db-bytes")
    result = s5_rawpack.build_raw_pack(tmp_path, timeout=180)
    assert result["status"] == "ok", result
    out = Path(result["path"])
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "export const a = 1" in text
    assert "db-bytes" not in text
    assert result["bytes"] > 0
    assert len(result["sha256"]) == 16
