"""C9（构想v10）：修复提案闭环——单 finding → LLM 生成 transact 草案（零 agent 路径）。

红线（C9 原文）：修复草案永远走 transact 闸门，不绕 G0-G6——本模块只生产
草案文件，执行只存在于 `vrp transact`（G0-G6 闸门+T4 人工闸 --confirm 照常
生效）；LLM 输出物理上只能变成 .vrp/proposals/fix-<n>.json（不执行/不落
findings/不碰 git/不起子进程）；对事务面的引用仅限 load_transaction 形状
校验器（单一契约源，事务契约 v1）——census 式源码守卫测试钉死
（tests/test_fixpropose.py::test_fixpropose_redline_no_execution_surface）。

循环骨架（auto_curate 同型）：无 function calling——确定性校验器即工具，
拒收回执（稳定码 PROPOSAL_*）驱动重试 ≤MAX_ATTEMPTS；单 finding 单文件
（edits 只准落 finding.file_path——跨文件修复走 agent 路径）；材料含现文件
全文（事务契约全文覆写语义：生产方必须先读现文件再产全文）；全自动+全
留痕 .vrp/fixpropose/run-<UTC>/（key 任何形态不落盘，LlmConfig.echo 掩码）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import etl as etl_mod
from .llm_client import chat as _default_chat
from .llm_client import extract_json, load_config
from .s6_transact import load_transaction

MAX_ATTEMPTS = 3
MAX_FILE_CHARS = 100_000
PROPOSAL_OPS = ("modify", "delete")

_PROMPT_PREAMBLE = (
    "You are the fix-proposal AI of the vrp restructuring pipeline.\n"
    "You receive ONE finding and the CURRENT FULL content of the file it points at.\n"
    "Your output becomes a transaction DRAFT only - it is never executed by you;\n"
    "a human reviews it and runs it through the vrp transact gates (G0-G6).\n"
    "Respond with exactly ONE JSON object and no other text (no markdown fences, no explanations)."
)

_FIX_CONTRACT = (
    "Produce a transaction draft (vrp transaction contract v1):\n"
    '{"id": "fix-<the finding id>", "type": "T1|T2|T3|T4", "summary": "<one-line>", '
    '"declared_moves": [{"from": "<qn>", "to": "<qn>"}], '
    '"edits": [{"file": "<the finding file>", "op": "modify|delete", "content": "<full new file content>"}]}\n'
    "Validation is deterministic and STRICT:\n"
    '- id MUST be exactly "fix-" + the finding id shown in the material (e.g. finding id 42 -> "fix-42")\n'
    "- every edit.file MUST be the finding file path verbatim (single-file scope: cross-file fixes are out of scope)\n"
    "- edit.op must be modify or delete (create is impossible - the finding file already exists)\n"
    "- modify.content is the COMPLETE new file content (full overwrite, NOT a diff/patch) - "
    "base it on the current content shown in the material; NEVER include line-number prefixes\n"
    "- type: T1 equivalence refactor (rename/move, behavior unchanged; declared_moves must be complete); "
    "T2 cleanup (delete dead/redundant code only); T3 fix (behavior change to fix the finding); "
    "T4 improvement (behavior change adding capability - requires human --confirm)\n"
    "- delete ignores content; declared_moves may be [] for T2/T3/T4\n"
    "If (and only if) the finding genuinely cannot be fixed by editing only this file, "
    "respond instead with an honest unfixable verdict:\n"
    '{"id": "fix-<the finding id>", "unfixable": true, "reason": "<why no single-file fix exists>"}\n'
    "Rejection codes you may receive: PROPOSAL_TX_SHAPE / PROPOSAL_WRONG_ID / PROPOSAL_WRONG_FILE / "
    "PROPOSAL_WRONG_OP / PROPOSAL_UNFIXABLE_SHAPE / FILE_WRONG_SHAPE."
)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8", newline="\n")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def load_finding(con: sqlite3.Connection, finding_id: int) -> dict:
    """按 findings.id 取单条 finding；缺失/无 file_path 诚实拒绝。"""
    row = con.execute(
        "SELECT id, pathology, kind, source, anchor_fid, related_fid,"
        " file_path, start_line, end_line, payload_json"
        " FROM findings WHERE id = ?",
        (finding_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"finding #{finding_id} not found in facts.db")
    finding = {
        "id": row[0],
        "pathology": row[1],
        "kind": row[2],
        "source": row[3],
        "anchor_fid": row[4],
        "related_fid": row[5],
        "file_path": row[6],
        "start_line": row[7],
        "end_line": row[8],
        "payload_json": row[9],
    }
    if not isinstance(finding["file_path"], str) or not finding["file_path"].strip():
        raise ValueError(
            f"finding #{finding_id} has no file_path - fixpropose only serves file-anchored findings "
            "(use the agent path: dossier + transact)"
        )
    return finding


def build_material(repo: Path, finding: dict) -> str:
    """材料面：finding 全字段 + 现文件全文（行号檐口）。

    全文是硬要求（事务契约全文覆写语义）；非 UTF-8 文本/超 cap 文件诚实拒绝
    ——截断喂半文产全文=幻觉温床，宁可拒绝走 agent 路径。
    """
    rel = finding["file_path"].replace("\\", "/")
    target = repo / rel
    if not target.is_file():
        raise ValueError(
            f"finding file missing on disk: {target} (stale facts? re-run 'vrp etl {repo}')"
        )
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"finding file is not valid UTF-8 text - fixpropose only handles text files: {rel}"
        ) from exc
    if len(content) > MAX_FILE_CHARS:
        raise ValueError(
            f"file too large for fixpropose ({len(content)} chars > {MAX_FILE_CHARS}): {rel}"
            " - use the agent path (dossier + transact)"
        )
    try:
        payload = json.loads(finding["payload_json"]) if finding["payload_json"] else None
    except (json.JSONDecodeError, TypeError):
        payload = finding["payload_json"]
    lines = content.splitlines()
    gutter = "\n".join(f"{i}: {line}" for i, line in enumerate(lines, 1))
    parts = [
        f"## Finding to fix (id: {finding['id']})",
        json.dumps(
            {
                "id": finding["id"],
                "pathology": finding["pathology"],
                "kind": finding["kind"],
                "source": finding["source"],
                "file_path": rel,
                "start_line": finding["start_line"],
                "end_line": finding["end_line"],
                "payload": payload,
            },
            ensure_ascii=False,
            default=str,
        ),
        "",
        f"## Current content of {rel} ({len(lines)} lines; line numbers are for orientation only - "
        "the modify content you produce must NOT contain them)",
        gutter if gutter else "(empty file)",
    ]
    return "\n".join(parts)


def validate_draft(draft, draft_path: Path, finding: dict) -> tuple[dict, dict | None]:
    """确定性校验器：事务契约形状（load_transaction 复用）+ 范围三加验 + unfixable 分支。

    返回 (receipt, tx)：receipt {"status": accepted|rejected|unfixable, ...}；
    tx 仅 accepted 时非 None（load_transaction 归一化产物）。
    """
    if isinstance(draft, dict) and draft.get("unfixable") is True:
        reason = draft.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            receipt = {
                "status": "rejected",
                "errors": [
                    {
                        "code": "PROPOSAL_UNFIXABLE_SHAPE",
                        "detail": "unfixable verdict requires a non-empty string reason",
                    }
                ],
            }
        else:
            receipt = {"status": "unfixable", "reason": reason.strip()}
        return receipt, None
    errors: list = []
    tx = None
    try:
        tx = load_transaction(draft_path)
    except ValueError as exc:
        errors.append({"code": "PROPOSAL_TX_SHAPE", "detail": str(exc)[:500]})
    if tx is not None:
        expected_id = f"fix-{finding['id']}"
        if tx["id"] != expected_id:
            errors.append(
                {"code": "PROPOSAL_WRONG_ID", "detail": f"id must be {expected_id!r}, got {tx['id']!r}"}
            )
        target = Path(finding["file_path"].replace("\\", "/"))
        for edit in tx["edits"]:
            if edit["file"].as_posix() != target.as_posix():
                errors.append(
                    {
                        "code": "PROPOSAL_WRONG_FILE",
                        "detail": f"edit.file must be the finding file {target.as_posix()!r}, "
                        f"got {edit['file'].as_posix()!r}",
                    }
                )
            if edit["op"] not in PROPOSAL_OPS:
                errors.append(
                    {
                        "code": "PROPOSAL_WRONG_OP",
                        "detail": f"edit.op must be one of {PROPOSAL_OPS} for fixpropose, got {edit['op']!r}",
                    }
                )
    if errors:
        return {"status": "rejected", "errors": errors}, None
    return {"status": "accepted"}, tx


def _messages(material: str, feedback: str) -> list:
    system = f"{_PROMPT_PREAMBLE}\n\n{_FIX_CONTRACT}"
    user = material
    if feedback:
        user += (
            "\n\n## Previous attempt rejected\n"
            + feedback
            + "\n\nFix the draft per the rejection codes above and submit the corrected JSON."
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _proposal_payload(tx: dict) -> dict:
    """归一化 tx → 可序列化草案（edits.file 转 as_posix；round-trip 过 load_transaction）。"""
    return {
        "id": tx["id"],
        "type": tx["type"],
        "summary": tx["summary"],
        "declared_moves": tx["declared_moves"],
        "edits": [
            {"file": edit["file"].as_posix(), "op": edit["op"], "content": edit["content"]}
            for edit in tx["edits"]
        ],
    }


def run_fixpropose(
    repo: Path,
    finding_id: int,
    model_override: str = "",
    force: bool = False,
    chat_fn=None,
) -> dict:
    """单 finding → LLM 修复提案循环入口；返回 summary dict（含 rc）。

    chat_fn(config, messages) -> {"content", "finish_reason", "usage"} 可注入（测试）。
    配置缺失/非法抛 ValueError；facts.db 缺失抛 SystemExit；M4 过期抛 RuntimeError。
    """
    repo = Path(repo)
    config = load_config(repo)
    if model_override:
        config.model = model_override
    call = chat_fn or _default_chat
    facts = repo / ".vrp" / "facts.db"
    if not facts.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts}")
    con = sqlite3.connect(facts)
    try:
        etl_mod.ensure_schema(con)
        etl_mod.check_facts_freshness(con, repo)
        finding = load_finding(con, finding_id)
    finally:
        con.close()
    material = build_material(repo, finding)
    proposal_path = repo / ".vrp" / "proposals" / f"fix-{finding_id}.json"
    if proposal_path.exists() and not force:
        raise ValueError(
            f"proposal already exists (reviewed drafts are never silently overwritten): {proposal_path}"
            " - delete it first or pass --force"
        )
    started_at = _utc_now()
    # 同秒冲突走 -2/-3 后缀（auto_curate 同型）：无后缀时同秒双跑会静默合并审计目录。
    base_id = f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    run_id = base_id
    n = 2
    while (repo / ".vrp" / "fixpropose" / run_id).exists():
        run_id = f"{base_id}-{n}"
        n += 1
    audit = repo / ".vrp" / "fixpropose" / run_id
    steps_dir = audit / "steps"
    steps_dir.mkdir(parents=True)  # 新目录：竞争窗口抢注时响亮 FileExistsError，不静默合并
    _write_json(audit / "config.echo.json", config.echo())
    _write_json(steps_dir / "01-finding.json", finding)

    result: dict = {"status": "failed", "attempts": []}
    feedback = ""
    accepted_tx = None
    unfixable_reason = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prefix = steps_dir / f"02-fixpropose-attempt-{attempt}"
        messages = _messages(material, feedback)
        _write_json(Path(f"{prefix}.request.json"), messages)
        try:
            response = call(config, messages)
        except ValueError as exc:
            result["attempts"].append({"attempt": attempt, "outcome": "request_error", "detail": str(exc)[:500]})
            _write_json(Path(f"{prefix}.error.json"), {"error": str(exc)})
            return _finish(repo, audit, started_at, finding_id, finding, config, result, None, "")
        _write_text(Path(f"{prefix}.response.txt"), response["content"])
        _write_json(
            Path(f"{prefix}.meta.json"),
            {"finish_reason": response.get("finish_reason"), "usage": response.get("usage", {})},
        )
        # 推理模型可能把全部输出预算烧在 reasoning 上：空响应+length 快速诚实失败（auto_curate 同型）
        if not response["content"].strip() and response.get("finish_reason") == "length":
            detail = (
                "llm response is empty - all output tokens consumed before content "
                "(finish_reason=length); raise llm.max_output_tokens in config"
            )
            result["attempts"].append({"attempt": attempt, "outcome": "request_error", "detail": detail})
            _write_json(Path(f"{prefix}.error.json"), {"error": detail, "finish_reason": "length"})
            return _finish(repo, audit, started_at, finding_id, finding, config, result, None, "")
        try:
            draft = extract_json(response["content"])
        except ValueError as exc:
            detail = str(exc)[:500]
            if response.get("finish_reason") == "length":
                detail += "; response truncated (finish_reason=length) - keep the draft short"
            result["attempts"].append({"attempt": attempt, "outcome": "parse_error", "detail": detail})
            feedback = f"Your previous response could not be parsed as JSON: {detail}"
            continue
        _write_json(Path(f"{prefix}.draft.json"), draft)
        receipt, tx = validate_draft(draft, Path(f"{prefix}.draft.json"), finding)
        _write_json(Path(f"{prefix}.receipt.json"), receipt)
        result["attempts"].append(
            {"attempt": attempt, "outcome": receipt["status"], "errors": receipt.get("errors", [])}
        )
        if receipt["status"] == "accepted":
            result["status"] = "accepted"
            accepted_tx = tx
            break
        if receipt["status"] == "unfixable":
            result["status"] = "unfixable"
            unfixable_reason = receipt["reason"]
            break
        feedback = json.dumps(receipt, ensure_ascii=False)
    return _finish(
        repo, audit, started_at, finding_id, finding, config, result, accepted_tx, unfixable_reason
    )


def _finish(
    repo: Path,
    audit: Path,
    started_at: str,
    finding_id: int,
    finding: dict,
    config,
    result: dict,
    accepted_tx,
    unfixable_reason: str,
) -> dict:
    """收尾：写交付物（仅 accepted）+summary；返回 summary。"""
    proposal_rel = f".vrp/proposals/fix-{finding_id}.json"
    if accepted_tx is not None:
        proposal_path = repo / ".vrp" / "proposals" / f"fix-{finding_id}.json"
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(proposal_path, _proposal_payload(accepted_tx))
    summary = {
        "run_id": audit.name,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "finding_id": finding_id,
        "finding": {
            "pathology": finding["pathology"],
            "kind": finding["kind"],
            "source": finding["source"],
            "file_path": finding["file_path"],
        },
        "model": config.model,
        "status": result["status"],
        "attempts": result["attempts"],
        "proposal": proposal_rel if accepted_tx is not None else None,
        "unfixable_reason": unfixable_reason or None,
        "next": (
            f"vrp transact {repo} .vrp/proposals/fix-{finding_id}.json --preview"
            "  # review the diff, then run without --preview (G0-G6 gates; T4 needs --confirm)"
            if accepted_tx is not None
            else None
        ),
        "audit_dir": str(audit),
        "rc": 0 if result["status"] in ("accepted", "unfixable") else 1,
    }
    _write_json(audit / "summary.json", summary)
    return summary
