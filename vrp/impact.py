"""#146 测试影响：变更→受影响符号→建议测试（学 CBM detect_changes/test_impact）。

agent 改码后问"我动了什么、该跑哪些测试"的只读查询面：
- 变更面：默认 worktree+index 相对 HEAD（`git status --porcelain`）；
  `--base B` 时 merge-base..HEAD 已提交 diff ∪ worktree 并集（CBM 三路
  并集同款）；`--paths` 显式播种跳过 git。
- 播种（CBM hunk 行区间重叠同款）：`git diff --unified=0` hunk 与符号
  (file_path, start_line, end_line) 区间重叠 → 种子；双侧（旧/新行号）
  并集宽松播种——"每个近似都多选测试，绝不少选"；新增文件无 hunk →
  整文件播种；有 hunk 但零重叠 → 回退整文件播种；hunk cap 4096 超限
  整体退整文件播种（绝不静默欠播种）；结构容器 label 剔除。
- 影响面：多源 BFS（递归 CTE）——跨种子 MIN(hop)、hop 上限 clamp 15、
  行上限 5000 触顶 saturated 诚实注记（CBM bfs_multi 同款）。
- 建议测试：消费 etl 已落库的 test_linkage（cbm+etl-js-ts+etl-rust 三源）
  ——target 命中（种子 ∪ 受影响）的测试符号按测试文件聚合；shadow 哲学
  （建议面非 gate 面）：suggested ≠ sufficient，全量测试由消费方决定。
"""

import re
import sqlite3
from pathlib import Path

from . import dbpath, procs

IMPACT_DEFAULT_DEPTH = 2
IMPACT_MAX_DEPTH = 15          # CBM clamp 上限同款
IMPACT_ROW_CAP = 5000          # CBM MCP_BFS_LIMIT_MAX 同款（context-bomb 防护）
HUNK_CAP = 4096                # CBM hunk 上限同款：超限整体退整文件播种
IMPACT_MODULE_CAP = 256        # CBM 模块聚合上限同款
TESTS_SAMPLE_CAP = 5           # 每测试文件样本帽
IMPACT_EDGE_TYPES = ("CALLS", "CALL_REFERENCE")  # render_fusion _IMPACT_EDGE_TYPES 同口径
STRUCTURE_LABELS = frozenset({"File", "Folder", "Project", "Module", "Package", "Section"})
DIRECTIONS = ("inbound", "outbound", "both")

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _connect_ro(repo: Path) -> sqlite3.Connection:
    db = Path(repo) / ".vrp" / "facts.db"
    if not db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {db}")
    return dbpath.ro_connect(db)


def _git(repo: Path, *args: str) -> str:
    """git 单步调用（procs.run_tree 树杀纪律，R-AP 同款）；rc!=0 诚实失败。"""
    res = procs.run_tree(["git", *args], cwd=repo, timeout=60)
    if res["timed_out"]:
        raise SystemExit(f"impact: git {' '.join(args[:2])} timed out")
    if res["returncode"] != 0:
        raise SystemExit(
            f"impact: git {' '.join(args[:2])} failed (rc={res['returncode']}): "
            f"{res['stderr'].strip()[:500]}"
        )
    return res["stdout"]


def _is_git_repo(repo: Path) -> bool:
    res = procs.run_tree(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo, timeout=30)
    return res["returncode"] == 0 and res["stdout"].strip() == "true"


def _parse_status(out: str) -> list[str]:
    """porcelain 解析：XY 码+rename/copy `orig -> new` 双计；
    quotepath=false 已由调用方钉（非 ASCII 原样，R-N 同款）。"""
    paths: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        xy = line[:2]
        rest = line[3:]
        if xy[0] in "RC" and " -> " in rest:
            orig, new = rest.split(" -> ", 1)
            paths.append(orig)
            paths.append(new)
        else:
            paths.append(rest)
    return paths


def _parse_hunks(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """`git diff --unified=0` → {file: [(start,end) 行区间]}。
    双侧（- 旧行号=HEAD/facts 坐标，+ 新行号）并集宽松播种；
    count=0 空区间跳过；`diff --git`/`index`/`---`/`+++` 头行忽略。"""
    hunks: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++"):
            # `+++ /dev/null`（删除）：无新侧区间，切出当前文件防错挂前一个
            current = line[6:] if line.startswith("+++ b/") else None
            if current:
                hunks.setdefault(current, [])
            continue
        if not current:
            continue
        m = _HUNK_RE.match(line)
        if not m:
            continue
        for start_raw, count_raw in ((m.group(1), m.group(2)), (m.group(3), m.group(4))):
            start = int(start_raw)
            count = 1 if count_raw is None else int(count_raw)
            if count == 0:
                continue
            hunks[current].append((start, start + count - 1))
    return hunks


def _changed_face(repo: Path, base: str | None) -> tuple[list[str], dict[str, list[tuple[int, int]]]]:
    """变更面：changed 文件清单+hunk 行区间。"""
    status_out = _git(repo, "-c", "core.quotepath=false", "status", "--porcelain")
    changed = sorted(set(_parse_status(status_out)))
    diff_specs: list[str] = []
    if base is not None:
        merge_base = _git(repo, "merge-base", base, "HEAD").strip().splitlines()[-1]
        if not merge_base:
            raise SystemExit(f"impact: no merge base between {base} and HEAD")
        diff_specs.append(merge_base)
        committed = _git(repo, "diff", "--name-only", "-z", merge_base, "HEAD")
        changed = sorted(set(changed) | {p for p in committed.split("\0") if p})
    diff_specs.append("HEAD")
    hunks: dict[str, list[tuple[int, int]]] = {}
    for spec in diff_specs:
        text = _git(repo, "diff", "--unified=0", spec)
        for path, ranges in _parse_hunks(text).items():
            hunks.setdefault(path, []).extend(ranges)
    return changed, hunks


def _seed(con: sqlite3.Connection, changed: list[str], hunks: dict[str, list[tuple[int, int]]]) -> list[dict]:
    """播种：hunk 行区间重叠（CBM cbm_detect_node_in_hunks 同款：
    start_line <= hunk.end && end_line >= hunk.start）；无 hunk/零重叠/
    hunk 超限 → 整文件播种；结构容器 label 剔除。"""
    total_hunks = sum(len(v) for v in hunks.values())
    hunk_capped = total_hunks > HUNK_CAP
    seeds: list[dict] = []
    for path in changed:
        norm = path.replace("\\", "/")
        ranges = [] if hunk_capped else hunks.get(norm, [])
        if ranges:
            rows = con.execute(
                "SELECT fid, qualified_name, file_path, start_line, end_line, label"
                " FROM symbols WHERE file_path = ? AND label NOT IN (?,?,?,?,?,?)",
                (norm, *sorted(STRUCTURE_LABELS)),
            ).fetchall()
            hit = [
                r for r in rows
                if r[3] is not None and r[4] is not None
                and any(r[3] <= end and r[4] >= start for start, end in ranges)
            ]
            if hit:
                seeds.extend(_seed_rows(hit))
                continue
        # 无 hunk（新增/untracked/--paths 显式）或零重叠 → 整文件播种（CBM 回退同款）
        rows = con.execute(
            "SELECT fid, qualified_name, file_path, start_line, end_line, label"
            " FROM symbols WHERE file_path = ? AND label NOT IN (?,?,?,?,?,?)",
            (norm, *sorted(STRUCTURE_LABELS)),
        ).fetchall()
        seeds.extend(_seed_rows(rows))
    return _dedupe_seeds(seeds)


def _seed_rows(rows) -> list[dict]:
    out = []
    for fid, qn, file_path, start_line, end_line, label in rows:
        out.append({
            "fid": fid,
            "qn": qn,
            "file": file_path,
            "start_line": start_line,
            "end_line": end_line,
            "label": label,
        })
    return out


def _dedupe_seeds(seeds: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for s in sorted(seeds, key=lambda s: (s["file"], s["qn"])):
        if s["fid"] in seen:
            continue
        seen.add(s["fid"])
        out.append(s)
    return out


def _bfs(con: sqlite3.Connection, seed_fids: list[str], direction: str, depth: int) -> tuple[list[dict], bool]:
    """多源 BFS（递归 CTE）：跨种子 MIN(hop)、(hop, qn) 稳定序；
    行上限 IMPACT_ROW_CAP 触顶 saturated 诚实（种子自身排除——CBM 同款）。"""
    if not seed_fids:
        return [], False
    con.execute("CREATE TEMP TABLE IF NOT EXISTS impact_seeds (fid TEXT PRIMARY KEY)")
    con.execute("DELETE FROM impact_seeds")
    con.executemany("INSERT OR IGNORE INTO impact_seeds (fid) VALUES (?)", [(f,) for f in seed_fids])
    marks = ",".join("?" * len(IMPACT_EDGE_TYPES))
    if direction == "inbound":
        neighbor = "e.src_fid"
        join_on = "e.dst_fid = b.fid"
    elif direction == "outbound":
        neighbor = "e.dst_fid"
        join_on = "e.src_fid = b.fid"
    else:  # both：CASE 取对端（单递归项，不赌多递归项方言）
        neighbor = "CASE WHEN e.src_fid = b.fid THEN e.dst_fid ELSE e.src_fid END"
        join_on = "e.src_fid = b.fid OR e.dst_fid = b.fid"
    step = (
        f"SELECT {neighbor}, b.hop + 1 FROM bfs b"
        f" JOIN edges e ON {join_on}"
        f" WHERE b.hop < ? AND e.type IN ({marks})"
    )
    params = (depth, *IMPACT_EDGE_TYPES)
    sql = (
        "WITH RECURSIVE bfs(fid, hop) AS ("
        " SELECT fid, 0 FROM impact_seeds"
        " UNION " + step +
        ") SELECT b.fid, s.qualified_name, s.file_path, MIN(b.hop) AS hop"
        " FROM bfs b JOIN symbols s ON s.fid = b.fid"
        " WHERE b.hop > 0 AND b.fid NOT IN (SELECT fid FROM impact_seeds)"
        " GROUP BY b.fid, s.qualified_name, s.file_path"
        f" ORDER BY hop, s.qualified_name LIMIT {IMPACT_ROW_CAP + 1}"
    )
    rows = con.execute(sql, params).fetchall()
    con.execute("DELETE FROM impact_seeds")
    saturated = len(rows) > IMPACT_ROW_CAP
    impacted = [
        {"fid": fid, "qn": qn, "file": file_path, "hop": hop}
        for fid, qn, file_path, hop in rows[:IMPACT_ROW_CAP]
    ]
    return impacted, saturated


def _suggested_tests(con: sqlite3.Connection, fids: list[str]) -> list[dict]:
    """test_linkage 消费面：target 命中（种子 ∪ 受影响）的测试符号按测试
    文件聚合（count=distinct 测试符号数；sample qn cap TESTS_SAMPLE_CAP）。"""
    if not fids:
        return []
    per_chunk = 500
    hits: dict[str, dict] = {}
    for i in range(0, len(fids), per_chunk):
        chunk = fids[i:i + per_chunk]
        marks = ",".join("?" * len(chunk))
        rows = con.execute(
            "SELECT DISTINCT ts.file_path, ts.qualified_name FROM test_linkage t"
            f" JOIN symbols ts ON ts.fid = t.test_fid WHERE t.target_fid IN ({marks})",
            chunk,
        ).fetchall()
        for test_file, test_qn in rows:
            entry = hits.setdefault(test_file, {"count": 0, "qns": []})
            entry["count"] += 1
            if len(entry["qns"]) < TESTS_SAMPLE_CAP:
                entry["qns"].append(test_qn)
    return [
        {"test_file": f, "tests": hits[f]["count"], "sample_qns": hits[f]["qns"]}
        for f in sorted(hits, key=lambda f: (-hits[f]["count"], f))
    ]


def _modules(paths: list[str]) -> list[dict]:
    """top-2 路径段聚合（CBM 同款），cap IMPACT_MODULE_CAP 溢出 '(other)'。"""
    counts: dict[str, int] = {}
    for p in paths:
        parts = p.replace("\\", "/").split("/")
        key = "/".join(parts[:2]) if len(parts) > 1 else parts[0]
        counts[key] = counts.get(key, 0) + 1
    out = [{"module": m, "count": counts[m]} for m in sorted(counts, key=lambda m: (-counts[m], m))]
    if len(out) > IMPACT_MODULE_CAP:
        kept = out[:IMPACT_MODULE_CAP]
        rest = sum(e["count"] for e in out[IMPACT_MODULE_CAP:])
        kept.append({"module": "(other)", "count": rest})
        out = kept
    return out


def impact_report(
    repo: Path,
    base: str | None = None,
    paths: list[str] | None = None,
    depth: int = IMPACT_DEFAULT_DEPTH,
    direction: str = "inbound",
    tests: bool = True,
) -> dict:
    """变更影响+建议测试查询面（只读）。

    - paths 显式：跳过 git 直接播种（CI/调试场景）。
    - 默认：worktree+index 相对 HEAD 变更；--base：merge-base..HEAD ∪ worktree。
    - depth clamp [1, IMPACT_MAX_DEPTH]（CBM WARN-clamp 同款语义，诚实回显）。
    """
    if direction not in DIRECTIONS:
        raise SystemExit(f"impact: direction must be one of {DIRECTIONS}: {direction}")
    eff_depth = max(1, min(int(depth), IMPACT_MAX_DEPTH))
    if paths:
        changed = sorted({p.replace("\\", "/") for p in paths})
        hunks: dict[str, list[tuple[int, int]]] = {}
    else:
        if not _is_git_repo(repo):
            raise SystemExit(
                "impact: not a git repo and no --paths given - nothing to diff against"
            )
        changed, hunks = _changed_face(repo, base)
    con = _connect_ro(repo)
    try:
        seeds = _seed(con, changed, hunks)
        seed_fids = [s["fid"] for s in seeds]
        impacted, saturated = _bfs(con, seed_fids, direction, eff_depth)
        impacted_fids = [i["fid"] for i in impacted]
        suggested = _suggested_tests(con, seed_fids + impacted_fids) if tests else []
    finally:
        con.close()
    module_paths = [s["file"] for s in seeds] + [i["file"] for i in impacted]
    return {
        "level": "impact",
        "base": base or "HEAD (worktree+index)",
        "direction": direction,
        "depth": eff_depth,
        "changed_files": changed,
        "seeds": seeds,
        "impacted_total": len(impacted),
        "impacted": impacted,
        "saturated": saturated,
        "suggested_tests": suggested,
        "modules": _modules(module_paths),
        "note": "changed-face diff -> hunk-range-overlap seeding -> multi-source BFS (MIN hop over seeds, "
        f"edges {IMPACT_EDGE_TYPES}, row cap {IMPACT_ROW_CAP}, saturated means totals are lower bounds); "
        "suggested_tests from test_linkage (static derivation, best-effort): suggested is NOT sufficient - "
        "seeds approximate (facts.db is the last etl snapshot; hunk line ranges may drift vs worktree)",
    }
