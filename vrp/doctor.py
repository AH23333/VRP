"""I4 doctor: vrp 工具链环境自诊断（构想v11 甜点，npm doctor/rustup doctor 形态）。

设计三要点（#168）：
- 单一事实源：doctor 不重写任何探测逻辑——聚合各适配器已有的 find_* 发现函数
  （env 覆盖 → work 沙箱 → PATH 三级降级全部复用），doctor 只加"聚合 + 分层 +
  提示"这层皮。适配器改发现顺序，doctor 报告自动跟上。
- 分层语义：core（python>=3.11/git/node/CBM）缺失 = 管道无法端到端 → rc=1；
  engine（21 引擎外链 + scc）缺失 = 特定能力降级 → 只报告不判红。分层让
  doctor 在"部分安装"机器上诚实可用，而不是一票否决。
- 版本面诚实边界：git/node 走廉价 --version 探针（run_tree，15s 上限），
  python 报 sys 实况；其余工具只报定位路径不猜版本——版本钉位归 bootstrap.py
  （Z26），doctor 不做第二个版本真值源。
"""

from __future__ import annotations

import platform
import shutil
import sys

from . import procs
from . import s1_cbm, s2_astgrep, s2_cargo_mutants, s2_jscpd, s2_mutate_run, s2_p4, s2_stryker
from . import s5_rawpack, s6_difftastic, s7_enforce, s7_enforce_go, s7_enforce_ts
from . import render as render_mod
from . import xcheck as xcheck_mod

PROBE_TIMEOUT = 15

MIN_PYTHON = (3, 11)

# finder: (模块属性名, 函数名) 延迟解析——测试可 monkeypatch 模块属性。
def _spec(name: str, tier: str, hint: str, finder: tuple[object, str] | None = None) -> dict:
    return {"name": name, "tier": tier, "hint": hint, "finder": finder}


CORE_SPEC: list[dict] = [
    _spec("python", "core", "install Python >= 3.11"),
    _spec("git", "core", "install git (git-scm.com)"),
    _spec("node", "core", "install Node.js (nodejs.org)"),
    _spec("cbm", "core", "install codebase-memory-mcp (PATH or %LOCALAPPDATA%\\Programs\\codebase-memory-mcp)", (s1_cbm, "find_cbm")),
]

ENGINE_SPEC: list[dict] = [
    _spec("astgrep", "engine", "bootstrap.py work/astgrep 沙箱 or VRP_ASTGREP_BIN", (s2_astgrep, "find_astgrep")),
    _spec("jscpd", "engine", "bootstrap.py work/jscpd 沙箱 or VRP_JSCPD_BIN", (s2_jscpd, "find_jscpd")),
    _spec("knip", "engine", "bootstrap.py work/knip 沙箱 or VRP_KNIP_BIN", (s2_p4, "find_knip")),
    _spec("vulture", "engine", "pip install vulture", (s2_p4, "find_vulture")),
    _spec("mutmut", "engine", "pip install mutmut==2.4.5 or VRP_MUTMUT_BIN", (s2_mutate_run, "find_mutmut")),
    _spec("stryker", "engine", "bootstrap.py work/stryker 沙箱 or VRP_STRYKER_BIN", (s2_stryker, "find_stryker")),
    _spec("cargo-mutants", "engine", "cargo install cargo-mutants --locked or VRP_CARGO_MUTANTS_BIN", (s2_cargo_mutants, "find_cargo_mutants")),
    _spec("difftastic", "engine", "bootstrap.py 钉 DIFFT_VERSION or VRP_DIFFTASTIC_BIN", (s6_difftastic, "find_difftastic")),
    _spec("repomix", "engine", "set VRP_REPOMIX_BIN", (s5_rawpack, "find_bin")),
    _spec("import-linter", "engine", "pip install import-linter or VRP_IMPORT_LINTER_BIN", (s7_enforce, "find_import_linter")),
    _spec("depcruise", "engine", "bootstrap.py work/depcruise 沙箱 or VRP_DEPCRUISE_BIN", (s7_enforce_ts, "find_depcruise")),
    _spec("go", "engine", "install Go or VRP_GO_BIN", (s7_enforce_go, "find_go")),
    _spec("archify", "engine", "vrp/vendor/archify 收编缺失 or VRP_ARCHIFY_MJS", (render_mod, "find_archify_mjs")),
    _spec("code-atlas", "engine", "work/code-atlas-npx 沙箱 or VRP_CODE_ATLAS_JS", (xcheck_mod, "ca_js_path")),
    _spec("scc", "engine", "install scc for fast census (census_fallback 兜底)"),
]


def _probe_version(cmd: list[str]) -> str | None:
    """廉价 --version 探针：超时/非零/空输出 → None（可用性才是判定，版本只是信息）。"""
    res = procs.run_tree(cmd, timeout=PROBE_TIMEOUT)
    if res["timed_out"] or res["returncode"] != 0:
        return None
    line = (res["stdout"] or "").strip().splitlines()
    if not line:
        return None
    text = line[0].strip()
    # "git version 2.44.0" / "v22.x" 形态归一
    for prefix in ("git version ", "v"):
        if text.startswith(prefix):
            return text[len(prefix):].strip() or None
    return text


def _locate(finder: tuple[object, str] | None, name: str) -> tuple[str | None, str | None]:
    """返回 (located, version)；cbm 缺失抛 FileNotFoundError → 诚实转 (None, None)。"""
    if name == "python":
        return sys.executable, platform.python_version()
    if name == "git":
        found = shutil.which("git")
        if not found:
            return None, None
        return found, _probe_version(["git", "--version"])
    if name == "node":
        found = shutil.which("node")
        if not found:
            return None, None
        return found, _probe_version(["node", "--version"])
    if name == "scc":
        found = shutil.which("scc")
        return (found, None) if found else (None, None)
    if finder is None:
        return None, None
    module, func = finder
    try:
        found = getattr(module, func)()
    except FileNotFoundError:
        return None, None
    if not found:
        return None, None
    return str(found), None


def _check(spec: dict) -> dict:
    located, version = _locate(spec["finder"], spec["name"])
    return {
        "name": spec["name"],
        "tier": spec["tier"],
        "ok": located is not None,
        "located": located,
        "version": version,
        "hint": None if located is not None else spec["hint"],
    }


def run_doctor() -> dict:
    """聚合诊断：core 缺失名单 + engine 缺失名单 + 逐项检查面。"""
    checks = [_check(spec) for spec in CORE_SPEC + ENGINE_SPEC]
    missing_core = [c["name"] for c in checks if c["tier"] == "core" and not c["ok"]]
    missing_engine = [c["name"] for c in checks if c["tier"] == "engine" and not c["ok"]]
    return {
        "checked": len(checks),
        "ok": sum(1 for c in checks if c["ok"]),
        "missing_core": missing_core,
        "missing_engine": missing_engine,
        "python_ok": sys.version_info >= MIN_PYTHON,
        "checks": checks,
    }


def doctor_rc(result: dict) -> int:
    """rc 语义：core 缺失 → 1；engine 缺失只降级不判红。"""
    return 1 if result["missing_core"] else 0


def render_text(result: dict) -> str:
    lines = ["vrp doctor - 工具链环境自诊断", ""]
    for tier, title in (("core", "CORE（管道阻断层）"), ("engine", "ENGINE（能力降级层）")):
        lines.append(title)
        for c in result["checks"]:
            if c["tier"] != tier:
                continue
            if c["ok"]:
                ver = f" {c['version']}" if c["version"] else ""
                lines.append(f"  [ok]      {c['name']}{ver} - {c['located']}")
            else:
                lines.append(f"  [missing] {c['name']} - {c['hint']}")
        lines.append("")
    lines.append(
        f"summary: {result['ok']}/{result['checked']} ok"
        f" | core missing: {', '.join(result['missing_core']) or '-'}"
        f" | engine missing: {', '.join(result['missing_engine']) or '-'}"
    )
    if not result["python_ok"]:
        lines.append(f"WARNING: Python < {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")
    return "\n".join(lines)
