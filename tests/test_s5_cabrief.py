import json
import sys
import threading
from pathlib import Path

import pytest

from vrp import s5_cabrief

FAKE_SERVER = """
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
        print(json.dumps(out), flush=True)
    elif msg.get("method") == "notifications/initialized":
        continue
    elif msg.get("method") == "tools/call":
        args = msg["params"]["arguments"]
        text = "fake repo map, max_tokens=" + str(args.get("max_tokens"))
        out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}]}}
        print(json.dumps(out), flush=True)
"""


def test_repo_map_brief_skipped_without_dist(tmp_path, monkeypatch):
    monkeypatch.setattr("vrp.s5_cabrief.ca_js_path", lambda: None)
    result = s5_cabrief.repo_map_brief(tmp_path)
    assert result["status"] == "skipped"


def test_send_and_read_roundtrip(tmp_path):
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    proc = __import__("subprocess").Popen(
        [sys.executable, str(script)],
        stdin=__import__("subprocess").PIPE,
        stdout=__import__("subprocess").PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        s5_cabrief._send(proc.stdin, {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}})
        response = s5_cabrief._read_response(proc.stdout, 7)
        assert response["id"] == 7
        assert response["result"]["protocolVersion"] == "2024-11-05"
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_read_response_skips_nondict_and_noise_lines():
    """R-X：合法 JSON 非 dict 行（标量/数组，异源 stdout 噪音的一种形态）与坏 JSON
    行同列跳过——旧代码 42.get("id") 裸抛 AttributeError，打穿整个 cabrief。"""
    lines = iter(["42\n", '"noise"\n', "not-json\n", '{"jsonrpc": "2.0", "id": 7, "result": {}}\n'])
    response = s5_cabrief._read_response(lines, 7)
    assert response["id"] == 7


HANG_SERVER = """
import subprocess
import sys

# 孙进程继承 stdout（写端持有者）后父进程进入 stdin 循环、永不响应——
# 单杀直子时孙进程使 EOF 永不到达（R-L 孤儿持管道机制）。
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
for line in sys.stdin:
    pass
"""


def test_call_code_atlas_watchdog_kills_tree_not_just_child(tmp_path, monkeypatch):
    """R-Y：watchdog 必须树杀——孙进程持 stdout 写端时 proc.kill() 单杀直子使
    EOF 永不到达，_read_response 永久挂死（watchdog 只触发一次）。修后
    kill_tree 整树击杀 -> EOF -> 诚实 failed (timeout) 返回。"""
    script = tmp_path / "hang_server.py"
    script.write_text(HANG_SERVER, encoding="utf-8")
    monkeypatch.setattr(s5_cabrief, "ca_js_path", lambda: script)
    monkeypatch.setattr(s5_cabrief.shutil, "which", lambda name: sys.executable if name == "node" else None)
    holder: dict = {}

    def _run():
        holder["result"] = s5_cabrief.call_code_atlas(tmp_path, "repo_map", {}, timeout=2)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=30)  # 安全网非判据：挂死=响亮红，30s/2s=15x 余量
    assert not worker.is_alive(), "call_code_atlas hung: watchdog kill left a pipe-holding grandchild alive"
    result = holder["result"]
    assert result["status"] == "failed"
    assert "timeout" in result["reason"]


@pytest.mark.skipif(s5_cabrief.ca_js_path() is None, reason="code-atlas dist not installed")
def test_repo_map_brief_real():
    repo = Path(__file__).parent.parent / "verify" / "code-atlas-lusiem"
    result = s5_cabrief.repo_map_brief(repo, max_tokens=2000, timeout=180)
    assert result["status"] == "ok", result
    assert result["chars"] > 100
    assert "max_tokens" in result
    payload = json.dumps(result["text"])
    assert len(payload) > 100
