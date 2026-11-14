import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vrp import s6_difftastic

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

RENAME_PARSED = {
    "language": "Python",
    "path": "b_rename.py",
    "status": "changed",
    "chunks": [
        [
            {
                "lhs": {"line_number": 0, "changes": [{"start": 4, "end": 7, "content": "add", "highlight": "normal"}]},
                "rhs": {"line_number": 0, "changes": [{"start": 4, "end": 8, "content": "plus", "highlight": "normal"}]},
            }
        ]
    ],
}


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".vrp/\n", encoding="utf-8")
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    return repo


def _mk_span_db(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    for fid, qn, file_path, start, end in rows:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (fid, "p", qn, "Function", qn.split(".")[-1], file_path, start, end, "h-" + fid),
        )
    con.commit()
    con.close()
    return path


def _mk_tx(tx_type: str, moves=None, files=("a.py",)) -> dict:
    return {
        "id": "tx-d",
        "type": tx_type,
        "summary": "test",
        "declared_moves": moves or [],
        "edits": [{"file": Path(f), "op": "modify", "content": "x = 1\n"} for f in files],
    }


def test_find_difftastic_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "difft.exe"
    fake.write_bytes(b"MZ")
    monkeypatch.setenv("VRP_DIFFTASTIC_BIN", str(fake))
    assert s6_difftastic.find_difftastic() == str(fake)


def test_find_difftastic_broken_env_falls_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("VRP_DIFFTASTIC_BIN", str(tmp_path / "missing.exe"))
    fake_default = tmp_path / "work" / "difftastic" / "difft.exe"
    fake_default.parent.mkdir(parents=True)
    fake_default.write_bytes(b"MZ")
    monkeypatch.setattr(s6_difftastic, "_DEFAULT_BIN", fake_default)
    assert s6_difftastic.find_difftastic() == str(fake_default)


def test_find_difftastic_none_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("VRP_DIFFTASTIC_BIN", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(s6_difftastic, "_DEFAULT_BIN", tmp_path / "missing-too.exe")
    monkeypatch.setattr(s6_difftastic, "which", lambda name: None)
    assert s6_difftastic.find_difftastic() is None


def test_run_difftastic_env_args_and_parse(tmp_path, monkeypatch):
    captured = {}

    def fake_tree(cmd, cwd=None, timeout=None, env=None):
        captured["cmd"] = list(cmd)
        captured["env"] = env
        return {"returncode": 0, "timed_out": False,
                "stdout": json.dumps(RENAME_PARSED), "stderr": ""}

    monkeypatch.setattr(s6_difftastic.procs, "run_tree", fake_tree)
    result = s6_difftastic.run_difftastic("difft", tmp_path / "old.py", tmp_path / "new.py")
    assert result["ok"] is True
    assert result["parsed"]["status"] == "changed"
    cmd = captured["cmd"]
    assert cmd[0] == "difft"
    assert "--display" in cmd and "json" in cmd
    assert "--ignore-comments" in cmd
    assert captured["env"]["DFT_UNSTABLE"] == "yes"


def test_run_difftastic_rc_nonzero_not_ok(tmp_path, monkeypatch):
    def fake_tree(cmd, cwd=None, timeout=None, env=None):
        return {"returncode": 2, "timed_out": False, "stdout": "",
                "stderr": "JSON output is an unstable feature"}

    monkeypatch.setattr(s6_difftastic.procs, "run_tree", fake_tree)
    result = s6_difftastic.run_difftastic("difft", tmp_path / "old.py", tmp_path / "new.py")
    assert result["ok"] is False
    assert result["returncode"] == 2
    assert "unstable" in result["stderr_tail"]


def test_run_difftastic_timeout(tmp_path, monkeypatch):
    def fake_tree(cmd, cwd=None, timeout=None, env=None):
        return {"returncode": -1, "timed_out": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(s6_difftastic.procs, "run_tree", fake_tree)
    result = s6_difftastic.run_difftastic("difft", tmp_path / "old.py", tmp_path / "new.py")
    assert result == {"ok": False, "reason": "timeout"}


def test_run_difftastic_json_array_shape(tmp_path, monkeypatch):
    def fake_tree(cmd, cwd=None, timeout=None, env=None):
        return {"returncode": 0, "timed_out": False,
                "stdout": json.dumps([RENAME_PARSED]), "stderr": ""}

    monkeypatch.setattr(s6_difftastic.procs, "run_tree", fake_tree)
    result = s6_difftastic.run_difftastic("difft", tmp_path / "old.py", tmp_path / "new.py")
    assert result["ok"] is True
    assert result["parsed"]["status"] == "changed"


def test_extract_blocks_probe_shape():
    blocks = s6_difftastic.extract_blocks(RENAME_PARSED)
    assert len(blocks) == 1
    assert blocks[0]["lhs"] == {"line": 0, "tokens": ["add"]}
    assert blocks[0]["rhs"] == {"line": 0, "tokens": ["plus"]}


def test_segment_pairs_only_differing_segments():
    moves = [
        {"from": "p.pkg.calc.add", "to": "p.pkg.calc.plus"},
        {"from": "p.old.util", "to": "p.new.util"},
    ]
    assert s6_difftastic._segment_pairs(moves) == [("add", "plus"), ("old", "new")]


def test_segment_pairs_depth_mismatch_contributes_none():
    moves = [{"from": "p.calc.add", "to": "p.add"}]
    assert s6_difftastic._segment_pairs(moves) == []


def test_pairs_match():
    pairs = [("add", "plus"), ("old", "new")]
    assert s6_difftastic._pairs_match(["add"], ["plus"], pairs)
    assert s6_difftastic._pairs_match(["old", "add"], ["new", "plus"], pairs)
    assert not s6_difftastic._pairs_match(["add"], ["minus"], pairs)
    assert not s6_difftastic._pairs_match(["add", "b"], ["plus"], pairs)
    assert not s6_difftastic._pairs_match(["add"], ["plus"], [])


def test_check_edits_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(s6_difftastic, "find_difftastic", lambda: None)
    report = s6_difftastic.check_edits(tmp_path, _mk_tx("T1"), tmp_path / "pre.db", tmp_path / "post.db")
    assert report == {"status": "not-found"}


def test_check_edits_skips_without_modify_ops(tmp_path, monkeypatch):
    monkeypatch.setattr(s6_difftastic, "find_difftastic", lambda: "difft")
    tx = _mk_tx("T1")
    tx["edits"] = [{"file": Path("b.py"), "op": "delete", "content": None}]
    report = s6_difftastic.check_edits(tmp_path, tx, tmp_path / "pre.db", tmp_path / "post.db")
    assert report["status"] == "skipped"
    assert "no modify-op" in report["reason"]


def _patch_run(monkeypatch, parsed_by_name):
    def fake_run(binary, old_path, new_path, timeout=60):
        return {"ok": True, "parsed": parsed_by_name[new_path.name]}

    monkeypatch.setattr(s6_difftastic, "run_difftastic", fake_run)


def test_check_edits_t1_sneak_in_same_file_fails(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    pre_db = _mk_span_db(tmp_path / "pre.db", [("f1", "p.a.add", "a.py", 1, 2)])
    post_db = _mk_span_db(tmp_path / "post.db", [("f2", "p.a.plus", "a.py", 1, 2)])
    parsed = {
        "status": "changed",
        "chunks": [
            [
                {
                    "lhs": {"line_number": 0, "changes": [{"content": "add"}]},
                    "rhs": {"line_number": 0, "changes": [{"content": "plus"}]},
                },
                {
                    "rhs": {"line_number": 1, "changes": [{"content": "+"}, {"content": "0"}]},
                },
            ]
        ],
    }
    monkeypatch.setattr(s6_difftastic, "find_difftastic", lambda: "difft")
    _patch_run(monkeypatch, {"a.py": parsed})
    tx = _mk_tx("T1", moves=[{"from": "p.a.add", "to": "p.a.plus"}])
    report = s6_difftastic.check_edits(repo, tx, pre_db, post_db)
    assert report["status"] == "fail"
    assert report["files"]["a.py"] == "unaccounted"
    assert report["unaccounted"][0]["file"] == "a.py"
    bad = report["unaccounted"][0]["blocks"][0]
    assert bad["rhs"]["line"] == 2
    assert bad["rhs"]["tokens"] == ["+", "0"]


def test_check_edits_t1_pure_rename_ok(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    pre_db = _mk_span_db(tmp_path / "pre.db", [("f1", "p.a.add", "a.py", 1, 2)])
    post_db = _mk_span_db(tmp_path / "post.db", [("f2", "p.a.plus", "a.py", 1, 2)])
    monkeypatch.setattr(s6_difftastic, "find_difftastic", lambda: "difft")
    _patch_run(monkeypatch, {"a.py": RENAME_PARSED})
    tx = _mk_tx("T1", moves=[{"from": "p.a.add", "to": "p.a.plus"}])
    report = s6_difftastic.check_edits(repo, tx, pre_db, post_db)
    assert report["status"] == "ok"
    assert report["files"]["a.py"] == "accounted"
    assert report["unaccounted"] == []


def test_check_edits_t1_cross_file_move_and_caller_update_ok(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "b.py").write_text("from pkg.x import add\n", encoding="utf-8")
    (repo / "b_new.py").write_text("x = 0\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".vrp/\n", encoding="utf-8")
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    pre_db = _mk_span_db(tmp_path / "pre.db", [("f1", "p.x.add", "a.py", 1, 2)])
    post_db = _mk_span_db(tmp_path / "post.db", [("f2", "p.y.add", "b_new.py", 1, 2)])
    a_parsed = {
        "status": "changed",
        "chunks": [[{"lhs": {"line_number": 0, "changes": [{"content": "def"}, {"content": "add"}]}}]],
    }
    b_new_parsed = {
        "status": "changed",
        "chunks": [[{"rhs": {"line_number": 0, "changes": [{"content": "def"}, {"content": "add"}]}}]],
    }
    caller_parsed = {
        "status": "changed",
        "chunks": [
            [
                {
                    "lhs": {"line_number": 0, "changes": [{"content": "x"}]},
                    "rhs": {"line_number": 0, "changes": [{"content": "y"}]},
                }
            ]
        ],
    }
    monkeypatch.setattr(s6_difftastic, "find_difftastic", lambda: "difft")
    _patch_run(monkeypatch, {"a.py": a_parsed, "b.py": caller_parsed, "b_new.py": b_new_parsed})
    tx = _mk_tx("T1", moves=[{"from": "p.x.add", "to": "p.y.add"}], files=("a.py", "b.py", "b_new.py"))
    report = s6_difftastic.check_edits(repo, tx, pre_db, post_db)
    assert report["status"] == "ok"
    assert report["files"] == {"a.py": "accounted", "b.py": "accounted", "b_new.py": "accounted"}


def test_check_edits_t2_removals_ok_additions_fail(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    pre_db = _mk_span_db(tmp_path / "pre.db", [("f1", "p.a.add", "a.py", 1, 2)])
    post_db = _mk_span_db(tmp_path / "post.db", [("f2", "p.a.add", "a.py", 1, 2)])
    removal_only = {
        "status": "changed",
        "chunks": [[{"lhs": {"line_number": 1, "changes": [{"content": "sub"}]}}]],
    }
    with_addition = {
        "status": "changed",
        "chunks": [
            [
                {"lhs": {"line_number": 1, "changes": [{"content": "sub"}]}},
                {"rhs": {"line_number": 2, "changes": [{"content": "double"}]}},
            ]
        ],
    }
    monkeypatch.setattr(s6_difftastic, "find_difftastic", lambda: "difft")
    _patch_run(monkeypatch, {"a.py": removal_only})
    ok_report = s6_difftastic.check_edits(repo, _mk_tx("T2"), pre_db, post_db)
    assert ok_report["status"] == "ok"
    assert ok_report["files"]["a.py"] == "accounted"
    _patch_run(monkeypatch, {"a.py": with_addition})
    fail_report = s6_difftastic.check_edits(repo, _mk_tx("T2"), pre_db, post_db)
    assert fail_report["status"] == "fail"
    assert fail_report["unaccounted"][0]["blocks"][0]["rhs"]["tokens"] == ["double"]


def test_check_edits_t2_deletion_with_empty_rhs_tokens_ok(tmp_path, monkeypatch):
    """R6' agent 长跑实证（T2 删 vrp/render.archify_available 被误拒）：
    difftastic 纯删除形态 = lhs 有 token + rhs 存在但 token 空（非 rhs 缺失）——
    旧真值判定（`if lhs and not rhs`）漏此形态，误报 structural-change。"""
    repo = _mk_repo(tmp_path)
    pre_db = _mk_span_db(tmp_path / "pre.db", [("f1", "p.a.add", "a.py", 1, 2)])
    post_db = _mk_span_db(tmp_path / "post.db", [("f2", "p.a.add", "a.py", 1, 2)])
    deletion_paired_empty_rhs = {
        "status": "changed",
        "chunks": [
            [
                {
                    "lhs": {"line_number": 406, "changes": [{"content": "def"}, {"content": "gone"}]},
                    "rhs": {"line_number": 406, "changes": []},
                }
            ]
        ],
    }
    monkeypatch.setattr(s6_difftastic, "find_difftastic", lambda: "difft")
    _patch_run(monkeypatch, {"a.py": deletion_paired_empty_rhs})
    report = s6_difftastic.check_edits(repo, _mk_tx("T2"), pre_db, post_db)
    assert report["status"] == "ok"
    assert report["files"]["a.py"] == "accounted"
    assert report["unaccounted"] == []


def test_check_edits_degrades_per_file_on_run_failure(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    pre_db = _mk_span_db(tmp_path / "pre.db", [])
    post_db = _mk_span_db(tmp_path / "post.db", [])

    def fake_run(binary, old_path, new_path, timeout=60):
        return {"ok": False, "reason": "timeout"}

    monkeypatch.setattr(s6_difftastic, "find_difftastic", lambda: "difft")
    monkeypatch.setattr(s6_difftastic, "run_difftastic", fake_run)
    report = s6_difftastic.check_edits(repo, _mk_tx("T2"), pre_db, post_db)
    assert report["status"] == "ok"  # degraded, non-blocking
    assert report["degraded"] == [{"file": "a.py", "reason": "timeout"}]
    assert report["checked"] == 0


def test_check_edits_changed_no_blocks_degrades(tmp_path, monkeypatch):
    """changed 却零块 = 形状漂移——按文件降级，绝不静默 accounted（诚实性）。"""
    repo = _mk_repo(tmp_path)
    pre_db = _mk_span_db(tmp_path / "pre.db", [])
    post_db = _mk_span_db(tmp_path / "post.db", [])

    def fake_run(binary, old_path, new_path, timeout=60):
        return {"ok": True, "returncode": 0, "stderr_tail": "", "parsed": {"language": "Python", "path": "a.py", "status": "changed", "chunks": []}}

    monkeypatch.setattr(s6_difftastic, "find_difftastic", lambda: "difft")
    monkeypatch.setattr(s6_difftastic, "run_difftastic", fake_run)
    report = s6_difftastic.check_edits(repo, _mk_tx("T2"), pre_db, post_db)
    assert report["status"] == "ok"  # degraded, non-blocking
    assert report["degraded"] == [{"file": "a.py", "reason": "changed-no-blocks"}]
    assert "a.py" not in report["files"]
    assert report["checked"] == 0


def test_module_docstring_documents_probe():
    doc = s6_difftastic.__doc__
    assert "DFT_UNSTABLE" in doc
    assert "--display json" in doc
    assert "--ignore-comments" in doc
    assert "0.72.0" in doc
    assert "0-based" in doc
    assert "cross-file" in doc
    assert "lhs-only" in doc


def test_git_show_oserror_degrades_none(tmp_path, monkeypatch):
    """R-N：git 缺失（OSError）按契约降级 None（no-preimage），不向上崩。"""
    repo = _mk_repo(tmp_path)

    def boom(*args, **kwargs):
        raise FileNotFoundError("git not found (simulated)")

    monkeypatch.setattr(s6_difftastic.procs, "run_tree", boom)
    assert s6_difftastic._git_show(repo, "a.py") is None


def _fake_slow_difft(tmp_path: Path) -> Path:
    """假 difft 二进制：睡 30s——真树杀面垫片（wrapper 跨平台，R-AO 模式）。"""
    slow = tmp_path / "fake_difft_slow.py"
    slow.write_text("import time\n\ntime.sleep(30)\n", encoding="utf-8")
    if sys.platform == "win32":
        exe = tmp_path / "fake_difft_slow.cmd"
        exe.write_text(f'@"{sys.executable}" "{slow}" %*\n', encoding="utf-8")
    else:
        exe = tmp_path / "fake_difft_slow.sh"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{slow}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    return exe


def test_run_difftastic_timeout_kills_tree(tmp_path):
    """R-AP 真树杀面：超时必须杀全树（run_tree）。旧 subprocess.run 只杀
    直接子（win32 .cmd→cmd.exe→python 孙持管道），墙钟会拖满 30s。"""
    binary = _fake_slow_difft(tmp_path)
    (tmp_path / "old.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("a = 2\n", encoding="utf-8")
    t0 = time.monotonic()
    result = s6_difftastic.run_difftastic(
        str(binary), tmp_path / "old.py", tmp_path / "new.py", timeout=2
    )
    assert result == {"ok": False, "reason": "timeout"}
    assert time.monotonic() - t0 < 15
