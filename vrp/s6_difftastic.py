"""S6 G4 fifth signal: difftastic structural diff of edited files
(declared content equivalence). Adapter only (find/run/parse); the G4 wiring
lives in s6_transact.

Difftastic 0.72.0 probe facts (2026-11-04, work/probe_difft.py):
- CLI: `difft OLD NEW`; JSON output needs BOTH `--display json` AND env
  DFT_UNSTABLE=yes -- without the env: rc=2, empty stdout, stderr hint.
- We also pass --ignore-comments: comment add/remove then reports "unchanged"
  (comment churn tolerated; comments do not affect behaviour).
- JSON shape for a single file pair (one object, NOT an array):
  {language, path, status: "changed"|"unchanged", aligned_lines, chunks}.
  chunks: list of chunk lists; each block {"lhs": {...}?, "rhs": {...}?};
  a side is {line_number (0-based), changes: [{start, end, content,
  highlight}]}.   Pure removals -> lhs-only block; pure additions -> rhs-only
  block; modifications -> both sides. Unknown status values are treated as
  per-file degradation, never as pass/fail; a "changed" status with zero
  extractable blocks is likewise per-file degradation ("changed-no-blocks")
  -- shape drift must degrade, never silently pass as accounted.
- rc is always 0 under --display json (changed or unchanged); --exit-code
  unused by us.
- Whitespace/indentation reformat reports no changes (structural diff), but
  token-level rewrites DO count (quote style, added wrapping parens, wrap
  lines introducing tokens): only whitespace-level reformatting is tolerated.
- Parse errors fall back to a line-oriented diff (per-char "normal" changes);
  G1 already fails on parse errors in edited files before G4 runs, so this is
  an edge case that degrades per-file instead of blocking.
- Language detection by extension; unknown extensions diff as text;
  .py/.ts/.tsx/.js/.jsx/.rs all supported (--list-languages).

Accounting rules (what counts as declared content change):
- T1: (a) lhs-only block whose line falls inside the pre-span of a declared
  `from` symbol, and (b) rhs-only block inside the post-span of a declared
  `to` symbol -- both ONLY for cross-file moves (removal/addition of the
  moved definition); (c) both-side block whose changed tokens pair up 1:1
  through the differing path segments of a declared move (definition rename,
  call-site updates, import path updates). Everything else is unaccounted.
- T2: lhs-only blocks (pure removals) are accounted; any addition or
  modification is unaccounted (T2's claim is "only remove dead code").
- Scope: modify-op edited files only. Created files have no preimage (new
  symbols are checked at set level, imports by verify_changes); deleted files
  have no postimage (set level). Residual: a cross-file move rewritten while
 moving is indistinguishable from a moved definition (span rules cover it);
  set-level, verify_changes, G3 and G5 remain the net for that case.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from shutil import which
from . import dbpath, procs

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BIN = _ROOT / "work" / "difftastic" / "difft.exe"
_KNOWN_STATUSES = ("changed", "unchanged")


def find_difftastic() -> str | None:
    env_value = os.environ.get("VRP_DIFFTASTIC_BIN")
    candidates = [Path(env_value) if env_value else None, _DEFAULT_BIN]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate)
    return which("difft")


def run_difftastic(binary: str, old_path: Path, new_path: Path, timeout: int = 60) -> dict:
    """Run `difft --display json --ignore-comments OLD NEW` (env-gated JSON).

    Returns {ok, returncode?, stderr_tail?, parsed?} or {ok: False, reason}.
    """
    env = dict(os.environ)
    env["DFT_UNSTABLE"] = "yes"
    res = procs.run_tree(
        [binary, "--display", "json", "--ignore-comments", str(old_path), str(new_path)],
        timeout=timeout,
        env=env,
    )
    if res["timed_out"]:
        return {"ok": False, "reason": "timeout"}
    result: dict = {"ok": res["returncode"] == 0, "returncode": res["returncode"], "stderr_tail": res["stderr"][-300:]}
    if not result["ok"]:
        return result
    try:
        parsed = json.loads(res["stdout"])
    except ValueError:
        result.update({"ok": False, "reason": "json-parse", "stdout_tail": proc.stdout[-300:]})
        return result
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else None
    if not isinstance(parsed, dict):
        result.update({"ok": False, "reason": "json-shape"})
        return result
    result["parsed"] = parsed
    return result


def extract_blocks(parsed: dict) -> list[dict]:
    """Flatten difftastic JSON chunks into blocks with per-side line + tokens."""
    blocks: list[dict] = []
    for chunk in parsed.get("chunks") or []:
        if not isinstance(chunk, list):
            continue
        for block in chunk:
            if not isinstance(block, dict):
                continue
            entry: dict = {}
            for side in ("lhs", "rhs"):
                data = block.get(side)
                if not isinstance(data, dict):
                    continue
                line = data.get("line_number")
                tokens = [(c.get("content") or "") for c in (data.get("changes") or []) if isinstance(c, dict)]
                entry[side] = {"line": line if isinstance(line, int) else None, "tokens": tokens}
            if entry:
                blocks.append(entry)
    return blocks


def _segment_pairs(moves: list[dict]) -> list[tuple[str, str]]:
    """Differing path segments of each declared move -> allowed token pairs.

    A move with mismatched segment counts contributes no pair (both-side
    blocks then stay unaccounted -- honest residual, documented).
    """
    pairs: list[tuple[str, str]] = []
    for move in moves:
        from_segs = move["from"].split(".")
        to_segs = move["to"].split(".")
        if len(from_segs) != len(to_segs):
            continue
        for from_seg, to_seg in zip(from_segs, to_segs):
            if from_seg != to_seg:
                pairs.append((from_seg, to_seg))
    return pairs


def _pairs_match(lhs_tokens: list[str], rhs_tokens: list[str], allowed_pairs: list[tuple[str, str]]) -> bool:
    if not allowed_pairs or len(lhs_tokens) != len(rhs_tokens):
        return False
    lhs_count = Counter(lhs_tokens)
    rhs_count = Counter(rhs_tokens)
    for from_seg, to_seg in allowed_pairs:
        use = min(lhs_count[from_seg], rhs_count[to_seg])
        if use:
            lhs_count[from_seg] -= use
            rhs_count[to_seg] -= use
    return sum(lhs_count.values()) == 0 and sum(rhs_count.values()) == 0


def _symbol_spans(db_path: Path, qns: set[str]) -> dict[str, tuple[str, int, int]]:
    """qualified_name -> (file_path forward-slashed, start_line, end_line).

    Symbols without usable spans (NULL lines, empty path) are omitted.
    """
    spans: dict[str, tuple[str, int, int]] = {}
    if not qns:
        return spans
    con = dbpath.ro_connect(db_path)
    try:
        placeholders = ",".join("?" for _ in qns)
        rows = con.execute(
            "SELECT qualified_name, file_path, start_line, end_line FROM symbols"
            f" WHERE qualified_name IN ({placeholders})",
            tuple(sorted(qns)),
        ).fetchall()
    finally:
        con.close()
    for qn, file_path, start, end in rows:
        normalized = (file_path or "").replace("\\", "/")
        if normalized and isinstance(start, int) and isinstance(end, int) and 1 <= start <= end:
            spans[qn] = (normalized, start, end)
    return spans


def _git_show(repo: Path, rel: str) -> str | None:
    try:
        res = procs.run_tree(
            ["git", "show", f"HEAD:{rel}"],
            cwd=repo,
            timeout=60,
        )
    except OSError:
        # OSError（git 不在 PATH）按契约降级 None（"no-preimage"），
        # 与 etl.current_git_head 同型——R-N 补齐错误面。
        return None
    if res["timed_out"] or res["returncode"] != 0:
        return None
    return res["stdout"]


def _block_summary(block: dict) -> dict:
    summary: dict = {}
    for side in ("lhs", "rhs"):
        data = block.get(side)
        if not data:
            continue
        summary[side] = {
            "line": (data["line"] + 1) if data["line"] is not None else None,
            "tokens": data["tokens"][:8],
        }
    return summary


def check_edits(repo: Path, tx: dict, pre_db: Path, post_db: Path, timeout: int = 60) -> dict:
    """Structural diff of modify-op edited files vs declared intent.

    Returns one of:
    - {"status": "not-found"}                       binary missing (degrade)
    - {"status": "skipped", "reason": ...}          nothing to diff
    - {"status": "ok", "files": {...}, "checked": N, "unaccounted": []}
    - {"status": "fail", "files": {...}, "checked": N,
       "unaccounted": [{"file", "blocks": [...]}]}
    Per-file run failures land in "degraded" (belt-and-braces, non-blocking).
    """
    binary = find_difftastic()
    if not binary:
        return {"status": "not-found"}
    modify_files = sorted({edit["file"].as_posix() for edit in tx["edits"] if edit["op"] == "modify"})
    if not modify_files:
        return {"status": "skipped", "reason": "no modify-op edited files"}
    moves = tx["declared_moves"] if tx["type"] == "T1" else []
    allowed_pairs = _segment_pairs(moves)
    from_spans = _symbol_spans(pre_db, {move["from"] for move in moves})
    to_spans = _symbol_spans(post_db, {move["to"] for move in moves})
    removal_lines: dict[str, set[int]] = {}
    addition_lines: dict[str, set[int]] = {}
    for move in moves:
        from_span = from_spans.get(move["from"])
        to_span = to_spans.get(move["to"])
        if not from_span or not to_span or from_span[0] == to_span[0]:
            continue  # cross-file moves only (same-file renames go through token pairs)
        removal_lines.setdefault(from_span[0], set()).update(range(from_span[1], from_span[2] + 1))
        addition_lines.setdefault(to_span[0], set()).update(range(to_span[1], to_span[2] + 1))
    files_report: dict[str, str] = {}
    unaccounted: list[dict] = []
    degraded: list[dict] = []
    for rel in modify_files:
        pre_text = _git_show(repo, rel)
        if pre_text is None:
            degraded.append({"file": rel, "reason": "no-preimage"})
            continue
        with tempfile.TemporaryDirectory(prefix="vrp-difft-") as tmp:
            pre_path = Path(tmp) / Path(rel).name
            pre_path.write_text(pre_text, encoding="utf-8", newline="\n")
            run = run_difftastic(binary, pre_path, repo / rel, timeout=timeout)
        if not run.get("ok"):
            degraded.append({"file": rel, "reason": run.get("reason") or f"rc={run.get('returncode')}"})
            continue
        parsed = run["parsed"]
        status = parsed.get("status")
        if status == "unchanged":
            files_report[rel] = "unchanged"
            continue
        if status not in _KNOWN_STATUSES:
            degraded.append({"file": rel, "reason": f"status={status}"})
            continue
        blocks = extract_blocks(parsed)
        if not blocks:
            # changed 却提取不出任何块 = JSON 形状漂移（0.72.0 的 changed
            # 文件必有 chunks）——按文件降级上报，绝不静默当 accounted 放行。
            degraded.append({"file": rel, "reason": "changed-no-blocks"})
            continue
        bad = []
        for block in blocks:
            lhs = block.get("lhs")
            rhs = block.get("rhs")
            # 分类按 token 有无而非侧字典真值：difftastic 的纯删除是
            # lhs 有 token + rhs 存在但 token 空（R6' agent 长跑 T2 实证，
            # 旧 `if lhs and not rhs` 漏此形态误报 structural-change）。
            lhs_tokens = (lhs or {}).get("tokens") or []
            rhs_tokens = (rhs or {}).get("tokens") or []
            if lhs_tokens and not rhs_tokens:
                if tx["type"] == "T2" or (lhs["line"] is not None and (lhs["line"] + 1) in removal_lines.get(rel, set())):
                    continue
            elif rhs_tokens and not lhs_tokens:
                if rhs["line"] is not None and (rhs["line"] + 1) in addition_lines.get(rel, set()):
                    continue
            elif lhs_tokens and rhs_tokens:
                if _pairs_match(lhs_tokens, rhs_tokens, allowed_pairs):
                    continue
            else:
                continue  # 双侧皆无 token：无结构内容（噪声块）
            bad.append(_block_summary(block))
        if bad:
            unaccounted.append({"file": rel, "blocks": bad[:5]})
            files_report[rel] = "unaccounted"
        else:
            files_report[rel] = "accounted"
    report = {
        "status": "fail" if unaccounted else "ok",
        "files": files_report,
        "checked": len(files_report),
        "unaccounted": unaccounted,
    }
    if degraded:
        report["degraded"] = degraded
    return report
