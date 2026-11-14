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


def test_build_raw_pack_timeout_tree_killed(tmp_path, monkeypatch):
    """R-AO：repomix 超时树杀即抛 TimeoutExpired（raise 契约保留——s5_dossier 捕获
    降级、test_s5_atomic 钉；MCP dossier 工具直调链活面）。修前只杀 .cmd 直子
    cmd.exe，node 孙进程持管道，异常延迟到孤儿退出才交付（探针 15.07s vs 2s
    预算的 7.5× 超支）。"""
    import subprocess
    import sys
    import time

    slow = tmp_path / "slow_repomix.py"
    slow.write_text("import time\ntime.sleep(20)\nprint('ok')\n", encoding="utf-8")
    if sys.platform == "win32":
        exe = tmp_path / "fake_repomix.cmd"
        exe.write_text(f'@"{sys.executable}" "{slow}" %*\n', encoding="utf-8")
    else:
        exe = tmp_path / "fake_repomix.sh"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{slow}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("VRP_REPOMIX_BIN", str(exe))

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        s5_rawpack.build_raw_pack(tmp_path, timeout=2)
    wall = time.monotonic() - start
    assert wall < 15, f"tree-kill must raise promptly, wall={wall:.1f}s (orphan-held pipe)"
    assert not list((tmp_path / ".vrp").glob("*.tmp-*")), "timed-out run must leave no temp residue"
