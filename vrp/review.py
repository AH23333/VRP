"""T9（构想v12）：SARIF → GitHub review comments 本地转换器。

学 reviewdog 的行锚定语义与 GitHub Review Comments API 载荷形状
（path/line/side/body），零服务端红线内：产物是本地 JSON 数组
（``.vrp/report/review-comments.json``），贴不贴、贴几条由人决定。

转换规则（v1）：一条 SARIF result → 一条 comment（1:1 不聚合——
聚合是 S5 finding 聚合的立项面）；side 恒 RIGHT（PR 后侧）；
body = ``vrp [ruleId] message.text``；缺 location 的 result 诚实
跳过并计数（skipped_no_location）；``--limit`` 只截断写出面，
summary 恒真（findings --limit 同哲学）。

确定性（复现性红线）：载荷不含任何时间戳——同 SARIF 同输出，
重跑字节级同 review-comments.json。
"""

from __future__ import annotations

import json
from pathlib import Path

COMMENT_SIDE = "RIGHT"  # GitHub Review Comments API：PR diff 后侧
DEFAULT_SARIF_REL = (".vrp", "report", "findings.sarif")
OUT_NAME = "review-comments.json"


def load_sarif(path: Path) -> dict:
    """读 SARIF；缺失诚实 SystemExit（cmd_xcheck 同型提示语）。"""
    if not path.is_file():
        raise SystemExit(f"findings.sarif missing - run 'vrp report' first: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def to_review_comments(sarif: dict, limit: int | None = None) -> tuple[list[dict], dict]:
    """SARIF 2.1.0 → review comments 载荷；返回 (comments, stats)。

    stats 恒真：total_results（SARIF result 总数）与 skipped_no_location
    （缺 location/uri/line 的 result 数）不随 limit 变化。
    """
    results: list = []
    for run in sarif.get("runs") or []:
        results.extend(run.get("results") or [])
    comments: list[dict] = []
    skipped = 0
    for res in results:
        if not isinstance(res, dict):
            skipped += 1
            continue
        locations = res.get("locations") or []
        phys = locations[0].get("physicalLocation") if locations else None
        uri = (phys.get("artifactLocation") or {}).get("uri") if phys else None
        region = phys.get("region") if phys else None
        line = region.get("startLine") if region else None
        if not uri or not line:
            skipped += 1
            continue
        rule = res.get("ruleId") or "unknown"
        text = (res.get("message") or {}).get("text") or ""
        comments.append({
            "path": uri,
            "line": int(line),
            "side": COMMENT_SIDE,
            "body": f"vrp [{rule}] {text}".strip(),
        })
    if limit is not None:
        comments = comments[:limit]
    return comments, {"total_results": len(results), "skipped_no_location": skipped}


def run_review(repo: Path, sarif_path: Path | None = None, limit: int | None = None) -> dict:
    """全链：读 SARIF（默认 <repo>/.vrp/report/findings.sarif）→ 转换 →
    写 <sarif 所在目录>/review-comments.json → 返回 summary（含载荷）。
    """
    src = sarif_path if sarif_path is not None else repo.joinpath(*DEFAULT_SARIF_REL)
    sarif = load_sarif(src)
    comments, stats = to_review_comments(sarif, limit)
    out = src.parent / OUT_NAME
    out.write_text(
        json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    return {
        "repo": str(repo),
        "sarif": str(src),
        "out": str(out),
        "total_results": stats["total_results"],
        "skipped_no_location": stats["skipped_no_location"],
        "limit": limit,
        "comments": comments,
    }


def render_text(result: dict) -> str:
    return "\n".join([
        f"vrp review - repo {result['repo']}",
        f"  sarif    : {result['sarif']}",
        f"  results  : {result['total_results']} sarif results"
        f" ({result['skipped_no_location']} skipped: no location)",
        f"  comments : {len(result['comments'])} written"
        f" ({f'limit {result['limit']}' if result['limit'] is not None else 'no limit'})",
        f"  output   : {result['out']}",
        "  paste    : gh api repos/{owner}/{repo}/pulls/{n}/comments"
        " (body/path/line/side per item; zero server-side)",
    ])
