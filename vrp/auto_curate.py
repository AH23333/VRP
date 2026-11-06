"""内置 LLM 梳理循环（构想v7 §13 G 批次，G2）——零 agent 路径。

六步（对齐 skills/vrp-curation 协议）：dossier 取材 → slices → structure
→ enforce → drift_report → summary。核心不变量：

- 无 function calling（D1）：LLM 直接产出工件 JSON，curate 确定性校验器即工具，
  拒收回执（稳定码）驱动重试，≤MAX_ATTEMPTS/工件
- 信任边界（D4）：本模块只走 curate 数据面（s5_dossier/curate/s7_enforce），
  不 import 重构事务面——LLM 输出物理上只能变成 facts.db 里的梳理数据
- 全自动+全留痕（D5）：prompt/响应/工件/回执全落 .vrp/auto/run-<UTC>/，
  key 任何形态不落盘（llm_client.LlmConfig.echo 掩码）
- curated 内容默认英文红线写进 prompt（全局默认英文指令延续）
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import curate as curate_mod
from . import etl as etl_mod
from . import s5_dossier
from . import s7_enforce
from .llm_client import chat as _default_chat
from .llm_client import extract_json, load_config

MAX_ATTEMPTS = 3
DEFAULT_ENFORCE_TIMEOUT = 600

_PROMPT_PREAMBLE = (
    "You are the curation AI of the vrp restructuring pipeline.\n"
    "Your knowledge of the codebase comes ONLY from the material provided below "
    "(dossier / facts.db views) - never invent fids; use only the fids that appear in the material.\n"
    "Default output language for curated content (names, summaries, responsibilities, notes): English.\n"
    "Respond with exactly ONE JSON artifact and no other text (no markdown fences, no explanations)."
)

_SLICES_CONTRACT = (
    "Produce a JSON array of business-line slices. Each slice:\n"
    '{"slice_id": "<unique str>", "name": "<str>", "summary": "<one-line str>", '
    '"steps": [{"fid": "<real fid from material>", "role": "entry|process|branch|persist|respond", '
    '"note": "<optional str>"}], "acts": [{"name": "<str>", "from_step": <int>, "to_step": <int>}]}\n'
    "acts is optional. Validation is deterministic: every steps[].fid must exist in symbols; "
    "CONSECUTIVE steps must have a real edge between them in facts.db (any direction: "
    "CALLS / CALL_REFERENCE / HTTP_CALLS) - no skipping hops; slice_id / name / summary non-empty; "
    "acts (when present) must partition all steps monotonically and seamlessly "
    "(first from_step=0, last to_step=len-1, adjacent act to_step+1=next from_step).\n"
    "Rejection codes you may receive: SLICE_SCHEMA / SLICE_EMPTY_NAME / SLICE_EMPTY_SUMMARY / "
    "SLICE_UNKNOWN_FID / SLICE_MISSING_EDGE / SLICE_ACT_BOUNDS / FILE_WRONG_SHAPE."
)

_STRUCTURE_CONTRACT = (
    "Produce ONE JSON object with the overall structure:\n"
    '{"modules": [{"module_id": "<unique str>", "name": "<str>", "responsibility": "<str>", '
    '"symbol_fids": ["<real fid from material>"], "layer": "<layer name>"}], '
    '"layers": [{"name": "<str>", "may_depend_on": ["<layer names>"]}], '
    '"rules": [{"rule": "<str>", "enforcement_hint": "<optional str>", '
    '"from_layer": "<layer>", "to_layer": "<layer>"}]}\n'
    "rules is optional. Validation is deterministic: all symbol_fids must exist in symbols; "
    "one fid belongs to at most one module; layers may_depend_on must form a DAG (no cycles); "
    "module.layer and rule from_layer/to_layer must reference defined layers; "
    "module_id / name / responsibility non-empty; modules and layers non-empty.\n"
    "Rejection codes you may receive: STRUCT_SCHEMA / STRUCT_EMPTY_NAME / STRUCT_EMPTY_RESPONSIBILITY / "
    "STRUCT_UNKNOWN_FID / STRUCT_FID_OVERLAP / STRUCT_LAYER_CYCLE / STRUCT_UNKNOWN_LAYER / STRUCT_RULE_LAYER."
)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _materials(dossier: dict, existing_slices: list, existing_structure, accepted_slices=None) -> str:
    parts = [
        "## Intake material: dossier (facts.db-derived, task-typed budget)",
        json.dumps(dossier, ensure_ascii=False, default=str),
    ]
    if existing_slices:
        parts.append(
            "## Existing ingested slices (context; you may refine or replace them)\n"
            + json.dumps(existing_slices, ensure_ascii=False)
        )
    if accepted_slices:
        parts.append(
            "## Slices accepted this run (context for module thinking)\n"
            + json.dumps(accepted_slices, ensure_ascii=False)
        )
    if existing_structure:
        parts.append(
            "## Existing ingested structure (context; you may refine or replace it)\n"
            + json.dumps(existing_structure, ensure_ascii=False)
        )
    return "\n\n".join(parts)


def _messages(preamble: str, contract: str, materials: str, feedback: str) -> list:
    system = f"{preamble}\n\n{contract}"
    user = materials
    if feedback:
        user += (
            "\n\n## Previous attempt rejected\n"
            + feedback
            + "\n\nFix the artifact per the rejection codes above and submit the corrected JSON."
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _produce_artifact(
    call,
    config,
    kind: str,
    step_name: str,
    contract: str,
    materials: str,
    con: sqlite3.Connection,
    steps_dir: Path,
) -> dict:
    """单工件生产：单发 → 剥离 → curate_submit → 回执重试 ≤MAX_ATTEMPTS。

    返回 {"status": accepted|failed, "attempts": [...], "artifact": <仅成功时>}。
    """
    result: dict = {"kind": kind, "status": "failed", "attempts": []}
    feedback = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prefix = steps_dir / f"{step_name}-attempt-{attempt}"
        messages = _messages(_PROMPT_PREAMBLE, contract, materials, feedback)
        _write_json(Path(f"{prefix}.request.json"), messages)
        try:
            response = call(config, messages)
        except ValueError as exc:
            result["attempts"].append({"attempt": attempt, "outcome": "request_error", "detail": str(exc)[:500]})
            _write_json(Path(f"{prefix}.error.json"), {"error": str(exc)})
            return result
        _write_text(Path(f"{prefix}.response.txt"), response["content"])
        try:
            artifact = extract_json(response["content"])
        except ValueError as exc:
            result["attempts"].append({"attempt": attempt, "outcome": "parse_error", "detail": str(exc)[:500]})
            feedback = f"Your previous response could not be parsed as JSON: {exc}"
            continue
        _write_json(Path(f"{prefix}.artifact.json"), artifact)
        receipt = curate_mod.curate_payload(con, kind, json.dumps(artifact, ensure_ascii=False))
        _write_json(Path(f"{prefix}.receipt.json"), receipt)
        result["attempts"].append(
            {"attempt": attempt, "outcome": receipt["status"], "errors": receipt.get("errors", [])}
        )
        if receipt["status"] == "accepted":
            result["status"] = "accepted"
            result["artifact"] = artifact
            return result
        feedback = json.dumps(receipt, ensure_ascii=False)
    return result


def run_auto_curate(
    repo: Path,
    task: str = s5_dossier.DEFAULT_TASK,
    budget: int = s5_dossier.DEFAULT_BUDGET,
    model_override: str = "",
    enforce_timeout: int = DEFAULT_ENFORCE_TIMEOUT,
    chat_fn=None,
) -> dict:
    """内置六步循环入口；返回 summary dict（含 rc）。CLI 层打印并返回 rc。

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
    finally:
        con.close()
    started_at = _utc_now()
    run_id = f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    audit = repo / ".vrp" / "auto" / run_id
    steps_dir = audit / "steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    _write_json(audit / "config.echo.json", config.echo())

    # 1. 取材（skip_external=True：快速+确定性，§13.4）
    dossier = s5_dossier.build_dossier(
        repo, facts, repo.name, budget=budget, task=task, skip_external=True
    )
    _write_json(steps_dir / "01-dossier.json", dossier)

    con = sqlite3.connect(facts)
    try:
        etl_mod.ensure_schema(con)
        existing_slices = curate_mod.list_slices(con)
        existing_structure = (curate_mod.get_structure(con) or {}).get("payload")

        # 2. slices
        slices_materials = _materials(dossier, existing_slices, existing_structure)
        slices_result = _produce_artifact(
            call, config, "slices", "02-slices", _SLICES_CONTRACT, slices_materials, con, steps_dir
        )

        # 3. structure（slices 失败即跳过：材料质量依赖 slices，§13.4 中止语义）
        structure_result: dict = {"kind": "structure", "status": "skipped", "attempts": []}
        if slices_result["status"] == "accepted":
            structure_materials = _materials(
                dossier,
                existing_slices,
                existing_structure,
                accepted_slices=slices_result.get("artifact"),
            )
            structure_result = _produce_artifact(
                call,
                config,
                "structure",
                "03-structure",
                _STRUCTURE_CONTRACT,
                structure_materials,
                con,
                steps_dir,
            )

        # 4. enforce（库态决定，诚实 skip 语义同 s7_enforce.run）
        enforce_result = s7_enforce.run(repo, con, enforce_timeout)
        _write_json(steps_dir / "04-enforce.json", enforce_result)

        # 5. drift_report
        drift_result = curate_mod.drift_report(con)
        _write_json(steps_dir / "05-drift.json", drift_result)
    finally:
        con.close()

    rc = 0
    if slices_result["status"] != "accepted":
        rc = 1
    elif structure_result["status"] != "accepted":
        rc = 1
    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "task": task,
        "budget": budget,
        "model": config.model,
        "slices": {"status": slices_result["status"], "attempts": slices_result["attempts"]},
        "structure": {"status": structure_result["status"], "attempts": structure_result["attempts"]},
        "enforce": {
            "status": enforce_result.get("status"),
            "violations": enforce_result.get("violations", enforce_result.get("count")),
        },
        "drift": drift_result.get("summary", {}),
        "audit_dir": str(audit),
        "rc": rc,
    }
    _write_json(audit / "summary.json", summary)
    log_path = repo / ".vrp" / "auto" / "log.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "run_id": run_id,
                    "started_at": started_at,
                    "task": task,
                    "model": config.model,
                    "slices": slices_result["status"],
                    "structure": structure_result["status"],
                    "enforce": enforce_result.get("status"),
                    "rc": rc,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return summary
