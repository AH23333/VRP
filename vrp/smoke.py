"""K12 跨仓冒烟一键化（构想v11 甜点，发布纪律脚本化——v0.1.2 是手工跑的）。

设计三要点（#172）：
- verify 仓自动发现：verify/ 下含 .git 的目录即冒烟对象（新验证仓落
  目录即入面，无注册表第二处）；--only 按名过滤。
- 全链真跑：每仓 S0→S1→S1.5→xcheck 四阶段（同 `vrp run` 编排），
  阶段异常诚实 fail 带详情；content-repo 判定 skip（成功的不适用，
  P-2 语义）；facts.db 计数（symbols/edges/findings）入汇总。
- --skip-run 快面：不跑管道只读已有 facts.db + M4 freshness 核对
  （陈旧=fail——发布需要新鲜事实面）；rc=1 任一仓 fail。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import dbpath, etl as etl_mod, s0_precheck, s1_cbm, xcheck

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
VERIFY_DIR = "verify"


def discover_verify_repos(root: Path) -> list[dict]:
    """verify/ 下含 .git 的子目录（名序确定；无 verify/ → 空表）。"""
    vdir = Path(root) / VERIFY_DIR
    if not vdir.is_dir():
        return []
    return [
        {"name": d.name, "path": d}
        for d in sorted(vdir.iterdir())
        if d.is_dir() and (d / ".git").exists()
    ]


def _facts_counts(repo: Path) -> dict:
    facts = repo / ".vrp" / "facts.db"
    if not facts.exists():
        return {"facts_db": False}
    con = dbpath.ro_connect(facts)
    try:
        symbols = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        findings = con.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    finally:
        con.close()
    return {"facts_db": True, "symbols": symbols, "edges": edges, "findings": findings}


def smoke_repo(repo: Path, skip_run: bool = False) -> dict:
    """单仓冒烟：全链四阶段或 skip-run 快面；异常/陈旧诚实 fail。"""
    repo = Path(repo)
    result: dict = {
        "name": repo.name,
        "path": str(repo),
        "mode": "skip-run" if skip_run else "full",
        "skipped": None,
        "stages": {},
        "counts": {},
        "status": "fail",
        "duration_s": None,
    }
    t0 = time.monotonic()
    try:
        if skip_run:
            facts = repo / ".vrp" / "facts.db"
            if not facts.exists():
                raise RuntimeError(f"facts.db missing (run 'vrp run' first): {facts}")
            con = sqlite3.connect(facts)
            try:
                etl_mod.check_facts_freshness(con, repo)
            finally:
                con.close()
            result["stages"]["freshness"] = {"status": "ok"}
        else:
            pre = s0_precheck.precheck(repo)
            result["stages"]["precheck"] = {"status": "ok", "name": pre.get("name")}
            cr = pre.get("content_repo") or {}
            if cr.get("detected"):
                result["skipped"] = "content-repo"
                result["status"] = "pass"
                result["duration_s"] = round(time.monotonic() - t0, 3)
                return result
            idx = s1_cbm.index_repository(repo, repo.name)
            result["stages"]["cbm_index"] = idx
            etl_res = etl_mod.run_etl(repo, repo.name)
            result["stages"]["etl"] = {k: etl_res.get(k) for k in ("status", "symbols", "edges")}
            xc = xcheck.run_xcheck(repo, repo / ".vrp" / "facts.db", reindex=False)
            result["stages"]["xcheck"] = {k: xc.get(k) for k in ("status", "agreement_pct")}
        result["counts"] = _facts_counts(repo)
        if not result["counts"].get("facts_db"):
            raise RuntimeError("facts.db not produced by pipeline")
        if not result["counts"].get("symbols"):
            raise RuntimeError("facts.db has zero symbols")
        result["status"] = "pass"
    except Exception as exc:  # 阶段异常诚实 fail 带详情
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["status"] = "fail"
    result["duration_s"] = round(time.monotonic() - t0, 3)
    return result


def run_smoke(root: Path | None = None, only: list[str] | None = None, skip_run: bool = False) -> dict:
    root = Path(root) if root else DEFAULT_ROOT
    repos = discover_verify_repos(root)
    if only:
        wanted = set(only)
        names = {r["name"] for r in repos}
        missing = sorted(wanted - names)
        if missing:
            raise SystemExit(f"unknown verify repos: {missing} (known: {sorted(names)})")
        repos = [r for r in repos if r["name"] in wanted]
    results = [smoke_repo(r["path"], skip_run=skip_run) for r in repos]
    return {
        "root": str(root),
        "repos": len(results),
        "pass": sum(1 for r in results if r["status"] == "pass"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
        "skip_run": skip_run,
        "results": results,
    }


def smoke_rc(result: dict) -> int:
    return 1 if result["fail"] else 0


def render_text(result: dict) -> str:
    lines = ["vrp smoke - 跨仓冒烟汇总", f"  root: {result['root']}  mode: {'skip-run' if result['skip_run'] else 'full'}"]
    for r in result["results"]:
        if r["skipped"]:
            lines.append(
                f"  [skip] {r['name']:<28} {r['skipped']}  ({r['duration_s']}s)"
            )
            continue
        counts = r.get("counts") or {}
        nums = (
            f"symbols={counts.get('symbols')} edges={counts.get('edges')}"
            f" findings={counts.get('findings')}"
            if counts.get("facts_db")
            else "no facts.db"
        )
        mark = "pass" if r["status"] == "pass" else "FAIL"
        lines.append(f"  [{mark:<4}] {r['name']:<28} {nums}  ({r['duration_s']}s)")
        if r["status"] == "fail":
            lines.append(f"         error: {r.get('error', '')}")
    lines.append(
        f"  summary: {result['pass']} pass / {result['fail']} fail / {result['repos']} repos"
    )
    return "\n".join(lines)
