import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CBM_CACHE = Path(r"D:\cbm-cache")
VERIFY_DIR = ROOT / "verify"


def rmtree_retry(path, attempts=6, delay=5.0):
    """批量删除 Windows 退避重试（dogfood 2026-11-06 实证）：

    stryker fixture teardown 删刚被 npm/stryker 写热的 node_modules 时，
    实时 AV 扫描（SYSTEM 进程，非管理员 handle64 不可见）在删除中途抓走
    目录句柄——子项全删光、顶层目录 WinError 32，失败瞬间 handle64 查无
    用户态持有者；onexc 立即重试必输竞速。有界退避重试覆盖扫描窗口；
    下轮 setup 的 if repo.exists(): rmtree 兜底自愈。
    """

    def _clear_readonly(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path, onexc=_clear_readonly)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(delay * attempt)


def run_vrp(args, cwd, expect_rc=0, timeout=3600):
    env = {**os.environ, "CBM_CACHE_DIR": str(CBM_CACHE)}
    proc = subprocess.run(
        [sys.executable, "-m", "vrp.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != expect_rc:
        raise AssertionError(
            f"vrp {' '.join(args)} rc={proc.returncode} != {expect_rc}\n"
            f"stdout: {proc.stdout[-3000:]}\nstderr: {proc.stderr[-3000:]}"
        )
    return proc.stdout


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} rc={proc.returncode}: {proc.stdout} {proc.stderr}")
    return proc.stdout


def read_verdict(repo: Path, tx_name: str) -> dict:
    return json.loads((repo / ".vrp" / "verdicts" / tx_name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def tx_scratch(tmp_path_factory):
    repo = tmp_path_factory.mktemp("deep-tx")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (repo / "test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".vrp/\n__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "init")
    cache_db = CBM_CACHE / f"{repo.name}.db"
    if cache_db.exists():
        cache_db.unlink()
    run_vrp(["index", str(repo), "--reindex"], cwd=ROOT)
    run_vrp(["etl", str(repo)], cwd=ROOT)
    yield repo
    if cache_db.exists():
        cache_db.unlink()


@pytest.fixture(scope="session")
def big_repo():
    repo = VERIFY_DIR / "tree-sitter-analyzer"
    if not (repo / ".vrp" / "facts.db").exists():
        pytest.skip("tree-sitter-analyzer big repo not prepared")
    return repo


@pytest.fixture(scope="session")
def lusiem_repo():
    repo = VERIFY_DIR / "code-atlas-lusiem"
    if not repo.exists():
        pytest.skip("code-atlas-lusiem verify repo not present")
    return repo
