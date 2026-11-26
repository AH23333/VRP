"""CLI repo 位置参数默认当前目录（省略=`.`）+ 主 help 分组渲染守卫。

任务 A/D 批改造的锁面：
- repo 位置参数 nargs="?" default="."——省略=当前目录，绝对/相对皆可；
- 非目录路径仍走 _repo_arg 的 "not a directory" 硬拒；
- 主 help 66 子命令按 README §CLI 六组渲染；usage 怪兽串 metavar 收敛；
- 12 高频子命令 help 尾部 EXAMPLES。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vrp import cli


def test_repo_default_equals_explicit_dot(tmp_path, monkeypatch, capsys):
    """省略 repo：cli.main(['status','--json']) == cli.main(['status','.','--json']）（chdir 后全链等价）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    assert cli.main(["status", "--json"]) == 0
    out_no_arg = capsys.readouterr().out
    assert cli.main(["status", ".", "--json"]) == 0
    out_dot = capsys.readouterr().out
    assert out_no_arg == out_dot
    payload = json.loads(out_dot)
    assert payload["facts_db"] is None  # 无 facts.db 诚实报 null（非报错）


def test_status_default_human_face(tmp_path, monkeypatch, capsys):
    """E 批：status 缺省 = 英文人读面（--json 才是机读面）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("vrp status - ")
    assert "not found - run 'vrp run' first" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)  # 缺省面不再是 JSON 墙


def test_repo_relative_path_resolves_from_cwd(tmp_path, monkeypatch, capsys):
    """相对路径以当前终端目录解析：status repo == status <绝对路径>。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    assert cli.main(["status", "repo"]) == 0
    out_rel = capsys.readouterr().out
    assert cli.main(["status", str(repo)]) == 0
    out_abs = capsys.readouterr().out
    assert out_rel == out_abs


def test_repo_missing_dir_rejected(tmp_path):
    """非目录路径照旧硬拒（SystemExit "not a directory"——缺省值不放宽校验）。"""
    with pytest.raises(SystemExit, match="not a directory"):
        cli.main(["status", str(tmp_path / "missing-repo")])


def test_repo_default_wired_across_parsers():
    """带 repo 位置参数的子命令：省略解析为 '.'，显式传参不受影响。"""
    parser = cli.build_parser()
    for argv in (["status"], ["run"], ["s2"], ["etl"], ["findings"], ["report"]):
        args = parser.parse_args(argv)
        assert args.repo == ".", f"{argv[0]}: default repo != '.'"
    assert parser.parse_args(["status", "some/repo"]).repo == "some/repo"


def test_main_help_grouped_and_collapsed():
    """主 help：六组分组渲染 + usage 怪兽串收敛 + 新手引导。"""
    parser = cli.build_parser()
    text = parser.format_help()
    assert text.startswith("usage: vrp [-h] [--version] <command> ...")
    for group in ("pipeline", "curation & enforcement", "refactor transactions",
                  "pathology & analysis", "evidence & audit", "governance & engineering"):
        assert f"{group}:" in text
    assert "{precheck," not in text  # 66 命令怪兽串不再平铺
    assert "First time? vrp run <repo>" in text
    assert "vrp config" in text
    for name in ("run", "curate", "transact", "mutate-run", "session", "mcp"):
        assert f"\n  {name} " in text, f"{name} missing from grouped help"


def test_high_freq_subcommand_help_examples():
    """12 高频子命令 --help：尾部 EXAMPLES + repo 参数说明。"""
    parser = cli.build_parser()
    for name in ("run", "status", "findings", "dossier", "report", "s2",
                 "curate", "transact", "verdicts", "mcp", "etl", "enforce"):
        buf = io.StringIO()
        with pytest.raises(SystemExit) as exc:
            with contextlib.redirect_stdout(buf):
                parser.parse_args([name, "--help"])
        assert exc.value.code == 0
        out = buf.getvalue()
        assert "EXAMPLES:" in out, f"{name}: EXAMPLES epilog missing"
        assert "target repo path" in out, f"{name}: repo help missing"
