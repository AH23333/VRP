"""K11（构想v11 甜点）：release notes 半自动生成测试。

- 素材面：git log `#NNN:` 解析 / 交接条目 `NNN. ~~标题~~` live 解析 /
  交叉核对警告（commit 有码交接无条目）
- 实时核码（#153 教训）：测试计数调 doclint._run_collect（monkeypatch
  注入固定值——不真跑 pytest）；执法语言 glob 活数
- 确定性：date 注入 → 同输入字节级同草稿
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from vrp import relnotes
from vrp.cli import build_parser, cmd_relnotes
from vrp.cli import main as cli_main


def _git(repo: Path, *args: str) -> None:
    from vrp import procs

    res = procs.run_tree(["git", *args], cwd=repo, timeout=60)
    assert res["returncode"] == 0, f"git {args} failed: {res['stderr']}"


def _mk_repo(tmp_path: Path, name: str = "relrepo") -> Path:
    repo = tmp_path / name
    (repo / "docs").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    return repo


def _commit(repo: Path, msg: str, fname: str) -> None:
    (repo / fname).write_text(fname + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _seed(repo: Path) -> None:
    _commit(repo, "base", "base.txt")
    _git(repo, "tag", "v0.0.1")
    _commit(repo, "#101: I4 doctor 环境自诊断（聚合 find_*）", "a.txt")
    _commit(repo, "chore: routine cleanup", "b.txt")
    _commit(repo, "#102: I5 explain 错误码速查", "c.txt")
    (repo / "docs" / "交接文档.md").write_text(
        "101. ~~I4 工具链环境自诊断 `vrp doctor`（构想v11 甜点）~~ **已完成**（x）\n"
        "102. ~~I5 错误码人话速查 `vrp explain`~~ **已完成**（y）\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs: handover entries")


def _patch_counts(monkeypatch, quick=100, deep=50, total=150) -> None:
    monkeypatch.setattr(
        relnotes.doclint,
        "_run_collect",
        lambda repo: ({"quick": quick, "deep": deep, "total": total}, None),
    )


def test_collect_commits_parses_batch_and_plain(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    commits = relnotes.collect_commits(repo, "v0.0.1", "HEAD")
    subjects = [c["subject"] for c in commits]
    assert len(commits) == 4
    assert [c["num"] for c in commits] == [101, None, 102, None]  # 时间正序
    assert commits[0]["hash"]
    assert subjects[0].startswith("#101: I4 doctor")
    assert subjects[-1] == "docs: handover entries"


def test_collect_commits_bad_ref_exits(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed(repo)
    with pytest.raises(SystemExit, match="git log"):
        relnotes.collect_commits(repo, "no-such-ref", "HEAD")


def test_parse_handover_titles_live(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "docs" / "交接文档.md").write_text(
        "168. ~~标题A（构想v11）~~ **已完成**（x）\n169. ~~标题B~~ **已完成**（y）\n",
        encoding="utf-8",
    )
    titles = relnotes.parse_handover_titles(repo)
    assert titles[168] == "标题A（构想v11）"
    assert titles[169] == "标题B"


def test_count_enforce_languages_glob(tmp_path):
    repo = tmp_path / "langrepo"
    (repo / "vrp").mkdir(parents=True)
    assert relnotes.count_enforce_languages(repo) == 0
    (repo / "vrp" / "s7_enforce.py").write_text("", encoding="utf-8")
    (repo / "vrp" / "s7_enforce_ts.py").write_text("", encoding="utf-8")
    (repo / "vrp" / "s7_enforce_go.py").write_text("", encoding="utf-8")
    (repo / "vrp" / "other.py").write_text("", encoding="utf-8")
    assert relnotes.count_enforce_languages(repo) == 3  # 本体 1 + 2 语言模块


def test_build_notes_cross_check_and_counts(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path, "crossrepo")
    _commit(repo, "base", "base.txt")
    _git(repo, "tag", "v0")
    _commit(repo, "#201: 有交接条目的批次", "a.txt")
    _commit(repo, "#202: 无交接条目的批次", "b.txt")
    (repo / "docs" / "交接文档.md").write_text(
        "201. ~~有条目~~ **已完成**（x）\n", encoding="utf-8"
    )
    _patch_counts(monkeypatch, quick=10, deep=5, total=15)
    result = relnotes.build_notes(
        repo, "v0", version="v9.9.9", notes_date=date(2026, 11, 18)
    )
    assert result["commits"] == 2  # #201/#202 两发；交接文档未 commit（磁盘读取即得）
    assert result["batch_range"] == [201, 202]
    assert result["warnings"] == ["#202: commit has no handover entry"]
    assert result["test_counts"] == {"quick": 10, "deep": 5, "total": 15}
    assert result["languages"] == 0  # 无 vrp/ 目录诚实 0
    assert result["batches"][0]["title"] == "有条目"


def test_build_notes_deterministic_same_date(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path, "detrepo")
    _seed(repo)
    _patch_counts(monkeypatch)
    kwargs = {"version": "v1.0.0", "notes_date": date(2026, 11, 18)}
    a = relnotes.build_notes(repo, "v0.0.1", **kwargs)
    b = relnotes.build_notes(repo, "v0.0.1", **kwargs)
    assert a == b
    assert a["markdown"] == b["markdown"]


def test_render_markdown_shape(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path, "shapegrep")
    _seed(repo)
    _patch_counts(monkeypatch, quick=1653, deep=1523, total=3176)
    result = relnotes.build_notes(
        repo, "v0.0.1", version="v0.1.3", notes_date=date(2026, 11, 18)
    )
    md = result["markdown"]
    assert md.startswith("## v0.1.3 (2026-11-18)")
    assert "4 commits since v0.0.1" in md
    assert "批次 #101-#102" in md
    assert "- #101: I4 doctor 环境自诊断（聚合 find_*）" in md
    assert "### 非批次提交" in md and "- chore: routine cleanup" in md
    assert "快速层 1653 / 深度层 1523 / 全量 3176" in md
    assert "pytest --collect-only 生成时实测" in md
    assert "人审后发布" in md


def test_render_markdown_counts_error_honest(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path, "errrepo")
    _seed(repo)
    monkeypatch.setattr(relnotes.doclint, "_run_collect", lambda repo: (None, "pytest exploded"))
    result = relnotes.build_notes(repo, "v0.0.1", notes_date=date(2026, 11, 18))
    assert "测试计数不可得：pytest exploded" in result["markdown"]


def test_cli_relnotes_end_to_end(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path, "clirel")
    _seed(repo)
    _patch_counts(monkeypatch, quick=7, deep=3, total=10)
    rc = cli_main(["relnotes", str(repo), "--from", "v0.0.1", "--version", "v0.2.0", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["version"] == "v0.2.0"
    assert out["test_counts"]["quick"] == 7
    assert "markdown" in out


def test_cli_relnotes_text_draft(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path, "clitxt")
    _seed(repo)
    _patch_counts(monkeypatch)
    rc = cli_main(["relnotes", str(repo), "--from", "v0.0.1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("## draft (")


def test_parser_flags():
    args = build_parser().parse_args(["relnotes", "r", "--from", "v0.1.2"])
    assert args.from_ref == "v0.1.2"
    assert args.to == "HEAD"
    assert args.version is None
    assert args.func is cmd_relnotes
    with pytest.raises(SystemExit):
        build_parser().parse_args(["relnotes", "r"])  # --from 必填
