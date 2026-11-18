"""H1（构想v11 甜点）：secrets 独立化——探测器 CLI 化+git 历史面。

轮子裁决（构想v11）：学 gitleaks（规则组织+全 blob 历史遍历形态）；
trufflehog 活性验证**不做**（只检测不验证——离线红线）；detect-secrets
基线文件不引入（SECRETS.local.md 掩蔽清单仍归 render 链——本 CLI 纯只读
扫描 stdout 报告，不写盘）。

检测器**单源**：render_chain.mask_source_lines（_SECRET_INLINE 七型/
URL 凭据/私钥块）——探测逻辑零复制，本模块只做扫描编排（行喂入+命中
归位）。worktree 面与 render 链同口径（IGNORE_DIRS 剪枝——.vrp 内
SECRETS.local.md 是掩蔽产物非泄密面，不扫）。

历史面（gitleaks 核心形态）：`git rev-list --objects --all` 全对象枚举
→`cat-file --batch-check` 过滤 blob（大小封顶跳过计数）→`cat-file --batch`
流式扫描（procs.stream_lines——全 blob 流禁止 run_tree 全量捕获）。
blob 归位锚定=期望头精确匹配（oid+blob+size 三元组全等），内容行假冒
下一期望头的概率=精确 40-hex+size 全等（记档不防御）；行号近似
（cat-file 追加 LF 定界，末行 ±1）。已删文件的密钥在历史 blob 仍可发现
——worktree 面扫不到的即历史面价值。

rc 语义（gitleaks 同型）：命中 rc=1（CI 可用），零命中 rc=0。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import procs, s0_precheck
from .render_chain import mask_source_lines

WORKTREE_FILE_CAP = 4 * 1024 * 1024  # 4MB（单文件封顶，超限跳过计数）
BLOB_CAP = 2 * 1024 * 1024  # 2MB（历史 blob 封顶）
BINARY_SNIFF_BYTES = 8192


def _git(repo: Path, args: list[str], timeout: int = 600) -> str:
    result = procs.run_tree(["git", *args], cwd=repo, timeout=timeout)
    if result["timed_out"]:
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s")
    if result["returncode"] != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result['returncode']}): {(result['stderr'] or '')[-2000:]}"
        )
    return result["stdout"] or ""


def _scan_lines(lines: list[str]) -> list[dict]:
    """行扫描（检测器单源）：mask_source_lines 命中清单，掩蔽文本弃用。"""
    _, hits = mask_source_lines(lines)
    return hits


def scan_worktree(repo: Path, file_cap: int = WORKTREE_FILE_CAP) -> dict:
    """工作树面：os.walk（IGNORE_DIRS 剪枝同 s0 口径）文本文件逐个扫描。"""
    repo = Path(repo)
    scanned = 0
    skipped_binary = 0
    skipped_large = 0
    hits: list[dict] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in s0_precheck.IGNORE_DIRS]
        for name in files:
            path = Path(root) / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > file_cap:
                skipped_large += 1
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
                skipped_binary += 1
                continue
            rel = path.relative_to(repo).as_posix()
            scanned += 1
            lines = raw.decode("utf-8", errors="replace").splitlines()
            for hit in _scan_lines(lines):
                hits.append({"file": rel, **hit})
    hits.sort(key=lambda h: (h["file"], h["line"], h["type"]))
    return {
        "scanned_files": scanned,
        "skipped_binary": skipped_binary,
        "skipped_large": skipped_large,
        "hits": hits,
    }


def _enumerate_blobs(repo: Path, blob_cap: int) -> tuple[dict[str, int], dict[str, str], int]:
    """(oid→size, oid→path, skipped_large)：rev-list --objects --all 全对象
    枚举+batch-check 过滤 blob（大小封顶跳过计数）。路径归属 first-wins
    （rev-list 新→旧序，新 commit 路径优先）。"""
    out = _git(repo, ["rev-list", "--objects", "--all"])
    oid_path: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        oid = parts[0]
        if len(parts) == 2 and parts[1]:
            oid_path.setdefault(oid, parts[1])
    if not oid_path:
        return {}, {}, 0
    # batch-check：oid 清单 stdin 喂入（stream_lines 通道——run_tree 无 stdin 面），
    # 输出有界（行/对象）收集后过滤 blob
    check_stream = procs.stream_lines(
        ["git", "cat-file", "--batch-check"],
        cwd=repo,
        input_text="\n".join(sorted(oid_path)) + "\n",
        timeout=600,
    )
    oid_size: dict[str, int] = {}
    skipped = 0
    for line in check_stream:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "blob":
            oid, size = parts[0], int(parts[2])
            if oid in oid_path and oid not in oid_size:
                if size > blob_cap:
                    skipped += 1
                    continue
                oid_size[oid] = size
    return oid_size, oid_path, skipped


def scan_history(repo: Path, blob_cap: int = BLOB_CAP, timeout: int = 3600) -> dict:
    """历史面：全 blob 流式扫描（已删文件的密钥仍可发现——本面价值）。"""
    repo = Path(repo)
    oid_size, oid_path, skipped_large = _enumerate_blobs(repo, blob_cap)
    hits: list[dict] = []
    if not oid_size:
        return {"blobs": 0, "scanned_blobs": 0, "skipped_large": skipped_large, "hits": hits}
    ordered = sorted(oid_size)  # 确定性：oid 升序喂入
    input_text = "\n".join(ordered) + "\n"
    headers_seen = 0
    current_oid: str | None = None
    current_path: str | None = None
    line_no = 0
    expected = iter(ordered)
    next_oid = next(expected, None)
    stream = procs.stream_lines(
        ["git", "cat-file", "--batch"], cwd=repo, input_text=input_text, timeout=timeout
    )
    for line in stream:
        stripped = line.rstrip("\n")
        if next_oid is not None and stripped == f"{next_oid} blob {oid_size[next_oid]}":
            headers_seen += 1
            current_oid = next_oid
            current_path = oid_path.get(current_oid)
            line_no = 0
            next_oid = next(expected, None)
            continue
        if current_oid is None:
            continue  # 头前噪声（理论不达）
        line_no += 1
        for hit in _scan_lines([stripped]):
            hits.append(
                {"blob": current_oid, "path": current_path, "line": line_no, **hit}
            )
    if headers_seen != len(ordered):
        raise RuntimeError(
            f"cat-file stream desync: expected {len(ordered)} blob headers, saw {headers_seen}"
        )
    hits.sort(key=lambda h: (h["path"] or "", h["blob"], h["line"], h["type"]))
    return {
        "blobs": len(ordered),
        "scanned_blobs": len(ordered),
        "skipped_large": skipped_large,
        "hits": hits,
    }


def run_secrets(repo: Path, history: bool = False) -> dict:
    """编排：worktree 恒扫；history=True 加历史面（慢面显式 opt-in）。"""
    repo = Path(repo)
    worktree = scan_worktree(repo)
    result: dict = {"repo": str(repo), "worktree": worktree, "history": None}
    if history:
        result["history"] = scan_history(repo)
    result["total_hits"] = len(worktree["hits"]) + (
        len(result["history"]["hits"]) if result["history"] else 0
    )
    return result


def secrets_rc(result: dict) -> int:
    """gitleaks 同型：命中 rc=1（CI 可用），零命中 rc=0。"""
    return 1 if result["total_hits"] else 0


def render_text(result: dict) -> str:
    lines = [f"vrp secrets - 密钥扫描（检测器单源 render_chain，只检测不验证）"]
    worktree = result["worktree"]
    lines.append(
        f"  worktree: {worktree['scanned_files']} 文件扫过"
        f"  跳过 binary={worktree['skipped_binary']} large={worktree['skipped_large']}"
        f"  命中 {len(worktree['hits'])}"
    )
    for hit in worktree["hits"]:
        lines.append(f"    [hit] {hit['file']}:{hit['line']}  {hit['type']}  {hit['preview']}…")
    history = result["history"]
    if history is not None:
        lines.append(
            f"  history: {history['scanned_blobs']} blob 扫过"
            f"  跳过 large={history['skipped_large']}"
            f"  命中 {len(history['hits'])}"
        )
        for hit in history["hits"]:
            where = hit["path"] or f"blob:{hit['blob'][:12]}"
            lines.append(f"    [hit] {where}:{hit['line']}  {hit['type']}  {hit['preview']}…")
    lines.append(f"  summary: total_hits={result['total_hits']}  rc={'1' if result['total_hits'] else '0'}")
    return "\n".join(lines)
