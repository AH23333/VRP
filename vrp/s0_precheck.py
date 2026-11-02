"""S0 precheck: git snapshot, language census, command discovery."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .evidence import language_for_path

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
    "dist",
    ".code-atlas",
    ".codebase-memory",
    ".vrp",
}


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
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
    proc = subprocess.run(
        ["scc", "--by-file", "--format", "json", "."],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
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
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(repo).parts
        if any(p in IGNORE_DIRS for p in rel_parts):
            continue
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
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except (json.JSONDecodeError, OSError):
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
            commands["typecheck"].append("mypy")

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
    out_dir = repo / ".vrp"
    out_dir.mkdir(exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    (out_dir / "precheck.json").write_text(payload, encoding="utf-8")
    return result
