"""S0 precheck: git snapshot, language census, command discovery."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .evidence import EXT_LANGUAGES, language_for_path

IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    "build",
    "_build",
    "dist",
    ".code-atlas",
    ".codebase-memory",
    ".vrp",
}

# P-2（跨项目实测 HomeWork 案例）：内容仓库探针。JSON/HTML/PDF 等内容文件
# 占绝对多数、零源码文件、无构建系统标记的仓库（课件/笔记/素材站）对代码
# 分析不适用——precheck 提前给出结论，'vrp run' 据此跳过注定失败的大内存
# 索引尝试。判定保守（三条件缺一不可），提示语义而非硬门：单命令
# 'vrp index' / 'vrp etl' 仍可手动执行。
CONTENT_EXTS = frozenset(
    {
        ".json", ".html", ".htm", ".pdf", ".md", ".markdown", ".txt", ".rst",
        ".xml", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".ipynb",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp",
        ".mp3", ".mp4", ".wav", ".flac", ".avi", ".mov", ".mkv",
        ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".epub",
        ".zip", ".tar", ".gz", ".7z", ".rar",
    }
)
# 源码判定复用 evidence.EXT_LANGUAGES（单一事实源），剔除数据/标记格式：
# 它们同时出现在内容仓与代码仓，不能作为"这是代码仓"的证据。
_DATA_MARKUP_EXTS = frozenset({".json", ".yaml", ".yml", ".toml", ".md"})
SOURCE_EXTS = frozenset(EXT_LANGUAGES) - _DATA_MARKUP_EXTS
BUILD_MARKER_FILES = (
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Makefile",
    "CMakeLists.txt", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "composer.json", "mix.exs", "deno.json", "meson.build",
)


def content_repo_probe(repo: Path) -> dict:
    """P-2 内容仓库探针（heuristic hint，非硬门）。

    detected=True 仅当三条件同时满足：内容文件占比 > 90%、零源码文件
    （SOURCE_EXTS 任一出现即豁免——最强守卫，防误杀）、根目录无构建系统
    标记（package.json/Cargo.toml 等任一存在即否决）。
    """
    repo = repo.resolve()
    total = content = source = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for fname in files:
            total += 1
            ext = Path(fname).suffix.lower()
            if ext in CONTENT_EXTS:
                content += 1
            elif ext in SOURCE_EXTS:
                source += 1
    markers = [name for name in BUILD_MARKER_FILES if (repo / name).exists()]
    markers += sorted(p.name for p in repo.glob("*.csproj"))
    markers += sorted(p.name for p in repo.glob("*.sln"))
    share = round(100.0 * content / total, 1) if total else 0.0
    detected = bool(total) and share > 90.0 and source == 0 and not markers
    return {
        "detected": detected,
        "total_files": total,
        "content_files": content,
        "source_files": source,
        "content_share_pct": share,
        "build_markers": markers,
    }


def _run(cmd: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
    """运行外部命令；超时响亮失败（RuntimeError）而非无限挂死。

    git 本地子命令（rev-parse/status）无网络、无锁等待、无 stdin，能挂
    住 120s 的只剩死盘/网络驱动器级故障——届时 is_repo/dirty 的任何
    "降级"取值都是静默谎言（脏树前置是 R-A 的硬保证），必须 fail-fast
    让 cli 以 rc 1 诚实退出。
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git command timed out after {timeout}s: {' '.join(cmd)}") from exc
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def git_snapshot(repo: Path) -> dict:
    if not (repo / ".git").exists():
        return {"is_repo": False}
    rc_head, head = _run(["git", "rev-parse", "HEAD"], repo)
    rc_branch, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
    rc_status, status = _run(["git", "status", "--porcelain"], repo)
    dirty_files = [line[3:] for line in status.splitlines() if line.strip()]
    return {
        "is_repo": rc_head == 0,
        "head": head.strip() if rc_head == 0 else None,
        "branch": branch.strip() if rc_branch == 0 else None,
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files[:200],
        "dirty_count": len(dirty_files),
    }


def census_scc(repo: Path) -> dict | None:
    if not shutil.which("scc"):
        return None
    try:
        proc = subprocess.run(
            ["scc", "--by-file", "--format", "json", "."],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        # census 是展示性数据：降级到 census_fallback（既有路径），
        # 超时不炸 precheck
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    languages = []
    for lang in data:
        languages.append(
            {
                "name": lang.get("Name", ""),
                "files": lang.get("Count", 0),
                "lines": lang.get("Lines", 0),
                "code": lang.get("Code", 0),
            }
        )
    languages.sort(key=lambda x: -x["code"])
    return {"tool": "scc", "languages": languages[:20]}


def census_fallback(repo: Path) -> dict:
    counts: dict[str, dict] = {}
    # os.walk 原地剪枝：IGNORE_DIRS 目录不深入（rglob 全遍历会走进
    # .git/node_modules 深处，纯浪费时间——行为等价，计数不变）
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for fname in files:
            path = Path(root) / fname
            lang = language_for_path(str(path))
            if not lang:
                continue
            entry = counts.setdefault(lang, {"name": lang, "files": 0, "lines": 0, "code": 0})
            entry["files"] += 1
            try:
                with path.open("rb") as fh:
                    entry["lines"] += sum(1 for _ in fh)
            except OSError:
                pass
            entry["code"] = entry["lines"]
    languages = sorted(counts.values(), key=lambda x: -x["code"])
    return {"tool": "fallback-ext", "languages": languages[:20]}


_SCRIPT_KEY_MAP = {
    "build": "build",
    "compile": "build",
    "test": "test",
    "test:unit": "test",
    "lint": "lint",
    "eslint": "lint",
    "typecheck": "typecheck",
    "type-check": "typecheck",
    "check": "typecheck",
}


def discover_commands(repo: Path) -> dict:
    commands: dict[str, list[str]] = {"build": [], "test": [], "lint": [], "typecheck": []}

    pkg = repo / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            data = None
        scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
        if not isinstance(scripts, dict):
            scripts = {}
        for key, value in scripts.items():
            slot = _SCRIPT_KEY_MAP.get(key)
            if slot and isinstance(value, str):
                commands[slot].append(f"npm run {key}")

    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        if "pytest" in text:
            commands["test"].append("pytest")
        if "ruff" in text:
            commands["lint"].append("ruff check .")
        if "mypy" in text:
            # bare `mypy` is a usage error (no target); `mypy .` typechecks
            # the repo tree and still honors [tool.mypy] in pyproject.toml
            commands["typecheck"].append("mypy .")

    makefile = repo / "Makefile"
    if makefile.exists():
        text = makefile.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("test:"):
                commands["test"].append("make test")
            elif line.startswith("build:"):
                commands["build"].append("make build")
            elif line.startswith("lint:"):
                commands["lint"].append("make lint")

    if (repo / "Cargo.toml").exists():
        commands["build"].append("cargo build")
        commands["test"].append("cargo test")
        commands["lint"].append("cargo clippy")

    if (repo / "go.mod").exists():
        commands["build"].append("go build ./...")
        commands["test"].append("go test ./...")

    return {k: v for k, v in commands.items() if v}


def precheck(repo: Path) -> dict:
    repo = repo.resolve()
    result = {
        "repo": str(repo),
        "name": repo.name,
        "git": git_snapshot(repo),
        "commands": discover_commands(repo),
    }
    census = census_scc(repo)
    if census is None:
        census = census_fallback(repo)
    result["languages"] = census
    result["content_repo"] = content_repo_probe(repo)
    out_dir = repo / ".vrp"
    out_dir.mkdir(exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    (out_dir / "precheck.json").write_text(payload, encoding="utf-8")
    return result
