"""F16（构想v10）：坑→测试防复发索引——#148 自检方法论延伸。

踩坑历史（docs/踩坑历史.md，条目=节内编号列表项）里关键坑已带测试锁定
引用（`test_[a-z0-9_]+` token——24 token/100 条实测密度），本命令自动生成
"坑→锁定测试"反查表：哪条坑还没有测试锁着=**防复发缺口**；坑里引用的
测试在收集面不存在=**token 漂移**（改名/删测试后坑文档失锚）。gap 是
待办信息，drift 是失锚红面——两类分开呈现。

锁定判定双面（首跑 dogfood 迭代实录）：token 命中 pytest 收集的**测试名**
（参数化 `test_y[case]` 归 `test_y`——`[` 边界前缀非子串，防 test_a 误锁
test_abc）或**测试文件 stem**（"守卫在 test_io_census 里"式模块级引用，
163 文件实测面）；路径提及（`/` 前缀）与 `.py` 后缀引用是文件引用非测试
名引用，排除——首版天真正则把 8 条外仓路径提及（`tests/unit/test_
uml_activity.py` 等）误报漂移，辨伪后修正（#148：首红先辨伪）。

判定（构想原文）：造——纯文本处理+测试收集，零新依赖。pytest 收集面
走 procs.run_tree（R-AQ 守卫同 doclint pytest-collect 源），`-m ''` 覆盖
addopts 的 `-m 'not deep'`（坑 token 含深层测试，quick-only 会误报）；
测试注入替身可测（run_tree setattr 先例）。

rc 语义：报告面恒 0（缺口是信息不是错误，CI/人读消费 --json/text）。
确定性：不写任何文件；坑文档+收集面同则同输出。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .procs import run_tree

PITFALLS_MD = "docs/踩坑历史.md"
COLLECT_TIMEOUT = 300  # doclint 同值：collect-only ~5s，300s 上界留足
# 路径提及排除：`/`/`\` 前缀（tests/unit/test_x.py 形态）与 `.py` 后缀
# （test_x.py:168 文件行号引用）都是文件引用非测试名引用——首跑 dogfood
# 8 条"漂移"全为此类假阳性（#148 自检方法论：首红先辨伪）。
_TEST_TOKEN = re.compile(r"(?<![/\\])\btest_[a-z0-9_]{3,}\b(?!\.py)")
_ENTRY_RE = re.compile(r"^(\d+)\. (.*)$")


def parse_pitfalls(text: str) -> list[dict]:
    """坑文档解析：节（##）+节内编号条目（缩进续行归条目）；
    条目全文（标题+正文）提取 test token 集为锁定引用。"""
    entries: list[dict] = []
    section = ""
    current: dict | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            current = None
            continue
        m = _ENTRY_RE.match(line)
        if m:
            current = {
                "section": section,
                "index": int(m.group(1)),
                "title": m.group(2).strip(),
                "_body": [],
            }
            entries.append(current)
            continue
        if current is not None and line.strip() and not line.startswith("#"):
            current["_body"].append(line)
    for e in entries:
        full = e["title"] + "\n" + "\n".join(e.pop("_body"))
        e["tests"] = sorted(set(_TEST_TOKEN.findall(full)))
        e["ref"] = f"{e['section']} #{e['index']}" if e["section"] else f"#{e['index']}"
    return entries


def collect_tests(repo: Path) -> tuple[set[str], set[str], str | None]:
    """pytest 收集面 (测试名全集, 测试文件 stem 全集, 错误)；
    `-m ''` 覆盖 addopts 的 `-m 'not deep'`——坑文档 token 含深层测试
    （test_deep_cargo_mutants 等），quick-only 收集会把深层锁定误报漂移；
    文件 stem 面承接模块级引用（"守卫在 test_io_census 里"）——163 stem
    实测；失败诚实返回 (空集, 空集, 错误)——真值源缺失不猜。"""
    res = run_tree(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-m", ""],
        cwd=repo,
        timeout=COLLECT_TIMEOUT,
    )
    if res["timed_out"]:
        return set(), set(), f"pytest collect timed out after {COLLECT_TIMEOUT}s"
    if res["returncode"] != 0:
        return set(), set(), f"pytest collect failed (rc={res['returncode']})"
    names: set[str] = set()
    stems: set[str] = set()
    for line in (res["stdout"] or "").splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("=", "-", "*", " ")):
            path, _, name = line.rpartition("::")
            names.add(name.strip())
            stems.add(Path(path.strip()).stem)
    return names, stems, None


def _token_locked(token: str, names: set[str], stems: set[str]) -> bool:
    """token 锁定判定：全名相等、参数化前缀（test_y[case] 归 test_y）、
    或测试文件 stem（模块级引用）。"""
    return (
        token in names
        or any(n.startswith(token + "[") for n in names)
        or token in stems
    )


def run_pitindex(repo: Path) -> dict:
    """`vrp pitindex` 入口：坑文档缺失/收集失败诚实 SystemExit。"""
    repo = Path(repo).resolve()
    md = repo / PITFALLS_MD
    if not md.exists():
        raise SystemExit(f"pitfalls doc missing: {md}")
    entries = parse_pitfalls(md.read_text(encoding="utf-8"))
    names, stems, err = collect_tests(repo)
    if err:
        raise SystemExit(err)
    locked: list[dict] = []
    gaps: list[dict] = []
    drifted: list[dict] = []
    for e in entries:
        if not e["tests"]:
            gaps.append({"ref": e["ref"], "title": e["title"], "tests": []})
            continue
        hits = [t for t in e["tests"] if _token_locked(t, names, stems)]
        misses = [t for t in e["tests"] if not _token_locked(t, names, stems)]
        if hits:
            locked.append({"ref": e["ref"], "title": e["title"], "tests": hits})
        else:
            gaps.append({"ref": e["ref"], "title": e["title"], "tests": []})
        if misses:
            drifted.append({"ref": e["ref"], "title": e["title"], "drifted_tokens": misses})
    return {
        "repo": str(repo),
        "summary": {
            "total": len(entries),
            "locked": len(locked),
            "gaps": len(gaps),
            "drifted": len(drifted),
            "collected_tests": len(names),
            "collected_files": len(stems),
        },
        "locked": locked,
        "gaps": gaps,
        "drifted": drifted,
    }


def render_text(result: dict) -> str:
    """人读呈现（chrome 英文，惯例同报告链；缺口=待办信息，漂移=失锚红面）。"""
    s = result["summary"]
    lines = [f"vrp pitindex - {Path(result['repo']).name}"]
    lines.append(
        f"  pitfalls  : {s['total']} total - {s['locked']} test-locked,"
        f" {s['gaps']} gap(s), {s['drifted']} drifted"
        f" (collected {s['collected_tests']} tests)"
    )
    if result["drifted"]:
        lines.append("  DRIFT (pitfall cites a test that no longer exists):")
        for d in result["drifted"]:
            lines.append(f"    {d['ref']}  {', '.join(d['drifted_tokens'])}  - {d['title'][:60]}")
    if result["gaps"]:
        lines.append("  GAPS (pitfall without a locking test - recurrence risk):")
        for g in result["gaps"]:
            lines.append(f"    {g['ref']}  {g['title'][:70]}")
    if result["locked"]:
        lines.append("  locked (pitfall -> test(s)):")
        for e in result["locked"]:
            lines.append(f"    {e['ref']}  {', '.join(e['tests'])}")
    return "\n".join(lines)
