"""共享 git 测试助手（#214 深度审查：test_cochange/test_pyz jaccard 1.0 重复面单源化）。

变体不统一：其余测试文件的 `_git` 签名/实现各异（subprocess 直调/不同
超时/不同返回）——名称撞车非语义重复，不强行归一。
"""

from __future__ import annotations

from pathlib import Path

from vrp import procs


def _git(repo: Path, *args: str, env: dict | None = None, ok: bool = True):
    res = procs.run_tree(["git", *args], cwd=repo, timeout=30, env=env)
    if ok:
        assert res["returncode"] == 0, f"git {' '.join(args)} failed: {res['stderr']}"
    return res


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    return path
