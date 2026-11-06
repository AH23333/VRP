"""S7 架构规则固化（构想v7 §4）：AI 理解，工具执法。

curate 入库的 structure（modules/layers/rules）→ import-linter 配置生成 →
lint-imports 执行（cwd=repo）→ Broken contracts 解析 → 违例 = P9 findings
（kind='layer-violation'，source='s7-enforce'，幂等 replace-all）→ dossier
侧进 AI 重构目标。

六引擎：本模块 Python 路径（import-linter 2.15，pip 全局；
VRP_IMPORT_LINTER_BIN 覆盖）；TS/JS 路径见 s7_enforce_ts.py（dependency-cruiser
18 沙箱，work/dep-cruise-probe）；Go 路径见 s7_enforce_go.py（go list -e -json）；
Rust 路径见 s7_enforce_rs.py（内置源级 use 树解析）；Java 路径见
s7_enforce_java.py（内置源级 package/import 解析）；C# 路径见
s7_enforce_cs.py（内置源级 namespace/using 解析）。run() 按语言路由：
.py 容器 → Python；.ts/.tsx/.js/.jsx/.mjs/.cjs 目录前缀 → TS 引擎；.go 容器
→ Go 引擎；.rs 符号 → Rust 引擎；.java 符号 → Java 引擎；.cs 符号 →
C# 引擎；混语言 not-enforceable。

探针事实（work/probe_r4_importlinter.py + probe_r4_forbidden.py，2026-11-05）：
- rc 0=kept / 1=broken；rc 1 且 stdout 无 "Broken contracts" 块 = 配置/环境
  错误 → enforcer-failed（不能当违例）
- broken 块格式：契约名行 + 破折线 + "desc:"（以冒号结尾）+ "- importer -> imported (l.N)"；
  同一条链聚合组/明细组各列一次 + 描述行 ~70 字符折行（tsa dogfood 实证）→
  parse_broken 按 (importer, imported, line) 去重保首、"is not allowed to
  import" 片段跳过
- forbidden 契约容器可为文件级模块（pkg.mod）；契约 name 可中文
- 层 DAG：每对不允许的 (A,B) 生成一条 forbidden 契约（覆盖线性栈与任意 DAG）；
  rules[]：from_layer/to_layer 或 "X 不许 import Y"（X/Y=层名）文本模式映射为
  同型契约；映射不出的规则进 unenforced_rules 诚实清单
- 容器推导：module.symbol_fids → symbols.file_path → 点分模块名
  （strip .py、/→.、__init__.py→父包名；非 .py 跳过）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import curate as curate_mod

CONF_NAME = ".vrp/importlinter.ini"
FINDINGS_SOURCE = "s7-enforce"
PATHOLOGY = "P9"
KIND = "layer-violation"
DEFAULT_TIMEOUT = 600

_RULE_TEXT = re.compile(r"^\s*(.+?)\s*(?:不许|不能|禁止|must\s+not)\s*import\s*(.+?)\s*$")
_STATS = re.compile(r"Contracts:\s+(\d+)\s+kept,\s+(\d+)\s+broken")
_VIOLATION = re.compile(r"^-\s+(\S+)\s+->\s+(\S+)\s+\(l\.(\d+)[^)]*\)$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def find_import_linter() -> str | None:
    import os

    env = os.environ.get("VRP_IMPORT_LINTER_BIN")
    if env and Path(env).is_file():
        return env
    return shutil.which("lint-imports")


def _dotted(file_path: str) -> str | None:
    """相对文件路径 → 点分模块名；非 .py / 顶层 __init__.py → None。"""
    p = file_path.replace("\\", "/")
    if p == "__init__.py":
        return None  # 仓根 __init__.py 无点分模块名
    if p.endswith("/__init__.py"):
        p = p[: -len("/__init__.py")]
    elif p.endswith(".py"):
        p = p[:-3]
    else:
        return None
    if not p or p.endswith("/") or p.startswith("."):
        return None
    return p.replace("/", ".")


def _dotted_to_paths(dotted: str) -> list[str]:
    """点分模块名 → 候选相对路径（pkg → pkg/__init__.py 优先，pkg.py 兜底）。"""
    slashed = dotted.replace(".", "/")
    return [f"{slashed}/__init__.py", f"{slashed}.py"]


def derive_containers(con: sqlite3.Connection, payload: dict) -> dict:
    """module_id → 排序去重的 .py 容器点分名；非 .py 文件计入 extensions 观测。"""
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
            name = _dotted(file_path or "")
            if name is None:
                ext = Path(file_path or "").suffix
                if ext:
                    extensions.add(ext)
                continue
            containers[fid_to_module[fid]].add(name)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
    }


def _layer_map(payload: dict, containers: dict[str, list[str]]) -> dict[str, list[str]]:
    """层名 → 该层全部模块的容器并集。"""
    layer_names: dict[str, list[str]] = {}
    for mod in payload.get("modules", []):
        if not isinstance(mod, dict):
            continue
        layer = mod.get("layer")
        mid = mod.get("module_id")
        if isinstance(layer, str) and isinstance(mid, str):
            layer_names.setdefault(layer, []).extend(containers.get(mid, []))
    return {name: sorted(set(mods)) for name, mods in layer_names.items()}


def _forbidden_pairs(payload: dict) -> list[tuple[str, str]]:
    """层 DAG：may_depend_on 之外的每一对 (A,B)（A≠B）不允许。"""
    deps: dict[str, list[str]] = {}
    for layer in payload.get("layers", []):
        if isinstance(layer, dict) and isinstance(layer.get("name"), str):
            may = layer.get("may_depend_on", [])
            deps[layer["name"]] = [m for m in may if isinstance(m, str)]
    names = list(deps)
    pairs = [
        (a, b)
        for a in names
        for b in names
        if a != b and b not in deps[a]
    ]
    return sorted(pairs)


def _rule_pair(rule: dict, layer_names: set[str]) -> tuple[str, str] | None:
    """rule 对象 → (from_layer, to_layer)；from/to 字段优先，文本模式兜底。"""
    a, b = rule.get("from_layer"), rule.get("to_layer")
    if isinstance(a, str) and isinstance(b, str) and a in layer_names and b in layer_names:
        return a, b
    text = rule.get("rule")
    if not isinstance(text, str):
        return None
    m = _RULE_TEXT.match(text)
    if m and m.group(1) in layer_names and m.group(2) in layer_names:
        return m.group(1), m.group(2)
    return None


def build_contracts(payload: dict, containers: dict[str, list[str]]) -> dict:
    """层 DAG 对 + 可映射 rules → forbidden 契约（按 (from,to) 去重排序）。"""
    layer_mods = _layer_map(payload, containers)
    layer_names = {layer["name"] for layer in payload.get("layers", []) if isinstance(layer, dict) and isinstance(layer.get("name"), str)}
    pairs = set(_forbidden_pairs(payload))
    unenforced_rules: list[str] = []
    rules = payload.get("rules") or []
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            pair = _rule_pair(rule, layer_names)
            if pair is None:
                text = rule.get("rule")
                unenforced_rules.append(text if isinstance(text, str) else repr(rule))
            else:
                pairs.add(pair)
    contracts = []
    unenforced_layers = sorted(
        name for name, mods in layer_mods.items() if not mods
    )
    for a, b in sorted(pairs):
        source = layer_mods.get(a, [])
        forbidden = layer_mods.get(b, [])
        if not source or not forbidden:
            continue
        contracts.append(
            {
                "id": f"layer-{a}-{b}",
                "name": f"layer: {a} must not import {b}",
                "source_layer": a,
                "forbidden_layer": b,
                "source_modules": source,
                "forbidden_modules": forbidden,
            }
        )
    return {"contracts": contracts, "unenforced_rules": unenforced_rules, "unenforced_layers": unenforced_layers}


def _enforceable_container(repo: Path, dotted: str) -> bool:
    """容器可被 import-linter 执法判定（真实冒烟实证）：

    root_packages 必须是可导入的包——顶层组件必须是目录（根级 .py 模块如
    _verify_workflow_structure_helpers.py 会让 lint-imports 直接 config error
    "'X' is a module, not a package"）；连字符目录（benchmarks/agent-tasks/）
    不是合法标识符组件，同样不可执法。无 __init__.py 的顶层目录是命名空间包，
    import-linter 可正常执法，不做剔除。
    """
    parts = dotted.split(".")
    if not all(_IDENT.match(p) for p in parts):
        return False
    return (repo / parts[0]).is_dir()


def _filter_contracts(
    repo: Path, contracts: list[dict]
) -> tuple[list[dict], list[str]]:
    """契约过滤：source/forbidden 各自剔除不可执法容器，单侧清空的契约丢弃。

    返回 (剩余契约, 被剔除容器排序去重清单)——诚实上报 unenforced_containers。
    """
    dropped: set[str] = set()
    kept_contracts: list[dict] = []
    for contract in contracts:
        source = []
        for m in contract["source_modules"]:
            if _enforceable_container(repo, m):
                source.append(m)
            else:
                dropped.add(m)
        forbidden = []
        for m in contract["forbidden_modules"]:
            if _enforceable_container(repo, m):
                forbidden.append(m)
            else:
                dropped.add(m)
        if not source or not forbidden:
            continue
        kept_contracts.append({**contract, "source_modules": source, "forbidden_modules": forbidden})
    return kept_contracts, sorted(dropped)


def build_config(contracts: list[dict], root_packages: list[str]) -> str:
    lines = ["[importlinter]", "root_packages ="]
    lines += [f"    {pkg}" for pkg in root_packages]
    for i, contract in enumerate(contracts):
        lines += [
            "",
            f"[importlinter:contract:contract-{i}]",
            f"name = {contract['name']}",
            "type = forbidden",
            "source_modules =",
        ]
        lines += [f"    {m}" for m in contract["source_modules"]]
        lines.append("forbidden_modules =")
        lines += [f"    {m}" for m in contract["forbidden_modules"]]
    lines.append("")
    return "\n".join(lines)


def parse_broken(stdout: str) -> list[dict]:
    """Broken contracts 块 → 违例列表（按 (contract, importer, imported, line) 去重保首）。

    tsa dogfood 实证（work/probe_r4_tsa_raw.py，2026-11-05）：forbidden 契约
    同一条链列两遍——(容器,被禁模块) 聚合组在前、(模块,被禁模块) 明细组在后，
    且描述行 ~70 字符折行，折行片段无冒号尾（"X is not allowed to import"），
    不能当契约名；去重保首（首现于聚合组，契约名归属正确），描述片段按
    "is not allowed to import" 子串识别跳过。

    tsa 冒烟实证（2026-11-06，多链真实输出）：(1) 头行行号可多值 "(l.14, l.224)"
    ——正则允许多值取首个；(2) 链尾/链中边行（"a -> b (l.N)"）含 " -> "，绝不能
    落进契约名兜底分支污染 current_contract；(3) 同一条直接 import 边可同时违反
    两个契约——去重键必须含契约维度。
    """
    violations: list[dict] = []
    seen: set[tuple[str | None, str, str, int]] = set()
    current_contract: str | None = None
    in_block = False
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped == "Broken contracts":
            in_block = True
            continue
        if not in_block or not stripped:
            continue
        m = _VIOLATION.match(stripped)
        if m:
            key = (current_contract, m.group(1), m.group(2), int(m.group(3)))
            if key not in seen:
                seen.add(key)
                violations.append(
                    {
                        "contract": current_contract,
                        "importer": m.group(1),
                        "imported": m.group(2),
                        "line": int(m.group(3)),
                    }
                )
            continue
        if " -> " in stripped:
            continue  # 链中/链尾边行：不是违例头，也绝不能当契约名
        if stripped.endswith(":") or set(stripped) <= {"-"}:
            continue  # 描述行 / 破折线
        if "is not allowed to import" in stripped:
            continue  # 描述行（含折行片段），不覆盖契约名
        current_contract = stripped  # 契约名行
    return violations


def _execute(bin_path: str, repo: Path, config: Path, timeout: int):
    """lint-imports 子进程（可测缝）：返回 (rc, stdout) 或 None=超时。

    child env 钉 PYTHONUTF8=1：lint-imports 以 locale 编码（中文 Windows=GBK）
    读 UTF-8 的 importlinter.ini（契约名含中文）→ UnicodeDecodeError rc=1 无
    broken 块（R6' agent 长跑在 vrp 自仓实证）。

    child env 钉 COLUMNS=100000：click 在非 tty 下按 80 列折行输出（tsa 冒烟
    实证），长点分名把 "X is not allowed to import Y:" 折成两行 → parse_broken
    全部漏解析 → broken 契约 0 违例 0 入 P9。os.get_terminal_size 优先读
    COLUMNS，钉大值即不折行。
    """
    try:
        proc = subprocess.run(
            [bin_path, f"--config={config}", "--no-cache", "--no-logo"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "PYTHONUTF8": "1", "COLUMNS": "100000"},
        )
    except subprocess.TimeoutExpired:
        return None
    return proc.returncode, proc.stdout or ""


def _anchor_for(con: sqlite3.Connection, file_path: str | None, line: int) -> str | None:
    """违例行所属符号 fid：覆盖行者优先，无则该文件首符号，再无则 None。"""
    if not file_path:
        return None
    row = con.execute(
        "SELECT fid FROM symbols WHERE file_path = ? AND start_line <= ? AND end_line >= ?"
        " ORDER BY start_line LIMIT 1",
        (file_path, line, line),
    ).fetchone()
    if row is None:
        row = con.execute(
            "SELECT fid FROM symbols WHERE file_path = ? ORDER BY start_line LIMIT 1",
            (file_path,),
        ).fetchone()
    return row[0] if row else None


def _write_findings(con: sqlite3.Connection, violations: list[dict], contracts: list[dict], structure_status: str) -> dict:
    con.execute("DELETE FROM findings WHERE source = ?", (FINDINGS_SOURCE,))
    by_name = {c["name"]: c for c in contracts}
    rows = []
    anchored = 0
    for v in violations:
        contract = by_name.get(v["contract"] or "", {})
        importer_file = None
        for candidate in _dotted_to_paths(v["importer"]):
            if con.execute(
                "SELECT 1 FROM symbols WHERE file_path = ? LIMIT 1", (candidate,)
            ).fetchone():
                importer_file = candidate
                break
        anchor = _anchor_for(con, importer_file, v["line"])
        if anchor:
            anchored += 1
        payload = json.dumps(
            {
                "contract": v["contract"],
                "source_layer": contract.get("source_layer"),
                "forbidden_layer": contract.get("forbidden_layer"),
                "importer": v["importer"],
                "imported": v["imported"],
                "line": v["line"],
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
                importer_file,
                v["line"],
                v["line"],
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


def run(repo: Path, con: sqlite3.Connection, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """执法入口：structure(active) → 契约生成 → lint-imports → P9 findings（幂等）。"""
    repo = Path(repo)
    base = {"step": "s7-enforce"}
    info = curate_mod.get_structure(con)
    if info is None or info["payload"] is None:
        return {
            **base,
            "status": "skipped",
            "reason": "no ingested structure (run 'vrp curate' first)",
        }
    payload = info["payload"]
    structure_status = info["status"]
    if structure_status != "active":
        return {
            **base,
            "status": "skipped",
            "reason": f"structure status={structure_status} (only active is enforceable)",
        }
    derived = derive_containers(con, payload)
    containers = derived["containers"]
    from .s7_enforce_ts import derive_ts_containers, run_ts  # 惰性导入防环（ts 模块顶层反向 import 本模块）
    from .s7_enforce_go import derive_go_containers, run_go  # 惰性导入防环（go 模块 import 本模块常量）
    from .s7_enforce_rs import derive_rs_containers, run_rs  # 惰性导入防环（rs 模块 import 本模块常量）
    from .s7_enforce_java import derive_java_containers, run_java  # 惰性导入防环（java 模块 import 本模块常量）
    from .s7_enforce_cs import derive_cs_containers, run_cs  # 惰性导入防环（cs 模块 import 本模块常量）

    ts_derived = derive_ts_containers(con, payload)
    go_derived = derive_go_containers(con, payload)
    rs_derived = derive_rs_containers(repo, con, payload, timeout)
    java_derived = derive_java_containers(repo, con, payload, timeout)
    cs_derived = derive_cs_containers(repo, con, payload, timeout)
    has_py = any(containers.values())
    has_ts = any(ts_derived["containers"].values())
    has_go = any(go_derived["containers"].values())
    has_rs = rs_derived["rs_files"] > 0
    has_java = java_derived["java_files"] > 0
    has_cs = cs_derived["cs_files"] > 0
    langs = [
        name
        for name, present in (
            ("py", has_py),
            ("ts", has_ts),
            ("go", has_go),
            ("rs", has_rs),
            ("java", has_java),
            ("cs", has_cs),
        )
        if present
    ]
    if len(langs) > 1:
        return {
            **base,
            "structure_status": structure_status,
            "status": "not-enforceable",
            "reason": (
                f"mixed-language structure ({' + '.join(langs)}):"
                " split per language into separate structures"
            ),
        }
    if has_go:
        return run_go(repo, con, payload, structure_status, go_derived["containers"], timeout)
    if has_rs:
        if rs_derived.get("timed_out"):
            return {
                **base,
                "structure_status": structure_status,
                "status": "enforcer-failed",
                "reason": f"rust mod tree build exceeded {timeout}s (partial results discarded)",
            }
        result = run_rs(repo, con, payload, structure_status, rs_derived["containers"], timeout)
        if rs_derived.get("orphan_files"):
            result["orphan_rs_files"] = rs_derived["orphan_files"]
        return result
    if has_java:
        if java_derived.get("timed_out"):
            return {
                **base,
                "structure_status": structure_status,
                "status": "enforcer-failed",
                "reason": f"java scan exceeded {timeout}s (partial results discarded)",
            }
        result = run_java(repo, con, payload, structure_status, java_derived["containers"], timeout)
        if java_derived.get("orphan_files"):
            result["orphan_java_files"] = java_derived["orphan_files"]
        return result
    if has_cs:
        if cs_derived.get("timed_out"):
            return {
                **base,
                "structure_status": structure_status,
                "status": "enforcer-failed",
                "reason": f"c# scan exceeded {timeout}s (partial results discarded)",
            }
        result = run_cs(repo, con, payload, structure_status, cs_derived["containers"], timeout)
        if cs_derived.get("orphan_files"):
            result["orphan_cs_files"] = cs_derived["orphan_files"]
        return result
    if has_ts:
        return run_ts(repo, con, payload, structure_status, ts_derived["containers"], timeout)
    if not has_py:
        if (
            derived["extensions"]
            or ts_derived["extensions"]
            or go_derived["extensions"]
            or rs_derived["extensions"]
            or java_derived["extensions"]
            or cs_derived["extensions"]
        ):
            seen = ", ".join(
                sorted(
                    set(derived["extensions"])
                    | set(ts_derived["extensions"])
                    | set(go_derived["extensions"])
                    | set(rs_derived["extensions"])
                    | set(java_derived["extensions"])
                    | set(cs_derived["extensions"])
                )
            )
            return {
                **base,
                "status": "language-unsupported",
                "reason": (
                    f"no enforceable containers (seen: {seen}); supported: .py (import-linter),"
                    " .ts/.tsx/.js/.jsx/.mjs/.cjs (dependency-cruiser), .go (go list),"
                    " .rs (built-in use parser), .java (built-in package/import parser),"
                    " .cs (built-in namespace/using parser)"
                ),
                "structure_status": structure_status,
            }
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no containers resolved from symbol_fids",
            "structure_status": structure_status,
        }
    built = build_contracts(payload, containers)
    contracts = built["contracts"]
    summary = {
        **base,
        "structure_status": structure_status,
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
    # 容器可执法性过滤（真实冒烟实证）：根级 .py 模块/连字符目录/非包顶层会让
    # lint-imports config error——过滤后诚实上报，无剩余契约则 not-enforceable
    contracts, unenforced_containers = _filter_contracts(repo, contracts)
    summary["contracts"] = len(contracts)
    if unenforced_containers:
        summary["unenforced_containers"] = unenforced_containers
    if not contracts:
        return {
            **summary,
            "status": "not-enforceable",
            "reason": (
                "all containers unenforceable by import-linter "
                "(root-level .py modules, non-identifier dirs, or non-package tops)"
            ),
        }
    bin_path = find_import_linter()
    if not bin_path:
        return {
            **summary,
            "status": "enforcer-not-found",
            "reason": "lint-imports not found (pip install import-linter, or set VRP_IMPORT_LINTER_BIN)",
        }
    root_packages = sorted({m.split(".")[0] for c in contracts for m in c["source_modules"] + c["forbidden_modules"]})
    config = repo / CONF_NAME
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(build_config(contracts, root_packages), encoding="utf-8")
    try:
        result = _execute(bin_path, repo, config, timeout)
    finally:
        config.unlink(missing_ok=True)
    if result is None:
        return {
            **summary,
            "status": "enforcer-failed",
            "reason": f"lint-imports timed out after {timeout}s",
        }
    rc, stdout = result
    if rc == 0:
        violations: list[dict] = []
    elif "Broken contracts" in stdout:
        violations = parse_broken(stdout)
    else:
        return {
            **summary,
            "status": "enforcer-failed",
            "reason": f"lint-imports rc={rc} without a broken-contracts block (config/environment error)",
            "stdout_tail": stdout[-500:],
        }
    stats = _STATS.search(stdout)
    kept = int(stats.group(1)) if stats else None
    broken = int(stats.group(2)) if stats else None
    written = _write_findings(con, violations, contracts, structure_status)
    return {
        **summary,
        "status": "ok",
        "enforcer": bin_path,
        "kept": kept,
        "broken": broken,
        "violations": len(violations),
        **written,
    }
