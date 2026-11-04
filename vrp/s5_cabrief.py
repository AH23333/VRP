"""S5 code-atlas one-shot MCP stdio client (v6 s7 预算化简报).

code-atlas CLI exposes only index/serve; repo_map and verify_changes are MCP
tools. This module speaks minimal MCP over stdio (initialize ->
notifications/initialized -> tools/call) against a light-mode server
(--no-watch --no-lsp --no-download --no-embeddings). Any failure degrades
gracefully per v6: the dossier falls back to the remaining views, the S6 gate
treats verify_changes as an optional cross-check.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

from . import __version__
from .xcheck import ca_js_path

PROTOCOL_VERSION = "2024-11-05"
SERVER_FLAGS = ("--no-watch", "--no-lsp", "--no-download", "--no-embeddings")


def _send(stdin, message: dict) -> None:
    stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    stdin.flush()


def _read_response(stdout, want_id: int) -> dict | None:
    for line in stdout:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == want_id:
            return message
    return None


def call_code_atlas(repo: Path, tool: str, arguments: dict, timeout: int = 120) -> dict:
    node = shutil.which("node")
    js = ca_js_path()
    if node is None:
        return {"status": "skipped", "reason": "node not found"}
    if js is None:
        return {"status": "skipped", "reason": "code-atlas dist not found (set VRP_CODE_ATLAS_JS)"}
    proc = subprocess.Popen(
        [node, str(js), "serve", "--root", str(repo), *SERVER_FLAGS],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = threading.Event()

    def _kill_on_timeout():
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, _kill_on_timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        _send(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "vrp", "version": __version__},
                },
            },
        )
        init = _read_response(proc.stdout, 1)
        if init is None:
            return {"status": "failed", "reason": "no initialize response" + (" (timeout)" if timed_out.is_set() else "")}
        _send(proc.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
        )
        response = _read_response(proc.stdout, 2)
        if response is None:
            return {"status": "failed", "reason": f"no {tool} response" + (" (timeout)" if timed_out.is_set() else "")}
    finally:
        watchdog.cancel()
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    if "error" in response:
        return {"status": "failed", "reason": str(response["error"])[:500]}
    content = response.get("result", {}).get("content", [])
    text = content[0].get("text", "") if content else ""
    if not text:
        return {"status": "failed", "reason": f"empty {tool} content"}
    return {"status": "ok", "text": text, "chars": len(text)}


def repo_map_brief(repo: Path, max_tokens: int = 4000, timeout: int = 120) -> dict:
    result = call_code_atlas(repo, "repo_map", {"max_tokens": max_tokens}, timeout)
    if result["status"] == "ok":
        result["max_tokens"] = max_tokens
    return result
