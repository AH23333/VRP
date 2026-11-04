"""vrp CLI: precheck | index | etl | xcheck | run | status."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import __version__
from . import etl as etl_mod
from . import s0_precheck, s1_cbm, s2_astgrep, s2_deadcode, s2_jscpd, s2_mutate, s2_mutate_run, s2_p4, s3, s4_freeze, s5_dossier, s6_transact, xcheck


def _repo_arg(value: str) -> Path:
    repo = Path(value).resolve()
    if not repo.is_dir():
        raise SystemExit(f"not a directory: {repo}")
    return repo


def cmd_precheck(args) -> int:
    repo = _repo_arg(args.repo)
    result = s0_precheck.precheck(repo)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_index(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.name or repo.name
    result = s1_cbm.index_repository(repo, name, reindex=args.reindex)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_etl(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.cbm_name or repo.name
    result = etl_mod.run_etl(repo, name)
    printable = {k: v for k, v in result.items()}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0


def cmd_xcheck(args) -> int:
    repo = _repo_arg(args.repo)
    facts_db = repo / ".vrp" / "facts.db"
    if not facts_db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp etl' first: {facts_db}")
    result = xcheck.run_xcheck(repo, facts_db, reindex=args.reindex)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_run(args) -> int:
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

    print(f"\nfacts.db: {repo / '.vrp' / 'facts.db'}")
    return 0


def cmd_s2(args) -> int:
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = sqlite3.connect(facts_path)
    try:
        result = s2_astgrep.scan(con, repo)
        dead = s2_deadcode.scan(con)
        p4 = s2_p4.scan(con, repo)
        p2 = s2_jscpd.scan(con, repo)
    finally:
        con.close()
    result["dead_code_candidates"] = dead["total"]
    result["p4"] = p4
    result["p2"] = p2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_mutate_plan(args) -> int:
    repo = _repo_arg(args.repo)
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = sqlite3.connect(f"file:{facts_path}?mode=ro", uri=True)
    try:
        targets = s2_mutate.mutation_targets(con, args.budget)
    finally:
        con.close()
    out = s2_mutate.write_plan(repo, targets)
    print(json.dumps({"budget": args.budget, "selected": len(targets), "plan": str(out)}, ensure_ascii=False, indent=2))
    return 0


def cmd_mutate_run(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        result = s2_mutate_run.run(repo, con, args.budget, args.timeout)
    finally:
        con.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _open_facts_rw(repo: Path):
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = sqlite3.connect(facts_path)
    etl_mod.ensure_schema(con)
    etl_mod.check_facts_freshness(con, repo)
    return con, facts_path


def cmd_validate(args) -> int:
    repo = _repo_arg(args.repo)
    con, _ = _open_facts_rw(repo)
    try:
        anchors = s3.validate_anchors(con, repo)
        cov = s3.coverage(con)
    finally:
        con.close()
    result = {"anchors": anchors, "coverage": {"total": cov["total"], "annotated": cov["annotated"]}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
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
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_freeze(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.name or repo.name
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
    name = args.name or repo.name
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


def cmd_transact(args) -> int:
    repo = _repo_arg(args.repo)
    if args.preview:
        preview = s6_transact.preview_transaction(repo, Path(args.transaction))
        _print_step(f"S6 transact preview {preview['type']}", preview)
        return 0 if preview["ok"] else 1
    name = args.name or repo.name
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
        print(json.dumps(match, ensure_ascii=False, indent=2, default=str))
        return 0
    summary = [
        {k: v.get(k) for k in ("tid", "type", "status", "failed_gate", "confidence", "verdict_at")}
        for v in verdicts
    ]
    _print_step("S6 verdicts", {"count": len(summary), "verdicts": summary})
    return 0


def cmd_transact_batch(args) -> int:
    repo = _repo_arg(args.repo)
    name = args.name or repo.name
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


def cmd_status(args) -> int:
    repo = _repo_arg(args.repo)
    facts_db = repo / ".vrp" / "facts.db"
    if not facts_db.exists():
        print(json.dumps({"facts_db": None}))
        return 0
    con = sqlite3.connect(f"file:{facts_db}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
        counts = {}
        for table in ("symbols", "edges", "usage_hints", "history_coupling", "test_linkage", "coverage_gaps", "findings", "discrepancy_ledger", "file_hashes"):
            counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        by_evidence = dict(con.execute("SELECT evidence, COUNT(*) FROM edges GROUP BY evidence"))
    finally:
        con.close()
    print(json.dumps({"meta": meta, "counts": counts, "edges_by_evidence": by_evidence}, ensure_ascii=False, indent=2))
    return 0


def _print_step(title: str, payload: dict) -> None:
    print(f"== {title} ==")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vrp", description="vibe-restruct pipeline")
    parser.add_argument("--version", action="version", version=f"vrp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("precheck", help="S0: git snapshot + language census + command discovery")
    p.add_argument("repo")
    p.set_defaults(func=cmd_precheck)

    p = sub.add_parser("index", help="S1: CBM index_repository")
    p.add_argument("repo")
    p.add_argument("--name", help="CBM project name override")
    p.add_argument("--reindex", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("etl", help="S1.5: CBM SQLite -> facts.db")
    p.add_argument("repo")
    p.add_argument("--cbm-name", help="CBM project name override (if indexed under a different name)")
    p.set_defaults(func=cmd_etl)

    p = sub.add_parser("xcheck", help="S1: code-atlas cross-validation -> discrepancy_ledger")
    p.add_argument("repo")
    p.add_argument("--reindex", action="store_true", help="re-run code-atlas index")
    p.set_defaults(func=cmd_xcheck)

    p = sub.add_parser("run", help="S0 -> S1 -> S1.5 -> xcheck end-to-end")
    p.add_argument("repo")
    p.add_argument("--reindex", action="store_true", help="force CBM re-index")
    p.add_argument("--cbm-name", help="CBM project name override")
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

    p = sub.add_parser("validate", help="S3: anchor revalidation (active/cosmetic/stale) + coverage -> gate_results")
    p.add_argument("repo")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("gate", help="S3: evidence gate merge of AI claims (quote-or-rejected)")
    p.add_argument("repo")
    p.add_argument("claims", help="JSON file with a claims array")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("freeze", help="S4: run discovered commands -> runtime_evidence + CBM graph baseline")
    p.add_argument("repo")
    p.add_argument("--name", help="CBM project name override")
    p.add_argument("--timeout", type=int, default=900, help="per-command timeout (s)")
    p.add_argument("--skip-commands", action="store_true")
    p.add_argument("--skip-graph", action="store_true", help="skip CBM persistence index + detect_changes")
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser(
        "dossier",
        help="S5: assemble dossier (six sections, task-typed budget profile)",
    )
    p.add_argument("repo")
    p.add_argument("--name", help="CBM project name override")
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
        "transact",
        help="S6: execute refactor transaction (edits -> G1-G5 gates -> git commit or rollback)",
    )
    p.add_argument("repo")
    p.add_argument("transaction", help="transaction JSON file path")
    p.add_argument("--name", help="CBM project name override")
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
    p.add_argument("--name", help="CBM project name override")
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

    p = sub.add_parser("status", help="facts.db summary")
    p.add_argument("repo")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
