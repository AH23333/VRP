"""S5 raw pack view: repomix CLI subprocess (v6 s7 原始上下文包, 接入零改造)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

OUTPUT_NAME = "repomix-output.xml"
IGNORE_PATTERNS = ".vrp,.codebase-memory,.code-atlas"


def find_bin() -> Path | None:
    env = os.environ.get("VRP_REPOMIX_BIN")
    if env and Path(env).is_file():
        return Path(env)
    local = (
        Path(__file__).resolve().parent.parent
        / "work"
        / "repomix"
        / "node_modules"
        / ".bin"
        / "repomix.cmd"
    )
    if local.is_file():
        return local
    return None


def build_raw_pack(repo: Path, timeout: int = 600) -> dict:
    bin_path = find_bin()
    if bin_path is None:
        return {"status": "skipped", "reason": "repomix not found (set VRP_REPOMIX_BIN)"}
    out = repo / ".vrp" / OUTPUT_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    # L3: let repomix write to a same-directory temp file and swap it in
    # atomically, so a failed/timed-out run never truncates the old artifact.
    tmp = out.with_name(f"{out.name}.tmp-{os.getpid()}")
    tmp.unlink(missing_ok=True)
    try:
        proc = subprocess.run(
            [
                str(bin_path),
                str(repo),
                "--style",
                "xml",
                "-o",
                str(tmp),
                "--ignore",
                IGNORE_PATTERNS,
                "--quiet",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        tmp.unlink(missing_ok=True)
        raise
    if proc.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        return {"status": "failed", "reason": (proc.stderr or proc.stdout)[-500:]}
    os.replace(tmp, out)
    data = out.read_bytes()
    return {
        "status": "ok",
        "path": str(out),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest()[:16],
    }
