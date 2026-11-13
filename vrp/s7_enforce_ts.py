"""S7 架构执法 TS/JS 引擎（R5'）：dependency-cruiser 18 沙箱子进程。

构想v7 §4 "AI 理解，工具执法" 的 TS/JS 垂直切片；Python 路径见 s7_enforce.py
（import-linter）。s7_enforce.run() 按语言路由：structure.symbol_fids 的
file_path 推导——.py 容器 → Python 引擎；.ts/.tsx/.js/.jsx/.mjs/.cjs 目录
前缀 → 本引擎；混语言（py+ts 同时有容器）not-enforceable 诚实拒绝。

探针事实（work/probe_r5_depcruise_contract.py，2026-11-05）：
- depcruise 18.7.2 + typescript **5.9.3 钉版**：typescript 7.x 超出 depcruise
  ">=2 <7" 支持界 → extensionsFound .ts available:false、totalCruised:0
  **静默空跑 rc=0**——版本红线必须钉，空跑不是错误是陷阱
- Windows：npm .bin 无扩展名项是 sh 脚本（WinError 193），必须走
  depcruise.cmd shim（cmd /c 包装）
- rc 语义（与 import-linter 相反）：-T json 输出下 rc 恒 0（含违例）；
  rc≠0 = 配置/环境错误（stderr 出错、stdout 空）——违例判定只能看 JSON
  内容（modules[].dependencies[].rules[] severity=error + summary.error），
  不能看 rc
- JSON **无顶层 violations 键**；模块级图无行号——payload line=None 诚实
  记录，findings 行号取锚符号 start_line（文件首符号）
- 配置 .dependency-cruiser.cjs：forbidden[] {name（可中文）, severity,
  from.path/to.path 正则}；按 "^(?:p1|p2)(?:/|$)" 组装防前缀污染
  （^src/core 误配 src/corefish.ts）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from .s7_enforce import (
    DEFAULT_TIMEOUT,
    FINDINGS_SOURCE,
    KIND,
    PATHOLOGY,
    build_contracts,
)

CONF_NAME = ".vrp/dependency-cruiser.cjs"
TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_SANDBOX_BIN = ("work", "dep-cruise-probe", "node_modules", ".bin", "depcruise.cmd")


def find_depcruise() -> Path | None:
    """depcruise 发现：VRP_DEPCRUISE_BIN → vrp 仓 work 沙箱（editable 装态）→ PATH。"""
    env = os.environ.get("VRP_DEPCRUISE_BIN")
    if env:
        path = Path(env)
        return path if path.is_file() else None
    sandbox = Path(__file__).resolve().parent.parent.joinpath(*_SANDBOX_BIN)
    if sandbox.is_file():
        return sandbox
    found = shutil.which("depcruise") or shutil.which("depcruise.cmd")
    return Path(found) if found else None


def _prefix(file_path: str) -> str | None:
    """TS/JS 文件 → 目录前缀（posix）；根级文件/非 TS/JS/node_modules → None。"""
    p = file_path.replace("\\", "/")
    if not p.lower().endswith(TS_SUFFIXES):
        return None
    if p.startswith(".") or p.startswith("node_modules/") or "/node_modules/" in p:
        return None
    slash = p.rfind("/")
    if slash <= 0:
        return None  # 根级文件无目录前缀
    return p[:slash]


def derive_ts_containers(con: sqlite3.Connection, payload: dict) -> dict:
    """module_id → 排序去重目录前缀；非 TS/JS 扩展计入 extensions 观测。"""
    fids_by_module: dict[str, list[str]] = {}
    for mod in payload.get("modules", []):
        if isinstance(mod, dict) and isinstance(mod.get("module_id"), str):
            fids_by_module[mod["module_id"]] = [
                f for f in mod.get("symbol_fids", []) if isinstance(f, str)
            ]
    fid_to_module = {f: mid for mid, fids in fids_by_module.items() for f in fids}
    containers: dict[str, set[str]] = {mid: set() for mid in fids_by_module}
    extensions: set[str] = set()
    all_fids = list(fid_to_module)
    for i in range(0, len(all_fids), 500):
        chunk = all_fids[i : i + 500]
        rows = con.execute(
            "SELECT fid, file_path FROM symbols WHERE fid IN (%s)"
            % ",".join("?" * len(chunk)),
            chunk,
        ).fetchall()
        for fid, file_path in rows:
            prefix = _prefix(file_path or "")
            if prefix is None:
                ext = Path(file_path or "").suffix
                if ext:
                    extensions.add(ext)
                continue
            containers[fid_to_module[fid]].add(prefix)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
    }


def build_config(contracts: list[dict]) -> str:
    """contracts → .dependency-cruiser.cjs 文本（forbidden[] from/to 正则）。"""
    rules = []
    for contract in contracts:
        src = "|".join(re.escape(m) for m in contract["source_modules"])
        dst = "|".join(re.escape(m) for m in contract["forbidden_modules"])
        rules.append(
            "    {\n"
            "      name: %s,\n"
            '      severity: "error",\n'
            '      from: { path: "^(?:%s)(?:/|$)" },\n'
            '      to: { path: "^(?:%s)(?:/|$)" },\n'
            "    }"
            % (json.dumps(contract["name"], ensure_ascii=False), src, dst)
        )
    # 规则间必须逗号分隔（真实冒烟实证：单规则探针未暴露，42 契约时 Node
    # SyntaxError: Unexpected token '{' → depcruise rc=1 enforcer-failed）
    return "module.exports = {\n  forbidden: [\n%s\n  ],\n};\n" % ",\n".join(rules)


def _execute(bin_path: Path, repo: Path, config: str, targets: list[str], timeout: int):
    """depcruise 子进程（可测缝）：返回 (rc, stdout, stderr) 或 None=超时。

    Windows .cmd shim 须 cmd /c 包装；posix shim 直调。
    """
    argv = [str(bin_path), *targets, f"--config={config}", "--output-type=json"]
    if bin_path.suffix.lower() == ".cmd":
        argv = ["cmd", "/c", *argv]
    try:
        proc = subprocess.run(
            argv,
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def parse_violations(stdout: str) -> tuple[list[dict], dict]:
    """depcruise json → (violations, summary 子集)；违例=deps.rules severity=error。

    无顶层 violations 键（实证）；按 (rule, importer, imported) 去重保序。
    line=None：模块级图无行号（诚实记录，锚定走文件首符号）。
    """
    data = json.loads(stdout)
    summary = data.get("summary") or {}
    violations: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for module in data.get("modules") or []:
        source = module.get("source") or ""
        for dep in module.get("dependencies") or []:
            resolved = dep.get("resolved") or ""
            for rule in dep.get("rules") or []:
                if rule.get("severity") != "error":
                    continue  # warn/advice 不算违例（我们只生成 error 规则）
                name = rule.get("name") or ""
                key = (name, source, resolved)
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    {
                        "contract": name,
                        "importer": source,
                        "imported": resolved,
                        "line": None,
                    }
                )
    subset = {
        "totalCruised": summary.get("totalCruised"),
        "error": summary.get("error"),
        "warn": summary.get("warn"),
    }
    return violations, subset


def _write_findings(con: sqlite3.Connection, violations: list[dict], contracts: list[dict], structure_status: str) -> dict:
    """违例 → P9 findings（幂等 replace-all）；锚定=违例文件首符号。"""
    con.execute("DELETE FROM findings WHERE source = ?", (FINDINGS_SOURCE,))
    by_name = {c["name"]: c for c in contracts}
    rows = []
    anchored = 0
    for v in violations:
        contract = by_name.get(v["contract"] or "", {})
        row = con.execute(
            "SELECT fid, start_line FROM symbols WHERE file_path = ? ORDER BY start_line LIMIT 1",
            (v["importer"],),
        ).fetchone()
        anchor, line = (row[0], row[1]) if row else (None, None)
        if anchor:
            anchored += 1
        payload = json.dumps(
            {
                "contract": v["contract"],
                "source_layer": contract.get("source_layer"),
                "forbidden_layer": contract.get("forbidden_layer"),
                "importer": v["importer"],
                "imported": v["imported"],
                "line": None,  # depcruise 模块级图无行号（探针实证）
                "structure_status": structure_status,
            },
            ensure_ascii=False,
        )
        rows.append(
            (
                PATHOLOGY,
                KIND,
                FINDINGS_SOURCE,
                anchor,
                None,
                v["importer"],
                line,
                line,
                payload,
            )
        )
    con.executemany(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid, file_path, start_line, end_line, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    return {"findings_written": len(rows), "anchored": anchored}


def run_ts(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """TS/JS 执法入口：契约生成（复用层 DAG）→ depcruise → P9 findings（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "dependency-cruiser",
        "structure_status": structure_status,
    }
    built = build_contracts(payload, containers)
    contracts = built["contracts"]
    summary = {
        **base,
        "contracts": len(contracts),
        "modules_with_containers": sum(1 for v in containers.values() if v),
        "unenforced_rules": built["unenforced_rules"],
        "unenforced_layers": built["unenforced_layers"],
    }
    if not contracts:
        return {
            **summary,
            "status": "not-enforceable",
            "reason": "no derivable contracts (single layer, fully-connected DAG, or layers without containers)",
        }
    bin_path = find_depcruise()
    if not bin_path:
        return {
            **summary,
            "status": "enforcer-not-found",
            "reason": "depcruise not found (work/dep-cruise-probe sandbox, or set VRP_DEPCRUISE_BIN)",
        }
    targets = [
        t
        for t in sorted({m for c in contracts for m in c["source_modules"] + c["forbidden_modules"]})
        if (repo / t).is_dir()
    ]
    if not targets:
        return {
            **summary,
            "status": "not-enforceable",
            "reason": "no container directories exist on disk (structure refers to deleted paths?)",
        }
    config = repo / CONF_NAME
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(build_config(contracts), encoding="utf-8", newline="\n")
    try:
        result = _execute(bin_path, repo, CONF_NAME, targets, timeout)
    finally:
        config.unlink(missing_ok=True)
    if result is None:
        return {
            **summary,
            "status": "enforcer-failed",
            "reason": f"depcruise timed out after {timeout}s",
        }
    rc, stdout, stderr = result
    if rc != 0:
        # -T json 下 rc 恒 0（探针实证）；rc≠0 = 配置/环境错误
        return {
            **summary,
            "status": "enforcer-failed",
            "reason": f"depcruise rc={rc} (config/environment error)",
            "stderr_tail": stderr[-500:],
        }
    try:
        violations, dc_summary = parse_violations(stdout)
    except ValueError:
        return {
            **summary,
            "status": "enforcer-failed",
            "reason": "depcruise output is not valid JSON",
            "stdout_tail": stdout[-500:],
        }
    written = _write_findings(con, violations, contracts, structure_status)
    return {
        **summary,
        "status": "ok",
        "enforcer": str(bin_path),
        "total_cruised": dc_summary["totalCruised"],
        "errors": dc_summary["error"],
        "violations": len(violations),
        **written,
    }
