"""T9 vrp review：SARIF → GitHub review comments 转换器守卫（构想v12 甜点）。

守卫重点：载荷形状（path/line/side=RIGHT/body 四键，学 reviewdog 行锚定
与 GitHub Review Comments API schema）、1:1 不聚合（聚合是 S5 立项面）、
缺 location 诚实跳过计数、--limit 只截断写出面 summary 恒真（findings
同哲学）、确定性（重跑字节级同 review-comments.json——无时间戳）、
缺失 SARIF 诚实 SystemExit 提示先跑 vrp report。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vrp import review as review_mod
from vrp.cli import build_parser, cmd_review
from vrp.cli import main as cli_main


def _sarif(results: list) -> dict:
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{"tool": {"driver": {"name": "vrp"}}, "results": results}],
    }


def _result(rule: str, uri: str, line: int, text: str = "dup clone block") -> dict:
    return {
        "ruleId": rule,
        "level": "warning",
        "message": {"text": text},
        "locations": [{
            "physicalLocation": {
                "artifactLocation": {"uri": uri},
                "region": {"startLine": line},
            }
        }],
    }


def _write_sarif(repo: Path, results: list) -> Path:
    out = repo / ".vrp" / "report" / "findings.sarif"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_sarif(results), ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ---------- 纯函数层 ----------


def test_to_comments_shape():
    """载荷形状：四键 path/line/side=RIGHT/body；body 含 ruleId+message；
    缺 location/uri/line 与非 dict result 诚实跳过并计数。
    """
    sarif = _sarif([
        _result("P4", "src/a.py", 12, "token-clone (source: s2)"),
        _result("P7", "src/b.py", 3),
        {"ruleId": "P1", "message": {"text": "no location"}},
        _result("P2", "src/c.py", 0),  # line 0 = 缺行
        "garbage",  # 非 dict result
    ])
    comments, stats = review_mod.to_review_comments(sarif)
    assert stats == {"total_results": 5, "skipped_no_location": 3}
    assert len(comments) == 2
    assert comments[0] == {
        "path": "src/a.py",
        "line": 12,
        "side": "RIGHT",
        "body": "vrp [P4] token-clone (source: s2)",
    }
    assert comments[1]["side"] == "RIGHT"
    assert comments[1]["body"].startswith("vrp [P7]")


def test_limit_summary_stays_true():
    """--limit 只截断写出面；total_results/skipped 恒真（findings 同哲学）。"""
    results = [_result("P4", f"f{i}.py", i + 1) for i in range(5)]
    comments, stats = review_mod.to_review_comments(_sarif(results), limit=2)
    assert stats == {"total_results": 5, "skipped_no_location": 0}
    assert [c["path"] for c in comments] == ["f0.py", "f1.py"]


def test_empty_results_honest_empty(tmp_path):
    """空 results → 空载荷（[] 非缺省），summary 诚实零。"""
    comments, stats = review_mod.to_review_comments(_sarif([]))
    assert comments == []
    assert stats == {"total_results": 0, "skipped_no_location": 0}


# ---------- 全链层 ----------


def test_run_review_e2e(tmp_path):
    """全链：读 SARIF → 转换 → 写 review-comments.json（载荷=纯数组）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_sarif(repo, [_result("P4", "src/a.py", 12, "token-clone (source: s2)")])
    result = review_mod.run_review(repo)
    out = Path(result["out"])
    assert out.name == "review-comments.json"
    assert out.parent == repo / ".vrp" / "report"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload == result["comments"]
    assert payload[0]["body"] == "vrp [P4] token-clone (source: s2)"
    assert result["total_results"] == 1
    assert result["limit"] is None


def test_run_review_deterministic(tmp_path):
    """确定性红线：重跑字节级同 review-comments.json（无时间戳）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_sarif(repo, [_result("P4", "a.py", 1), _result("P5", "b.py", 2)])
    r1 = review_mod.run_review(repo)
    r2 = review_mod.run_review(repo)
    out = Path(r1["out"])
    assert r1["comments"] == r2["comments"]
    assert out.read_bytes() == Path(r2["out"]).read_bytes()


def test_run_review_custom_sarif_path(tmp_path):
    """--sarif 显式路径：输出落 SARIF 同目录（不写 repo/.vrp）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "elsewhere" / "custom.sarif"
    external.parent.mkdir()
    external.write_text(json.dumps(_sarif([_result("P7", "x.py", 9)])), encoding="utf-8")
    result = review_mod.run_review(repo, sarif_path=external, limit=5)
    assert result["limit"] == 5
    assert Path(result["out"]) == external.parent / "review-comments.json"
    assert json.loads(Path(result["out"]).read_text(encoding="utf-8"))[0]["line"] == 9


def test_missing_sarif_exit(tmp_path):
    """缺失 SARIF：诚实 SystemExit 提示先跑 vrp report（cmd_xcheck 同型）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(SystemExit, match="run 'vrp report' first"):
        review_mod.run_review(repo)


# ---------- CLI 面 ----------


def test_render_text_shape():
    result = {
        "repo": "D:/x/repo",
        "sarif": "D:/x/repo/.vrp/report/findings.sarif",
        "out": "D:/x/repo/.vrp/report/review-comments.json",
        "total_results": 4,
        "skipped_no_location": 1,
        "limit": None,
        "comments": [{"path": "a.py", "line": 1, "side": "RIGHT", "body": "vrp [P4] x"}],
    }
    text = review_mod.render_text(result)
    assert text.startswith("vrp review - repo")
    assert "results  : 4 sarif results (1 skipped: no location)" in text
    assert "comments : 1 written (no limit)" in text
    assert "output   : D:/x/repo/.vrp/report/review-comments.json" in text
    assert "gh api repos/{owner}/{repo}/pulls/{n}/comments" in text


def test_cli_parser_accepts_review():
    args = build_parser().parse_args(["review", "somerepo"])
    assert args.func is cmd_review
    assert args.repo == "somerepo"
    assert args.sarif is None
    assert args.limit is None
    assert args.json is False
    args = build_parser().parse_args(
        ["review", "somerepo", "--json", "--sarif", "x.sarif", "--limit", "3"]
    )
    assert args.json is True
    assert args.sarif == "x.sarif"
    assert args.limit == 3


def test_cli_review_end_to_end(tmp_path, capsys):
    """CLI 全链：vrp review --json → rc 0 + JSON 可解析 + 载荷内嵌。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_sarif(repo, [_result("P4", "a.py", 7, "token-clone")])
    rc = cli_main(["review", str(repo), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_results"] == 1
    assert payload["comments"][0]["body"] == "vrp [P4] token-clone"
    assert (repo / ".vrp" / "report" / "review-comments.json").is_file()
