"""vrp CLI: precheck | index | etl | xcheck | run | status."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
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
    _print_json(result)
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
    result = doctor_mod.run_doctor()
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
    _print_json(result)
    return 0


def cmd_etl(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.cbm_name or repo.name
    result = etl_mod.run_etl(repo, name, full=args.full)
    printable = {k: v for k, v in result.items()}
    _print_json(printable)
    return 0


def cmd_xcheck(args) -> int:
    repo = _repo_arg(args.repo)
    facts_db = repo / ".vrp" / "facts.db"
    if not facts_db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp etl' first: {facts_db}")
    result = xcheck.run_xcheck(repo, facts_db, reindex=args.reindex)
    _print_json(result)
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
    _print_json(result)
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
    _print_json({"budget": args.budget, "selected": len(targets), "plan": str(out)})
    return 0


def cmd_mutate_run(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        result = s2_mutate_run.run(repo, con, args.budget, args.timeout)
    finally:
        con.close()
    _print_json(result)
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
    _print_json(result)
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
    _print_json(result)
    return 0


def cmd_gate(args) -> int:
    repo = _repo_arg(args.repo)
    claims_path = Path(args.claims)
    if not claims_path.is_file():
        raise SystemExit(f"claims file not found: {claims_path}")
    claims = json.loads(claims_path.read_text(encoding="utf-8"))
    if not isinstance(claims, list):
        raise SystemExit("claims file must be a JSON array")
    con, _ = _open_facts_rw(repo)
    try:
        result = s3.gate_claims(con, repo, claims)
    finally:
        con.close()
    _print_json(result)
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
    _print_step("S4 freeze", summary)
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
    _print_step(
        "S5 dossier",
        {k: dossier[k] for k in ("generated_at", "project", "budget", "task", "out")},
    )
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
    _print_step("S6 verdicts", {"count": len(summary), "verdicts": summary})
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
    _print_json(result)
    return 0


def cmd_propose(args) -> int:
    repo = _repo_arg(args.repo)
    result = propose_mod.propose_structure(
        repo,
        out=Path(args.out) if args.out else None,
        target_modules=args.target_modules,
        balance_cap=args.balance_cap,
    )
    _print_json(result)
    return 0 if result.get("ok") else 1


def cmd_traces(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        result = traces_mod.ingest_traces(con, Path(args.file), label=args.label)
    finally:
        con.close()
    _print_json(result)
    return 0 if result.get("ok") else 1


def cmd_testcov(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        result = testcov_mod.ingest_testcov(con, Path(args.file), label=args.label)
    finally:
        con.close()
    _print_json(result)
    return 0 if result.get("ok") else 1


def cmd_golden(args) -> int:
    repo = _repo_arg(args.repo)
    result = golden_mod.run_audit(repo, Path(args.file))
    _print_json(result)
    return 0


def cmd_coverage(args) -> int:
    repo = _repo_arg(args.repo)
    result = coverage_mod.check_coverage(repo, paths=args.paths, kinds=args.kinds)
    _print_json(result)
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
    _print_json(result)
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
    _print_step("S8 report", result)
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
        _print_json({"facts_db": None})
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
    _print_json(
        {
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
    )
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vrp", description="vibe-restruct pipeline")
    parser.add_argument("--version", action="version", version=f"vrp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("precheck", help="S0: git snapshot + language census + command discovery")
    p.add_argument("repo")
    p.set_defaults(func=cmd_precheck)

    p = sub.add_parser(
        "assess",
        help="T10: pre-adoption assessment (read-only; git snapshot + census + s7/mutate engine"
        " capability matrix from live engine constants + content-repo probe + risk hints)",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_assess)

    p = sub.add_parser(
        "graph",
        help="R1: network-science view (deterministic PageRank + LPA communities on the"
        " CALLS/CALL_REFERENCE graph) and reconciliation vs active structure modules",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser(
        "findings",
        help="S7: feedback-loop finding ranking (decisions dismiss history downweights"
        " a kind by 0.5^n - downweight reorders, never deletes); --dismiss records"
        " the gesture (decisions table, findings row untouched)",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--limit", type=int, default=feedback_mod.DEFAULT_LIMIT, help="max findings shown (summary.total stays true)")
    p.add_argument("--dismiss", type=int, help="dismiss a finding by id (feedback gesture; downweights its kind)")
    p.add_argument("--note", help="optional decision note recorded with --dismiss")
    p.set_defaults(func=cmd_findings)

    p = sub.add_parser(
        "cochange",
        help="R2: mine implicit co-change coupling from git history (--no-merges"
        " co-occurrence; formulas open: support/confidence per Tornhill temporal"
        " coupling) into co_changes (idempotent DELETE+INSERT full replace,"
        " OWNED across etl); thresholds filter the display, the table keeps all pairs",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--min-support", type=int, default=cochange_mod.DEFAULT_MIN_SUPPORT, help="display filter: min co-occurrence count (>= boundary, default 3)")
    p.add_argument("--min-confidence", type=float, default=cochange_mod.DEFAULT_MIN_CONFIDENCE, help="display filter: min confidence in either direction (>= boundary, default 0.3)")
    p.add_argument("--code-only", action="store_true", help="display filter: exclude pairs where either file is a non-code artifact (docs/data/config/assets/archives - .md/.json/.zip etc) - code-code implicit coupling focus")
    p.set_defaults(func=cmd_cochange)

    p = sub.add_parser(
        "timeline",
        help="A1: git history sampled replay -> rot timeline"
        " (.vrp/timeline.json, detached worktree serial advance,"
        " J8 incremental ETL chain)",
    )
    p.add_argument("repo")
    p.add_argument("--samples", type=int, default=timeline_mod.DEFAULT_SAMPLES, help="sample count (first+last commit always included, default 20)")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override (default repo dir name; timeline uses dedicated vrp-timeline-* cache db)")
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser(
        "review",
        help="T9: SARIF -> GitHub review comments JSON (local artifact"
        " .vrp/report/review-comments.json, zero server-side; one result ="
        " one comment, side RIGHT, line-anchored)",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--sarif", help="SARIF path (default <repo>/.vrp/report/findings.sarif)")
    p.add_argument("--limit", type=int, help="max comments written (summary stays true)")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser(
        "score",
        help="S6: finding priority scoring (formula open and explainable -"
        " every entry ships its components; score = w*impact + w*evidence"
        " - w*span where impact=log1p(anchor in-degree), evidence=log1p(distinct"
        " kinds on the same anchor), span=log1p(affected lines) as the repair"
        " cost proxy; weights configurable via --weights JSON, read-only)",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--limit", type=int, default=scoring_mod.DEFAULT_LIMIT, help="max findings shown (summary.total stays true)")
    p.add_argument("--weights", help='JSON object overriding default weights, e.g. \'{"impact": 2.0}\' (keys: impact, evidence, span)')
    p.set_defaults(func=cmd_score)

    p = sub.add_parser(
        "complexity",
        help="R3: complexity metrics v1 py/ts (CC per radon + cognitive"
        " complexity per SonarQube whitepaper simplified - nesting penalty,"
        " flat elif/else, per-operator logic, direct recursion; MI/Halstead"
        " honestly not done; py=stdlib ast, ts=sandbox typescript real AST"
        " with honest unavailable degrade; read-only, no facts.db)",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--cc-threshold", type=int, default=complexity_mod.CC_THRESHOLD, help="CC alarm line (default 10, radon/lizard convention)")
    p.add_argument("--limit", type=int, default=complexity_mod.DEFAULT_LIMIT, help="max functions shown (summary stays true)")
    p.set_defaults(func=cmd_complexity)

    p = sub.add_parser(
        "pitindex",
        help="F16: pitfall->test anti-recurrence index - parse"
        " docs/踩坑历史.md numbered entries, extract test_* tokens as"
        " locking refs, verify against pytest collection; GAPS (pitfall"
        " without a locking test) are info, DRIFT (cited test no longer"
        " exists) is a red face; report-only, rc=0",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_pitindex)

    p = sub.add_parser(
        "dossier-diff",
        help="A2: dossier diff report - recursive reconciliation of two"
        " dossier.json runs -> added/removed/changed list (release notes"
        " material); list alignment by identity key (fid/symbol/path...) so"
        " a prepend does not fake a whole-column changed; generated_at"
        " excluded (timestamp noise); pure stdlib, no deepdiff",
    )
    p.add_argument("old", help="previous dossier.json path")
    p.add_argument("new", help="current dossier.json path")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_dossier_diff)

    p = sub.add_parser(
        "scaffold",
        help="D10: strangler scaffold generator (propose's natural extension;"
        " Martin Fowler strangler-fig - migration order edge-before-core by"
        " cross-module edge count; Python first, other engines later):"
        " module skeletons + re-export shim templates + MIGRATION.md from"
        " .vrp/proposals/structure-proposal.json into .vrp/scaffold/;"
        " never touches user source files (adoption is manual)",
    )
    p.add_argument("repo")
    p.add_argument("--proposal", help="proposal path (default: <repo>/.vrp/proposals/structure-proposal.json)")
    p.add_argument("--out", help="output directory (default: <repo>/.vrp/scaffold)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_scaffold)

    p = sub.add_parser(
        "tour",
        help="T11: newcomer code tour from curated structure (curate consumer"
        " surface; VS Code Code Tour + Diataxis tutorial as form references;"
        " output product, not an operation protocol): per-module one-line"
        " curated responsibility + suggested reading order (layer topo -"
        " depended-on foundations first, then ascending symbol count -"
        " small modules first); top files + representative symbols as"
        " concrete anchors",
    )
    p.add_argument("repo")
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
        help="M16: curate artifact three-way merge (git merge base/ours/theirs"
        " semantics; OT conflict classification: non-conflicts auto-merge,"
        " conflicts marked for human adjudication): merge an external agent's"
        " curate payload (a directory with slices.json/structure.json, or a"
        " single JSON file) into facts.db; ours=active artifacts, base="
        " curate_base pre-overwrite snapshots (auto-maintained by ingest);"
        " conflicts recorded in decisions (kind=curate_merge) with ours kept"
        " - nothing is silently lost; rc=1 when conflicts await adjudication",
    )
    p.add_argument("repo")
    p.add_argument("--theirs", required=True, help="their payload: directory (slices.json/structure.json) or single JSON file")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_curate_merge)

    p = sub.add_parser(
        "groups",
        help="S5: group findings by root-cause fingerprint (Sentry grouping"
        " semantics; unordered fid pair normalized - cycles a->b/b->a and"
        " clone families share one key; grouping never deletes, read-only)",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--limit", type=int, default=grouping_mod.DEFAULT_LIMIT, help="max groups shown (summary stays true)")
    p.set_defaults(func=cmd_groups)

    p = sub.add_parser(
        "profile",
        help="R4: repo descriptive statistics (gini of in/out degree and"
        " file-symbol distributions, top1%%/top10%% concentration as the honest"
        " power-law proxy, edge density, fan-in/out hubs; read-only)",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser(
        "doclint",
        help="P23: living-doc drift check - <!-- doclint {...} --> markers in"
        " markdown claim numbers, truth sources (glob / recount /"
        " pytest-collect) recompute them; rc=1 on drift; read-only",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_doclint)

    p = sub.add_parser(
        "doctor",
        help="I4: toolchain environment self-diagnosis - aggregates the"
        " per-adapter find_* discovery (single source of truth) into"
        " core (python/git/node/CBM, rc=1 if missing) vs engine tiers"
        " (21-engine externals + scc, missing = degraded capability only);"
        " git/node get a cheap --version probe; read-only, no repo arg",
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "explain",
        help="I5: error-code lookup - gate/G0-G6 transact gate codes"
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
        help="J10: pipeline performance regression tracking - 'vrp run'"
        " auto-records per-stage timings (precheck/cbm_index/etl/xcheck)"
        " to .vrp/perf.jsonl; this reports last-vs-previous per-stage delta;"
        " regression flag = double gate (+25%% AND +1.0s, small-base noise"
        " immune); single-sample honest note; rc always 0 (report, not gate)",
    )
    p.add_argument("repo")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_perf)

    p = sub.add_parser(
        "relnotes",
        help="K11: release-notes draft generator - commit-range '#NNN:'"
        " subjects x handover-entry cross-check (drift warnings), numbers"
        " live-verified at generation time (pytest-collect via doclint,"
        " s7 glob language count - no snapshot numbers, #153 lesson);"
        " prints a markdown draft for human review; date-injectable so"
        " same input = byte-identical draft",
    )
    p.add_argument("repo")
    p.add_argument("--from", dest="from_ref", required=True, help="starting ref/tag (exclusive)")
    p.add_argument("--to", dest="to", default="HEAD", help="ending ref (default HEAD)")
    p.add_argument("--version", default=None, help="version header (e.g. v0.1.3)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_relnotes)

    p = sub.add_parser(
        "smoke",
        help="K12: cross-repo release smoke - auto-discovers verify/ repos"
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
        help="L14: CBM cache GC (npm cache clean shape) - audit all cached"
        " dbs (provenance via store_meta/vrp_repo_path, repo liveness check,"
        " size/nodes); repo-missing = the only auto GC candidate"
        " (unknown-source/unreadable reported but never auto-collected,"
        " #77 lesson); --dry-run lists would-delete + would-free bytes;"
        " --yes actually deletes (db + -wal/-shm sidecars, per-file failures"
        " -> rc=1)",
    )
    p.add_argument("--cache-dir", dest="cache_dir", default=None, help="CBM cache dir (default: s1_cbm.cache_dir())")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="list what --yes would delete, no disk changes")
    p.add_argument("--yes", action="store_true", help="confirm: actually delete GC candidates")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser(
        "flakes",
        help="L15: flake registry - recurrence tracking, shake-king list"
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
        help="N18: language capability matrix - rows = 21 s7 engine langs"
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
        help="N19: CALLS edge args x callee signature reconciliation -"
        " caller face = edge properties args[{i,e,k,v}] (CBM verbatim),"
        " callee face = symbols.signature (schema v3; the signature STRING"
        " is the only trusted base - CBM param_count counts type-annotated"
        " params only, ~21%% disagreement measured, reference not base);"
        " arity: required <= n_args <= total (positional+keyword; default-"
        " valued / ts-'?' params optional, *args suppresses the upper"
        " bound, **kwargs suppresses the kwarg-name check); unknown_kwarg"
        " = keyword key not among signature names; Method leading self/cls"
        " stripped; builtins.* and no-signature callees skipped and"
        " counted (no guessing); report not gate (mdcode/langmatrix"
        " precedent, rc always 0)",
    )
    p.add_argument("repo", help="repo root (must contain .vrp/facts.db)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_argcheck)

    p = sub.add_parser(
        "mdcode",
        help="N20: markdown code-block extraction - fenced blocks -> symbol"
        " extractors (py=stdlib ast true parse, ts/js=regex light) ->"
        " name+label match against facts.db symbols -> drift candidates"
        " (doc-only = renamed/removed/fictional API; kind-mismatch on the"
        " clear py face, Method~=Function same family; ts is name-only -"
        " label vocab unpinned, honest boundary); report not gate"
        " (langmatrix precedent, rc always 0); name-level match (params"
        " not in facts.db), common-name collisions possible - noted;"
        " excludes reuse watch.EXCLUDE_DIRS single source",
    )
    p.add_argument("repo", help="repo root (must contain .vrp/facts.db)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_mdcode)

    p = sub.add_parser(
        "etl-health",
        help="BB4: ETL health face - query-based invariant checklist over"
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
    p.add_argument("repo", help="repo root (must contain .vrp/facts.db)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_etl_health)

    p = sub.add_parser(
        "layermetrics",
        help="C8: layer metrics & thresholds (JDepend D-metric half-face"
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
    p.add_argument("repo", help="repo root (must contain .vrp/facts.db)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_layermetrics)

    p = sub.add_parser(
        "sweep",
        help="S8: threshold sensitivity scan (validation-curve shape"
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
    p.add_argument("repo", help="repo root (must contain .vrp/facts.db)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser(
        "migprog",
        help="D11: migration progress tracking - cross-seam edge"
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
    p.add_argument("repo", help="repo root (must contain .vrp/facts.db)")
    p.add_argument("--snapshot", action="store_true", help="append current seams snapshot to history")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_migprog)

    p = sub.add_parser(
        "xcheck-explain",
        help="Q25: xcheck finding narratization (rustc --explain shape:"
        " phenomenon -> cause -> fix) - reads discrepancy_ledger four"
        " categories + meta xcheck_* from facts.db; per-category counts,"
        " top-3 examples, low-agreement overall line (<60pct flagged LOW);"
        " real-confusion-case prerequisite met (27.7pct agreement case,"
        " xcheck.py:103); read-only, rc always 0 (explanation is not a"
        " gate); 'xcheck not run yet' honest message when ledger empty",
    )
    p.add_argument("repo", help="repo root (must contain .vrp/facts.db)")
    p.set_defaults(func=cmd_xcheck_explain)

    p = sub.add_parser(
        "secrets",
        help="H1: standalone secrets scan (gitleaks shape; detector single-"
        "source from render_chain - zero detection-logic copies, trufflehog"
        " live-verification NOT done - detect only): worktree walk"
        " (IGNORE_DIRS pruned, binary/oversize skips counted) + optional"
        " --history full-blob scan via streaming cat-file --batch (secrets"
        " in deleted files still found; oversize blobs skipped+counted);"
        " read-only stdout report (SECRETS.local.md manifest stays in the"
        " render chain); rc=1 on hits (CI-usable, gitleaks semantics)",
    )
    p.add_argument("repo", help="repository path to scan")
    p.add_argument("--history", action="store_true", help="also scan all git history blobs (slow face, explicit opt-in)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_secrets)

    p = sub.add_parser("index", help="S1: CBM index_repository")
    p.add_argument("repo")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override")
    p.add_argument("--reindex", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("etl", help="S1.5: CBM SQLite -> facts.db")
    p.add_argument("repo")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override (if indexed under a different name)")
    p.add_argument("--full", action="store_true", help="J8: force full rebuild (skip the incremental patch path)")
    p.set_defaults(func=cmd_etl)

    p = sub.add_parser("xcheck", help="S1: code-atlas cross-validation -> discrepancy_ledger")
    p.add_argument("repo")
    p.add_argument("--reindex", action="store_true", help="re-run code-atlas index")
    p.set_defaults(func=cmd_xcheck)

    p = sub.add_parser("run", help="S0 -> S1 -> S1.5 -> xcheck end-to-end")
    p.add_argument("repo")
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

    p = sub.add_parser("s2", help="S2: ast-grep pathology scan -> findings")
    p.add_argument("repo")
    p.set_defaults(func=cmd_s2)

    p = sub.add_parser("mutate-plan", help="S2 P8: mutation budget sampler (test_linkage pre-filter)")
    p.add_argument("repo")
    p.add_argument("--budget", type=int, default=50)
    p.set_defaults(func=cmd_mutate_plan)

    p = sub.add_parser("mutate-run", help="S2 P8: execute mutations via mutmut, reconcile against plan (survivors -> P8 findings + runtime evidence)")
    p.add_argument("repo")
    p.add_argument("--budget", type=int, default=10)
    p.add_argument("--timeout", type=int, default=3600, help="overall mutmut timeout (s)")
    p.set_defaults(func=cmd_mutate_run)

    p = sub.add_parser(
        "enforce",
        help="S7: enforce curate-ingested structure via import-linter (violations -> P9 findings)",
    )
    p.add_argument("repo")
    p.add_argument("--timeout", type=int, default=600, help="lint-imports timeout (s)")
    p.set_defaults(func=cmd_enforce)

    p = sub.add_parser("validate", help="S3: anchor revalidation (active/cosmetic/stale) + coverage -> gate_results")
    p.add_argument("repo")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("gate", help="S3: evidence gate merge of AI claims (quote-or-rejected)")
    p.add_argument("repo")
    p.add_argument("claims", help="JSON file with a claims array")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("freeze", help="S4: run discovered commands -> runtime_evidence + CBM graph baseline")
    p.add_argument("repo")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override")
    p.add_argument("--timeout", type=int, default=900, help="per-command timeout (s)")
    p.add_argument("--skip-commands", action="store_true")
    p.add_argument("--skip-graph", action="store_true", help="skip CBM persistence index + detect_changes")
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser(
        "dossier",
        help="S5: assemble dossier (six sections, task-typed budget profile)",
    )
    p.add_argument("repo")
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
        help="built-in LLM curation loop (zero-agent path; config in .vrp/config.json)",
    )
    p.add_argument("repo")
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
        help="C9: LLM fix proposal for one finding -> .vrp/proposals/fix-<n>.json (transact draft;"
        " execution only via 'vrp transact' - the draft is never executed here)",
    )
    p.add_argument("repo")
    p.add_argument("finding_id", type=int, help="findings.id to fix (must have file_path)")
    p.add_argument("--model", default="", help="override config llm.model for this run")
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing proposal file (default: refuse to protect reviewed drafts)",
    )
    p.set_defaults(func=cmd_fixpropose)

    p = sub.add_parser(
        "transact",
        help="S6: execute refactor transaction (edits -> G1-G5 gates -> git commit or rollback)",
    )
    p.add_argument("repo")
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
        help="S6: run every transaction JSON in a directory in sorted order (stop at first non-committed)",
    )
    p.add_argument("repo")
    p.add_argument("tx_dir", help="directory containing transaction JSON files (*.json)")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override")
    p.add_argument("--confirm", action="store_true", help="confirm T4 transactions")
    p.add_argument("--timeout", type=int, default=900, help="per-command timeout for gate commands")
    p.add_argument("--continue-on-fail", action="store_true", help="keep running after a non-committed transaction")
    p.set_defaults(func=cmd_transact_batch)

    p = sub.add_parser(
        "mcp",
        help="AI interface: MCP server over stdio (per-repo; requires pip install vrp[mcp])",
    )
    p.add_argument("repo")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("verdicts", help="S6: list transaction verdicts from .vrp/verdicts (open ones feed back into dossier evidence)")
    p.add_argument("repo")
    p.add_argument("--tid", help="show the full verdict JSON for one transaction id")
    p.set_defaults(func=cmd_verdicts)

    p = sub.add_parser(
        "curate",
        help="S2.5: curate AI combing artifacts (.vrp/ai/slices.json + structure.json -> validate -> ingest; --reject to veto)",
    )
    p.add_argument("repo")
    p.add_argument("--reject", help="veto an ingested artifact: a slice_id or the literal 'structure'")
    p.add_argument("--note", help="optional decision note recorded with --reject")
    p.set_defaults(func=cmd_curate)

    p = sub.add_parser(
        "drift",
        help="S2.5: unified drift report for curate artifacts (missing fids/edges + relocation candidates; read-only)",
    )
    p.add_argument("repo")
    p.add_argument("--history", type=int, default=0, metavar="N",
                   help="also print the last N drift_history revalidation events (A3 data-accumulation face; 0 = off)")
    p.set_defaults(func=cmd_drift)

    p = sub.add_parser(
        "remap",
        help="S2.5: apply drift remap proposals (unique qualified_name candidates) back to .vrp/ai/*.json working files",
    )
    p.add_argument("repo")
    p.set_defaults(func=cmd_remap)

    p = sub.add_parser(
        "seams",
        help="#143: cross-module seam aggregation over the active structure (read-only; unwind seams; "
        "module dependency graph raw material)",
    )
    p.add_argument("repo")
    p.set_defaults(func=cmd_seams)

    p = sub.add_parser(
        "propose",
        help="#143: deterministic structure partition proposal (unwind slices propose; top-dir seeds + "
        "edge-weight greedy merge + balance cap; writes .vrp/proposals/ NOT .vrp/ai/ - adopt via curate)",
    )
    p.add_argument("repo")
    p.add_argument("--out", help="output file (default: <repo>/.vrp/proposals/structure-proposal.json)")
    p.add_argument("--target-modules", type=int, default=None, help="target module count (default 8)")
    p.add_argument("--balance-cap", type=float, default=None,
                   help="max merged module share of candidates (default 0.6)")
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser(
        "traces",
        help="#144: ingest runtime call traces -> runtime_calls + observed-vs-static agreement "
        "(CBM ingest_traces; qn-keyed reindex-proof, per-label whole-run replace; external producers)",
    )
    p.add_argument("repo")
    p.add_argument("--file", required=True, help="traces JSON file ({traces: [path|caller/callee], label})")
    p.add_argument("--label", help="run label override (default: file 'label' field or 'unlabeled')")
    p.set_defaults(func=cmd_traces)

    p = sub.add_parser(
        "testcov",
        help="B4: ingest coverage.py JSON report (pytest --cov external producer, zero-dep)"
        " -> test_coverage (file-path-keyed OWNED table, per-label whole-run replace)"
        " + test_linkage-vs-coverage reconciliation (def-line excluded;"
        " linked_not_covered_measured = 'statically linked but never executed' false-linkage signal)",
    )
    p.add_argument("repo")
    p.add_argument("--file", required=True, help="coverage JSON file ({files: {path: {executed_lines, missing_lines}}})")
    p.add_argument("--label", help="run label (default 'unlabeled')")
    p.set_defaults(func=cmd_testcov)

    p = sub.add_parser(
        "golden",
        help="F15+BB3: audit findings/symbols against committed golden truth JSON"
        " (vrp-golden-1; violations multiset match + symbols scoped exact enum;"
        " precision/recall honest-null; rc always 0 - measurement, not a gate)",
    )
    p.add_argument("repo")
    p.add_argument("--file", required=True, help="golden truth JSON file (vrp-golden-1 format)")
    p.set_defaults(func=cmd_golden)

    p = sub.add_parser(
        "coverage",
        help="#145: index completeness check (CBM check_index_coverage; read-only; "
        "parse_partial/parse_unusable/not_indexed gap inventory + per-path status)",
    )
    p.add_argument("repo")
    p.add_argument("--path", dest="paths", action="append",
                   help="path to verify (repeatable, cap 128; exact + ancestor match, per-path status)")
    p.add_argument("--kind", dest="kinds", action="append", help="kind filter (repeatable; e.g. parse_partial)")
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser(
        "impact",
        help="#146: change impact + suggested tests (CBM detect_changes/test_impact; read-only): "
        "worktree diff -> hunk-overlap seeding -> multi-source BFS blast radius "
        "-> test_linkage consumption (suggested != sufficient)",
    )
    p.add_argument("repo")
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
        help="S0 -> S1 -> S1.5 -> xcheck -> drift -> remap -> curate -> report (one-shot drift recovery loop)",
    )
    p.add_argument("repo")
    p.add_argument("--reindex", action="store_true", help="force CBM re-index")
    p.add_argument("--cbm-name", "--name", dest="cbm_name", help="CBM project name override")
    p.add_argument("--out", help="report output directory (default: <repo>/.vrp/report)")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("status", help="facts.db summary")
    p.add_argument("repo")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "report",
        help="S8: static report -> .vrp/report/ (report.html + IR + SARIF + Mermaid; archify optional)",
    )
    p.add_argument("repo")
    p.add_argument("--out", help="output directory (default: <repo>/.vrp/report)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser(
        "session",
        help="R3.5': trust-mode session (begin/end/report; initial vs final delivery, design doc v7 §3.3)",
    )
    p.add_argument("repo")
    p.add_argument("action", choices=["begin", "end", "report"])
    p.add_argument("--note", help="session note recorded in session.json")
    p.add_argument("--id", dest="id", help="session id for report (default: latest ended)")
    p.add_argument("--out", help="report output dir (default: the session dir)")
    p.set_defaults(func=cmd_session)
    return parser


def main(argv: list[str] | None = None) -> int:
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
