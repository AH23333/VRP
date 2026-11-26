"""终端原生 AI 分析（`vrp analyze`）——单轮 LLM 结构化分析报告（零 agent 路径）。

诚实边界（记档）：
- 材料面确定：dossier T1（skip_external=True）+ findings top 20 均确定性派生自
  facts.db——同 facts.db 同字节（材料装配零随机、零时间戳：dossier 的
  generated_at/out 元字段不进材料）；总预算 MATERIAL_BUDGET 超出时确定性
  硬截断并计数记档（material.json stats）；
- LLM 响应面非确定：不承诺报告字节级复现——每次调用的 prompt/response/
  usage 全量留痕 .vrp/analyze/run-<UTC>/ 可审计；
- facts 缺失/陈旧复用 etl.check_facts_freshness（M4 硬闸同源判定），自动重建
  S0→S1→ETL→xcheck（逐阶段一行进度），失败诚实 SystemExit；
- timeline 仅 git 仓 + --with-timeline 才附加（20 采样回放过重，默认不跑）；
- key 任何形态不落盘（LlmConfig.echo 掩码，auto_curate 同型）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import etl as etl_mod
from . import feedback as feedback_mod
from . import s0_precheck, s1_cbm, s5_dossier, xcheck
from . import timeline as timeline_mod
from .llm_client import chat as _default_chat
from .llm_client import load_config

MATERIAL_BUDGET = 60_000
FINDINGS_TOP = 20
MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = 2.0

_PROMPT_TEMPLATE = f"""You are the terminal analysis AI of the vrp refactoring pipeline.
Everything you know about this codebase comes only from the material below (dossier T1 and findings top {FINDINGS_TOP}, both deterministically derived from facts.db) - never fabricate fid, P-code, or paths that are not in the material.

{{material}}

Based on the material above, produce a structured analysis report (markdown, in English) with exactly these sections:
## Architecture overview
## Main pathologies and evidence
## Risk points
## Recommended actions
Cite fid / P-code from the material as evidence; mark any judgement beyond the material coverage explicitly as "not covered by the material, needs verification"."""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8", newline="\n")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def facts_state(repo: Path) -> tuple[str, str]:
    """facts.db 状态：("fresh", "") | ("stale", detail) | ("missing", path)。

    复用 etl.check_facts_freshness（M4 硬闸同源判定）——本模块零重写；
    缺库在 exists 检查处分岔（check 需要可连的库）。
    """
    repo = Path(repo)
    facts = repo / ".vrp" / "facts.db"
    if not facts.exists():
        return "missing", str(facts)
    con = sqlite3.connect(facts)
    try:
        etl_mod.ensure_schema(con)
        etl_mod.check_facts_freshness(con, repo)
    except RuntimeError as exc:
        return "stale", str(exc)
    finally:
        con.close()
    return "fresh", ""


def run_pipeline(repo: Path, name: str, print_fn=None) -> dict:
    """S0→S1→ETL→xcheck 单次全链（cli._run_pipeline 同型；独立实现避免循环
    import——cli 注册本模块后反向 import 会成环）。

    逐阶段一行进度；失败异常上抛（调用方转诚实 SystemExit）；
    内容仓库（P-2 语义）对代码分析不适用——诚实 SystemExit。
    """
    say = print_fn or print
    repo = Path(repo)
    pre = s0_precheck.precheck(repo)
    say(f"[S0] precheck: {pre['name']} - {pre['repo']}")
    cr = pre.get("content_repo") or {}
    if cr.get("detected"):
        raise SystemExit(
            f"content repository detected (content share {cr.get('content_share_pct')}%) - "
            "code analysis not applicable; vrp analyze needs a code repo"
        )
    s1_cbm.index_repository(repo, name, reindex=False)
    say(f"[S1] cbm index: {name}")
    etl_mod.run_etl(repo, name)
    say(f"[ETL] facts.db: {repo / '.vrp' / 'facts.db'}")
    xcheck.run_xcheck(repo, repo / ".vrp" / "facts.db", reindex=False)
    say("[xcheck] done")
    return {"precheck": pre, "name": name}


def top_findings(con: sqlite3.Connection, limit: int = FINDINGS_TOP) -> dict:
    """findings top N（P 码/分数/分量）——analyze 材料与 chat /findings 共用。"""
    ranked = feedback_mod.rank_findings(con)
    top = ranked["findings"][: max(limit, 0)]
    return {
        "total": ranked["summary"]["total"],
        "shown": len(top),
        "findings": [
            {
                "id": e["id"],
                "pathology": e["pathology"],
                "kind": e["kind"],
                "source": e["source"],
                "anchor_fid": e["anchor_fid"],
                "file_path": e["file_path"],
                "start_line": e["start_line"],
                "end_line": e["end_line"],
                "score": e["score"],
                "components": e["components"],
                "weight": e["weight"],
            }
            for e in top
        ],
    }


def build_material_parts(repo: Path, with_timeline: bool = False) -> dict:
    """材料装配（确定性面）：dossier T1 + findings top N（+ timeline 可选）。

    dossier 的 generated_at/out 元字段剔除——材料面同 facts.db 同字节；
    timeline 仅 git 仓 + with_timeline 才跑（20 采样回放过重），非 git 诚实 skip。
    """
    repo = Path(repo)
    facts = repo / ".vrp" / "facts.db"
    dossier = s5_dossier.build_dossier(
        repo, facts, repo.name, budget=s5_dossier.DEFAULT_BUDGET, task="T1", skip_external=True
    )
    con = sqlite3.connect(facts)
    try:
        findings = top_findings(con)
    finally:
        con.close()
    parts = {
        "dossier_t1": {
            "project": dossier["project"],
            "task": "T1",
            "views": dossier["views"],
        },
        "findings_top": findings,
    }
    if with_timeline:
        if etl_mod.current_git_head(repo):
            tl = timeline_mod.run_timeline(repo)
            parts["timeline"] = {
                "samples_requested": tl.get("samples_requested"),
                "commits_total": tl.get("commits_total"),
                "points": tl.get("points"),
                "turning_points": tl.get("turning_points"),
            }
        else:
            parts["timeline"] = {"status": "skipped", "reason": "not a git checkout"}
    return parts


def assemble_material_text(parts: dict, cap: int = MATERIAL_BUDGET) -> tuple[str, dict]:
    """材料文本装配 + 总预算 cap：超出确定性硬截断 + 截断计数记档。"""
    sections = []
    for name in sorted(parts):  # 确定性序
        sections.append(f"### {name}\n" + json.dumps(parts[name], ensure_ascii=False, default=str))
    full = "\n\n".join(sections)
    truncated_chars = max(0, len(full) - cap)
    text = full[:cap] if truncated_chars else full
    stats = {
        "material_chars": len(text),
        "material_bytes": len(text.encode("utf-8")),
        "full_chars": len(full),
        "truncated_chars": truncated_chars,
        "truncated": bool(truncated_chars),
    }
    return text, stats


def run_analyze(
    repo: Path,
    with_timeline: bool = False,
    chat_fn=None,
    run_pipeline_fn=None,
    sleep_fn=None,
    print_fn=None,
) -> dict:
    """`vrp analyze` 入口：材料装配 → 单轮 LLM → 落盘/留痕；返回 summary（含 rc）。

    chat_fn(config, messages) -> {"content", "finish_reason", "usage"} 可注入（测试）；
    run_pipeline_fn(repo, name, print_fn) 可注入（测试 mock 管道）；
    sleep_fn 注入替 time.sleep（重试退避测试）。
    """
    repo = Path(repo)
    say = print_fn or print
    try:
        config = load_config(repo)
    except ValueError as exc:
        # 人话引导（消息语言纪律：exception 面与 print 面统一英文）——
        # G2：wizard 引导前置，手动模板（exc 内 JSON）退居其后
        raise SystemExit(f"LLM not configured: run 'vrp config' first to configure the LLM API (.vrp/config.json)\n{exc}")
    call = chat_fn or _default_chat
    pause = time.sleep if sleep_fn is None else sleep_fn
    state, _detail = facts_state(repo)
    if state != "fresh":
        say(f"facts {'missing' if state == 'missing' else 'stale'} - rebuilding automatically (S0->S1->ETL->xcheck)...")
        run = run_pipeline_fn or run_pipeline
        try:
            run(repo, repo.name, print_fn=say)
        except Exception as exc:  # SystemExit 为 BaseException 不在此列，原样上抛
            raise SystemExit(f"facts rebuild failed (S0->S1->ETL->xcheck): {exc}") from exc
        state2, detail2 = facts_state(repo)
        if state2 != "fresh":
            raise SystemExit(
                f"facts still {'missing' if state2 == 'missing' else 'stale'} after rebuild: {detail2}"
            )
    started_at = _utc_now()
    parts = build_material_parts(repo, with_timeline=with_timeline)
    material_text, stats = assemble_material_text(parts)
    # 审计目录（auto_curate 同型）：run-<UTC>，同秒冲突 -2/-3 后缀
    base_id = f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    run_id = base_id
    n = 2
    while (repo / ".vrp" / "analyze" / run_id).exists():
        run_id = f"{base_id}-{n}"
        n += 1
    audit = repo / ".vrp" / "analyze" / run_id
    audit.mkdir(parents=True)  # 新目录：竞争窗口抢注时响亮 FileExistsError
    _write_json(audit / "config.echo.json", config.echo())
    _write_json(audit / "material.json", {"stats": stats, "parts": parts})
    prompt = _PROMPT_TEMPLATE.format(material=material_text)
    _write_text(audit / "prompt.txt", prompt)
    messages = [{"role": "user", "content": prompt}]
    attempts: list[dict] = []
    content = ""
    finish_reason = None
    usage: dict = {}
    ok = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = call(config, messages)
        except ValueError as exc:
            attempts.append({"attempt": attempt, "outcome": "request_error", "detail": str(exc)[:500]})
            if attempt < MAX_ATTEMPTS:
                pause(_RETRY_BACKOFF_S * (2 ** (attempt - 1)))
            continue
        finish_reason = response.get("finish_reason")
        usage = response.get("usage") or {}
        if not response["content"].strip():
            fast = finish_reason == "length"
            detail = (
                "llm response is empty - all output tokens consumed before content "
                "(finish_reason=length); raise llm.max_output_tokens in config"
                if fast
                else "llm response content is empty"
            )
            attempts.append({"attempt": attempt, "outcome": "empty_response", "detail": detail})
            if fast:
                # 快速诚实失败（auto_curate 同型）：同预算重试必然同果
                break
            if attempt < MAX_ATTEMPTS:
                pause(_RETRY_BACKOFF_S * (2 ** (attempt - 1)))
            continue
        content = response["content"]
        attempts.append({"attempt": attempt, "outcome": "ok"})
        ok = True
        break
    if not ok:
        summary = {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "model": config.model,
            "with_timeline": bool(with_timeline),
            "material_chars": stats["material_chars"],
            "material_bytes": stats["material_bytes"],
            "truncated_chars": stats["truncated_chars"],
            "findings_total": parts["findings_top"]["total"],
            "findings_shown": parts["findings_top"]["shown"],
            "attempts": attempts,
            "report_path": None,
            "audit_dir": str(audit),
            "rc": 1,
        }
        _write_json(audit / "summary.json", summary)
        last = attempts[-1]["detail"] if attempts else "unknown"
        raise SystemExit(f"LLM call failed after {len(attempts)} attempt(s) without a valid response: {last}")
    _write_text(audit / "response.md", content)
    _write_json(audit / "usage.json", {"finish_reason": finish_reason, "usage": usage})
    report_path = repo / ".vrp" / "analysis.md"
    _write_text(report_path, content)
    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "model": config.model,
        "with_timeline": bool(with_timeline),
        "material_chars": stats["material_chars"],
        "material_bytes": stats["material_bytes"],
        "truncated_chars": stats["truncated_chars"],
        "findings_total": parts["findings_top"]["total"],
        "findings_shown": parts["findings_top"]["shown"],
        "attempts": attempts,
        "report": content,
        "report_path": str(report_path),
        "audit_dir": str(audit),
        "rc": 0,
    }
    _write_json(audit / "summary.json", {k: v for k, v in summary.items() if k != "report"})
    return summary
