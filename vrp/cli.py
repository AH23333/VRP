"""vrp CLI: precheck | index | etl | xcheck | run | status."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from . import __version__
from . import dbpath
from . import assess as assess_mod
from . import graphsci as graphsci_mod
from . import feedback as feedback_mod
from . import cochange as cochange_mod
from . import timeline as timeline_mod
from . import grouping as grouping_mod
from . import profile as profile_mod
from . import doclint as doclint_mod
from . import doctor as doctor_mod
from . import explain as explain_mod
from . import perf as perf_mod
from . import relnotes as relnotes_mod
from . import smoke as smoke_mod
from . import gc as gc_mod
from . import flakes as flakes_mod
from . import langmatrix as langmatrix_mod
from . import mdcode as mdcode_mod
from . import argcheck as argcheck_mod
from . import etl_health
from . import layer_metrics
from . import sweep
from . import migprog
from . import secrets as secrets_mod
from . import scoring as scoring_mod
from . import complexity as complexity_mod
from . import pitfall as pitfall_mod
from . import dossier_diff as dossier_diff_mod
from . import scaffold as scaffold_mod
from . import tour as tour_mod
from . import review as review_mod
from . import etl as etl_mod
from . import curate as curate_mod
from . import curate_merge as curate_merge_mod
from . import nav as nav_mod
from . import propose as propose_mod
from . import traces as traces_mod
from . import coverage as coverage_mod
from . import testcov as testcov_mod
from . import golden as golden_mod
from . import impact as impact_mod
from . import procs
from . import s0_precheck, s1_cbm, s2_astgrep, s2_deadcode, s2_jscpd, s2_mutate, s2_mutate_run, s2_p4, s3, s4_freeze, s5_dossier, s6_transact, s7_enforce, xcheck
from . import auto_curate
from . import fixpropose as fixpropose_mod
from . import analyze as analyze_mod
from . import chat as chat_mod
from . import config_cmd
from . import session as session_mod
from . import watch as watch_mod


def _repo_arg(value: str) -> Path:
    repo = Path(value).resolve()
    if not repo.is_dir():
        raise SystemExit(f"not a directory: {repo}")
    return repo


def cmd_precheck(args) -> int:
    repo = _repo_arg(args.repo)
    result = s0_precheck.precheck(repo)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_precheck(result))
    return 0


def cmd_assess(args) -> int:
    repo = _repo_arg(args.repo)
    result = assess_mod.assess(repo)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(assess_mod.render_text(result))
    return 0


def cmd_graph(args) -> int:
    repo = _repo_arg(args.repo)
    result = graphsci_mod.run_graph(repo)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(graphsci_mod.render_text(result))
    return 0


def cmd_findings(args) -> int:
    repo = _repo_arg(args.repo)
    if args.dismiss is not None:
        con, _ = _open_facts_rw(repo)
        try:
            result = feedback_mod.dismiss_finding(con, args.dismiss, args.note or "")
        finally:
            con.close()
        _print_json(result)
        return 0
    result = feedback_mod.run_findings(repo, args.limit)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(feedback_mod.render_text(result))
    return 0


def cmd_cochange(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        etl_mod.ensure_schema(con)
        result = cochange_mod.run_cochange(
            repo, con, args.min_support, args.min_confidence,
            code_only=args.code_only,
        )
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(cochange_mod.render_text(result))
    return 0


def cmd_timeline(args) -> int:
    repo = _repo_arg(args.repo)
    result = timeline_mod.run_timeline(
        repo, samples=args.samples, cbm_name=getattr(args, "cbm_name", None)
    )
    _print_text(timeline_mod.render_text(result))
    return 0


def cmd_review(args) -> int:
    repo = _repo_arg(args.repo)
    sarif = Path(args.sarif).resolve() if args.sarif else None
    result = review_mod.run_review(repo, sarif_path=sarif, limit=args.limit)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(review_mod.render_text(result))
    return 0


def cmd_score(args) -> int:
    repo = _repo_arg(args.repo)
    weights = scoring_mod.parse_weights(args.weights)
    result = scoring_mod.run_score(repo, args.limit, weights)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(scoring_mod.render_text(result))
    return 0


def cmd_complexity(args) -> int:
    repo = _repo_arg(args.repo)
    result = complexity_mod.run_complexity(repo, args.cc_threshold, args.limit)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(complexity_mod.render_text(result))
    return 0


def cmd_pitindex(args) -> int:
    repo = _repo_arg(args.repo)
    result = pitfall_mod.run_pitindex(repo)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(pitfall_mod.render_text(result))
    return 0


def cmd_dossier_diff(args) -> int:
    result = dossier_diff_mod.run_dossier_diff(args.old, args.new)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(dossier_diff_mod.render_text(result))
    return 0


def cmd_scaffold(args) -> int:
    result = scaffold_mod.run_scaffold(args.repo, args.proposal, args.out)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(scaffold_mod.render_text(result))
    return 0


def cmd_tour(args) -> int:
    result = tour_mod.run_tour(args.repo)
    if getattr(args, "html", False):
        # #223 archify v3.3.0 story #1036 概念吸收：opt-in 唯一写面
        # （默认路径仍只读零写入不变量）。
        out = Path(result["repo"]) / ".vrp" / "report" / "tour.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(tour_mod.render_html(result).encode("utf-8"))
        _print_text(f"tour.html written: {out}")
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(tour_mod.render_text(result))
    return 0


def cmd_groups(args) -> int:
    repo = _repo_arg(args.repo)
    result = grouping_mod.run_groups(repo, args.limit)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(grouping_mod.render_text(result))
    return 0


def cmd_profile(args) -> int:
    repo = _repo_arg(args.repo)
    result = profile_mod.run_profile(repo)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(profile_mod.render_text(result))
    return 0


def cmd_doclint(args) -> int:
    repo = _repo_arg(args.repo)
    result = doclint_mod.run_doclint(repo)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(doclint_mod.render_text(result))
    return 0 if result["summary"]["drift"] == 0 else 1


def cmd_doctor(args) -> int:
    repo = _repo_arg(args.repo)
    result = doctor_mod.run_doctor(repo)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(doctor_mod.render_text(result))
    return doctor_mod.doctor_rc(result)


def cmd_explain(args) -> int:
    result = explain_mod.run_explain(getattr(args, "code", None))
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(explain_mod.render_text(result))
    return explain_mod.explain_rc(result)


def cmd_index(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.cbm_name or repo.name
    result = s1_cbm.index_repository(repo, name, reindex=args.reindex)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_index(result))
    return 0


def cmd_etl(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.cbm_name or repo.name
    result = etl_mod.run_etl(repo, name, full=args.full)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_etl(result))
    return 0


def cmd_xcheck(args) -> int:
    repo = _repo_arg(args.repo)
    facts_db = repo / ".vrp" / "facts.db"
    if not facts_db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp etl' first: {facts_db}")
    result = xcheck.run_xcheck(repo, facts_db, reindex=args.reindex)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_xcheck(result))
    return 0


def cmd_xcheck_explain(args) -> int:
    """Q25 finding 叙事化——只读 facts.db，rc 恒 0（解释面非门）。"""
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp xcheck' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        result = xcheck.explain(con)
    finally:
        con.close()
    _print_text(xcheck.render_explain(result))
    return 0


def cmd_etl_health(args) -> int:
    """BB4 ETL 健康面——只读 facts.db；违例 rc=1（健康检查语义，CI 可用）。"""
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        result = etl_health.run_health(con)
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(etl_health.render_text(result))
    return 0 if result["healthy"] else 1


def cmd_layermetrics(args) -> int:
    """C8 层度量与阈值——fan_in/fan_out/instability+structure thresholds 违例 rc=1。"""
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        result = layer_metrics.run_layer_metrics(con)
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(layer_metrics.render_text(result))
    return 1 if result["violations"] else 0


def cmd_sweep(args) -> int:
    """S8 阈值敏感度扫描——findings payload 值域重切曲线+悬崖点；rc 恒 0。"""
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        result = sweep.run_sweep(con)
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(sweep.render_text(result))
    return 0


def cmd_migprog(args) -> int:
    """D11 迁移进度——seams 快照/收缩曲线；rc 恒 0（追踪面非门）。"""
    repo = _repo_arg(args.repo)
    if args.snapshot:
        result = migprog.save_snapshot(repo)
    else:
        result = migprog.progress(repo)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(migprog.render_text(result))
    return 0


def _run_pipeline(repo: Path, name: str, reindex: bool) -> int:
    """run 单次全链（S0→S1→S1.5→xcheck）——cmd_run 与 watch 重跑共用。"""
    # J10：四阶段 monotonic 计时——run 结束自动落档 .vrp/perf.jsonl（零额外命令）。
    t0 = time.monotonic()
    pre = s0_precheck.precheck(repo)
    t_precheck = time.monotonic() - t0
    _print_step("S0 precheck", {"name": pre["name"], "repo": pre["repo"], "commands": pre["commands"]})

    # P-2：内容仓库（课件/素材站类）对代码分析不适用——跳过 S1/S1.5/xcheck
    # 编排。提示语义而非硬门：单命令 'vrp index' / 'vrp etl' / 'vrp xcheck'
    # 仍可手动执行（返回 rc=0：这是成功的不适用判定，不是错误）。
    cr = pre.get("content_repo") or {}
    if cr.get("detected"):
        _print_step(
            "content-repo",
            {
                "conclusion": "code analysis not applicable - content repository",
                "content_share_pct": cr.get("content_share_pct"),
                "content_files": cr.get("content_files"),
                "total_files": cr.get("total_files"),
                "source_files": cr.get("source_files"),
                "skipped": "S1 cbm index, S1.5 etl, S1 xcheck",
                "override": "run 'vrp index' + 'vrp etl' + 'vrp xcheck' manually to force",
            },
        )
        _print_text("== done == content repository - code analysis not applicable (rc=0)")
        return 0

    t1 = time.monotonic()
    idx = s1_cbm.index_repository(repo, name, reindex=reindex)
    t_index = time.monotonic() - t1
    _print_step("S1 cbm index", idx)

    t2 = time.monotonic()
    etl_result = etl_mod.run_etl(repo, name)
    t_etl = time.monotonic() - t2
    _print_step("S1.5 etl", etl_result)

    t3 = time.monotonic()
    xc = xcheck.run_xcheck(repo, repo / ".vrp" / "facts.db", reindex=False)
    t_xcheck = time.monotonic() - t3
    _print_step("S1 xcheck", xc)

    stages = {
        "precheck": t_precheck,
        "cbm_index": t_index,
        "etl": t_etl,
        "xcheck": t_xcheck,
    }
    stages["total"] = sum(stages.values())
    perf_mod.record_run(
        repo,
        stages,
        git_head=(pre.get("git") or {}).get("head"),
        meta={"reindex": bool(reindex)},
    )
    _print_step("facts.db", {"facts_db": str(repo / ".vrp" / "facts.db")})
    # E 批 P1：run 完成总结——阶段数/总耗时 + findings 计数 + 下一步引导
    findings = _count_findings(repo)
    done = f"== done == {len(stages) - 1} stages in {stages['total']:.1f}s - {findings} findings in facts.db"
    if findings:
        _print_text(done)
        _print_text(
            f"next: 'vrp findings {repo}' to triage, 'vrp report {repo}' for the human report"
        )
    else:
        _print_text(done)
        _print_text(
            f"next: 'vrp s2 {repo}' to run pathology detection, 'vrp analyze {repo}' for AI analysis"
        )
    return 0


def cmd_run(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.cbm_name or repo.name
    # I7：watch 模式——初始全跑建基线后轮询 mtime 防抖重跑（前台循环，
    # Ctrl+C 退出；J8 增量链让重跑廉价）。--reindex 同样作用于每次重跑。
    if getattr(args, "watch", False):
        return watch_mod.run_watch(
            repo,
            interval=args.interval,
            debounce=args.debounce,
            run_once=lambda: _run_pipeline(repo, name, reindex=args.reindex),
        )
    return _run_pipeline(repo, name, reindex=args.reindex)


def cmd_perf(args) -> int:
    repo = _repo_arg(args.repo)
    result = perf_mod.run_perf(repo)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(perf_mod.render_text(result))
    return 0


def cmd_relnotes(args) -> int:
    repo = _repo_arg(args.repo)
    result = relnotes_mod.build_notes(
        repo,
        args.from_ref,
        to_ref=args.to,
        version=args.version,
    )
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(result["markdown"])
    return 0


def cmd_smoke(args) -> int:
    result = smoke_mod.run_smoke(
        getattr(args, "root", None),
        only=list(args.only) if args.only else None,
        skip_run=args.skip_run,
    )
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(smoke_mod.render_text(result))
    return smoke_mod.smoke_rc(result)


def cmd_gc(args) -> int:
    result = gc_mod.run_gc(
        getattr(args, "cache_dir", None),
        yes=args.yes,
        dry_run=args.dry_run,
        repo=getattr(args, "repo", None),
    )
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(gc_mod.render_text(result))
    return gc_mod.gc_rc(result)


def cmd_flakes(args) -> int:
    result = {"entries": flakes_mod.registry()}
    rc = flakes_mod.flakes_rc()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(flakes_mod.render_text())
    return rc


def cmd_langmatrix(args) -> int:
    result = {"rows": langmatrix_mod.matrix(), "summary": langmatrix_mod.face_summary()}
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(langmatrix_mod.render_text())
    return langmatrix_mod.matrix_rc()


def cmd_mdcode(args) -> int:
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        result = mdcode_mod.run_mdcode(con, repo)
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(mdcode_mod.render_text(result))
    return 0


def cmd_argcheck(args) -> int:
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        calls_result = argcheck_mod.run_argcheck(con)
        http_result = argcheck_mod.run_http_check(con)
    finally:
        con.close()
    result = {"calls": calls_result, "http": http_result}
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(
            argcheck_mod.render_text(calls_result)
            + "\n\n"
            + argcheck_mod.render_http_text(http_result)
        )
    return 0


def cmd_secrets(args) -> int:
    result = secrets_mod.run_secrets(Path(args.repo), history=args.history)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(secrets_mod.render_text(result))
    return secrets_mod.secrets_rc(result)


def _s2_workers() -> int:
    """J9 D3：VRP_S2_WORKERS 逃生门——未设/非整数=0（自动=引擎数 4）；
    1=串行回退（诡异环境/复现调试；VRP_ 命名族同 VRP_CHILD_PRIORITY）。"""
    raw = os.environ.get("VRP_S2_WORKERS", "")
    try:
        return int(raw)
    except ValueError:
        return 0


def cmd_s2(args) -> int:
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")

    # V17 (#205): deadcode 引擎 SQL 引用 resolution_label——pre-v2 库先经
    # rw ensure_schema 就地迁移（cmd_dossier 同模式前置；不带 M4 freshness
    # 检查，s2 语义面不变）。幂等：v2 库重跑零 ALTER。
    migrate_con = sqlite3.connect(facts_path, timeout=30)
    try:
        etl_mod.ensure_schema(migrate_con)
    finally:
        migrate_con.close()

    # J9 引擎级四并发（设计稿-J9 D1/D2）：四引擎读写面零交叉（各 findings
    # source+各 ledger category），每引擎线程自建连接（timeout=30 显式
    # busy——实际 commit 窗口毫秒级，30s 是病态余量）；引擎序固定
    # astgrep→deadcode→p4→jscpd，结果同序装配，输出与串行恒等（D6）。
    def _task(scan):
        def _run():
            con = sqlite3.connect(facts_path, timeout=30)
            try:
                return scan(con)
            finally:
                con.close()

        return _run

    thunks = [
        _task(lambda con: s2_astgrep.scan(con, repo)),
        _task(lambda con: s2_deadcode.scan(con)),
        _task(lambda con: s2_p4.scan(con, repo)),
        _task(lambda con: s2_jscpd.scan(con, repo)),
    ]
    result, dead, p4, p2 = procs.run_thunks(thunks, _s2_workers() or None)
    result["dead_code_candidates"] = dead["total"]
    result["dead_code_by_confidence"] = dead["by_confidence"]
    result["p4"] = p4
    result["p2"] = p2
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_s2(result))
    return 0


def cmd_mutate_plan(args) -> int:
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        targets = s2_mutate.mutation_targets(con, args.budget)
    finally:
        con.close()
    out = s2_mutate.write_plan(repo, targets)
    result = {"budget": args.budget, "selected": len(targets), "plan": str(out)}
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_mutate_plan(result))
    return 0


def cmd_mutate_run(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        result = s2_mutate_run.run(repo, con, args.budget, args.timeout)
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_mutate_run(result))
    return 0


def _open_facts_rw(repo: Path):
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = sqlite3.connect(facts_path)
    try:
        etl_mod.ensure_schema(con)
        etl_mod.check_facts_freshness(con, repo)
    except (Exception, KeyboardInterrupt):
        # Ctrl-C 同样走 con.close()（KI 非 Exception 子类，只捕 Exception
        # 会绕过清理泄漏句柄——Windows 上锁住 facts.db）。
        con.close()
        raise
    return con, facts_path


def cmd_enforce(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        result = s7_enforce.run(repo, con, args.timeout)
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_enforce(result))
    return 0


def cmd_validate(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        anchors = s3.validate_anchors(con, repo)
        cov = s3.coverage(con)
    finally:
        con.close()
    result = {"anchors": anchors, "coverage": {"total": cov["total"], "annotated": cov["annotated"]}}
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_validate(result))
    return 0


def cmd_gate(args) -> int:
    repo = _repo_arg(args.repo)
    claims_path = Path(args.claims)
    if not claims_path.is_file():
        raise SystemExit(
            f"claims file not found: {claims_path} - expected a JSON array of"
            " S3 evidence-gate claims (AI-produced, quote-or-rejected);"
            " transaction artifacts live under .vrp/transactions/"
        )
    claims = json.loads(claims_path.read_text(encoding="utf-8"))
    if not isinstance(claims, list):
        raise SystemExit("claims file must be a JSON array")
    con, _ = _open_facts_rw(repo)
    try:
        result = s3.gate_claims(con, repo, claims)
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_gate(result))
    return 0


def cmd_freeze(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.cbm_name or repo.name
    facts_db = repo / ".vrp" / "facts.db"
    facts_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(facts_db)
    try:
        etl_mod.ensure_schema(con)
    finally:
        con.close()
    summary = s4_freeze.freeze(
        repo,
        facts_db,
        name,
        timeout=args.timeout,
        skip_commands=args.skip_commands,
        skip_graph=args.skip_graph,
    )
    if getattr(args, "json", False):
        _print_step("S4 freeze", summary)
    else:
        _print_text(_face_freeze(summary))
    return 0


def cmd_dossier(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.cbm_name or repo.name
    facts_db = repo / ".vrp" / "facts.db"
    if not facts_db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_db}")
    con, _ = _open_facts_rw(repo)
    con.close()
    dossier = s5_dossier.build_dossier(
        repo,
        facts_db,
        name,
        budget=args.budget,
        task=args.task,
        skip_external=args.skip_external,
    )
    summary = {k: dossier[k] for k in ("generated_at", "project", "budget", "task", "out")}
    if getattr(args, "json", False):
        _print_step("S5 dossier", summary)
    else:
        _print_text(_face_dossier(summary))
    return 0


def cmd_auto_curate(args) -> int:
    repo = _repo_arg(args.repo)
    try:
        summary = auto_curate.run_auto_curate(
            repo,
            task=args.task,
            budget=args.budget,
            model_override=args.model,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))
    _print_step(
        "auto-curate",
        {k: summary[k] for k in ("run_id", "task", "model", "slices", "structure", "enforce", "rc")},
    )
    return summary["rc"]


def cmd_fixpropose(args) -> int:
    repo = _repo_arg(args.repo)
    try:
        summary = fixpropose_mod.run_fixpropose(
            repo,
            args.finding_id,
            model_override=args.model,
            force=args.force,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))
    _print_step(
        "fixpropose",
        {k: summary[k] for k in ("run_id", "finding_id", "model", "status", "attempts", "proposal", "rc")},
    )
    return summary["rc"]


def cmd_analyze(args) -> int:
    repo = _repo_arg(args.repo)
    summary = analyze_mod.run_analyze(repo, with_timeline=args.with_timeline)
    if args.json:
        _print_json(
            {
                "run_id": summary["run_id"],
                "model": summary["model"],
                "material_bytes": summary["material_bytes"],
                "material_chars": summary["material_chars"],
                "truncated_chars": summary["truncated_chars"],
                "findings_total": summary["findings_total"],
                "attempts": summary["attempts"],
                "report_path": summary["report_path"],
                "audit_dir": summary["audit_dir"],
                "rc": summary["rc"],
            }
        )
    else:
        _print_text(summary["report"])
    return summary["rc"]


def cmd_chat(args) -> int:
    repo = _repo_arg(args.repo)
    summary = chat_mod.run_chat(repo, log=args.log)
    return summary["rc"]


def cmd_transact(args) -> int:
    repo = _repo_arg(args.repo)
    if args.preview:
        preview = s6_transact.preview_transaction(repo, Path(args.transaction))
        _print_step(f"S6 transact preview {preview['type']}", preview)
        return 0 if preview["ok"] else 1
    name = args.cbm_name or repo.name
    facts_db = repo / ".vrp" / "facts.db"
    if not facts_db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_db}")
    con, _ = _open_facts_rw(repo)
    con.close()
    if not s6_transact.git_clean(repo):
        raise SystemExit(f"working tree not clean - commit or stash first: {repo}")
    verdict = s6_transact.transact(
        repo,
        Path(args.transaction),
        name,
        confirm=args.confirm,
        timeout=args.timeout,
    )
    summary = {
        "tid": verdict["tid"],
        "status": verdict["status"],
        "failed_gate": verdict["failed_gate"],
        "commit": verdict.get("commit"),
        "confidence": verdict.get("confidence"),
    }
    _print_step(f"S6 transact {verdict['type']}", summary)
    return 0 if verdict["status"] == "committed" else 1


def cmd_mcp(args) -> int:
    repo = _repo_arg(args.repo)
    from .mcp_server import run_stdio

    run_stdio(repo)
    return 0


def cmd_verdicts(args) -> int:
    repo = _repo_arg(args.repo)
    verdicts = s6_transact.load_verdicts(repo)
    if args.tid:
        match = [v for v in verdicts if v.get("tid") == args.tid]
        if not match:
            raise SystemExit(f"no verdict found for tid: {args.tid}")
        _print_json(match)
        return 0
    summary = [
        {k: v.get(k) for k in ("tid", "type", "status", "failed_gate", "confidence", "verdict_at")}
        for v in verdicts
    ]
    payload = {"count": len(summary), "verdicts": summary}
    if getattr(args, "json", False):
        _print_step("S6 verdicts", payload)
    else:
        _print_text(_face_verdicts(payload))
    return 0


def cmd_transact_batch(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.cbm_name or repo.name
    facts_db = repo / ".vrp" / "facts.db"
    if not facts_db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_db}")
    con, _ = _open_facts_rw(repo)
    con.close()
    if not s6_transact.git_clean(repo):
        raise SystemExit(f"working tree not clean - commit or stash first: {repo}")
    tx_dir = Path(args.tx_dir)
    if not tx_dir.is_dir():
        raise SystemExit(f"transaction directory not found: {tx_dir}")
    result = s6_transact.transact_batch(
        repo,
        tx_dir,
        name,
        confirm=args.confirm,
        timeout=args.timeout,
        continue_on_fail=args.continue_on_fail,
    )
    summary = {k: result[k] for k in ("total", "count", "committed", "statuses", "stopped_at")}
    _print_step("S6 transact-batch", summary)
    all_committed = result["count"] == result["total"] and result["committed"] == result["count"] and result["total"] > 0
    return 0 if all_committed else 1


def cmd_curate(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        if args.reject:
            target = args.reject
            if target == "structure":
                result = curate_mod.reject(con, "structure", "structure", args.note or "")
            else:
                result = curate_mod.reject(con, "slice", target, args.note or "")
        else:
            result = curate_mod.curate(repo, con)
    finally:
        con.close()
    _print_step("S2.5 curate", result)
    if args.reject:
        return 0
    return 1 if any(f.get("status") == "rejected" for f in result.get("files", [])) else 0


def cmd_curate_merge(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        result = curate_merge_mod.run_curate_merge(repo, con, Path(args.theirs))
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(curate_merge_mod.render_text(result))
    # rc=1 = 有冲突待人工裁决（零静默丢信号，CI 可拦）
    return 1 if result["slices"]["conflict"] or result["structure"]["action"] == "conflict" else 0


def cmd_curate_refresh(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        result = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_curate_refresh(result))
    # rc=1 = structure 刷新失败（failed）或零成果（无任何 refreshed/fresh 工件
    # ——slices-only 仓的 structure missing 仍可 rc=0）；slice 部分跳过
    # （unremappable）属诚实部分成果，收据可查，rc 不拦
    action = (result.get("structure") or {}).get("action")
    did_work = action in ("refreshed", "fresh") or any(
        s.get("action") in ("refreshed", "fresh") for s in result.get("slices", [])
    )
    return 0 if did_work and action != "failed" else 1


def cmd_drift(args) -> int:
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    history_limit = max(0, getattr(args, "history", 0) or 0)
    con = dbpath.ro_connect(facts_path)
    try:
        result = curate_mod.drift_report(con)
        history = curate_mod.drift_history(con, history_limit) if history_limit else None
    finally:
        con.close()
    _print_step("S2.5 drift report", result)
    if history is not None:
        _print_step("S2.5 drift history", history)
    # rc=1 = stored active 实际已漂移（假 active——F2 复验链有洞的对账信号）
    return 1 if result["summary"]["active_but_drifted"] else 0


def cmd_seams(args) -> int:
    repo = _repo_arg(args.repo)
    result = nav_mod.nav_seams(repo)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_seams(result))
    return 0


def cmd_propose(args) -> int:
    repo = _repo_arg(args.repo)
    result = propose_mod.propose_structure(
        repo,
        out=Path(args.out) if args.out else None,
        target_modules=args.target_modules,
        balance_cap=args.balance_cap,
    )
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_propose(result))
    return 0 if result.get("ok") else 1


def cmd_traces(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        result = traces_mod.ingest_traces(con, Path(args.file), label=args.label)
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_traces(result))
    return 0 if result.get("ok") else 1


def cmd_testcov(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        result = testcov_mod.ingest_testcov(con, Path(args.file), label=args.label)
    finally:
        con.close()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_testcov(result))
    return 0 if result.get("ok") else 1


def cmd_golden(args) -> int:
    repo = _repo_arg(args.repo)
    result = golden_mod.run_audit(repo, Path(args.file))
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_golden(result))
    return 0


def cmd_coverage(args) -> int:
    repo = _repo_arg(args.repo)
    result = coverage_mod.check_coverage(repo, paths=args.paths, kinds=args.kinds)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_coverage(result))
    return 0


def cmd_impact(args) -> int:
    repo = _repo_arg(args.repo)
    result = impact_mod.impact_report(
        repo,
        base=args.base,
        paths=args.paths,
        depth=args.depth,
        direction=args.direction,
        tests=args.tests,
    )
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_text(_face_impact(result))
    return 0


def cmd_remap(args) -> int:
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        report = curate_mod.drift_report(con)
    finally:
        con.close()
    result = curate_mod.apply_remap_to_ai(repo, report)
    _print_step("S2.5 remap", result)
    # rc=1 = 存在无法唯一重定位的 missing fid（需 AI/人工判读）
    return 1 if result["unresolved_total"] else 0


def cmd_refresh(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.cbm_name or repo.name

    pre = s0_precheck.precheck(repo)
    _print_step("S0 precheck", {"name": pre["name"], "repo": pre["repo"], "commands": pre["commands"]})

    idx = s1_cbm.index_repository(repo, name, reindex=args.reindex)
    _print_step("S1 cbm index", idx)

    etl_result = etl_mod.run_etl(repo, name)
    _print_step("S1.5 etl", etl_result)

    xc = xcheck.run_xcheck(repo, repo / ".vrp" / "facts.db", reindex=False)
    _print_step("S1 xcheck", xc)

    facts_path = repo / ".vrp" / "facts.db"
    con = dbpath.ro_connect(facts_path)
    try:
        drift = curate_mod.drift_report(con)
    finally:
        con.close()
    _print_step("S2.5 drift", drift["summary"])

    remap_result = curate_mod.apply_remap_to_ai(repo, drift)
    _print_step("S2.5 remap", remap_result)

    con, _ = _open_facts_rw(repo)
    try:
        receipt = curate_mod.curate(repo, con)
    finally:
        con.close()
    _print_step("S2.5 curate", receipt)

    from . import render as render_mod

    report_result = render_mod.render_report(repo, Path(args.out) if args.out else None)
    _print_step("S8 report", report_result)

    rejected = any(f.get("status") == "rejected" for f in receipt.get("files", []))
    return 1 if (remap_result["unresolved_total"] or rejected) else 0


def cmd_report(args) -> int:
    from . import render as render_mod

    repo = _repo_arg(args.repo)
    out = Path(args.out) if args.out else None
    result = render_mod.render_report(repo, out)
    if getattr(args, "json", False):
        _print_step("S8 report", result)
    else:
        _print_text(_face_report(result))
    return 0


def cmd_session(args) -> int:
    repo = _repo_arg(args.repo)
    if args.action == "begin":
        result = session_mod.session_begin(repo, args.note or "")
        title = "R3.5' session begin"
    elif args.action == "end":
        result = session_mod.session_end(repo, args.note or "")
        title = "R3.5' session end"
    else:
        out = Path(args.out) if args.out else None
        result = session_mod.session_report(repo, args.id or "", out)
        title = "R3.5' session report"
    _print_step(title, result)
    return 0


def cmd_status(args) -> int:
    repo = _repo_arg(args.repo)
    facts_db = repo / ".vrp" / "facts.db"
    if not facts_db.exists():
        payload = {"facts_db": None}
        if getattr(args, "json", False):
            _print_json(payload)
        else:
            _print_text(_face_status(payload, repo.name))
        return 0
    con = dbpath.ro_connect(facts_db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
        counts = {}
        for table in ("symbols", "edges", "usage_hints", "history_coupling", "test_linkage", "coverage_gaps", "findings", "discrepancy_ledger", "file_hashes"):
            counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        by_evidence = dict(con.execute("SELECT evidence, COUNT(*) FROM edges GROUP BY evidence"))
    finally:
        con.close()
    from . import render as render_mod

    facts_head = str(meta.get("git_head", "") or "")
    staleness = render_mod._staleness(repo, facts_head)
    if not facts_head:
        state = "unknown"
    elif staleness is None:
        state = "fresh"
    else:
        state = "stale"
    freshness = {"state": state}
    if staleness is not None:
        freshness.update(staleness)
    # #144 A 件：CBM 版本软提示（非硬闸——CBM 版本变 facts 未必错，钉值复验才知道；
    # M4 git_head 是硬闸因代码变 facts 必错）。meta 无记录（pre-#144 库）→ unknown 诚实。
    recorded_cbm = meta.get("cbm_version", "") or ""
    current_cbm = s1_cbm.cbm_version() or ""
    if not recorded_cbm or not current_cbm:
        cbm_stale = None  # exe 缺失或无记录——不可判
    else:
        cbm_stale = recorded_cbm != current_cbm
    payload = {
        "meta": meta,
        "counts": counts,
        "edges_by_evidence": by_evidence,
        "freshness": freshness,
        "cbm_version": {
            "recorded": recorded_cbm or None,
            "current": current_cbm or None,
            "stale": cbm_stale,
            "note": "soft hint (not a gate) - a changed CBM version does not imply wrong facts; "
            "re-run etl to refresh provenance, pinned-count tests are the real tripwire"
            if cbm_stale is not None
            else "unknown (missing exe or no recorded version)",
        },
    }
    if getattr(args, "json", False):
        _print_json(payload)
    else:
        _print_text(_face_status(payload, repo.name))
    return 0


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _print_json(payload: dict) -> None:
    print(_dumps(payload))


def _print_text(text: str) -> None:
    # T10 assess 人读面：非 JSON 输出的统一 stdout 出口（print-surface 纪律
    # 同 _print_json/_print_step——test_cli_census 守卫 helper 集合含本函数）
    print(text)


def _print_step(title: str, payload: dict) -> None:
    print(f"== {title} ==")
    _print_json(payload)


# ---------- E 批：JSON 墙命令默认英文人读面（--json = 旧机读字节不变） ----------
# 形态基准：assess/feedback render_text（层进缩进、` - ` 分隔、空结果诚实
# 一行、长列表截断+计数）；面函数纯返回字符串，stdout 一律经 _print_text。

_LIST_CAP = 12


def _face_precheck(result: dict) -> str:
    lines = [f"vrp precheck - {result.get('name', '?')}"]
    lines.append(f"  repo        : {result.get('repo', '?')}")
    git = result.get("git") or {}
    if git.get("is_repo"):
        head = git.get("head") or "?"
        lines.append(
            f"  git         : {head[:12]} branch={git.get('branch')}"
            f" dirty={git.get('dirty_count', 0)}"
        )
    else:
        lines.append("  git         : not a git repository")
    cmds = result.get("commands") or {}
    slot = " / ".join(f"{k}={v}" for k, v in cmds.items()) or "(none discovered)"
    lines.append(f"  commands    : {slot}")
    census = result.get("languages") or {}
    langs = "; ".join(
        f"{l.get('name')} {l.get('files')} files/{l.get('lines'):,} lines"
        for l in census.get("languages", [])[:5]
    )
    lines.append(f"  census      : via {census.get('tool', '?')} - {langs or '(none)'}")
    cr = result.get("content_repo") or {}
    if cr.get("detected"):
        lines.append(
            f"  content-repo: detected ({cr.get('content_share_pct')}% content files,"
            f" {cr.get('source_files')} source files) - code analysis not applicable"
        )
    return "\n".join(lines)


def _face_index(result: dict) -> str:
    lines = ["vrp index - CBM repository index"]
    lines.append(f"  status      : {result.get('status', '?')}")
    lines.append(f"  project     : {result.get('project', '?')}")
    lines.append(f"  db          : {result.get('db', '?')}")
    lines.append(f"  nodes       : {result.get('nodes', 0):,}   edges: {result.get('edges', 0):,}")
    partial = result.get("parse_partial_count") or 0
    unusable = result.get("parse_unusable_count") or 0
    not_indexed = result.get("not_indexed_files_count") or 0
    lines.append(
        f"  gaps        : {not_indexed} files not indexed,"
        f" {partial} parse-partial, {unusable} parse-unusable"
    )
    if result.get("rebuild_reason"):
        lines.append(f"  note        : {result.get('note') or result['rebuild_reason']}")
    return "\n".join(lines)


def _face_etl(stats: dict) -> str:
    lines = ["vrp etl - CBM index -> facts.db"]
    lines.append(f"  mode        : {stats.get('mode', '?')}")
    lines.append(
        f"  symbols     : {stats.get('symbols', 0):,}   edges: {stats.get('edges', 0):,}"
        f"   usage hints: {stats.get('usage_hints', 0):,}"
    )
    lines.append(
        f"  test linkage: cbm {stats.get('test_linkage_cbm', 0):,}"
        f" / js-ts {stats.get('test_linkage_js_ts', 0):,}"
        f" / rust {stats.get('test_linkage_rust', 0):,}"
    )
    lines.append(
        f"  findings p2 : {stats.get('findings_p2', 0):,}"
        f"   history coupling: {stats.get('history_coupling', 0):,}"
    )
    lines.append(f"  facts.db    : {stats.get('facts_db', '?')}")
    t = stats.get("t_total_s")
    if t is not None:
        lines.append(f"  time        : {t}s total")
    if stats.get("incremental_fallback"):
        lines.append(f"  fallback    : {stats['incremental_fallback']}")
    return "\n".join(lines)


def _face_xcheck(result: dict) -> str:
    lines = ["vrp xcheck - code-atlas cross-validation"]
    # index 早退路径是状态字符串（"skipped"），全量路径是 dict（ca index 结果）
    idx = result.get("index", "?")
    if isinstance(idx, dict):
        idx = idx.get("status", "?")
    lines.append(f"  ca index    : {idx}")
    if result.get("reason"):
        lines.append(f"  skipped     : {result['reason']}")
        lines.append(f"  ledger rows : {result.get('ledger_rows', 0)}")
        return "\n".join(lines)
    pct = result.get("agreement_pct")
    pct_s = f"{pct}%" if pct is not None else "n/a"
    lines.append(
        f"  calls       : code-atlas {result.get('ca_calls', 0):,}"
        f" vs cbm {result.get('cbm_calls', 0):,}   agreement: {pct_s}"
    )
    lines.append(
        f"  ledger      : {result.get('ledger_rows', 0):,} rows"
        f" (ca_only {result.get('ca_only', 0)}, cbm_only {result.get('cbm_only', 0)},"
        f" cbm_only_test_source {result.get('cbm_only_test_source', 0)})"
    )
    return "\n".join(lines)


def _face_s2(result: dict) -> str:
    lines = ["vrp s2 - pathology detection (four engines)"]
    lines.append(
        f"  ast-grep    : {result.get('total', 0)} rule hits"
        f" (anchored {result.get('anchored', 0)}, rejected {result.get('rejected', 0)})"
    )
    by_conf = result.get("dead_code_by_confidence") or {}
    conf = ", ".join(f"{k} {v}" for k, v in sorted(by_conf.items())) or "none"
    lines.append(
        f"  dead code   : {result.get('dead_code_candidates', 0)} candidates ({conf})"
    )
    p4 = result.get("p4") or {}
    lines.append(
        f"  p4 (knip)   : {p4.get('knip', 'skipped')}"
        f"   p4 (vulture): {p4.get('vulture', 'skipped')}"
        f"   p4 ledger: {p4.get('ledger_rows', 0)}"
    )
    p2 = result.get("p2") or {}
    lines.append(
        f"  p2 (jscpd)  : {p2.get('jscpd', 'skipped')}"
        f"   p2 ledger: {p2.get('ledger_rows', 0)}"
    )
    lines.append("  next        : 'vrp findings <repo>' to triage what was written")
    return "\n".join(lines)


def _face_mutate_plan(result: dict) -> str:
    lines = ["vrp mutate-plan - mutation budget sampler"]
    lines.append(f"  budget      : {result.get('budget', '?')}")
    lines.append(f"  selected    : {result.get('selected', 0)} targets (test_linkage pre-filter)")
    lines.append(f"  plan        : {result.get('plan', '?')}")
    lines.append("  next        : 'vrp mutate-run <repo>' to execute the plan")
    return "\n".join(lines)


def _face_mutate_run(result: dict) -> str:
    status = result.get("status", "?")
    lines = ["vrp mutate-run - mutation execution + reconciliation"]
    lines.append(f"  status      : {status}")
    if status == "no-targets":
        lines.append("  no mutation targets - test_linkage is empty (run 'vrp run' first, or link tests)")
        return "\n".join(lines)
    if status != "ok":
        engines = result.get("engines") or {}
        if engines:
            lines.append("  engines     : " + ", ".join(f"{k} {v}" for k, v in engines.items()))
        if result.get("stderr_tail"):
            lines.append(f"  stderr tail : {result['stderr_tail']}")
        return "\n".join(lines)
    engines = result.get("engines") or {}
    lines.append("  engines     : " + (", ".join(f"{k} {v}" for k, v in engines.items()) or "(none)"))
    lines.append(
        f"  mutants     : {result.get('mutants_total', 0)} over {result.get('targets', 0)} targets"
        f" - killed {result.get('killed', 0)}, survived {result.get('survived', 0)},"
        f" timeout {result.get('timeout', 0)}"
    )
    rate = result.get("survival_rate")
    if rate is not None:
        lines.append(f"  survival    : {round(100 * rate, 1)}% (surviving targets: {result.get('surviving_targets', 0)})")
    if result.get("findings") is not None:
        lines.append(f"  findings    : {result['findings']} P8 findings written")
    if result.get("report"):
        lines.append(f"  report      : {result['report']}")
    return "\n".join(lines)


def _face_enforce(result: dict) -> str:
    status = result.get("status", "?")
    lines = ["vrp enforce - S7 architecture enforcement"]
    lines.append(f"  status      : {status}")
    if result.get("reason"):
        lines.append(f"  reason      : {result['reason']}")
    languages = result.get("languages") or {}
    if languages:
        for lang, r in languages.items():
            lang_status = r.get("status", "?")
            extra = f" - {r['reason']}" if r.get("reason") else ""
            lines.append(f"  {lang:11}: {lang_status}{extra}")
    if status == "ok" and not languages:
        if result.get("enforcer"):
            lines.append(f"  enforcer    : {result['enforcer']}")
        if result.get("kept") is not None:
            lines.append(f"  contracts   : {result.get('kept')} kept, {result.get('broken')} broken")
    if "violations" in result:
        lines.append(
            f"  violations  : {result.get('violations', 0)}"
            f" (findings written {result.get('findings_written', 0)}, anchored {result.get('anchored', 0)})"
        )
    return "\n".join(lines)


def _face_validate(result: dict) -> str:
    anchors = result.get("anchors") or {}
    counts = anchors.get("counts") or {}
    lines = ["vrp validate - anchor revalidation + coverage"]
    lines.append(
        f"  anchors     : {counts.get('active', 0)} active /"
        f" {counts.get('cosmetic', 0)} cosmetic / {counts.get('stale', 0)} stale"
    )
    reasons = anchors.get("stale_reasons") or {}
    if reasons:
        lines.append(
            "  stale why   : " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items()))
        )
    cov = result.get("coverage") or {}
    lines.append(
        f"  coverage    : {cov.get('annotated', 0)}/{cov.get('total', 0)} annotated"
    )
    return "\n".join(lines)


def _face_gate(result: dict) -> str:
    lines = ["vrp gate - AI claim evidence gate"]
    lines.append(
        f"  claims      : {result.get('accepted', 0)} accepted /"
        f" {len(result.get('rejected') or [])} rejected"
    )
    for r in (result.get("rejected") or [])[:_LIST_CAP]:
        lines.append(f"    claim #{r.get('index')} rejected - {r.get('reason')}")
    rejected = result.get("rejected") or []
    if len(rejected) > _LIST_CAP:
        lines.append(f"    ... {len(rejected) - _LIST_CAP} more")
    return "\n".join(lines)


def _face_freeze(summary: dict) -> str:
    git = summary.get("git") or {}
    lines = ["vrp freeze - baseline commands + graph snapshot"]
    head = git.get("commit") or "?"
    lines.append(f"  git         : {head[:12]} branch={git.get('branch') or '?'}")
    commands = summary.get("commands") or {}
    if commands:
        lines.append("  commands    :")
        for slot, runs in commands.items():
            for r in runs:
                lines.append(f"    {slot:10} {r}")
    else:
        lines.append("  commands    : (none run)")
    gb = summary.get("graph_baseline")
    if gb:
        lines.append(
            f"  graph       : {gb.get('status')} ({gb.get('bytes')} bytes) {gb.get('artifact') or ''}".rstrip()
        )
    else:
        lines.append("  graph       : not captured (skipped or failed)")
    if summary.get("test_impact_truncated"):
        lines.append("  note        : test impact report was truncated")
    return "\n".join(lines)


def _face_dossier(summary: dict) -> str:
    lines = ["vrp dossier - five-view briefing assembled"]
    lines.append(f"  project     : {summary.get('project', '?')}")
    lines.append(
        f"  task        : {summary.get('task', '?')}"
        f" (char budget {summary.get('budget', '?')})"
    )
    lines.append(f"  out         : {summary.get('out', '?')}")
    lines.append("  next        : feed it to an LLM, or run 'vrp analyze <repo>' / 'vrp chat'")
    return "\n".join(lines)


def _face_verdicts(payload: dict) -> str:
    verdicts = payload.get("verdicts") or []
    lines = ["vrp verdicts - transaction verdict history"]
    lines.append(f"  count       : {payload.get('count', len(verdicts))}")
    if not verdicts:
        lines.append("  (no verdicts yet - execute a transaction with 'vrp transact')")
    for v in verdicts:
        gate = f" ({v.get('failed_gate')})" if v.get("failed_gate") else ""
        conf = f"  confidence {v['confidence']}" if v.get("confidence") is not None else ""
        when = f"  {v.get('verdict_at')}" if v.get("verdict_at") else ""
        lines.append(f"  {v.get('tid')}  {v.get('type')}  {v.get('status')}{gate}{conf}{when}")
    if verdicts:
        lines.append("  full JSON   : 'vrp verdicts <repo> --tid <id>'")
    return "\n".join(lines)


def _face_curate_refresh(receipt: dict) -> str:
    lines = ["vrp curate-refresh - fid remap in-place refresh"]
    st = receipt.get("structure") or {}
    action = st.get("action") or "?"
    if action == "missing":
        lines.append("  structure   : missing (no active/stale structure row)")
    else:
        bits = [f"action={action}"]
        if "remapped" in st:
            bits.append(f"remapped={st['remapped']}")
        if "dropped" in st:
            bits.append(f"dropped={st['dropped']}")
        for e in st.get("errors") or []:
            bits.append(f"error={e}")
        lines.append(f"  structure   : {', '.join(bits)}")
    slices = receipt.get("slices") or []
    if slices:
        lines.append(f"  slices      : {len(slices)}")
        for s in slices[:_LIST_CAP]:
            reason = f" - {s['reason']}" if s.get("reason") else ""
            lines.append(f"    {s.get('slice_id')}  {s.get('action')}{reason}")
        if len(slices) > _LIST_CAP:
            lines.append(f"    ... {len(slices) - _LIST_CAP} more")
    else:
        lines.append("  slices      : (none)")
    lines.append("  receipt     : .vrp/curate-refresh-receipt.json")
    return "\n".join(lines)


def _face_seams(result: dict) -> str:
    if result.get("structure") == "missing":
        return "vrp seams - " + (result.get("note") or "no ingested structure")
    seams = result.get("seams") or []
    modules = result.get("modules") or []
    lines = ["vrp seams - cross-module edges over the active structure"]
    lines.append(f"  modules     : {len(modules)}")
    lines.append(f"  seams       : {len(seams)} module pairs")
    lines.append(f"  internal    : {result.get('internal_edges', 0):,} edges inside modules")
    lines.append(f"  unassigned  : {result.get('unassigned_edges', 0):,} edges outside any module")
    for s in seams[:_LIST_CAP]:
        by = " ".join(f"{k}:{v}" for k, v in (s.get("by_type") or {}).items())
        lines.append(
            f"    {s.get('from')} -> {s.get('to')} : {s.get('edges')} edges"
            + (f" ({by})" if by else "")
        )
    if len(seams) > _LIST_CAP:
        lines.append(f"    ... {len(seams) - _LIST_CAP} more pairs")
    return "\n".join(lines)


def _face_propose(result: dict) -> str:
    lines = ["vrp propose - deterministic structure partition proposal"]
    if not result.get("ok"):
        lines.append(f"  reason      : {result.get('reason', '?')}")
        if result.get("note"):
            lines.append(f"  note        : {result['note']}")
        return "\n".join(lines)
    lines.append(f"  proposal    : {result.get('proposal')}")
    lines.append(
        f"  candidates  : {result.get('candidates', 0)} symbols ->"
        f" {result.get('modules', 0)} modules (target {result.get('target_modules', '?')})"
    )
    sizes = result.get("module_sizes") or {}
    if sizes:
        lines.append("  module sizes:")
        for mid in sorted(sizes)[:_LIST_CAP]:
            lines.append(f"    {mid} : {sizes[mid]}")
        if len(sizes) > _LIST_CAP:
            lines.append(f"    ... {len(sizes) - _LIST_CAP} more")
    lines.append("  next        : review + rename, then place in .vrp/ai/ and run 'vrp curate'")
    return "\n".join(lines)


def _face_traces(result: dict) -> str:
    lines = ["vrp traces - runtime call evidence ingested"]
    lines.append(f"  label       : {result.get('label')}")
    lines.append(
        f"  traces      : {result.get('trace_items', 0)} ->"
        f" {result.get('distinct_pairs', 0)} distinct pairs"
        f" ({result.get('stored_pairs', 0)} stored,"
        f" {result.get('skipped_unresolved', 0)} unresolved skipped)"
    )
    a = result.get("agreement") or {}
    lines.append(
        f"  agreement   : agreed {a.get('agreed', 0)} /"
        f" observed-only {a.get('observed_only', 0)} /"
        f" static-only {a.get('static_only', 0)}"
        f" (static pairs {a.get('static_pairs_total', 0)})"
    )
    for s in (a.get("observed_only_sample") or [])[:5]:
        lines.append(f"    dynamic-only: {s}")
    unresolved = result.get("unresolved_sample") or []
    if unresolved:
        lines.append(f"  unresolved  : {', '.join(unresolved[:5])}")
    return "\n".join(lines)


def _face_testcov(result: dict) -> str:
    lines = ["vrp testcov - coverage report ingested"]
    lines.append(f"  label       : {result.get('label')}")
    lines.append(
        f"  coverage    : {result.get('files_in_run', 0)} files in run"
        f" ({result.get('files_matched', 0)} matched facts.db),"
        f" {result.get('lines_executed_total', 0):,} lines executed"
    )
    a = result.get("agreement") or {}
    lines.append(
        f"  linkage     : {a.get('linkage_targets', 0)} targets -"
        f" covered {a.get('linked_and_covered', 0)},"
        f" not-covered-measured {a.get('linked_not_covered_measured', 0)},"
        f" not-covered-unmeasured {a.get('linked_not_covered_unmeasured', 0)},"
        f" undetermined {a.get('linked_undetermined', 0)}"
    )
    lines.append(f"  covered-not-linked: {a.get('covered_not_linked', 0)}")
    unresolved = result.get("unresolved_files") or []
    if unresolved:
        lines.append(
            f"  files outside facts.db: {result.get('unresolved_files_total', 0)}"
            f" (e.g. {unresolved[0]})"
        )
    return "\n".join(lines)


def _face_golden(result: dict) -> str:
    lines = [f"vrp golden - {result.get('name', '?')} ({result.get('language', '?')})"]
    faces = result.get("faces") or {}
    if not faces:
        lines.append("  (no faces declared in the truth file)")
    for fname, face in faces.items():
        prec = face.get("precision")
        rec = face.get("recall")
        prec_s = f"{prec:.2f}" if isinstance(prec, (int, float)) else "?"
        rec_s = f"{rec:.2f}" if isinstance(rec, (int, float)) else "?"
        lines.append(
            f"  {fname:11}: matched {face.get('matched', 0)}/{face.get('truth', 0)} truth,"
            f" missing {face.get('missing', 0)}, spurious {face.get('spurious', 0)}"
            f" (precision {prec_s}, recall {rec_s})"
        )
    return "\n".join(lines)


def _face_coverage(result: dict) -> str:
    lines = ["vrp coverage - index completeness"]
    if result.get("mode") == "check":
        for p in result.get("paths") or []:
            lines.append(
                f"  {p.get('path')} : {p.get('status')} -> {p.get('recommended_action')}"
            )
            for m in (p.get("matched") or [])[:_LIST_CAP]:
                lines.append(f"    {m.get('match')} {m.get('kind')} {m.get('file_path')}")
        if not result.get("paths"):
            lines.append("  (no paths checked)")
        return "\n".join(lines)
    kinds = result.get("kinds") or {}
    lines.append(f"  gap rows    : {result.get('total_rows', 0)}")
    if not kinds:
        lines.append(
            "  (no coverage gaps recorded - clean means no recorded gap,"
            " not proof of completeness)"
        )
    for kind, info in sorted(kinds.items()):
        sample = (info.get("samples") or ["-"])[0]
        lines.append(f"    {kind:24} {info.get('count', 0):6}  e.g. {sample}")
    return "\n".join(lines)


def _face_impact(result: dict) -> str:
    lines = ["vrp impact - change blast radius"]
    lines.append(
        f"  base        : {result.get('base')}   direction: {result.get('direction')}"
        f"   depth: {result.get('depth')}"
    )
    changed = result.get("changed_files") or []
    lines.append(f"  changed     : {len(changed)} file(s)")
    for c in changed[:_LIST_CAP]:
        lines.append(f"    {c}")
    if len(changed) > _LIST_CAP:
        lines.append(f"    ... {len(changed) - _LIST_CAP} more")
    sat = " (saturated - totals are lower bounds)" if result.get("saturated") else ""
    lines.append(f"  impacted    : {result.get('impacted_total', 0)} symbols{sat}")
    tests = result.get("suggested_tests") or []
    lines.append(f"  tests       : {len(tests)} suggested (suggested is NOT sufficient)")
    return "\n".join(lines)


def _face_report(result: dict) -> str:
    lines = [f"Report generated: {result.get('report', '?')}"]
    artifacts = result.get("artifacts") or []
    if artifacts:
        lines.append("artifacts:")
        for a in artifacts:
            lines.append(f"  - {a}")
    degraded = result.get("degraded") or []
    if degraded:
        lines.append("degraded (honest notes):")
        for d in degraded:
            lines.append(f"  - {d.replace(': ', ' - ', 1)}")
    counts = result.get("counts") or {}
    if counts:
        summary = ", ".join(f"{k} {v}" for k, v in counts.items())
        lines.append(f"counts: {summary}")
    lines.append(f"open {result.get('report', '?')} in a browser")
    return "\n".join(lines)


def _face_status(payload: dict, repo_name: str) -> str:
    # 缺库哨兵：缺失分支 payload={"facts_db": None}；全量 payload 无 "facts_db"
    # 键——用 "counts" 在场与否判别（全量必有 counts，缺失分支必无）。
    if "counts" not in payload:
        return "\n".join([
            f"vrp status - {repo_name}",
            "  facts.db    : not found - run 'vrp run' first",
        ])
    lines = [f"vrp status - {repo_name}"]
    counts = payload.get("counts") or {}
    summary = ", ".join(f"{k} {v:,}" for k, v in counts.items())
    lines.append(f"  counts      : {summary or '(empty)'}")
    by = payload.get("edges_by_evidence") or {}
    if by:
        lines.append("  edges by    : " + ", ".join(f"{k} {v:,}" for k, v in by.items()))
    fresh = payload.get("freshness") or {}
    state = fresh.get("state", "?")
    if state == "stale":
        behind = fresh.get("behind")
        detail = f" ({behind} commits behind HEAD)" if behind is not None else ""
        lines.append(f"  freshness   : stale{detail} - re-run 'vrp run' to refresh")
    else:
        lines.append(f"  freshness   : {state}")
    cbm = payload.get("cbm_version") or {}
    rec = cbm.get("recorded") or "?"
    cur = cbm.get("current") or "?"
    flag = " (stale)" if cbm.get("stale") else ""
    lines.append(f"  cbm         : recorded {rec}, current {cur}{flag}")
    return "\n".join(lines)


def _count_findings(repo: Path) -> int:
    """run 完成总结的 findings 计数（只读；坏库诚实 0，不阻塞总结）。"""
    facts_db = repo / ".vrp" / "facts.db"
    if not facts_db.exists():
        return 0
    try:
        con = dbpath.ro_connect(facts_db)
    except (sqlite3.Error, OSError):
        return 0
    try:
        return con.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        con.close()


# ---------- CLI 体验改造：主 help 按场景分组渲染（纯 stdlib argparse） ----------

# 六组与 README.md §CLI 命令一致；组内序=文档序；mcp（AI 接口）挂管道主线尾。
_COMMAND_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    (
        "pipeline",
        ("run", "precheck", "index", "etl", "status", "etl-health", "xcheck",
         "xcheck-explain", "s2", "freeze", "dossier", "report", "session", "mcp"),
    ),
    (
        "curation & enforcement",
        ("curate", "curate-auto", "curate-merge", "curate-refresh", "drift", "remap",
         "refresh", "enforce", "seams", "propose", "scaffold", "migprog", "tour"),
    ),
    (
        "refactor transactions",
        ("transact", "transact-batch", "verdicts", "fixpropose", "validate", "gate"),
    ),
    (
        "pathology & analysis",
        ("assess", "graph", "findings", "score", "groups", "complexity", "cochange",
         "timeline", "layermetrics", "profile", "pitindex", "dossier-diff",
         "analyze", "chat"),
    ),
    (
        "evidence & audit",
        ("traces", "coverage", "impact", "testcov", "golden", "mdcode", "argcheck",
         "sweep", "review"),
    ),
    (
        "governance & engineering",
        ("doclint", "doctor", "explain", "perf", "relnotes", "smoke", "gc", "flakes",
         "langmatrix", "secrets", "mutate-plan", "mutate-run", "config"),
    ),
]

_MAIN_EPILOG = (
    "First time? vrp run <repo> -> vrp findings <repo> -> vrp report <repo>\n"
    "AI terminal analysis: vrp config to set up the LLM -> vrp analyze for reports / vrp chat for Q&A"
)

# E 批：原"JSON 墙"命令（stdout 纯 JSON 无人类面）——--json = 原样机读面，
# 缺省 = _face_* 英文人读面。curate-refresh/relnotes 历史上已有 --json，不在列。
_JSON_WALL_COMMANDS: tuple[str, ...] = (
    "precheck", "index", "etl", "xcheck", "s2", "mutate-plan", "mutate-run",
    "enforce", "validate", "gate", "freeze", "dossier", "curate-refresh",
    "verdicts", "seams", "propose", "traces", "testcov", "golden", "coverage",
    "impact", "report", "status",
)


class _VrpHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """主 help：子命令按 _COMMAND_GROUPS 六组渲染；description/epilog 保换行。"""

    def add_argument(self, action):
        if isinstance(action, argparse._SubParsersAction):
            self._add_item(self._format_grouped_subcommands, [action])
        else:
            super().add_argument(action)

    def _format_grouped_subcommands(self, action):
        helps = {ca.dest: ca.help for ca in action._choices_actions}
        names = list(action.choices)
        pad = max((len(n) for n in names), default=0)
        mapped = {n for _, members in _COMMAND_GROUPS for n in members}
        blocks = []
        for title, members in _COMMAND_GROUPS:
            present = [n for n in members if n in names]
            if present:
                blocks.append(self._format_command_group(title, present, helps, pad))
        extra = [n for n in names if n not in mapped]
        if extra:
            blocks.append(self._format_command_group("other", extra, helps, pad))
        return "\n\n".join(blocks)

    def _format_command_group(self, title, members, helps, pad):
        lines = [f"{title}:"]
        for name in members:
            lines.extend(self._format_command_line(name, helps.get(name, ""), pad))
        return "\n".join(lines)

    def _format_command_line(self, name, help_text, pad):
        prefix = "  " + name.ljust(pad) + "  "
        if not help_text:
            return [prefix.rstrip()]
        width = max(self._width - len(prefix), 11)
        wrapped = textwrap.wrap(help_text, width=width) or [""]
        return [prefix + wrapped[0]] + [" " * len(prefix) + line for line in wrapped[1:]]


class _VrpSubParser(argparse.ArgumentParser):
    """子命令 parser：默认挂 _VrpHelpFormatter（description/epilog 保换行）。"""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _VrpHelpFormatter)
        super().__init__(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vrp",
        description="vrp - Vibe project restructuring analysis pipeline: turns repos rapidly"
        " stacked up by AI into something with a factual basis, verifiable and"
        " executable for restructuring.",
        epilog=_MAIN_EPILOG,
        formatter_class=_VrpHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"vrp {__version__}")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="<command>",
        parser_class=_VrpSubParser,
    )
    parser._positionals.title = None

    p = sub.add_parser(
        "precheck",
        help="git snapshot, language census and command discovery (S0 precheck)",
        description="S0: git snapshot + language census + command discovery",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.set_defaults(func=cmd_precheck)

    p = sub.add_parser(
        "assess",
        help="pre-migration assessment: engine capability matrix and risk hints (read-only)",
        description="T10: pre-adoption assessment (read-only; git snapshot + census + s7/mutate engine"
        " capability matrix from live engine constants + content-repo probe + risk hints)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_assess)

    p = sub.add_parser(
        "graph",
        help="call-graph network metrics: PageRank + LPA communities + module reconciliation",
        description="R1: network-science view (deterministic PageRank + LPA communities on the"
        " CALLS/CALL_REFERENCE graph) and reconciliation vs active structure modules",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser(
        "findings",
        help="pathology finding list (open-scored ranking; --dismiss feedback downweights, reorders only)",
        description="S7: feedback-loop finding ranking (decisions dismiss history downweights"
        " a kind by 0.5^n - downweight reorders, never deletes); --dismiss records"
        " the gesture (decisions table, findings row untouched)",
        epilog=(
            "EXAMPLES:\n"
            "  vrp findings <repo> --limit 20            # top 20 findings by score\n"
            "  vrp findings <repo> --json                # machine-readable output\n"
            "  vrp findings <repo> --dismiss 7 --note x  # dismiss with a recorded note (reorders, never deletes)"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--limit", type=int, default=feedback_mod.DEFAULT_LIMIT, help="max findings shown (summary.total stays true)")
    p.add_argument("--dismiss", type=int, help="dismiss a finding by id (feedback gesture; downweights its kind)")
    p.add_argument("--note", help="optional decision note recorded with --dismiss")
    p.set_defaults(func=cmd_findings)

    p = sub.add_parser(
        "cochange",
        help="implicit coupling edge mining from git co-changes (open support/confidence formulas)",
        description="R2: mine implicit co-change coupling from git history (--no-merges"
        " co-occurrence; formulas open: support/confidence per Tornhill temporal"
        " coupling) into co_changes (idempotent DELETE+INSERT full replace,"
        " OWNED across etl); thresholds filter the display, the table keeps all pairs",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--min-support", type=int, default=cochange_mod.DEFAULT_MIN_SUPPORT, help="display filter: min co-occurrence count (>= boundary, default 3)")
    p.add_argument("--min-confidence", type=float, default=cochange_mod.DEFAULT_MIN_CONFIDENCE, help="display filter: min confidence in either direction (>= boundary, default 0.3)")
    p.add_argument("--code-only", action="store_true", help="display filter: exclude pairs where either file is a non-code artifact (docs/data/config/assets/archives - .md/.json/.zip etc) - code-code implicit coupling focus")
    p.set_defaults(func=cmd_cochange)

    p = sub.add_parser(
        "timeline",
        help="architecture-rot timeline: git sampled replay + turning-point narrative",
        description="A1: git history sampled replay -> rot timeline"
        " (.vrp/timeline.json, detached worktree serial advance,"
        " J8 incremental ETL chain)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--samples", type=int, default=timeline_mod.DEFAULT_SAMPLES, help="sample count (first+last commit always included, default 20)")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override (default repo dir name; timeline uses dedicated vrp-timeline-* cache db)")
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser(
        "review",
        help="SARIF -> PR review comments payload (local conversion, zero server-side)",
        description="T9: SARIF -> GitHub review comments JSON (local artifact"
        " .vrp/report/review-comments.json, zero server-side; one result ="
        " one comment, side RIGHT, line-anchored)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--sarif", help="SARIF path (default <repo>/.vrp/report/findings.sarif)")
    p.add_argument("--limit", type=int, help="max comments written (summary stays true)")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser(
        "score",
        help="finding scoring: component breakdown hand-verifiable (--weights override)",
        description="S6: finding priority scoring (formula open and explainable -"
        " every entry ships its components; score = w*impact + w*evidence"
        " - w*span where impact=log1p(anchor in-degree), evidence=log1p(distinct"
        " kinds on the same anchor), span=log1p(affected lines) as the repair"
        " cost proxy; weights configurable via --weights JSON, read-only)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--limit", type=int, default=scoring_mod.DEFAULT_LIMIT, help="max findings shown (summary.total stays true)")
    p.add_argument("--weights", help='JSON object overriding default weights, e.g. \'{"impact": 2.0}\' (keys: impact, evidence, span)')
    p.set_defaults(func=cmd_score)

    p = sub.add_parser(
        "complexity",
        help="cyclomatic + cognitive complexity metrics (py/ts first, read-only)",
        description="R3: complexity metrics v1 py/ts (CC per radon + cognitive"
        " complexity per SonarQube whitepaper simplified - nesting penalty,"
        " flat elif/else, per-operator logic, direct recursion; MI/Halstead"
        " honestly not done; py=stdlib ast, ts=sandbox typescript real AST"
        " with honest unavailable degrade; read-only, no facts.db)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--cc-threshold", type=int, default=complexity_mod.CC_THRESHOLD, help="CC alarm line (default 10, radon/lizard convention)")
    p.add_argument("--limit", type=int, default=complexity_mod.DEFAULT_LIMIT, help="max functions shown (summary stays true)")
    p.set_defaults(func=cmd_complexity)

    p = sub.add_parser(
        "pitindex",
        help="pitfall -> regression-test index (GAP/DRIFT shown separately)",
        description="F16: pitfall->test anti-recurrence index - parse"
        " docs/踩坑历史.md numbered entries, extract test_* tokens as"
        " locking refs, verify against pytest collection; GAPS (pitfall"
        " without a locking test) are info, DRIFT (cited test no longer"
        " exists) is a red face; report-only, rc=0",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_pitindex)

    p = sub.add_parser(
        "dossier-diff",
        help="reconcile two dossiers (added/removed/changed lists)",
        description="A2: dossier diff report - recursive reconciliation of two"
        " dossier.json runs -> added/removed/changed list (release notes"
        " material); list alignment by identity key (fid/symbol/path...)"
        " so a prepend does not fake a whole-column changed; generated_at"
        " excluded (timestamp noise); pure stdlib, no deepdiff",
    )
    p.add_argument("old", help="previous dossier.json path")
    p.add_argument("new", help="current dossier.json path")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_dossier_diff)

    p = sub.add_parser(
        "scaffold",
        help="strangler scaffold: module skeleton + shim template + migration order",
        description="D10: strangler scaffold generator (propose's natural extension;"
        " Martin Fowler strangler-fig - migration order edge-before-core by"
        " cross-module edge count; Python first, other engines later):"
        " module skeletons + re-export shim templates + MIGRATION.md from"
        " .vrp/proposals/structure-proposal.json into .vrp/scaffold/;"
        " never touches user source files (adoption is manual)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--proposal", help="proposal path (default: <repo>/.vrp/proposals/structure-proposal.json)")
    p.add_argument("--out", help="output directory (default: <repo>/.vrp/scaffold)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_scaffold)

    p = sub.add_parser(
        "tour",
        help="newcomer module tour (one-line responsibility + reading order + anchors)",
        description="T11: newcomer code tour from curated structure (curate consumer"
        " surface; VS Code Code Tour + Diataxis tutorial as form references;"
        " output product, not an operation protocol): per-module one-line"
        " curated responsibility + suggested reading order (layer topo -"
        " depended-on foundations first, then ascending symbol count -"
        " small modules first); top files + representative symbols as"
        " concrete anchors",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument(
        "--html", action="store_true",
        help="write static HTML walkthrough to <repo>/.vrp/report/tour.html"
        " (archify v3.3.0 story #1036 form absorption; opt-in write surface -"
        " default remains read-only)",
    )
    p.set_defaults(func=cmd_tour)

    p = sub.add_parser(
        "curate-merge",
        help="three-way merge of two agents' curation artifacts (conflicts recorded, ours kept; rc=1 when pending)",
        description="M16: curate artifact three-way merge (git merge base/ours/theirs"
        " semantics; OT conflict classification: non-conflicts auto-merge,"
        " conflicts marked for human adjudication): merge an external agent's"
        " curate payload (a directory with slices.json/structure.json, or a"
        " single JSON file) into facts.db; ours=active artifacts, base="
        " curate_base pre-overwrite snapshots (auto-maintained by ingest);"
        " conflicts recorded in decisions (kind=curate_merge) with ours kept"
        " - nothing is silently lost; rc=1 when conflicts await adjudication",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--theirs", required=True, help="their payload: directory (slices.json/structure.json) or single JSON file")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_curate_merge)

    p = sub.add_parser(
        "curate-refresh",
        help="in-place fid remap (drift-recovery write path, full revalidation before commit)",
        description="S2.5: deterministically remap dead fids in structure/slices via"
        " fid_ledger qn (content churn + project rename both bridged by the"
        " stable qualified_name key; module/slice semantics preserved verbatim,"
        " only fid pointers refreshed; unremappable fids honestly dropped and"
        " counted, empty modules dropped, all-dead structures refused);"
        " receipt at <repo>/.vrp/curate-refresh-receipt.json;"
        " rc=1 when structure is missing/failed",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_curate_refresh)

    p = sub.add_parser(
        "groups",
        help="same-root-cause finding grouping (Sentry fingerprint semantics, read-only)",
        description="S5: group findings by root-cause fingerprint (Sentry grouping"
        " semantics; unordered fid pair normalized - cycles a->b/b->a and"
        " clone families share one key; grouping never deletes, read-only)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--limit", type=int, default=grouping_mod.DEFAULT_LIMIT, help="max groups shown (summary stays true)")
    p.set_defaults(func=cmd_groups)

    p = sub.add_parser(
        "profile",
        help="repo digital profile: Gini/concentration/edge density/top-5 hubs",
        description="R4: repo descriptive statistics (gini of in/out degree and"
        " file-symbol distributions, top1%/top10% concentration as the honest"
        " power-law proxy, edge density, fan-in/out hubs; read-only)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser(
        "doclint",
        help="living-doc drift guard: markdown numeric assertions vs sources of truth",
        description="P23: living-doc drift check - <!-- doclint {...} --> markers in"
        " markdown claim numbers, truth sources (glob / recount /"
        " pytest-collect) recompute them; rc=1 on drift; read-only",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_doclint)

    p = sub.add_parser(
        "doctor",
        help="toolchain self-diagnosis (core missing -> rc=1; engine missing -> degraded only)",
        description="I4: toolchain environment self-diagnosis - aggregates the"
        " per-adapter find_* discovery (single source of truth) into"
        " core (python/git/node/CBM, rc=1 if missing) vs engine tiers"
        " (21-engine externals + scc, missing = degraded capability only);"
        " git/node get a cheap --version probe; read-only",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (LLM config section; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "explain",
        help="error-code lookup in plain words (gate/curate/P families)",
        description="I5: error-code lookup - gate/G0-G6 transact gate codes"
        " (single-sourced from s6_transact.RULE_CODES), curate rejection"
        " codes SLICE_*/STRUCT_*/FILE_* (AST-extracted from source - no"
        " second registry), pathology P-codes (render.PATHOLOGY_LEGEND);"
        " no arg = family overview; prefix match case-insensitive; rc=1"
        " on no match; read-only, no repo arg",
    )
    p.add_argument("code", nargs="?", default=None, help="code or prefix to look up (e.g. G4, SLICE_MISSING_EDGE, P9)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser(
        "perf",
        help="performance regression tracking (run auto-times; double gate flags)",
        description="J10: pipeline performance regression tracking - 'vrp run'"
        " auto-records per-stage timings (precheck/cbm_index/etl/xcheck)"
        " to .vrp/perf.jsonl; this reports last-vs-previous per-stage delta;"
        " regression flag = double gate (+25% AND +1.0s, small-base noise"
        " immune); single-sample honest note; rc always 0 (report, not gate)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_perf)

    p = sub.add_parser(
        "relnotes",
        help="semi-automatic release notes draft (commits x handover cross-check)",
        description="K11: release-notes draft generator - commit-range '#NNN:'"
        " subjects x handover-entry cross-check (drift warnings), numbers"
        " live-verified at generation time (pytest-collect via doclint,"
        " s7 glob language count - no snapshot numbers, #153 lesson);"
        " prints a markdown draft for human review; date-injectable so"
        " same input = byte-identical draft",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--from", dest="from_ref", required=True, help="starting ref/tag (exclusive)")
    p.add_argument("--to", dest="to", default="HEAD", help="ending ref (default HEAD)")
    p.add_argument("--version", default=None, help="version header (e.g. v0.1.3)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_relnotes)

    p = sub.add_parser(
        "smoke",
        help="cross-repo smoke testing in one shot (verify/ per-repo full chain)",
        description="K12: cross-repo release smoke - auto-discovers verify/ repos"
        " (dirs with .git - no registry), runs the full S0->xcheck chain"
        " per repo, aggregates symbols/edges/findings counts;"
        " content-repo skip = pass (P-2 semantics); --skip-run = counts +"
        " M4 freshness check only (stale = fail); rc=1 on any fail",
    )
    p.add_argument("root", nargs="?", default=None, help="vrp checkout root (default: this package's parent)")
    p.add_argument("--only", action="append", default=None, help="restrict to named verify repo (repeatable)")
    p.add_argument("--skip-run", dest="skip_run", action="store_true", help="read existing facts.db + freshness check only")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser(
        "gc",
        help="CBM cache GC (audit -> dry-run -> real delete, three stages)",
        description="L14: CBM cache GC (npm cache clean shape) - audit all cached"
        " dbs (provenance via store_meta/vrp_repo_path, repo liveness check,"
        " size/nodes); repo-missing = the only auto GC candidate"
        " (unknown-source/unreadable reported but never auto-collected,"
        " #77 lesson); --dry-run lists would-delete + would-free bytes;"
        " --yes actually deletes (db + -wal/-shm sidecars, per-file failures"
        " -> rc=1)",
    )
    p.add_argument("repo", nargs="?", default=None, help="repo whose per-repo cache (<repo>/.vrp/cache) to audit (default: CBM_CACHE_DIR env or home fallback)")
    p.add_argument("--cache-dir", dest="cache_dir", default=None, help="CBM cache dir (default: <repo>/.vrp/cache, CBM_CACHE_DIR env, or home fallback)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="list what --yes would delete, no disk changes")
    p.add_argument("--yes", action="store_true", help="confirm: actually delete GC candidates")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser(
        "flakes",
        help="flake registry (record, never mask; drift guard)",
        description="L15: flake registry - recurrence tracking, shake-king list"
        " (count>=2 = isolation candidates), record-don't-mask"
        " (pytest-rerunfailures NOT adopted); first observation goes to"
        " handover prose, recurrence enters this registry; validate()"
        " drift-guards active entries (renamed/deleted test = red);"
        " fd0d7db misjudgment precedent: recurrence -> find root cause"
        " first, confirmed defect gets fixed not registered",
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_flakes)

    p = sub.add_parser(
        "langmatrix",
        help="language capability matrix (every cell live-verified, zero hardcoding)",
        description="N18: language capability matrix - rows = 21 s7 engine langs"
        " (assess single source) + ext-recognized no-engine langs"
        " (C++/GDScript - the indexed-but-not-enforced honesty rows);"
        " columns = per-language faces each derived live from its owning"
        " module (#153 discipline, zero hardcoded language lists):"
        " astgrep rule-id prefix counts / mutate engine ext-constant map"
        " / deadcode-p4 adapter gates / testsig self-build exts;"
        " language-agnostic faces (jscpd/S3/S5/dossier) noted, not"
        " per-row; rc always 0 (gaps are backlog, not errors)",
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_langmatrix)

    p = sub.add_parser(
        "argcheck",
        help="parameter-level reconciliation: too_few/too_many/unknown_kwarg",
        description="N19: CALLS edge args x callee signature reconciliation -"
        " caller face = edge properties args[{i,e,k,v}] (CBM verbatim),"
        " callee face = symbols.signature (schema v3; the signature STRING"
        " is the only trusted base - CBM param_count counts type-annotated"
        " params only, ~21% disagreement measured, reference not base);"
        " arity: required <= n_args <= total (positional+keyword; default-"
        " valued / ts-'?' params optional, *args suppresses the upper"
        " bound, **kwargs suppresses the kwarg-name check); unknown_kwarg"
        " = keyword key not among signature names; Method leading self/cls"
        " stripped; builtins.* and no-signature callees skipped and"
        " counted (no guessing); report not gate (mdcode/langmatrix"
        " precedent, rc always 0)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_argcheck)

    p = sub.add_parser(
        "mdcode",
        help="markdown code blocks vs facts symbols comparison (drift candidates)",
        description="N20: markdown code-block extraction - fenced blocks -> symbol"
        " extractors (py=stdlib ast true parse, ts/js=regex light) ->"
        " name+label match against facts.db symbols -> drift candidates"
        " (doc-only = renamed/removed/fictional API; kind-mismatch on the"
        " clear py face, Method~=Function same family; ts is name-only -"
        " label vocab unpinned, honest boundary); report not gate"
        " (langmatrix precedent, rc always 0); name-level match (params"
        " not in facts.db), common-name collisions possible - noted;"
        " excludes reuse watch.EXCLUDE_DIRS single source",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_mdcode)

    p = sub.add_parser(
        "etl-health",
        help="facts.db ETL health invariants (violations -> rc=1)",
        description="BB4: ETL health face - query-based invariant checklist over"
        " facts.db (mirrors deep ref_integrity assertions as a runtime"
        " defense-in-depth layer: fid unique / six-way zero orphans"
        " (edges src+dst, usage_hints, test_linkage) / zero self-loops /"
        " 8-tuple full-row dedup / symbols_stale_files = CGC BUG-005"
        " analog - symbol claims a file absent from the J8 etl_file_state"
        " snapshot: deleted file or stale CBM index); detection only,"
        " NO in-place self-heal (owned tables are rebuild-derived -"
        " same-facts.db same-bytes red line; repair = rerun 'vrp run');"
        " stale schema (missing tables) reported honestly, not crashed;"
        " rc 0 healthy / 1 violations (health-check semantics, CI-usable)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_etl_health)

    p = sub.add_parser(
        "layermetrics",
        help="layer metrics: fan-in/out, instability, optional threshold violations",
        description="C8: layer metrics & thresholds (JDepend D-metric half-face"
        " learned + deptrac threshold semantics learned; rules stay in"
        " structure JSON - no DSL): per-module/per-layer fan_in (Ca)"
        " / fan_out (Ce) over inter-scope DEDUPED edge pairs"
        " (class-dependency semantics, not call counts; multi-module fid"
        " membership counted per pair - s7 precedent; non-structure fids"
        " excluded as noise floor) + instability I=Ce/(Ca+Ce) (isolated"
        " scope I=None honest-undefined); abstractness NOT derivable"
        " (facts.db has no modifier data - BB2 boundary; D=|A+I-1|"
        " documented half-face, no fabricated A); optional structure"
        " payload top key 'thresholds' = {'modules'|'layers': {name:"
        " {max_fan_in, max_fan_out, max_instability}}} (curate tolerates"
        " unknown keys - pure data face); violations rc=1 (CI-usable),"
        " no thresholds = metrics report rc=0; structure not curated ="
        " honest note rc=0 (metrics face, not a gate); naming-convention"
        " rules deferred (v10: expressiveness on-demand, no over-design)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_layermetrics)

    p = sub.add_parser(
        "sweep",
        help="threshold sensitivity sweep (offline payload re-cut, cliff points for picking thresholds)",
        description="S8: threshold sensitivity scan (validation-curve shape"
        " learned, scikit-learn NOT imported): threshold x finding-count"
        " curves + cliff points to help pick jscpd minTokens/minLines;"
        " OFFLINE - no engine re-run, pure re-cut of stored findings"
        " payloads (count = findings with value >= threshold, monotone"
        " non-increasing); sweepable = s2-jscpd tokens/lines (own-repo"
        " real case: 1000+ P2 findings); non-sweepable honest notes"
        " (rule hits boolean, deadcode/vulture no numeric field);"
        " jscpd default 50 marked current on the tokens curve;"
        " rc always 0 (analysis face, not a gate)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser(
        "migprog",
        help="migration progress tracking (cross-boundary edge shrink %; --snapshot appends audit trail)",
        description="D11: migration progress tracking - cross-seam edge"
        " snapshots -> strangler completion pct (shrink curve);"
        " seams single-source = nav_seams (#143, zero reimplementation);"
        " snapshot appended to .vrp/migprog-history.jsonl (audit-append"
        " semantics, runtime_evidence precedent; user sets the cadence);"
        " progress = (baseline - current) / baseline cross edges,"
        " baseline = first snapshot (migration start), negative ="
        " coupling grew (honest); zero baseline = pct None honest;"
        " no snapshot yet = honest note; rc always 0 (tracking face,"
        " not a gate)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--snapshot", action="store_true", help="append current seams snapshot to history")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_migprog)

    p = sub.add_parser(
        "xcheck-explain",
        help="xcheck discrepancy explanations in plain words (symptom -> cause -> fix)",
        description="Q25: xcheck finding narratization (rustc --explain shape:"
        " phenomenon -> cause -> fix) - reads discrepancy_ledger four"
        " categories + meta xcheck_* from facts.db; per-category counts,"
        " top-3 examples, low-agreement overall line (<60pct flagged LOW);"
        " real-confusion-case prerequisite met (27.7pct agreement case,"
        " xcheck.py:103); read-only, rc always 0 (explanation is not a"
        " gate); 'xcheck not run yet' honest message when ledger empty",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.set_defaults(func=cmd_xcheck_explain)

    p = sub.add_parser(
        "secrets",
        help="secrets scan (worktree + --history blobs; hits -> rc=1)",
        description="H1: standalone secrets scan (gitleaks shape; detector single-"
        "source from render_chain - zero detection-logic copies, trufflehog"
        " live-verification NOT done - detect only): worktree walk"
        " (IGNORE_DIRS pruned, binary/oversize skips counted) + optional"
        " --history full-blob scan via streaming cat-file --batch (secrets"
        " in deleted files still found; oversize blobs skipped+counted);"
        " read-only stdout report (SECRETS.local.md manifest stays in the"
        " render chain); rc=1 on hits (CI-usable, gitleaks semantics)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--history", action="store_true", help="also scan all git history blobs (slow face, explicit opt-in)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_secrets)

    p = sub.add_parser(
        "index",
        help="build the CBM index (S1)",
        description="S1: CBM index_repository",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override")
    p.add_argument("--reindex", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser(
        "etl",
        help="CBM index -> facts.db (S1.5, incremental by default)",
        description="S1.5: CBM index -> facts.db (incremental by default)",
        epilog=(
            "EXAMPLES:\n"
            "  vrp etl <repo>            # incremental ingest (file-grained, default)\n"
            "  vrp etl <repo> --full     # force full rebuild\n"
            "  vrp etl                   # omit path = current directory"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override (if indexed under a different name)")
    p.add_argument("--full", action="store_true", help="J8: force full rebuild (skip the incremental patch path)")
    p.set_defaults(func=cmd_etl)

    p = sub.add_parser(
        "xcheck",
        help="code-atlas cross-validation -> discrepancy ledger",
        description="S1: code-atlas cross-validation -> discrepancy_ledger",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--reindex", action="store_true", help="re-run code-atlas index")
    p.set_defaults(func=cmd_xcheck)

    p = sub.add_parser(
        "run",
        help="full pipeline in one shot: S0->S1->ETL->xcheck (run this first)",
        description="S0 -> S1 -> S1.5 -> xcheck one-shot pipeline",
        epilog=(
            "EXAMPLES:\n"
            "  vrp run <repo>              # first full-pipeline run (S0->S1->ETL->xcheck)\n"
            "  vrp run <repo> --watch      # auto re-run on file changes (Ctrl+C to exit)\n"
            "  vrp run <repo> --reindex    # force CBM index rebuild"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--reindex", action="store_true", help="force CBM re-index")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override")
    p.add_argument(
        "--watch",
        action="store_true",
        help="I7: keep watching after the initial run - poll file mtimes and re-run"
        " the pipeline on changes (debounced; foreground loop, Ctrl+C stops;"
        " reruns ride the J8 incremental chain; --reindex applies to every rerun)",
    )
    p.add_argument("--interval", type=float, default=2.0, help="watch poll interval in seconds (default 2.0)")
    p.add_argument("--debounce", type=float, default=1.0, help="watch quiet window in seconds before re-running (default 1.0)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser(
        "s2",
        help="pathology detection: ast-grep/dead-code/P4/jscpd four engines -> findings",
        description="S2: four-engine pathology detection -> findings",
        epilog=(
            "EXAMPLES:\n"
            "  vrp s2 <repo>    # four-engine pathology scan (needs 'vrp run' first to build facts.db)\n"
            "  vrp s2           # omit path = current directory"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.set_defaults(func=cmd_s2)

    p = sub.add_parser(
        "mutate-plan",
        help="mutation budget sampling (test_linkage pre-filter)",
        description="S2 P8: mutation budget sampler (test_linkage pre-filter)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--budget", type=int, default=50)
    p.set_defaults(func=cmd_mutate_plan)

    p = sub.add_parser(
        "mutate-run",
        help="mutation execution (mutmut etc) + survivor reconciliation -> P8 findings",
        description="S2 P8: execute mutations via mutmut, reconcile against plan (survivors -> P8 findings + runtime evidence)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--budget", type=int, default=10)
    p.add_argument("--timeout", type=int, default=3600, help="overall mutmut timeout (s)")
    p.set_defaults(func=cmd_mutate_run)

    p = sub.add_parser(
        "enforce",
        help="S7 architecture enforcement: layer DAG + rule violations -> P9 findings",
        description="S7: architecture enforcement",
        epilog=(
            "EXAMPLES:\n"
            "  vrp enforce <repo>              # architecture enforcement scan (needs curated structure)\n"
            "  vrp enforce <repo> --timeout 900  # longer lint-imports timeout (seconds)"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--timeout", type=int, default=600, help="lint-imports timeout (s)")
    p.set_defaults(func=cmd_enforce)

    p = sub.add_parser(
        "validate",
        help="anchor revalidation + coverage -> gate_results (S3)",
        description="S3: anchor revalidation (active/cosmetic/stale) + coverage -> gate_results",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser(
        "gate",
        help="AI claim evidence gate (quote-or-rejected, S3)",
        description="S3: evidence gate merge of AI claims (quote-or-rejected)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("claims", help="JSON file with a claims array")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser(
        "freeze",
        help="baseline command run + graph snapshot + change pre-screen (S4)",
        description="S4: run discovered commands -> runtime_evidence + CBM graph baseline",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override")
    p.add_argument("--timeout", type=int, default=900, help="per-command timeout (s)")
    p.add_argument("--skip-commands", action="store_true")
    p.add_argument("--skip-graph", action="store_true", help="skip CBM persistence index + detect_changes")
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser(
        "dossier",
        help="five-view information assembly (T1-T4 task-typed budget, the territory map before work)",
        description="S5: task-typed dossier",
        epilog=(
            "EXAMPLES:\n"
            "  vrp dossier <repo> --task T1          # refactoring kick-off briefing\n"
            "  vrp dossier <repo> --skip-external    # offline assembly (skip external tool calls)\n"
            "  vrp dossier <repo> --budget 16000     # tighter total character budget"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override")
    p.add_argument("--budget", type=int, default=32000, help="total char budget for dossier JSON")
    p.add_argument(
        "--task",
        choices=sorted(s5_dossier.TASK_PROFILES),
        default=s5_dossier.DEFAULT_TASK,
        help="task profile: T1 equivalent-refactor / T2 cleanup / T3 fix / T4 improvement",
    )
    p.add_argument(
        "--skip-external",
        action="store_true",
        help="skip external tool calls (CBM get_architecture, code-atlas repo_map, repomix)",
    )
    p.set_defaults(func=cmd_dossier)

    p = sub.add_parser(
        "curate-auto",
        help="built-in LLM curation loop (automatic six steps; fully audited, key never on disk)",
        description="built-in LLM curation loop (zero-agent path; config in .vrp/config.json)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--budget", type=int, default=32000, help="dossier char budget")
    p.add_argument(
        "--task",
        choices=sorted(s5_dossier.TASK_PROFILES),
        default=s5_dossier.DEFAULT_TASK,
        help="dossier task profile (T1-T4)",
    )
    p.add_argument("--model", default="", help="override config llm.model for this run")
    p.set_defaults(func=cmd_auto_curate)

    p = sub.add_parser(
        "fixpropose",
        help="single finding -> LLM-drafted transaction (drafts are never executed)",
        description="C9: LLM fix proposal for one finding -> .vrp/proposals/fix-<n>.json (transact draft;"
        " execution only via 'vrp transact' - the draft is never executed here)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("finding_id", type=int, help="findings.id to fix (must have file_path)")
    p.add_argument("--model", default="", help="override config llm.model for this run")
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing proposal file (default: refuse to protect reviewed drafts)",
    )
    p.set_defaults(func=cmd_fixpropose)

    p = sub.add_parser(
        "analyze",
        help="terminal-native AI analysis: single-turn LLM report",
        description="terminal-native AI analysis: material = dossier T1 + findings top 20"
        " (deterministic from facts.db); auto-rebuilds stale facts (S0->S1->ETL->xcheck);"
        " writes .vrp/analysis.md; audit in .vrp/analyze/; LLM config via 'vrp config'",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument(
        "--with-timeline",
        action="store_true",
        help="attach git timeline material (heavy: 20-commit sampled replay; git repos only, default off)",
    )
    p.add_argument("--json", action="store_true", help="machine-readable metadata output instead of the markdown report")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser(
        "chat",
        help="terminal-native interactive LLM Q&A REPL (TTY only)",
        description="terminal-native interactive Q&A over the repo (dossier T1 brief injected;"
        " slash commands: /quit /dossier /findings /refresh /clear; session log only with --log;"
        " for one-shot analysis use 'vrp analyze')",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument(
        "--log",
        action="store_true",
        help="write session log to .vrp/chat/ (default off: interactive privacy)",
    )
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser(
        "config",
        help="LLM API configuration wizard (OpenAI-compatible)",
        description="interactive LLM configuration wizard; writes <repo>/.vrp/config.json;"
        " --show masked echo, --test connectivity ping (max_tokens=1),"
        " --store-key stores the key in plaintext (an env variable is safer)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--show", action="store_true", help="show current config (key always masked)")
    p.add_argument("--test", action="store_true", help="load config and ping the LLM API (max_tokens=1)")
    p.add_argument("--store-key", action="store_true", help="store api_key in plaintext inside config.json (less secure)")
    p.set_defaults(func=config_cmd.cmd_config)

    p = sub.add_parser(
        "transact",
        help="refactor transaction execution: edits -> G1-G5 gates -> atomic commit or rollback",
        description="S6: refactor transaction execution",
        epilog=(
            "EXAMPLES:\n"
            "  vrp transact <repo> tx.json --preview   # diff preview, tree untouched, no gates\n"
            "  vrp transact <repo> tx.json             # through the gates: atomic commit or full rollback\n"
            "  vrp transact <repo> tx.json --confirm   # confirmed execution of the T4 content tweak transaction"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("transaction", help="transaction JSON file path")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override")
    p.add_argument("--confirm", action="store_true", help="confirm a T4 transaction")
    p.add_argument("--timeout", type=int, default=900, help="per-command timeout for gate commands")
    p.add_argument(
        "--preview",
        action="store_true",
        help="preview only: per-edit unified diff + difftastic structural signal (no gates, no tree mutation)",
    )
    p.set_defaults(func=cmd_transact)

    p = sub.add_parser(
        "transact-batch",
        help="run directory transactions in order (stop on first failure)",
        description="S6: run every transaction JSON in a directory in sorted order (stop at first non-committed)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("tx_dir", help="directory containing transaction JSON files (*.json)")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override")
    p.add_argument("--confirm", action="store_true", help="confirm T4 transactions")
    p.add_argument("--timeout", type=int, default=900, help="per-command timeout for gate commands")
    p.add_argument("--continue-on-fail", action="store_true", help="keep running after a non-committed transaction")
    p.set_defaults(func=cmd_transact_batch)

    p = sub.add_parser(
        "mcp",
        help="AI interface: MCP server over stdio (per-repo)",
        description="R5: MCP server over stdio (per-repo)",
        epilog=(
            "EXAMPLES:\n"
            "  vrp mcp <repo>    # start the per-repo MCP server (stdio; requires pip install vrp[mcp])\n"
            "  vrp mcp           # omit path = current directory"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser(
        "verdicts",
        help="verdict history query (open items feed back into dossier)",
        description="S6: verdict history",
        epilog=(
            "EXAMPLES:\n"
            "  vrp verdicts <repo>            # full transaction verdict history\n"
            "  vrp verdicts <repo> --tid abc  # single full verdict JSON"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--tid", help="show the full verdict JSON for one transaction id")
    p.set_defaults(func=cmd_verdicts)

    p = sub.add_parser(
        "curate",
        help="AI curation artifact acceptance into the DB (all-or-nothing; --reject records a veto)",
        description="S2.5: curation acceptance",
        epilog=(
            "EXAMPLES:\n"
            "  vrp curate <repo>                          # accept .vrp/ai/ artifacts into the DB\n"
            "  vrp curate <repo> --reject <slice_id> --note x  # veto with a recorded note (reorders, never deletes)\n"
            "  vrp curate <repo> --reject structure --note x   # veto the whole structure"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--reject", help="veto an ingested artifact: a slice_id or the literal 'structure'")
    p.add_argument("--note", help="optional decision note recorded with --reject")
    p.set_defaults(func=cmd_curate)

    p = sub.add_parser(
        "drift",
        help="curation artifact drift query (relocation candidates; drift -> rc=1)",
        description="S2.5: unified drift report for curate artifacts (missing fids/edges + relocation candidates; read-only)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--history", type=int, default=0, metavar="N",
                   help="also print the last N drift_history revalidation events (A3 data-accumulation face; includes nonzero_drift_events full-table signal count for the A3 check-in gate; 0 = off)")
    p.set_defaults(func=cmd_drift)

    p = sub.add_parser(
        "remap",
        help="semi-automatic drift recovery: unique candidates written back to .vrp/ai/*.json",
        description="S2.5: apply drift remap proposals (unique qualified_name candidates) back to .vrp/ai/*.json working files",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.set_defaults(func=cmd_remap)

    p = sub.add_parser(
        "seams",
        help="cross-module edge aggregation (module dependency graph input, read-only)",
        description="#143: cross-module seam aggregation over the active structure (read-only; unwind seams; "
        "module dependency graph raw material)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.set_defaults(func=cmd_seams)

    p = sub.add_parser(
        "propose",
        help="structure re-partitioning proposal (deterministic draft -> .vrp/proposals/)",
        description="#143: deterministic structure partition proposal (unwind slices propose; top-dir seeds + "
        "edge-weight greedy merge + balance cap; writes .vrp/proposals/ NOT .vrp/ai/ - adopt via curate)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--out", help="output file (default: <repo>/.vrp/proposals/structure-proposal.json)")
    p.add_argument("--target-modules", type=int, default=None, help="target module count (default 8)")
    p.add_argument("--balance-cap", type=float, default=None,
                   help="max merged module share of candidates (default 0.6)")
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser(
        "traces",
        help="runtime call evidence import + static/observed reconciliation",
        description="#144: ingest runtime call traces -> runtime_calls + observed-vs-static agreement "
        "(CBM ingest_traces; qn-keyed reindex-proof, per-label whole-run replace; external producers)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--file", required=True, help="traces JSON file ({traces: [path|caller/callee], label})")
    p.add_argument("--label", help="run label override (default: file 'label' field or 'unlabeled')")
    p.set_defaults(func=cmd_traces)

    p = sub.add_parser(
        "testcov",
        help="static test linkage vs actual line coverage reconciliation (fake linkages exposed)",
        description="B4: ingest coverage.py JSON report (pytest --cov external producer, zero-dep)"
        " -> test_coverage (file-path-keyed OWNED table, per-label whole-run replace)"
        " + test_linkage-vs-coverage reconciliation (def-line excluded;"
        " linked_not_covered_measured = 'statically linked but never executed' false-linkage signal)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--file", required=True, help="coverage JSON file ({files: {path: {executed_lines, missing_lines}}})")
    p.add_argument("--label", help="run label (default 'unlabeled')")
    p.set_defaults(func=cmd_testcov)

    p = sub.add_parser(
        "golden",
        help="golden audit matrix: two-sided audit against committed truth",
        description="F15+BB3: audit findings/symbols against committed golden truth JSON"
        " (vrp-golden-1; violations multiset match + symbols scoped exact enum;"
        " precision/recall honest-null; rc always 0 - measurement, not a gate)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--file", required=True, help="golden truth JSON file (vrp-golden-1 format)")
    p.set_defaults(func=cmd_golden)

    p = sub.add_parser(
        "coverage",
        help="index completeness verification (six-state gap inventory + advice)",
        description="#145: index completeness check (CBM check_index_coverage; read-only; "
        "parse_partial/parse_unusable/not_indexed gap inventory + per-path status)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--path", dest="paths", action="append",
                   help="path to verify (repeatable, cap 128; exact + ancestor match, per-path status)")
    p.add_argument("--kind", dest="kinds", action="append", help="kind filter (repeatable; e.g. parse_partial)")
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser(
        "impact",
        help="change blast radius BFS + suggested tests (suggested != sufficient)",
        description="#146: change impact + suggested tests (CBM detect_changes/test_impact; read-only): "
        "worktree diff -> hunk-overlap seeding -> multi-source BFS blast radius "
        "-> test_linkage consumption (suggested != sufficient)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--base", help="compare against a base ref (merge-base..HEAD union worktree; default: worktree vs HEAD)")
    p.add_argument("--path", dest="paths", action="append",
                   help="explicit changed paths (repeatable; skips git diff - CI/debug seeding)")
    p.add_argument("--depth", type=int, default=2, help="BFS hop cap (default 2, clamped to 15)")
    p.add_argument("--direction", choices=["inbound", "outbound", "both"], default="inbound",
                   help="inbound=callers blast radius (default), outbound=dependencies, both=union")
    p.add_argument("--no-tests", dest="tests", action="store_false",
                   help="skip suggested-tests lookup (test_linkage consumption)")
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser(
        "refresh",
        help="one-shot drift recovery loop: S0->...->remap->curate->report",
        description="S0 -> S1 -> S1.5 -> xcheck -> drift -> remap -> curate -> report (one-shot drift recovery loop)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--reindex", action="store_true", help="force CBM re-index")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override")
    p.add_argument("--out", help="report output directory (default: <repo>/.vrp/report)")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser(
        "status",
        help="facts.db summary and freshness snapshot",
        description="facts.db summary",
        epilog=(
            "EXAMPLES:\n"
            "  vrp status <repo>    # facts.db counts and freshness for the given repo\n"
            "  vrp status           # omit path = current directory"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "report",
        help="human artifacts: static report under .vrp/report/ (archify/SARIF/Mermaid)",
        description="S8: static report -> .vrp/report/ (report.html + IR + SARIF + Mermaid; archify optional)",
        epilog=(
            "EXAMPLES:\n"
            "  vrp report <repo>            # generate the .vrp/report/ static report\n"
            "  vrp report <repo> --out out/  # choose the output directory"
        ),
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("--out", help="output directory (default: <repo>/.vrp/report)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser(
        "session",
        help="trust-mode session: begin/end snapshots + initial-vs-final reconciliation",
        description="R3.5': trust-mode session (begin/end/report; initial vs final delivery, design doc v7 §3.3)",
    )
    p.add_argument("repo", nargs="?", default=".", help="target repo path (absolute or relative; omit = current directory)")
    p.add_argument("action", choices=["begin", "end", "report"])
    p.add_argument("--note", help="session note recorded in session.json")
    p.add_argument("--id", dest="id", help="session id for report (default: latest ended)")
    p.add_argument("--out", help="report output dir (default: the session dir)")
    p.set_defaults(func=cmd_session)

    # E 批：纯 JSON 墙命令统一补 --json（curate-refresh 已有则跳过）——
    # 机读面 = 原样 JSON（字节不变），缺省走 _face_* 英文人读面。
    for name in _JSON_WALL_COMMANDS:
        wall = sub.choices[name]
        if "--json" not in wall._option_string_actions:
            wall.add_argument("--json", action="store_true", help="machine-readable JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    # F3 编码兜底：非 TTY stdout（管道/重定向）在某些 Windows 控制台配置下
    # 默认非 UTF-8（GBK/cp1252），人读面/JSON 面的非 ASCII 数据会 UnicodeEncodeError。
    # reconfigure 尽力而为——失败（旧运行时/特殊流）不阻塞，异常兜底由打印层承担。
    if not sys.stdout.isatty():
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        RuntimeError,
        ValueError,
        OSError,
        subprocess.TimeoutExpired,
        sqlite3.Error,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
