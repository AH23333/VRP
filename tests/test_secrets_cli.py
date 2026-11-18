"""H1（构想v11 甜点）：secrets 独立化测试。

- 检测器单源锁：本模块零检测逻辑（mask_source_lines 复用），七型样张
  （aws/github/gitlab/slack/google/openai/jwt）+URL 凭据+私钥块全谱
- worktree 面：命中归位（file/line/type/preview）+正常代码零误报+binary
  跳过计数+大小封顶+IGNORE_DIRS 剪枝（.vrp 不扫）+确定性排序
- 历史面（杀手锏）：已删文件的密钥在 blob 仍发现；大小封顶 blob 跳过；
  零密钥仓零命中；desync 诚实红
- procs.stream_lines：行流+input_text+超时杀树+close 回收
- rc 语义（gitleaks 同型）：命中 1 零命中 0；CLI E2E
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vrp import procs, secrets
from vrp.cli import main as cli_main

AK = "AKIAIOSFODNN7EXAMPLE"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sec-repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def test_detector_single_source_all_seven_types_plus_url_and_pk(tmp_path):
    repo = _mk_repo(tmp_path)
    src = "\n".join(
        [
            f"aws_key = '{AK}'",
            f"gh = '{GH}'",
            "gitlab = 'glpat-" + "x1" * 12 + "'",
            "slack = 'xoxb-" + "y2" * 8 + "'",
            "google = 'AIza" + "z" * 35 + "'",
            "openai = 'sk-" + "w3" * 12 + "'",
            f"jwt = '{JWT}'",
            "db = 'postgres://admin:hunter2secret@db.example.com/x'",
            "-----BEGIN RSA PRIVATE KEY-----",
            "MIIabc",
            "-----END RSA PRIVATE KEY-----",
            "normal = 'AKIA_not_a_key'",
            "print('hello world')",
        ]
    )
    (repo / "conf.py").write_text(src + "\n", encoding="utf-8")
    result = secrets.run_secrets(repo)
    hits = result["worktree"]["hits"]
    types = {h["type"] for h in hits}
    assert {"aws-access-key", "github-token", "gitlab-token", "slack-token",
            "google-api-key", "openai-key", "jwt", "url-credential",
            "private-key-header", "private-key-body"} <= types
    by_type = {h["type"]: h for h in hits}
    assert by_type["aws-access-key"]["file"] == "conf.py"
    assert by_type["aws-access-key"]["line"] == 1
    assert by_type["private-key-body"]["line"] == 10
    assert by_type["url-credential"]["preview"] == "hunt"
    # 正常代码零误报（'AKIA_not_a_key' 非命中）
    assert not any("not_a_key" in h["preview"] for h in hits)
    assert result["total_hits"] == len(hits)
    assert secrets.secrets_rc(result) == 1


def test_worktree_clean_repo_zero_hits_rc0(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    result = secrets.run_secrets(repo)
    assert result["worktree"]["hits"] == []
    assert result["total_hits"] == 0
    assert secrets.secrets_rc(result) == 0


def test_worktree_binary_and_large_skips_counted(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02" * 10)
    (repo / "big.txt").write_bytes(b"a" * (secrets.WORKTREE_FILE_CAP + 1))
    result = secrets.run_secrets(repo)
    wt = result["worktree"]
    assert wt["skipped_binary"] == 1
    assert wt["skipped_large"] == 1
    assert wt["scanned_files"] == 0


def test_worktree_ignore_dirs_pruned(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / ".vrp").mkdir()
    (repo / ".vrp" / "SECRETS.local.md").write_text(f"leak {AK}\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.js").write_text(f"t = '{GH}'\n", encoding="utf-8")
    result = secrets.run_secrets(repo)
    assert result["worktree"]["hits"] == []  # 掩蔽产物/依赖树不扫
    assert result["worktree"]["scanned_files"] == 0


def test_history_finds_secret_in_deleted_file(tmp_path):
    """历史面杀手锏：文件已删，blob 内密钥仍发现（worktree 面扫不到）。"""
    repo = _mk_repo(tmp_path)
    (repo / "leak.py").write_text(f"key = '{AK}'\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "add leak")
    (repo / "leak.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "remove leak")
    result = secrets.run_secrets(repo, history=True)
    assert result["worktree"]["hits"] == []  # 工作树已无此文件
    hist_hits = result["history"]["hits"]
    assert len(hist_hits) == 1
    assert hist_hits[0]["path"] == "leak.py"
    assert hist_hits[0]["type"] == "aws-access-key"
    assert hist_hits[0]["line"] == 1
    assert result["history"]["scanned_blobs"] >= 1
    assert result["total_hits"] == 1
    assert secrets.secrets_rc(result) == 1


def test_history_clean_repo_zero_hits(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "app")
    result = secrets.run_secrets(repo, history=True)
    assert result["history"]["hits"] == []
    assert secrets.secrets_rc(result) == 0


def test_history_large_blob_skipped_counted(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "big.py").write_bytes(b"a" * (secrets.BLOB_CAP + 1))
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "big")
    result = secrets.run_secrets(repo, history=True)
    assert result["history"]["skipped_large"] == 1
    assert result["history"]["scanned_blobs"] == 0


def test_history_empty_repo_honest_empty(tmp_path):
    repo = _mk_repo(tmp_path)
    result = secrets.run_secrets(repo, history=True)
    assert result["history"]["blobs"] == 0
    assert secrets.secrets_rc(result) == 0


def test_render_text_shape(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "c.py").write_text(f"k = '{AK}'\n", encoding="utf-8")
    text = secrets.render_text(secrets.run_secrets(repo))
    assert "vrp secrets" in text
    assert "[hit] c.py:1" in text and "aws-access-key" in text
    assert "total_hits=1" in text and "rc=1" in text


def test_stream_lines_yields_and_reaps(tmp_path):
    """procs.stream_lines：行流+input_text+耗尽回收；close 早停回收不泄漏。"""
    script = "import sys\nfor line in sys.stdin:\n    print('echo:' + line.strip())\n"
    out = []
    for line in procs.stream_lines([sys.executable, "-c", script], input_text="a\nb\n", timeout=60):
        out.append(line.strip())
    assert out == ["echo:a", "echo:b"]
    # 早停 close：GeneratorExit → finally kill_tree 回收（flush=True 防 stdout
    # 块缓冲假挂——print 缓冲不冲 readline 悬 60s，#176 实录）
    gen = procs.stream_lines(
        [sys.executable, "-u", "-c", "print('x', flush=True)\nimport time\ntime.sleep(60)\nprint('y')"],
        timeout=60,
    )
    first = next(gen)
    assert first.strip() == "x"
    gen.close()  # kill_tree 同步回收（taskkill+wait），无需回收窗口 sleep


def test_stream_lines_large_bidirectional():
    """#178 自检回归锁：stdin/stdout 双超管道缓冲（4KB/8KB）不互锁——
    同步"先写完再读"在 2948 oid 真仓死锁实录（fixture 仓 oid 数小=盲区），
    写线程并行后秒回。子进程逐行流式 echo（不攒缓冲）模拟 git batch 形态。"""
    script = (
        "import sys\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write('oid-' + line)\n"
    )
    n = 5000  # ~25KB stdin + ~35KB stdout，双超 4KB 管道缓冲
    t0 = time.perf_counter()
    out = list(
        procs.stream_lines(
            [sys.executable, "-u", "-c", script],
            input_text="".join(f"line-{i}\n" for i in range(n)),
            timeout=120,
        )
    )
    assert len(out) == n
    assert out[0].strip() == "oid-line-0"
    assert time.perf_counter() - t0 < 60  # 挂→红转换（修复前=120s 看门杀后 rc 红）


def test_stream_lines_nonzero_rc_honest_error(tmp_path):
    """#178：耗尽路径 rc!=0（看门杀/崩溃）诚实 RuntimeError——流可能截断，
    静默部分结果比死锁更糟（135/2948 行漏扫实录）。"""
    with pytest.raises(RuntimeError, match="exited rc="):
        list(
            procs.stream_lines(
                [sys.executable, "-u", "-c", "print('partial line')\nimport sys; sys.exit(3)"],
                timeout=60,
            )
        )


def test_enumerate_batch_check_truncation_guard(tmp_path):
    """#178：batch-check 行数对账——流被截断（行数<oid 数）诚实 RuntimeError。"""
    repo = _mk_repo(tmp_path)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("y = 2\n", encoding="utf-8")  # 2 个 pathed oid——1 行截断才可见
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "ab")
    real = procs.stream_lines

    def truncated(cmd, cwd=None, input_text=None, timeout=3600):
        for line in real(cmd, cwd=cwd, input_text=input_text, timeout=timeout):
            yield line
            return  # 只放行第一行=截断流

    procs.stream_lines = truncated
    try:
        with pytest.raises(RuntimeError, match="truncated"):
            secrets.run_secrets(repo, history=True)
    finally:
        procs.stream_lines = real
    # 未截断路径不受影响（真跑全链绿）
    assert secrets.run_secrets(repo, history=True)["history"]["blobs"] >= 1


def test_history_private_key_block_body_detected(tmp_path):
    """#178 自检语义锁：历史面按 blob 批量喂——私钥块状态机在 blob 内正常
    运转（原逐行喂每行重置状态机，body 从未检出=worktree/历史面语义分叉）。"""
    repo = _mk_repo(tmp_path)
    (repo / "k.pem").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEbodyline\n-----END RSA PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "k")
    result = secrets.run_secrets(repo, history=True)
    hist_types = [h["type"] for h in result["history"]["hits"]]
    assert "private-key-header" in hist_types
    assert "private-key-body" in hist_types  # 逐行喂时代此断言红（body 漏检）


def test_render_text_aggregates_body_runs(tmp_path):
    """#178 自检修锁：连续 private-key-body 折叠区间×N——单断言字符串不再
    放大成逐行噪声（CBM 仓 6957 连续命中实录）；JSON 面全量不折叠。"""
    result = {
        "repo": ".",
        "worktree": {
            "scanned_files": 1,
            "skipped_binary": 0,
            "skipped_large": 0,
            "hits": [
                {"file": "a.c", "line": 10, "type": "private-key-header", "preview": ""},
                {"file": "a.c", "line": 11, "type": "private-key-body", "preview": "MIII"},
                {"file": "a.c", "line": 12, "type": "private-key-body", "preview": "MIID"},
                {"file": "a.c", "line": 13, "type": "private-key-body", "preview": "MIIE"},
                {"file": "b.py", "line": 3, "type": "aws-access-key", "preview": "AKIA"},
            ],
        },
        "history": None,
        "total_hits": 5,
    }
    text = secrets.render_text(result)
    assert "[hit] a.c:11-13  private-key-body  ×3" in text  # 连续 run 折叠
    assert "[hit] b.py:3  aws-access-key  AKIA…" in text
    assert "MIII" not in text  # 折叠后不再逐行 preview
    # 单 body 不折叠（无区间形态）
    result["worktree"]["hits"] = result["worktree"]["hits"][:2]
    result["total_hits"] = 2
    text = secrets.render_text(result)
    assert "[hit] a.c:11  private-key-body  MIII…" in text


def test_cli_end_to_end(tmp_path, capsys):
    repo = _mk_repo(tmp_path)
    (repo / "c.py").write_text(f"k = '{AK}'\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "c")
    rc = cli_main(["secrets", str(repo), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1  # gitleaks 语义：命中 rc=1
    assert out["total_hits"] == 1
    rc = cli_main(["secrets", str(repo), "--history", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["history"] is not None and out["history"]["hits"]
    clean = _mk_repo(tmp_path / "clean")
    rc = cli_main(["secrets", str(clean), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["total_hits"] == 0
