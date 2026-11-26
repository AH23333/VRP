"""终端原生交互问答（`vrp chat`）——REPL 多轮 LLM 对话 + 只读查询工具环（纯 stdlib，零 agent 路径）。

诚实边界（记档）：
- 首轮注入 dossier T1 简报（截断预算 DOSSIER_BRIEF_BUDGET，确定性派生自
  facts.db——同 analyze 材料面同源）；facts.db 缺失时诚实提示并降级为
  仅通用问答（不假装有材料）；
- 工具环（无 function calling，curate-auto 结构化输出+回执同款已验证模式）：
  LLM 结构化输出 {"tool": <type>, "args": {...}} → 复用 vrp_query 同源的
  17 只读类型现有实现执行查询 → 结果经 mcp_budget.emit（24K 预算+JSON
  感知确定性收缩）回喂 → 循环直到 {"answer": "..."}；非 JSON 回复按普通
  回答原样落地（诚实回退，不强迫协议）；
- 设计定位（防扩权裁决）：本工具环是**领域限定的分析面 harness**，不是
  通用 agent 框架——工具面恒等于显式白名单常量 CHAT_TOOL_TYPES（恰为
  vrp_query 的 17 只读类型，白名单制非黑名单制），写操作（curate_submit/
  transact/enforce 等）物理上不在集内；扩成通用 agent（自定义工具/写
  路径/多框架路由）须明确裁决并另立模块，禁止在本文件内演化；
- 单问跳数硬顶 MAX_TOOL_HOPS=5（按已执行查询计，含诚实报错的查询；
  超限诚实终止并告知用户）；坏结构化输出（JSON 但非协议形）回执重试
  ≤MAX_PROTOCOL_RETRIES，耗尽诚实终止；
- 查询实现零重写：单查询直接调用 nav/coverage/impact/curate/s6_transact
  层现有函数（mcp_server.vrp_query 同源调度）；facts.db 一律只读连接
  （dbpath.ro_connect，mode=ro——只读边界的连接级保证）；
- 进度提示：LLM 调用前打 [thinking...]，查询执行前打
  [query: <type> <target>]（print 后 flush，不打断 REPL 流）；
- 多轮上下文最近 TURNS_WINDOW 轮滑动窗口——更早轮次诚实丢弃；工具环
  中间交换（tool call/回喂结果）是瞬态 loop 上下文，不进窗口，只有最终
  answer 进对话历史；
- 会话日志默认不写（交互隐私默认关）——仅 --log 显式开启才落
  .vrp/chat/session-<UTC>.jsonl（工具环留痕 tool_call/tool_result/
  tool_abort 亦仅此时落盘，stdout 只有单行进度提示）；
- 非 TTY 诚实拒绝（不支持管道模式，一次性分析用 vrp analyze）；
- key 任何形态不落盘（LlmConfig.echo 掩码；日志只记对话内容）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from . import analyze as analyze_mod
from . import coverage as coverage_mod
from . import curate as curate_mod
from . import dbpath
from . import etl as etl_mod
from . import impact as impact_mod
from . import mcp_budget
from . import nav as nav_mod
from . import s5_dossier
from . import s6_transact
from .llm_client import chat as _default_chat
from .llm_client import extract_json, load_config
from .mcp_server import _opt_bool, _opt_int, _opt_str, _opt_str_list

DOSSIER_BRIEF_BUDGET = 8_000
TURNS_WINDOW = 10
PROMPT = "vrp> "
SLASH_COMMANDS = ("/quit", "/dossier", "/findings", "/refresh", "/clear")
# 工具环单问跳数硬顶：按已执行查询计（含诚实报错的查询），超限诚实终止
MAX_TOOL_HOPS = 5
# 坏结构化输出（JSON 但非 tool/answer 协议形）回执重试上限，耗尽诚实终止
MAX_PROTOCOL_RETRIES = 2

# 只读工具面显式白名单（信任边界：白名单制非黑名单制）——恰为 vrp_query
# 的 17 只读类型，与 mcp_server._VRP_QUERY_TYPES 同源（测试钉死防漂移）。
# 写操作（curate_submit/curate_reject/curate_refresh/tx_submit/tx_run/
# tx_preview/enforce/render/session/dossier）物理上不在集内。
CHAT_TOOL_TYPES = (
    "search", "architecture", "module", "symbol", "facts", "callers", "callees",
    "slice", "path", "seams", "coverage", "impact", "drift", "curate_slices",
    "curate_structure", "curate_receipt", "tx_verdicts",
)

_TOOL_PROTOCOL = (
    "\n\n## Read-only query tools (drill-down loop)\n"
    "You can drill down into the codebase with read-only queries before answering.\n"
    'To run a query respond with exactly one JSON object: {"tool": "<type>", "args": {"target": "<primary key>", "options": {<optional secondary params>}}}\n'
    "Tool types (17, read-only): "
    + " ".join(CHAT_TOOL_TYPES)
    + ".\n"
    "target carries the primary key - query text (search), fid (symbol/callers/callees), "
    "module_id i.e. file_path (module), slice_id (slice), sql (facts), tid "
    "(curate_receipt/tx_verdicts), or 'from_fid->to_fid' (path); options carries secondary "
    'params, e.g. {"limit": 50}.\n'
    "The query result is fed back to you as the next message; you may run at most "
    f"{MAX_TOOL_HOPS} queries per question - never fabricate results, rely only on the "
    "injected material and the fed-back results.\n"
    'To give your final answer respond with exactly one JSON object: {"answer": "<your answer text>"}.'
)

_SYSTEM_HEADER = (
    "You are the terminal Q&A AI of the vrp refactoring pipeline.\n"
    "Everything you know about this codebase comes only from the material injected below"
    " (derived from facts.db); for anything the material does not cover, answer honestly"
    ' "not covered by the material, needs verification" - never fabricate fid, P-code, or paths.\n'
    "Answer in English."
) + _TOOL_PROTOCOL


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_brief(repo: Path, budget: int = DOSSIER_BRIEF_BUDGET) -> tuple[str, dict]:
    """dossier T1 简报（views JSON 序列化，超预算硬截断）+ 元信息。

    元信息：模块数（active structure）与顶层文件 top 数（repomap
    files_included）——供「dossier brief injected (N modules / top files ...)」打印。
    """
    repo = Path(repo)
    facts = repo / ".vrp" / "facts.db"
    dossier = s5_dossier.build_dossier(
        repo, facts, repo.name, budget=s5_dossier.DEFAULT_BUDGET, task="T1", skip_external=True
    )
    repomap = ((dossier.get("views") or {}).get("repomap") or {})
    con = sqlite3.connect(facts)
    try:
        etl_mod.ensure_schema(con)
        structure = curate_mod.get_structure(con)
    finally:
        con.close()
    payload = (structure or {}).get("payload")
    modules = payload.get("modules") if isinstance(payload, dict) else None
    text = json.dumps(dossier.get("views") or {}, ensure_ascii=False, default=str)
    truncated = len(text) > budget
    if truncated:
        text = text[:budget]
    meta = {
        "modules": len(modules) if isinstance(modules, list) else 0,
        "top_files": repomap.get("files_included", 0) if isinstance(repomap, dict) else 0,
        "truncated": truncated,
    }
    return text, meta


def _inject_brief(repo: Path, say) -> dict:
    """首轮（及 /clear、/refresh 后）system 上下文注入；返回 system message。"""
    facts = repo / ".vrp" / "facts.db"
    if not facts.exists():
        say("facts.db missing - run /refresh to rebuild (or 'vrp run'); generic Q&A only until rebuilt")
        return {"role": "system", "content": _SYSTEM_HEADER}
    brief, meta = build_brief(repo)
    say(f"dossier brief injected ({meta['modules']} modules / top files {meta['top_files']}...)")
    return {"role": "system", "content": _SYSTEM_HEADER + "\n\n## Injected material (dossier T1 brief)\n" + brief}


def _dossier_system(repo: Path, say) -> dict | None:
    """/dossier：重注入完整材料（analyze 同源装配，60K cap）。"""
    facts = repo / ".vrp" / "facts.db"
    if not facts.exists():
        say("facts.db missing - run /refresh to rebuild")
        return None
    parts = analyze_mod.build_material_parts(repo, with_timeline=False)
    text, _stats = analyze_mod.assemble_material_text(parts)
    return {"role": "system", "content": _SYSTEM_HEADER + "\n\n## Injected full material (dossier T1 + findings top 20)\n" + text}


def _findings_text(repo: Path) -> str | None:
    facts = repo / ".vrp" / "facts.db"
    if not facts.exists():
        return None
    con = sqlite3.connect(facts)
    try:
        findings = analyze_mod.top_findings(con)
    finally:
        con.close()
    return json.dumps(findings, ensure_ascii=False, default=str)


def _log(log_file, kind: str, content: str) -> None:
    if log_file is None:
        return
    log_file.write(
        json.dumps({"ts": _utc_now(), "kind": kind, "content": content}, ensure_ascii=False) + "\n"
    )
    log_file.flush()


def _print_flush(text: str) -> None:
    """stdout 默认输出面：print 后 flush——进度提示不打断 REPL 流。"""
    print(text, flush=True)


def _open_facts_ro(repo: Path) -> sqlite3.Connection:
    """只读打开 facts.db（dbpath.ro_connect mode=ro——只读边界的连接级保证）。"""
    facts = repo / ".vrp" / "facts.db"
    if not facts.exists():
        raise SystemExit(f"facts.db missing - run /refresh to rebuild (or 'vrp run'): {facts}")
    return dbpath.ro_connect(facts)


def _run_tool_query(repo: Path, tool: str, args: dict) -> dict:
    """执行白名单内单条只读查询——纯转发现有实现，零查询逻辑重写。

    调度面与 mcp_server.vrp_query 同源（vrp/mcp_server.py:181-274）：
    search/architecture/module/symbol/facts/callers/callees/slice/path/seams
    → nav 层；coverage → coverage.check_coverage；impact →
    impact.impact_report；drift/curate_slices/curate_structure/curate_receipt
    → curate 层（drift/slices/structure 走只读连接）；tx_verdicts →
    s6_transact.load_verdicts。options 强制转换复用 mcp_server 的 _opt_*
    助手（同口径错误语义）。错误语义同 mcp_server._guard：调用方捕
    (SystemExit, Exception) 转 {"error": ...} 回喂——诚实报错不编造。
    """
    repo = Path(repo)
    target = str(args.get("target", ""))
    opts = args.get("options")
    if opts is None:
        opts = {}
    mcp_budget.check_input("target", target, mcp_budget.MAX_QUERY_INPUT_CHARS)
    if tool == "search":
        return nav_mod.nav_search(
            repo, target,
            _opt_int(opts, "limit", mcp_budget.get_tool_result_limit("nav_search", nav_mod.SEARCH_DEFAULT_LIMIT)),
        )
    if tool == "architecture":
        return nav_mod.nav_architecture(repo)
    if tool == "module":
        return nav_mod.nav_module(
            repo, target, _opt_int(opts, "limit", mcp_budget.get_tool_result_limit("nav_module", nav_mod.MODULE_SYMBOL_CAP))
        )
    if tool == "symbol":
        return nav_mod.nav_symbol(repo, target)
    if tool == "facts":
        return nav_mod.query_facts(
            repo, target,
            _opt_str(opts, "params", ""),
            _opt_int(opts, "max_rows", mcp_budget.get_tool_result_limit("query_facts", nav_mod.QUERY_MAX_ROWS)),
        )
    if tool == "callers":
        return nav_mod.nav_callers(
            repo, target, _opt_int(opts, "limit", mcp_budget.get_tool_result_limit("nav_callers", nav_mod.NEIGHBOR_LIST_DEFAULT_LIMIT))
        )
    if tool == "callees":
        return nav_mod.nav_callees(
            repo, target, _opt_int(opts, "limit", mcp_budget.get_tool_result_limit("nav_callees", nav_mod.NEIGHBOR_LIST_DEFAULT_LIMIT))
        )
    if tool == "slice":
        return nav_mod.nav_slice(repo, target)
    if tool == "path":
        if "->" not in target:
            raise ValueError("path target must be 'from_fid->to_fid'")
        from_fid, to_fid = target.split("->", 1)
        return nav_mod.nav_path(repo, from_fid.strip(), to_fid.strip(), _opt_int(opts, "max_hops", nav_mod.PATH_DEFAULT_MAX_HOPS))
    if tool == "seams":
        return nav_mod.nav_seams(repo)
    if tool == "coverage":
        return coverage_mod.check_coverage(repo, paths=_opt_str_list(opts, "paths"), kinds=_opt_str_list(opts, "kinds"))
    if tool == "impact":
        base_raw = opts.get("base")
        if base_raw is not None and not isinstance(base_raw, str):
            raise ValueError("impact option 'base' must be a string")
        return impact_mod.impact_report(
            repo,
            base=base_raw or None,
            paths=_opt_str_list(opts, "paths"),
            depth=_opt_int(opts, "depth", 2),
            direction=_opt_str(opts, "direction", "inbound"),
            tests=_opt_bool(opts, "tests", True),
        )
    if tool == "drift":
        con = _open_facts_ro(repo)
        try:
            return curate_mod.drift_report(con)
        finally:
            con.close()
    if tool == "curate_slices":
        con = _open_facts_ro(repo)
        try:
            return {"slices": curate_mod.list_slices(con)}
        finally:
            con.close()
    if tool == "curate_structure":
        con = _open_facts_ro(repo)
        try:
            return curate_mod.get_structure(con) or {"structure": None}
        finally:
            con.close()
    if tool == "curate_receipt":
        return curate_mod.read_receipt(repo) or {"receipt": None}
    if tool == "tx_verdicts":
        verdicts = s6_transact.load_verdicts(repo)
        tid = target.strip()
        if tid:
            match = [v for v in verdicts if v.get("tid") == tid]
            if not match:
                raise SystemExit(f"tx_verdicts: no verdict for tid: {tid}")
            return match
        return {"count": len(verdicts), "verdicts": verdicts}
    # 防御性不可达：白名单外类型在 _answer_question 已拒（保持函数自洽）
    raise ValueError(f"unknown tool type: {tool!r} (read-only whitelist: {' '.join(CHAT_TOOL_TYPES)})")


def _answer_question(repo, config, call, messages, say, log_file) -> str | None:
    """工具环单问求解：结构化输出 → 只读查询 → 回喂循环（Gap 1 + Gap 2）。

    协议（无 function calling）：LLM 每步回 {"tool": ..., "args": {...}}
    请求查询，或 {"answer": ...} 给最终回答（answer 键优先判定）；非 JSON
    回复按普通回答原样落地（诚实回退，不强迫协议）。查询结果经
    mcp_budget.emit 预算回喂；跳数超 MAX_TOOL_HOPS / 坏结构化输出重试超
    MAX_PROTOCOL_RETRIES 诚实终止并告知用户。返回最终回答文本；返回 None
    表示该问未产生回答（调用方 history.pop()，失败轮不进窗口）。
    messages 为本问基础上下文（system+滑动窗），工具中间交换 append 其上
    （瞬态 loop 上下文，不进对话历史——只有最终 answer 进 history）。
    """
    repo = Path(repo)
    hops = 0
    faults = 0
    while True:
        say("[thinking...]")
        try:
            response = call(config, messages)
        except ValueError as exc:
            say(f"LLM call failed: {exc}")
            _log(log_file, "error", str(exc))
            return None
        content = response["content"]
        try:
            parsed = extract_json(content)
        except ValueError:
            return content  # 非 JSON：按普通回答原样落地（诚实回退）
        if isinstance(parsed, dict) and isinstance(parsed.get("answer"), str):
            return parsed["answer"]
        detail = None
        if isinstance(parsed, dict) and isinstance(parsed.get("tool"), str):
            tool = parsed["tool"].strip()
            args = parsed.get("args", {})
            if tool in CHAT_TOOL_TYPES and isinstance(args, dict):
                if hops >= MAX_TOOL_HOPS:
                    msg = (
                        f"tool loop aborted: reached the max {MAX_TOOL_HOPS} query hops "
                        "for this question without a final answer"
                    )
                    say(msg)
                    _log(log_file, "tool_abort", "hop_limit")
                    return None
                hops += 1
                target = str(args.get("target", ""))
                say(f"[query: {' '.join(p for p in (tool, target) if p)}]")
                _log(log_file, "tool_call", json.dumps({"tool": tool, "args": args}, ensure_ascii=False))
                try:
                    result = _run_tool_query(repo, tool, args)
                except (SystemExit, Exception) as exc:
                    result = {"error": str(exc) or exc.__class__.__name__}
                result_text = mcp_budget.emit(result)
                _log(log_file, "tool_result", result_text)
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": f"## Query result ({tool})\n{result_text}\n\n"
                    '(continue with a {"tool": ...} query, or finish with {"answer": ...})',
                })
                continue
            if tool not in CHAT_TOOL_TYPES:
                detail = f"unknown tool type: {tool!r} - read-only tools: {' '.join(CHAT_TOOL_TYPES)}"
            else:
                detail = 'tool "args" must be a JSON object like {"target": "...", "options": {...}}'
        elif isinstance(parsed, dict):
            detail = ('protocol error: respond with exactly one JSON object - '
                      '{"tool": "<type>", "args": {...}} to query, or {"answer": "<text>"} to finish')
        else:
            detail = ('protocol error: response JSON must be an object - '
                      '{"tool": "<type>", "args": {...}} to query, or {"answer": "<text>"} to finish')
        # 坏结构化输出：回执回喂重试 ≤MAX_PROTOCOL_RETRIES，耗尽诚实终止
        faults += 1
        if faults > MAX_PROTOCOL_RETRIES:
            say("tool loop aborted: repeated malformed responses (neither a valid tool call nor an answer)")
            _log(log_file, "tool_abort", "protocol")
            return None
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": f"## Tool loop feedback\n{detail}"})


def run_chat(
    repo: Path,
    log: bool = False,
    chat_fn=None,
    input_fn=None,
    output_fn=None,
    isatty: bool | None = None,
    run_pipeline_fn=None,
) -> dict:
    """`vrp chat` 入口：REPL 循环；返回 {"turns", "log_path", "rc"}。

    每问经工具环求解（_answer_question：只读查询回喂循环，跳数 ≤5）。
    chat_fn(config, messages) 可注入（测试）；input_fn(prompt)/output_fn(text)
    注入替 input/print；isatty 注入替 sys.stdin.isatty()；
    run_pipeline_fn(repo, name, print_fn) 注入替 analyze.run_pipeline（/refresh 测试）。
    """
    repo = Path(repo)
    say = output_fn or _print_flush
    ask = input_fn or input
    if isatty is None:
        isatty = sys.stdin.isatty()
    if not isatty:
        # 消息语言纪律：exception 面与 print 面统一英文
        raise SystemExit(
            "vrp chat requires an interactive terminal (TTY) - pipe mode is not supported;"
            " for a one-shot analysis use 'vrp analyze'"
        )
    try:
        config = load_config(repo)
    except ValueError as exc:
        # G2：wizard 引导前置，手动模板（exc 内 JSON）退居其后
        raise SystemExit(f"LLM not configured: run 'vrp config' first to configure the LLM API (.vrp/config.json)\n{exc}")
    call = chat_fn or _default_chat
    system = _inject_brief(repo, say)
    history: list[dict] = []
    turns = 0
    log_path = None
    log_file = None
    if log:
        log_dir = repo / ".vrp" / "chat"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"session-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl"
        log_file = log_path.open("a", encoding="utf-8", newline="\n")
    try:
        while True:
            try:
                line = ask(PROMPT)
            except (EOFError, KeyboardInterrupt):
                break
            text = line.strip()
            if not text:
                continue
            _log(log_file, "user", text)
            if text.startswith("/"):
                cmd = text.split()[0]
                _log(log_file, "slash", text)
                if cmd == "/quit":
                    break
                if cmd == "/dossier":
                    replacement = _dossier_system(repo, say)
                    if replacement is not None:
                        system = replacement
                        say("full dossier material re-injected (same source as 'vrp analyze')")
                elif cmd == "/findings":
                    injected = _findings_text(repo)
                    if injected is None:
                        say("facts.db missing - run /refresh to rebuild")
                    else:
                        system = {
                            "role": "system",
                            "content": system["content"] + "\n\n## Appended injection: findings top 20\n" + injected,
                        }
                        say("findings top 20 injected")
                elif cmd == "/refresh":
                    state, _detail = analyze_mod.facts_state(repo)
                    if state == "fresh":
                        say("facts fresh - no rebuild needed")
                    else:
                        say(f"facts {'missing' if state == 'missing' else 'stale'} - rebuilding (S0->S1->ETL->xcheck)...")
                        run = run_pipeline_fn or analyze_mod.run_pipeline
                        try:
                            run(repo, repo.name, print_fn=say)
                        except Exception as exc:  # SystemExit 原样上抛
                            say(f"facts rebuild failed: {exc}")
                        else:
                            state2, detail2 = analyze_mod.facts_state(repo)
                            if state2 != "fresh":
                                say(f"facts still {'missing' if state2 == 'missing' else 'stale'} after rebuild: {detail2}")
                            else:
                                say("facts rebuilt")
                                system = _inject_brief(repo, say)
                elif cmd == "/clear":
                    history = []
                    system = _inject_brief(repo, say)
                    say("context cleared - back to first-turn state")
                else:
                    say(f"unknown slash command: {cmd} - available: {' '.join(SLASH_COMMANDS)}")
                continue
            history.append({"role": "user", "content": text})
            # 最近 TURNS_WINDOW 轮滑动窗口：调用时 history 恒为奇长（user 刚
            # 入列），裸尾切片会落在孤 assistant 上——对齐到 user 起始的整轮
            window = history[-(TURNS_WINDOW * 2):]
            while window and window[0]["role"] != "user":
                window = window[1:]
            messages = [dict(system)] + window
            # 工具环求解（Gap 1+2）：结构化输出→只读查询→回喂循环；
            # 失败/跳数超限/协议耗尽返回 None，该轮不进窗口
            answer = _answer_question(repo, config, call, messages, say, log_file)
            if answer is None:
                history.pop()
                continue
            history.append({"role": "assistant", "content": answer})
            turns += 1
            say(answer)
            _log(log_file, "assistant", answer)
    finally:
        if log_file is not None:
            log_file.close()
    say("bye")
    return {"turns": turns, "log_path": str(log_path) if log_path is not None else None, "rc": 0}
