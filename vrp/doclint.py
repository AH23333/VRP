"""P23（构想v11 亮点首推）：活文档防漂移——文档数字断言对账 `vrp doclint`。

本仓 test_docs_drift 守卫（README 可证伪断言对账）的产品化泛化：文档中
的数字断言（测试数/工具数/规则文件数）自动对账真实值——"文档说谎检测器"。

设计三要点（构想v11 §P23）：
- **声明式标记**：`<!-- doclint {...} -->` HTML 注释内嵌 JSON——渲染不可
  见、解析走 json.loads 无自造语法。标记可在文档任意位置（claim 正则对
  全文搜索，无需与断言文本相邻）。**体以 `{"` 起始才算断言标记**：文档
  描述 doclint 本身的示例惯用 `{...}`/`{json}` 省略形态（本仓 README
  CLI 表即活例），静默跳过不误报；`{"` 起始但解析失败/非对象=真打字
  错误，错误条目抓红（标记静默消失=漂移失察，是更坏的失败模式）。
- **真值源注册**（v1 三种只读源，kind 白名单外诚实报错不猜）：
  - glob：`{"kind": "glob", "pattern": "<相对仓根的 glob>"}` → 匹配文件数
  - recount：`{"kind": "recount", "path": "<文件>", "regex": "<正则>"}` → 文件内匹配数
  - pytest-collect：`{"kind": "pytest-collect", "component": "quick|deep|total"}`
    → `pytest --collect-only -q` 计数（同仓 dogfood 真值源；走 procs.run_tree
    树杀——R-AQ 守卫：vrp/ 内不落裸 subprocess.run）
- **失败呈现**：断言→期望（claimed）→实际（actual）→修法指引；错误类
  （标记坏/claim 不中/真值源失败）诚实报错不崩整跑（R-F 降级契约）。

诚实边界记档：v1 断言值只认十进制整数（claim 捕获组 int() 解析）——
中文数字/浮点/版本号对账不做，真需求再立项；单标记单断言（一行注释
一个 JSON 对象）。pytest-collect 源在被分析仓内执行 pytest——信任模型
同 `vrp run`（本地分析工具跑本地仓，非远程不可信输入面）。

只读面：零写入（不触 facts.db、不落工件）；确定性（输出无时间戳，
断言序=扫描序+文档序，扫描序=路径排序）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .procs import run_tree

MARKER_PREFIX = "<!-- doclint "
MARKER_SUFFIX = "-->"
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".vrp",
        ".venv",
        "__pycache__",
        "node_modules",
        "work",
        "dist",
        "build",
    }
)
COLLECT_TIMEOUT = 300  # test_docs_drift 同值：collect-only ~5s，300s 上界留足
COMPONENTS = ("quick", "deep", "total")
DRIFT_HINT = "doc number is stale or the truth drifted - update the doc or investigate"


def find_markers(text: str) -> list[tuple[int, str]]:
    """提取全部 doclint 标记体：(文档偏移, JSON 体)。

    语法=前缀 `<!-- doclint ` 到下一个 `-->`；体为单行 JSON 对象。
    无闭合的残缺标记诚实忽略（尾部截断文档不造幻影断言）。
    """
    markers: list[tuple[int, str]] = []
    pos = 0
    while True:
        start = text.find(MARKER_PREFIX, pos)
        if start < 0:
            return markers
        end = text.find(MARKER_SUFFIX, start + len(MARKER_PREFIX))
        if end < 0:
            return markers
        markers.append((start, text[start + len(MARKER_PREFIX):end].strip()))
        pos = end + len(MARKER_SUFFIX)


def _parse_body(body: str) -> dict | None:
    """标记体 → dict；非 JSON 对象（解析失败/标量/数组）诚实 None。"""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _scan_docs(repo: Path) -> list[Path]:
    """仓内全部 markdown（README+rglob），跳过生成物目录；路径排序确定。"""
    found: list[Path] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        for name in files:
            if name.endswith(".md"):
                found.append(Path(root) / name)
    return sorted(set(found))


def _run_collect(repo: Path) -> tuple[dict | None, str | None]:
    """pytest collect 真值 (quick/total/deep) 或 (None, 错误)。

    两种汇总形态：分层仓 `N/M tests collected (D deselected)` →
    quick=N/total=M/deep=D；普通仓 `N tests collected` → 三面同 N（deep=0）。
    走 run_tree 树杀（R-AQ：超时杀整树，pytest runner 孙进程不残留）。
    """
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONIOENCODING"] = "utf-8"
    res = run_tree(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=repo,
        timeout=COLLECT_TIMEOUT,
        env=env,
    )
    if res["timed_out"]:
        return None, f"pytest collect timed out after {COLLECT_TIMEOUT}s"
    out = res["stdout"]
    m = re.search(r"(\d+)/(\d+) tests collected \((\d+) deselected\)", out)
    if m:
        return (
            {
                "quick": int(m.group(1)),
                "total": int(m.group(2)),
                "deep": int(m.group(3)),
            },
            None,
        )
    m = re.search(r"(\d+) tests? collected", out)
    if m:
        n = int(m.group(1))
        return {"quick": n, "total": n, "deep": 0}, None
    return None, f"pytest collect output not parseable (rc={res['returncode']}): {out[-200:].strip()!r}"


def _actual(source: object, repo: Path, collect_cache: dict) -> tuple[int | None, str | None]:
    """真值源求值 → (actual, None) 或 (None, 错误)。kind 白名单外报错。"""
    if not isinstance(source, dict):
        return None, "source must be a JSON object"
    kind = source.get("kind")
    if kind == "glob":
        pattern = source.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return None, "glob source needs a non-empty pattern"
        try:
            return len(list(repo.glob(pattern))), None
        except (NotImplementedError, ValueError) as exc:
            return None, f"glob pattern invalid: {exc}"
    if kind == "recount":
        path = source.get("path")
        rx = source.get("regex")
        if not isinstance(path, str) or not path:
            return None, "recount source needs a path"
        if not isinstance(rx, str) or not rx:
            return None, "recount source needs a regex"
        target = repo / path
        if not target.is_file():
            return None, f"recount target missing: {path}"
        try:
            compiled = re.compile(rx)
        except re.error as exc:
            return None, f"recount regex invalid: {exc}"
        return len(compiled.findall(target.read_text(encoding="utf-8-sig"))), None
    if kind == "pytest-collect":
        component = source.get("component", "quick")
        if component not in COMPONENTS:
            return None, f"unknown pytest-collect component: {component!r}"
        key = str(repo)
        if key not in collect_cache:
            got, err = _run_collect(repo)
            if err is not None:
                return None, err
            collect_cache[key] = got if got is not None else {}
        return collect_cache[key].get(component), None
    return None, f"unknown source kind: {kind!r}"


def _evaluate(
    spec: dict,
    doc_rel: str,
    doc_text: str,
    repo: Path,
    collect_cache: dict,
) -> dict:
    """单断言求值：claimed（claim 正则首捕全文）vs actual（真值源）。"""
    entry: dict = {"id": "?", "doc": doc_rel, "ok": False}
    aid = spec.get("id")
    if isinstance(aid, str) and aid:
        entry["id"] = aid
    claim = spec.get("claim")
    if not isinstance(claim, str) or not claim:
        entry["error"] = "marker missing claim regex"
        return entry
    try:
        compiled = re.compile(claim)
    except re.error as exc:
        entry["error"] = f"claim regex invalid: {exc}"
        return entry
    m = compiled.search(doc_text)
    if m is None:
        entry["error"] = "claim pattern not found in doc (text rewritten or deleted)"
        return entry
    if compiled.groups < 1 or m.group(1) is None:
        entry["error"] = "claim regex must capture the number in group 1 (no groups or group 1 unmatched)"
        return entry
    try:
        claimed = int(m.group(1))
    except ValueError:
        entry["error"] = f"claim capture is not an integer: {m.group(1)!r}"
        return entry
    entry["claimed"] = claimed
    actual, err = _actual(spec.get("source"), repo, collect_cache)
    if err is not None or actual is None:
        entry["error"] = err or "truth source returned nothing"
        return entry
    entry["actual"] = actual
    entry["ok"] = claimed == actual
    if not entry["ok"]:
        entry["hint"] = DRIFT_HINT
    return entry


def run_doclint(repo: Path) -> dict:
    """`vrp doclint` 只读入口：扫全仓 markdown，逐标记对账，坏文件降级不崩。"""
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise SystemExit(f"not a directory: {repo}")
    collect_cache: dict = {}
    entries: list[dict] = []
    files_scanned = 0
    unreadable: list[str] = []
    for doc in _scan_docs(repo):
        try:
            text = doc.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            rel = str(doc.relative_to(repo))
            unreadable.append(rel)
            continue
        rel = str(doc.relative_to(repo))
        files_scanned += 1
        for _, body in find_markers(text):
            if not body.startswith('{"'):
                continue  # 示例/散文形态（如 README CLI 表的 `{...}`）：非断言
            spec = _parse_body(body)
            if spec is None:
                entries.append(
                    {
                        "id": "?",
                        "doc": rel,
                        "ok": False,
                        "error": "marker body is not a JSON object",
                    }
                )
                continue
            entries.append(_evaluate(spec, rel, text, repo, collect_cache))
    ok = sum(1 for e in entries if e.get("ok"))
    return {
        "repo": str(repo),
        "summary": {
            "files_scanned": files_scanned,
            "unreadable_files": len(unreadable),
            "assertions": len(entries),
            "ok": ok,
            "drift": len(entries) - ok,
        },
        "assertions": entries,
    }


def render_text(result: dict) -> str:
    """人读呈现（chrome 英文，惯例同 profile/groups 链）。"""
    s = result["summary"]
    lines = [f"vrp doclint - {Path(result['repo']).name}"]
    lines.append(
        f"  scanned  : {s['files_scanned']} markdown files"
        f" ({s['unreadable_files']} unreadable skipped)"
    )
    lines.append(
        f"  asserted : {s['assertions']}  ok {s['ok']}  drift {s['drift']}"
    )
    if s["assertions"] == 0:
        lines.append(
            '  note     : no doclint markers - add <!-- doclint {"id": ...,'
            ' "claim": "...(\\\\d+)...", "source": {...}} --> to make doc'
            " numbers falsifiable"
        )
    for e in result["assertions"]:
        if e.get("ok"):
            lines.append(f"  [ok]   {e['id']}: {e['doc']} claims {e['claimed']} == actual")
        elif "error" in e:
            lines.append(f"  [fail] {e['id']}: {e['doc']} - {e['error']}")
        else:
            lines.append(
                f"  [fail] {e['id']}: {e['doc']} claims {e['claimed']}"
                f" but actual is {e['actual']} - {DRIFT_HINT}"
            )
    return "\n".join(lines)
