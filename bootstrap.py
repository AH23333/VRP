"""vrp 环境自举：重建 work/ 工具沙箱 + 安装 Python 依赖（全部钉版本）。

用法：
    python bootstrap.py                     # npm 六沙箱 + difftastic + pip 依赖
    python bootstrap.py --skip-npm          # 跳过 npm 沙箱
    python bootstrap.py --skip-pip          # 跳过 pip 安装
    python bootstrap.py --skip-difft        # 跳过 difftastic 下载
    python bootstrap.py --with-cargo-mutants  # 额外 cargo install cargo-mutants（Rust 工具链，~25min 编译）

前置：Python >= 3.11、Node.js + npm（沙箱与 code-atlas/difftastic 需要）、git。
产物：work/ 下的工具沙箱（已被 .gitignore 忽略，可随时删除后重跑本脚本重建）。
不在本脚本范围的系统级安装（提示后人工执行）：
    codebase-memory-mcp（CBM，S1 索引核心）—— https://github.com/DeusData/codebase-memory-mcp/releases （0.12.1）
    Node.js —— https://nodejs.org/
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"

NPM_SANDBOXES: list[tuple[str, dict[str, str]]] = [
    ("astgrep", {"@ast-grep/cli": "0.51.0"}),
    ("knip", {"knip": "6.44.1"}),
    ("jscpd", {"jscpd": "5.4.2"}),
    (
        "stryker",
        {
            "@stryker-mutator/core": "10.1.1",
            "@stryker-mutator/vitest-runner": "10.1.1",
            "typescript": "5.9.3",
        },
    ),
    ("repomix", {"repomix": "1.18.1"}),
    ("code-atlas-npx", {"@lusiem/code-atlas": "0.6.0"}),
]

PIP_CORE = ["pytest==9.1.1", "mutmut==2.4.5", "vulture==2.16"]
PIP_DEEP = [
    "hypothesis==6.170.14",
    "ruff==0.17.3",
    "mypy==2.4.1",
    "pytest-xdist==3.8.0",
    "pytest-timeout==2.4.0",
    "pytest-benchmark==5.3.1",
    "pytest-rerunfailures==16.7",
]

DIFFT_VERSION = "0.72.0"
DIFFT_URL = (
    "https://github.com/Wilfred/difftastic/releases/download/"
    f"{DIFFT_VERSION}/difft-{DIFFT_VERSION}-x86_64-pc-windows-msvc.zip"
)
DIFFT_EXE = WORK / "difftastic" / "difft.exe"

CARGO_MUTANTS_VERSION = "27.1.0"
CBM_URL = "https://github.com/DeusData/codebase-memory-mcp/releases"
CBM_VERSION = "0.12.1"

ARCHIFY_URL = "https://github.com/tt-a1i/archify.git"
ARCHIFY_COMMIT = "25cf3708"  # v3.2.0 (pinned)
ARCHIFY_MJS = WORK / "archify" / "archify" / "bin" / "archify.mjs"


def _run(cmd: list[str], **kwargs) -> int:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, **kwargs).returncode


def _npm_bin() -> str:
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm not found - install Node.js first: https://nodejs.org/")
    return npm


def install_npm_sandboxes() -> None:
    npm = _npm_bin()
    for name, deps in NPM_SANDBOXES:
        target = WORK / name
        target.mkdir(parents=True, exist_ok=True)
        manifest = target / "package.json"
        payload = {"name": f"vrp-sandbox-{name}", "private": True, "version": "1.0.0", "dependencies": deps}
        manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"[npm] {name}: {json.dumps(deps, sort_keys=True)}")
        rc = _run([npm, "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=target)
        if rc != 0:
            raise SystemExit(f"npm install failed for sandbox {name} (rc={rc})")


def install_difftastic() -> None:
    if not platform.system().startswith("Windows"):
        print("[difft] non-Windows host - install difftastic via your package manager or `cargo install difftastic`")
        return
    if DIFFT_EXE.is_file():
        print(f"[difft] {DIFFT_EXE} already present - skip")
        return
    DIFFT_EXE.parent.mkdir(parents=True, exist_ok=True)
    archive = DIFFT_EXE.parent / f"difft-{DIFFT_VERSION}.zip"
    print(f"[difft] downloading {DIFFT_URL}")
    with urllib.request.urlopen(DIFFT_URL, timeout=300) as resp, archive.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    with zipfile.ZipFile(archive) as zf:
        member = next(m for m in zf.namelist() if m.endswith("difft.exe"))
        DIFFT_EXE.write_bytes(zf.read(member))
    archive.unlink()
    print(f"[difft] installed {DIFFT_EXE}")


def install_pip() -> None:
    packages = PIP_CORE + PIP_DEEP
    print(f"[pip] {', '.join(packages)}")
    rc = _run([sys.executable, "-m", "pip", "install", *packages])
    if rc != 0:
        raise SystemExit(f"pip install failed (rc={rc})")


def install_cargo_mutants() -> None:
    cargo = shutil.which("cargo")
    if not cargo:
        print("[cargo-mutants] cargo not found - install Rust toolchain first")
        return
    rc = _run(
        [cargo, "install", "cargo-mutants", "--locked", "--version", CARGO_MUTANTS_VERSION]
    )
    if rc != 0:
        raise SystemExit(f"cargo install cargo-mutants failed (rc={rc})")


def install_archify() -> None:
    """S8 渲染工具：优先复制同级已有 archify 项目（勿重复拉取），缺失才 git clone 钉 commit。

    同级项目 = ROOT.parent / "archify"（本仓外的工作区 checkout，用户维护）；
    复制其 archify/ 包子目录。版本跟随同级 checkout（当前 25cf3708 与钉值一致）。
    """
    target = WORK / "archify"
    if ARCHIFY_MJS.is_file():
        print(f"[archify] {ARCHIFY_MJS} already present - skip")
        return
    sibling = ROOT.parent / "archify" / "archify"
    if (sibling / "bin" / "archify.mjs").is_file():
        if target.exists():
            shutil.rmtree(target)
        print(f"[archify] copying sibling project {sibling} -> {target / 'archify'}")
        shutil.copytree(sibling, target / "archify")
        if ARCHIFY_MJS.is_file():
            print(f"[archify] installed (copied) {ARCHIFY_MJS}")
            return
        print("[archify] sibling copy incomplete - falling back to clone")
    git = shutil.which("git")
    if not git:
        print("[archify] git not found - install git first")
        return
    if target.exists():
        shutil.rmtree(target)
    print(f"[archify] cloning {ARCHIFY_URL} @ {ARCHIFY_COMMIT}")
    rc = _run([git, "clone", ARCHIFY_URL, str(target)])
    if rc != 0:
        raise SystemExit(f"git clone archify failed (rc={rc})")
    rc = _run([git, "-C", str(target), "checkout", ARCHIFY_COMMIT])
    if rc != 0:
        raise SystemExit(f"archify checkout {ARCHIFY_COMMIT} failed (rc={rc})")
    if not ARCHIFY_MJS.is_file():
        raise SystemExit(f"archify.mjs not found after checkout: {ARCHIFY_MJS}")
    print(f"[archify] installed {ARCHIFY_MJS}")


def _exists(rel: str) -> bool:
    return (ROOT / rel).exists()


def report() -> int:
    checks: list[tuple[str, str, bool]] = [
        ("CBM (S1 索引)", "codebase-memory-mcp on PATH", shutil.which("codebase-memory-mcp") is not None or shutil.which("cbm") is not None),
        ("ast-grep (S2)", "work/astgrep/node_modules/@ast-grep/*/ast-grep.exe", _glob_exists("work/astgrep/node_modules/@ast-grep/*/ast-grep*")),
        ("knip (S2 P4)", "work/knip/node_modules/.bin/knip*", _glob_exists("work/knip/node_modules/.bin/knip*")),
        ("jscpd (S2 P2)", "work/jscpd/node_modules/.bin/jscpd*", _glob_exists("work/jscpd/node_modules/.bin/jscpd*")),
        ("stryker (S2 变异)", "work/stryker/node_modules/.bin/stryker*", _glob_exists("work/stryker/node_modules/.bin/stryker*")),
        ("repomix (S5 rawpack)", "work/repomix/node_modules/.bin/repomix*", _glob_exists("work/repomix/node_modules/.bin/repomix*")),
        ("code-atlas (xcheck)", "work/code-atlas-npx/node_modules/@lusiem/code-atlas/dist/index.js", _exists("work/code-atlas-npx/node_modules/@lusiem/code-atlas/dist/index.js")),
        ("difftastic (G4 结构 diff)", "work/difftastic/difft.exe", DIFFT_EXE.is_file()),
        ("archify (S8 渲染)", "work/archify/archify/bin/archify.mjs", ARCHIFY_MJS.is_file()),
        ("mutmut (S2 变异)", "mutmut on PATH", shutil.which("mutmut") is not None),
        ("vulture (S2 P4)", "vulture on PATH", shutil.which("vulture") is not None),
        ("cargo-mutants (S2 变异)", "cargo-mutants on PATH", shutil.which("cargo-mutants") is not None),
        ("pytest", "pytest on PATH", shutil.which("pytest") is not None),
    ]
    print("\n=== 就绪状态 ===")
    for label, where, ok in checks:
        print(f"  {'OK  ' if ok else 'MISS'} {label}  ({where})")
    missing_cbm = not checks[0][2]
    if missing_cbm:
        print(f"\nCBM 未安装：到 {CBM_URL} 下载 {CBM_VERSION} 安装（S1 索引核心，pip/npm 均不可替代）")
    return 0


def _glob_exists(pattern: str) -> bool:
    return any(ROOT.glob(pattern))


def main() -> None:
    parser = argparse.ArgumentParser(description="vrp 环境自举（钉版本重建工具沙箱）")
    parser.add_argument("--skip-npm", action="store_true", help="跳过 npm 六沙箱")
    parser.add_argument("--skip-pip", action="store_true", help="跳过 pip 依赖")
    parser.add_argument("--skip-difft", action="store_true", help="跳过 difftastic 下载")
    parser.add_argument("--with-cargo-mutants", action="store_true", help="额外安装 cargo-mutants（需 Rust 工具链）")
    parser.add_argument("--with-archify", action="store_true", help="额外安装 archify 到 work/ 沙箱（S8 渲染，git clone 钉 commit）")
    args = parser.parse_args()

    if sys.version_info < (3, 11):
        raise SystemExit("Python >= 3.11 required")
    WORK.mkdir(exist_ok=True)

    if not args.skip_pip:
        install_pip()
    if not args.skip_npm:
        install_npm_sandboxes()
    if not args.skip_difft:
        install_difftastic()
    if args.with_cargo_mutants:
        install_cargo_mutants()
    if args.with_archify:
        install_archify()
    report()


if __name__ == "__main__":
    main()
