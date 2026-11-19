"""A1（构想v10）：架构腐化时间线——git 历史采样回放 ETL → 度量演化序列。

"这个仓库哪天开始烂的"：git log 采样（stride 步进恒含首尾 commit）→
单 detached worktree 串行推进（用户工作树零接触红线；worktree 在系统
temp，finally 强制清理+prune）→ 每样本 CBM 索引（专用名
`vrp-timeline-<name>`，不碰用户主 CBM 缓存库；运行毕删除时间线缓存
库——缓存卫生）→ J8 增量 ETL 链（链式 facts.db 相邻样本未变文件
content-hash 命中跳过重解析，#190 是本模块的成本前提）→ 步间度量查
询 → `.vrp/timeline.json` 确定性静态件（git 历史+参数的确定函数，固
定键序无 wall-clock 字段——同仓同参数字节级复现）+ 转折点文本叙事。

学 Tornhill（hotspot=chg×complexity 度量定义；git log 挖掘形态与
cochange 同源）+ 造（回放调度与链式推进是 facts.db 底座独有优势）。

错误契约（设计稿 §6）：非 git 仓/无 commit 响亮 RuntimeError（测量失
败不是零样本——静默空曲线是说谎）；单样本 ETL 失败中止整回放不跳样
本（跳样本=曲线缺口说谎，增量链断不可续）；<2 样本正常单点输出+明示。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import complexity
from . import etl as etl_mod
from . import procs
from . import s1_cbm
from . import s2_deadcode

DEFAULT_SAMPLES = 20
GIT_TIMEOUT = 60
US = "\x1f"
GIT_LOG_FORMAT = "%x1f%H%x1f%aI"
# CBM 语义清单合成行前缀（CBM_SEMANTIC_INPUT_PREFIX，pipeline_internal.h
# 1585）：file_hashes/etl_file_state 会携带这些"非实体文件"行（J8 wt_hash
# 对缺失文件容忍空串）——files/loc 计数必须排除，否则实读取 FileNotFoundError。
_SYNTHETIC_PREFIX = ".codebase-memory/.semantic-input/"
_TURNING_METRICS = ("dead_code_candidates", "scc_count", "edge_density")
_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> dict:
    res = procs.run_tree(["git", *args], cwd=repo, timeout=timeout)
    if res["timed_out"]:
        raise RuntimeError(
            f"git {' '.join(args)} timed out after {timeout}s (tree killed): {(res['stderr'] or '')[:300]}"
        )
    if res["returncode"] != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({res['returncode']}): {(res['stderr'] or '')[:300]}"
        )
    return res


def _git_best_effort(repo: Path, *args: str) -> None:
    """清理路径专用：失败吞掉不遮在飞异常（finally 纪律）。"""
    try:
        procs.run_tree(["git", *args], cwd=repo, timeout=GIT_TIMEOUT)
    except Exception:  # noqa: BLE001 - best-effort 清理
        pass


def parse_history(raw: str) -> list[dict]:
    """git log 原始输出 → commit 列表（git log 序：新→旧）。

    每元素 {"sha", "date", "files"}；文件去重+规范化同 cochange.parse_log
    先例（去首尾空白+反斜杠转正斜杠+per-commit 排序）。US 起始行 =
    commit 头（US+%H+US+%aI）。
    """
    commits: list[dict] = []
    current: dict | None = None
    for line in raw.splitlines():
        if line.startswith(US):
            if current is not None:
                commits.append(current)
            parts = line[1:].split(US)
            current = {
                "sha": parts[0] if parts else "",
                "date": parts[1] if len(parts) > 1 else "",
                "files": set(),
            }
        elif current is not None and line.strip():
            path = line.strip().replace("\\", "/")
            if path:
                current["files"].add(path)
    if current is not None:
        commits.append(current)
    return [
        {"sha": c["sha"], "date": c["date"], "files": sorted(c["files"])} for c in commits
    ]


def pick_samples(n: int, samples: int) -> list[int]:
    """采样索引（history 新→旧序）：0（最新）与 n-1（最旧=首 commit）恒含。

    stride=ceil(n/samples) 均匀步进；追加尾点溢出 samples 时丢弃尾前点
    保首尾（点数恒 ≤ samples）。n<=samples 全采。
    """
    if n <= 0:
        return []
    if samples >= n:
        return list(range(n))
    stride = -(-n // samples)
    idxs = list(range(0, n, stride))
    if idxs[-1] != n - 1:
        idxs.append(n - 1)
        if len(idxs) > samples:
            del idxs[-2]
    return idxs


def count_file_cycles(adj: dict[str, set[str]]) -> int:
    """文件级强连通分量计数（size>1）——迭代 Tarjan（大图不炸栈）。

    自环不算（文件自依赖无意义）；邻接表键序遍历+平局字典序——纯确定。
    """
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    cycles = 0
    for root in sorted(adj):
        if root in index_of:
            continue
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work: list[tuple[str, object]] = [(root, iter(sorted(adj[root])))]
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:  # type: ignore[union-attr]
                if nxt not in adj:
                    continue
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(adj[nxt]))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                size = 0
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    size += 1
                    if w == node:
                        break
                if size > 1:
                    cycles += 1
    return cycles


def _line_count(data: bytes) -> int:
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _collect_sample(
    con: sqlite3.Connection,
    worktree: Path,
    window: list[dict],
    prev_fids: set[str] | None,
    loc_cache: dict[tuple[str, str], int],
) -> tuple[dict, set[str]]:
    """链库步间度量查询（设计稿 §3 十字段）。

    loc 缓存键 (file_path, wt_hash)——J8 wt_hash 是整文件 sha256，字节
    不变行数不变，相邻样本未变文件零重读。
    """
    files = 0
    loc = 0
    for fp, wt_hash in con.execute("SELECT file_path, wt_hash FROM etl_file_state"):
        if fp.startswith(_SYNTHETIC_PREFIX):
            continue  # CBM 合成 semantic-input 行非实体文件（dogfood 实录）
        files += 1
        key = (fp, wt_hash)
        if key not in loc_cache:
            loc_cache[key] = _line_count((worktree / fp).read_bytes())
        loc += loc_cache[key]
    symbols = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    dead = len(s2_deadcode.dead_code_candidates(con))
    fids = {r[0] for r in con.execute("SELECT fid FROM symbols")}
    fid_file = dict(con.execute("SELECT fid, file_path FROM symbols"))
    adj: dict[str, set[str]] = {}
    for src, dst in con.execute("SELECT src_fid, dst_fid FROM edges"):
        a = fid_file.get(src)
        b = fid_file.get(dst)
        if not a or not b or a == b:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set())
    scc = count_file_cycles(adj)
    chg: dict[str, int] = {}
    for c in window:
        for f in c["files"]:
            chg[f] = chg.get(f, 0) + 1
    hotspots = []
    for fp in sorted(chg):
        if not fp.endswith(".py"):
            continue
        path = worktree / fp
        if not path.is_file():
            continue
        rows, ok = complexity.scan_py_file(path)
        if not ok:
            continue
        cc = sum(r["cc"] for r in rows)
        if cc > 0:
            hotspots.append({"file": fp, "chg": chg[fp], "cc": cc, "hotspot": chg[fp] * cc})
    hotspots.sort(key=lambda h: (-h["hotspot"], h["file"]))
    metrics = {
        "files": files,
        "loc": loc,
        "symbols": symbols,
        "edges": edges,
        "edge_density": round(edges / symbols, 6) if symbols else 0.0,
        "dead_code_candidates": dead,
        "births": len(fids - prev_fids) if prev_fids is not None else len(fids),
        "deaths": len(prev_fids - fids) if prev_fids is not None else 0,
        "scc_count": scc,
        "hotspot_top10": hotspots[:10],
    }
    return metrics, fids


def _turning_points(points: list[dict]) -> list[dict]:
    """相邻样本最大正向跳变（死代码/环数/耦合）——"哪天开始烂的"。

    平局取首个（时间序最早）；无正向恶化返回空表。
    """
    out: list[dict] = []
    for metric in _TURNING_METRICS:
        best: dict | None = None
        for a, b in zip(points, points[1:]):
            delta = b[metric] - a[metric]
            if isinstance(delta, float):
                delta = round(delta, 6)  # 浮点减法噪音（lusiem dogfood 实录 +0.08295299…）
            if delta > 0 and (best is None or delta > best["delta"]):
                best = {
                    "metric": metric,
                    "from_sha": a["sha"],
                    "from_date": a["date"],
                    "to_sha": b["sha"],
                    "to_date": b["date"],
                    "before": a[metric],
                    "after": b[metric],
                    "delta": delta,
                }
        if best is not None:
            out.append(best)
    return out


def run_timeline(
    repo: Path, samples: int = DEFAULT_SAMPLES, cbm_name: str | None = None
) -> dict:
    """采样回放主编排（设计稿 D1-D6）。返回 dict（含 artifact 落盘路径）。"""
    repo = repo.resolve()
    name = cbm_name or repo.name
    if "/" in name or "\\" in name:
        raise RuntimeError(f"unsafe cbm name for timeline: {name!r}")
    if samples < 1:
        raise RuntimeError(f"samples must be >= 1, got {samples}")
    res = _git(repo, "log", "--no-merges", "--name-only", f"--pretty=format:{GIT_LOG_FORMAT}")
    history = parse_history(res["stdout"] or "")
    if not history:
        raise RuntimeError(f"no commits in git history (--no-merges): {repo}")
    idxs = pick_samples(len(history), samples)
    order = list(reversed(idxs))  # 回放序：最旧→最新
    tl_name = f"vrp-timeline-{name}"
    tmpdir = Path(tempfile.mkdtemp(prefix="vrp-timeline-"))
    chain_db = tmpdir / "facts.db"
    worktree = tmpdir / "wt"
    points: list[dict] = []
    loc_cache: dict[tuple[str, str], int] = {}
    prev_fids: set[str] | None = None
    cbm_db: Path | None = None
    wt_added = False
    try:
        for k, i in enumerate(order):
            sha = history[i]["sha"]
            if k == 0:
                _git(repo, "worktree", "add", "--detach", str(worktree), sha)
                wt_added = True
            else:
                _git(worktree, "checkout", "--detach", "--force", sha)
            idx = s1_cbm.index_repository(worktree, tl_name)
            cbm_db = Path(str(idx["db"]))
            etl_mod.run_etl(worktree, tl_name, out_db=chain_db)
            prev_idx = order[k - 1] if k else len(history)
            window = history[i:prev_idx]
            con = sqlite3.connect(chain_db)
            try:
                metrics, fids = _collect_sample(con, worktree, window, prev_fids, loc_cache)
            finally:
                con.close()
            points.append({"sha": sha, "date": history[i]["date"], **metrics})
            prev_fids = fids
    finally:
        if wt_added:
            _git_best_effort(repo, "worktree", "remove", "--force", str(worktree))
        _git_best_effort(repo, "worktree", "prune")
        shutil.rmtree(tmpdir, ignore_errors=True)
        if cbm_db is not None:
            try:
                cbm_db.unlink(missing_ok=True)
            except OSError:
                pass
    turning = _turning_points(points)
    artifact = {
        "samples_requested": samples,
        "commits_total": len(history),
        "points": points,
        "turning_points": turning,
    }
    out = repo / ".vrp" / "timeline.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes((json.dumps(artifact, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {"artifact": str(out), **artifact}


def _spark(vals: list) -> str:
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return _SPARK_BLOCKS[3] * len(vals)
    return "".join(
        _SPARK_BLOCKS[int((v - lo) / (hi - lo) * 7 + 0.5)] for v in vals
    )


def render_text(result: dict) -> str:
    """人读呈现：趋势 sparkline + 逐样本表 + 转折点叙事。"""
    points = result["points"]
    lines = []
    lines.append("== A1 架构腐化时间线 ==")
    lines.append(
        f"commits: {result['commits_total']}  samples: {len(points)}  artifact: {result['artifact']}"
    )
    if len(points) < 2:
        lines.append("样本不足 2 点——无趋势可读（单 commit 仓）")
    for metric in ("symbols", "edges", "dead_code_candidates", "scc_count", "edge_density"):
        vals = [p[metric] for p in points]
        lines.append(f"{metric:22s} {_spark(vals)}  {vals[0]} -> {vals[-1]}")
    lines.append(
        f"{'#':>3} {'sha':8} {'date':10} {'files':>6} {'loc':>7} {'sym':>5}"
        f" {'edges':>6} {'dead':>5} {'scc':>4} {'density':>9} {'birth':>6} {'death':>6}"
    )
    for k, p in enumerate(points, 1):
        lines.append(
            f"{k:>3} {p['sha'][:8]:8} {p['date'][:10]:10} {p['files']:>6} {p['loc']:>7}"
            f" {p['symbols']:>5} {p['edges']:>6} {p['dead_code_candidates']:>5} {p['scc_count']:>4}"
            f" {p['edge_density']:>9} {p['births']:>6} {p['deaths']:>6}"
        )
    tp = result.get("turning_points") or []
    if tp:
        lines.append('转折点（相邻样本最大正向跳变——"哪天开始烂的"）:')
        for t in tp:
            lines.append(
                f"  {t['metric']}: {t['before']} -> {t['after']} (+{t['delta']})"
                f"  {t['from_sha'][:8]} -> {t['to_sha'][:8]}  @{t['to_date'][:10]}"
            )
    else:
        lines.append("转折点: 无正向恶化跳变（或样本不足）")
    return "\n".join(lines)
