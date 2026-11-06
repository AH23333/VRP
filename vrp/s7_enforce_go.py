"""S7 架构执法 Go 引擎：go list 原生导入图子进程（构想v7 §4 语言矩阵扩展）。

与 import-linter（Python）/dependency-cruiser（TS/JS）同位：s7_enforce.run()
按语言路由——.go 容器目录 → 本引擎。执法器 = Go 工具链自身（`go list -e -json
./...`），导入图编译器解析（非正则猜测），零第三方依赖。

语义与 import-linter forbidden 契约对齐：**传递闭包**违规——source 包经任意
in-module import 链到达 forbidden 包即违例；BFS 最短链入 payload（chain），
违例锚定 = 链上最后一条直接 import 边所在文件（扫描该 import path 的带引号
字面量定位行号；找不到则文件首符号行，诚实 line=None）。

探针事实（2026-11-06，go 1.26.10 实测）：
- `go list -e -json ./...`：-e 容错（缺 go.sum/外部依赖时包级 Error 继续），
  错误包从图剔除并 errored_packages 计数公开——静默缺图不诚实
- stdout 为连续 JSON 对象流（非数组）——JSONDecoder.raw_decode 循环解析
- 生产导入用 Imports 字段；TestImports/XTestImports（_test.go）不参与层执法
- in-module 判定 = ImportPath 以 go.mod module path 为前缀；root 包 rel dir
  记 "."（容器推导从 .go 文件路径出发不会产生 "."，天然不重叠）
- rc≠0：无 go.mod / 空模块（"no Go files"）——enforcer-failed/not-enforceable
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

GO_SUFFIX = ".go"


def find_go() -> str | None:
    """Go 工具链发现：PATH（VRP_GO_BIN 覆盖）。"""
    env = os.environ.get("VRP_GO_BIN")
    if env and Path(env).is_file():
        return env
    return shutil.which("go")


def _go_dir(file_path: str) -> str | None:
    """Go 文件 → 包目录（posix；根级 → "."）；非 .go/vendor → None。"""
    p = file_path.replace("\\", "/")
    if not p.lower().endswith(GO_SUFFIX):
        return None
    if p.startswith(".") or "/vendor/" in p or p.startswith("vendor/"):
        return None
    slash = p.rfind("/")
    return p[:slash] if slash > 0 else "."


def derive_go_containers(con: sqlite3.Connection, payload: dict) -> dict:
    """module_id → 排序去重包目录；非 .go 扩展计入 extensions 观测。"""
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
            name = _go_dir(file_path or "")
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


def _module_path(repo: Path) -> str | None:
    """go.mod module 行；缺失/无 module 行 → None。"""
    gomod = repo / "go.mod"
    if not gomod.is_file():
        return None
    try:
        text = gomod.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        m = re.match(r"^module\s+(\S+)", line)
        if m:
            return m.group(1)
    return None


def _execute(repo: Path, timeout: int):
    """go list 子进程（可测缝）：返回 (rc, stdout, stderr) 或 None=超时。"""
    go = find_go()
    if not go:
        return None, None, None  # 调用方先行检查 find_go，此返回仅防御
    try:
        proc = subprocess.run(
            [go, "list", "-e", "-json", "./..."],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, None, None
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _parse_json_stream(text: str) -> list[dict]:
    """连续 JSON 对象流 → 对象列表（go list -json 输出形态，非数组）。"""
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        obj, end = decoder.raw_decode(text, idx)
        if isinstance(obj, dict):
            objects.append(obj)
        idx = end
    return objects


def _import_graph(packages: list[dict], module_path: str) -> tuple[dict[str, list[str]], dict[str, dict], int]:
    """包流 → (rel_dir → in-module 直接 import 的 rel_dir 列表, rel_dir → 原始包对象, 错误包数)。

    错误包（-e 下带 Error 字段）剔除出图并计数——诚实上报不静默。
    """
    rel_of: dict[str, str] = {}
    for pkg in packages:
        ip = pkg.get("ImportPath") or ""
        if ip == module_path:
            rel_of[ip] = "."
        elif ip.startswith(module_path + "/"):
            rel_of[ip] = ip[len(module_path) + 1 :]
    graph: dict[str, list[str]] = {}
    pkg_by_dir: dict[str, dict] = {}
    errored = 0
    for pkg in packages:
        ip = pkg.get("ImportPath") or ""
        if ip not in rel_of:
            continue
        if pkg.get("Error"):
            errored += 1
            continue
        rel = rel_of[ip]
        imports = {rel_of[i] for i in (pkg.get("Imports") or []) if i in rel_of and i != ip}
        graph[rel] = sorted(imports)
        pkg_by_dir[rel] = pkg
    return graph, pkg_by_dir, errored


def _belongs(rel_dir: str, prefixes: list[str]) -> bool:
    """包目录是否属于容器组（前缀 + '/' 边界，根容器精确匹配）。"""
    return any(rel_dir == p or rel_dir.startswith(p + "/") for p in prefixes)


def _find_violations(graph: dict[str, list[str]], contracts: list[dict]) -> list[dict]:
    """每契约：source 包 BFS 最短链到达 forbidden 包 → 违例（链入 payload）。"""
    violations: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for contract in contracts:
        sources = [d for d in graph if _belongs(d, contract["source_modules"])]
        forbidden = {d for d in graph if _belongs(d, contract["forbidden_modules"])}
        for start in sources:
            prev: dict[str, str | None] = {start: None}
            queue = [start]
            hit = None
            while queue and hit is None:
                cur = queue.pop(0)
                for nxt in graph.get(cur, []):
                    if nxt in prev:
                        continue
                    prev[nxt] = cur
                    if nxt in forbidden:
                        hit = nxt
                        break
                    queue.append(nxt)
            if hit is None:
                continue
            chain: list[str] = []
            node: str | None = hit
            while node is not None:
                chain.append(node)
                node = prev[node]
            chain.reverse()
            key = (contract["name"], start, hit)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "contract": contract["name"],
                    "source_layer": contract.get("source_layer"),
                    "forbidden_layer": contract.get("forbidden_layer"),
                    "importer": start,
                    "imported": hit,
                    "chain": chain,
                }
            )
    return violations


def _locate_import(repo: Path, pkg: dict, target_rel_dir: str, module_path: str) -> tuple[str, int | None] | None:
    """违例链最后一条直接 import 边（pkg → forbidden 包）的文件与行号。

    扫描包 GoFiles 中带引号的 import path 字面量；找不到行号 → (文件, None)；
    包无 Go 文件 → None。
    """
    go_files = pkg.get("GoFiles") or []
    if not go_files:
        return None
    pkg_dir = pkg.get("Dir") or ""
    try:
        rel_pkg_dir = Path(pkg_dir).resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        rel_pkg_dir = ""
    import_path = module_path if target_rel_dir == "." else f"{module_path}/{target_rel_dir}"
    first_file: str | None = None
    for gf in go_files:
        rel_file = f"{rel_pkg_dir}/{gf}" if rel_pkg_dir else gf
        if first_file is None:
            first_file = rel_file
        try:
            text = (Path(pkg_dir) / gf).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if f'"{import_path}"' in line:
                return rel_file, i
    return (first_file, None) if first_file else None


def _write_findings(
    con: sqlite3.Connection,
    violations: list[dict],
    structure_status: str,
) -> dict:
    """违例 → P9 findings（幂等 replace-all）；锚定=违例 import 文件首符号。"""
    con.execute("DELETE FROM findings WHERE source = ?", (FINDINGS_SOURCE,))
    rows = []
    anchored = 0
    for v in violations:
        row = con.execute(
            "SELECT fid, start_line FROM symbols WHERE file_path = ? ORDER BY start_line LIMIT 1",
            (v["file_path"],),
        ).fetchone()
        anchor, sym_line = (row[0], row[1]) if row else (None, None)
        if anchor:
            anchored += 1
        line = v["line"] if v.get("line") is not None else sym_line
        payload = json.dumps(
            {
                "contract": v["contract"],
                "source_layer": v.get("source_layer"),
                "forbidden_layer": v.get("forbidden_layer"),
                "importer": v["importer"],
                "imported": v["imported"],
                "chain": v["chain"],
                "line": v.get("line"),
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
                v["file_path"],
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


def run_go(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Go 执法入口：契约生成（复用层 DAG）→ go list 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "go-list",
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
    go_bin = find_go()
    if not go_bin:
        return {
            **summary,
            "status": "enforcer-not-found",
            "reason": "go toolchain not found (install Go, or set VRP_GO_BIN)",
        }
    module_path = _module_path(repo)
    if module_path is None:
        return {
            **summary,
            "status": "not-enforceable",
            "reason": "no go.mod (not a Go module)",
        }
    result = _execute(repo, timeout)
    if result[0] is None:
        return {
            **summary,
            "status": "enforcer-failed",
            "reason": f"go list timed out or failed to start after {timeout}s",
        }
    rc, stdout, stderr = result
    if rc != 0:
        return {
            **summary,
            "status": "enforcer-failed",
            "reason": f"go list rc={rc}",
            "stderr_tail": stderr[-500:],
        }
    try:
        packages = _parse_json_stream(stdout)
    except json.JSONDecodeError:
        return {
            **summary,
            "status": "enforcer-failed",
            "reason": "go list output is not a valid JSON stream",
            "stdout_tail": stdout[-500:],
        }
    graph, pkg_by_dir, errored = _import_graph(packages, module_path)
    summary["packages"] = len(graph)
    if errored:
        summary["errored_packages"] = errored
    if not graph:
        return {
            **summary,
            "status": "not-enforceable",
            "reason": "go list returned no in-module packages",
        }
    violations = _find_violations(graph, contracts)
    for v in violations:
        # 锚定：链最后一条直接 import 边（chain[-2] → chain[-1]）所在文件
        edge_pkg = pkg_by_dir.get(v["chain"][-2]) if len(v["chain"]) >= 2 else None
        located = _locate_import(repo, edge_pkg, v["chain"][-1], module_path) if edge_pkg else None
        if located is None:
            # 兜底：违例 import 包自身首文件（无 GoFiles 的极端包 → 目录串，锚定失败计入未锚定）
            fallback_pkg = pkg_by_dir.get(v["importer"])
            if fallback_pkg and fallback_pkg.get("GoFiles"):
                pkg_dir = fallback_pkg.get("Dir") or ""
                try:
                    rel_pkg_dir = Path(pkg_dir).resolve().relative_to(repo.resolve()).as_posix()
                except ValueError:
                    rel_pkg_dir = ""
                gf = fallback_pkg["GoFiles"][0]
                v["file_path"] = f"{rel_pkg_dir}/{gf}" if rel_pkg_dir else gf
            else:
                v["file_path"] = v["importer"]
            v["line"] = None
            continue
        v["file_path"], v["line"] = located
    written = _write_findings(con, violations, structure_status)
    return {
        **summary,
        "status": "ok",
        "enforcer": go_bin,
        "violations": len(violations),
        **written,
    }
