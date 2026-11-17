"""K11 release notes 半自动生成（构想v11 甜点，changesets 累积模型+conventional-changelog 形态）。

设计三要点（#171）：
- 第一手素材：commit 域 `#NNN: subject` × 交接文档 `NNN. ~~标题~~` 条目
  交叉——批次提交列表 + 不一致警告（commit 有码交接无条目=漂移信号）。
- 实时核码（#153 教训落实）：数字一律生成时实测——测试计数调
  doclint._run_collect（pytest --collect-only 活数，不信 README 快照）；
  执法语言数 glob vrp/s7_enforce* 活数；commit 数 git log 活数。
- 半自动边界：产出是**草稿**（markdown 打印 stdout），人审后贴 GitHub
  release——数字带"生成时实测"注记；date 可注入（同输入同日字节级确定）。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from . import doclint, procs

COMMIT_RE = re.compile(r"^#(\d{1,4}):\s*(.+)$")
HANDOVER_RE = re.compile(r"^(\d{1,4})\. ~~(.+?)~~", re.M)

GIT_TIMEOUT = 120


def collect_commits(repo: Path, from_ref: str, to_ref: str) -> list[dict]:
    """git log from..to → [{num, subject, hash}]（num 无 #NNN 前缀时 None）。"""
    res = procs.run_tree(
        ["git", "log", "--pretty=format:%H%x00%s", f"{from_ref}..{to_ref}"],
        cwd=repo,
        timeout=GIT_TIMEOUT,
    )
    if res["timed_out"]:
        raise SystemExit(f"git log timed out after {GIT_TIMEOUT}s")
    if res["returncode"] != 0:
        raise SystemExit(
            f"git log {from_ref}..{to_ref} failed: {(res['stderr'] or '').strip()[:300]}"
        )
    commits: list[dict] = []
    for line in res["stdout"].splitlines():
        if not line.strip():
            continue
        parts = line.split("\x00", 1)
        if len(parts) != 2:
            continue
        commit_hash, subject = parts[0].strip(), parts[1].strip()
        m = COMMIT_RE.match(subject)
        commits.append(
            {
                "num": int(m.group(1)) if m else None,
                "subject": subject,
                "subject_clean": m.group(2).strip() if m else subject,
                "hash": commit_hash,
            }
        )
    commits.reverse()  # git log 新→旧；release notes 按 时间正序 #101→#102 阅读
    return commits


def parse_handover_titles(repo: Path) -> dict[int, str]:
    """交接文档.md 条目标题表 {批次号: 标题}（live 解析，非快照）。"""
    path = repo / "docs" / "交接文档.md"
    if not path.exists():
        return {}
    titles: dict[int, str] = {}
    for m in HANDOVER_RE.finditer(path.read_text(encoding="utf-8")):
        titles[int(m.group(1))] = m.group(2).strip()
    return titles


def count_enforce_languages(repo: Path) -> int:
    """执法语言活数：python 本体 1 + s7_enforce_<lang>.py 模块数（glob 实测）。"""
    base = repo / "vrp"
    if not base.is_dir():
        return 0
    langs = 1 if (base / "s7_enforce.py").exists() else 0
    langs += len(list(base.glob("s7_enforce_*.py")))
    return langs


def build_notes(
    repo: Path,
    from_ref: str,
    to_ref: str = "HEAD",
    version: str | None = None,
    notes_date: date | None = None,
) -> dict:
    """汇总+实时核码+markdown 草稿（同输入同日字节级确定）。"""
    commits = collect_commits(repo, from_ref, to_ref)
    titles = parse_handover_titles(repo)
    batch_commits = [c for c in commits if c["num"] is not None]
    other_commits = [c for c in commits if c["num"] is None]
    warnings = [
        f"#{c['num']}: commit has no handover entry"
        for c in batch_commits
        if c["num"] not in titles
    ]
    counts, collect_err = doclint._run_collect(repo)
    languages = count_enforce_languages(repo)
    nums = [c["num"] for c in batch_commits]
    result: dict = {
        "version": version,
        "from_ref": from_ref,
        "to_ref": to_ref,
        "date": (notes_date or date.today()).isoformat(),
        "commits": len(commits),
        "batch_range": [min(nums), max(nums)] if nums else [],
        "batches": [
            {"num": c["num"], "subject": c["subject_clean"], "title": titles.get(c["num"])}
            for c in batch_commits
        ],
        "other_commits": [c["subject"] for c in other_commits],
        "warnings": warnings,
        "test_counts": counts,
        "test_counts_error": collect_err,
        "languages": languages,
    }
    result["markdown"] = render_markdown(result)
    return result


def render_markdown(result: dict) -> str:
    head = result["version"] or "draft"
    lines = [f"## {head} ({result['date']})", ""]
    rng = (
        f"，批次 #{result['batch_range'][0]}-#{result['batch_range'][1]}（交接文档）"
        if result["batch_range"]
        else ""
    )
    lines.append(
        f"{result['commits']} commits since {result['from_ref']}{rng}。"
    )
    lines.append("")
    if result["batches"]:
        lines.append("### 变更")
        for b in result["batches"]:
            lines.append(f"- #{b['num']}: {b['subject']}")
        lines.append("")
    if result["other_commits"]:
        lines.append("### 非批次提交")
        for subject in result["other_commits"]:
            lines.append(f"- {subject}")
        lines.append("")
    lines.append("### 数字（实时核码，非快照）")
    tc = result["test_counts"]
    if tc:
        lines.append(
            f"- 测试：快速层 {tc['quick']} / 深度层 {tc['deep']} / 全量 {tc['total']}"
            "（pytest --collect-only 生成时实测）"
        )
    else:
        lines.append(f"- 测试计数不可得：{result['test_counts_error']}")
    lines.append(f"- 执法语言：{result['languages']}（vrp/s7_enforce* 模块生成时实测）")
    if result["warnings"]:
        lines.append("")
        lines.append("### 警告")
        for w in result["warnings"]:
            lines.append(f"- {w}")
    lines.append("")
    lines.append("> 半自动草稿：人审后发布（数字为生成时实测）。")
    return "\n".join(lines)
