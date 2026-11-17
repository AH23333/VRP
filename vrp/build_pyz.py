"""U13 zipapp 单文件分发（构想v12 甜点）：`python vrp.pyz` 零安装试用。

策略=**自解压+re-exec**（非 zip 内直读）：仓内 13 处 ``__file__`` 资源点
（schema/vendor-archify 需真文件喂 node 子进程/assets/rules）是硬约束，
zip 内直读需逐点改造且 node 子进程仍要落盘——故 pyz 根启动器检测 zip
模式 → 按 pyz sha256 键控解压到缓存目录（``VRP_PYZ_CACHE`` 环境变量可
覆写，默认 ``~/.vrp/pyz/``）→ re-exec 解压树 ``__main__.py``，zip/解压
两模式单代码路径，资源点零改动。

构建确定性（freeze graph.db.zst 字节级复现先例）：arcname 排序写入+
固定时间戳（1980-01-01）+固定 ZipInfo 属性（create_system=0/
external_attr=0）+ZIP_DEFLATED——同源仓双构建 sha256 全等。

用法：``python -m vrp.build_pyz [--out PATH]``（默认 ``dist/vrp.pyz``）。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_PACKAGE = _REPO_ROOT / "vrp"
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)  # zip 纪元下限：固定时间戳=确定性构建
_EXCLUDE_DIRS = frozenset({"__pycache__"})
_EXCLUDE_SUFFIXES = frozenset({".pyc"})

# pyz 根启动器（zip/解压双模式）。注意：勿把本串内容当模块运行——
# 它只在 pyz 根与解压树根两个位置被解释执行。
_LAUNCHER_SRC = '''"""vrp.pyz 启动器：zip 模式 → sha256 键控解压缓存 → re-exec 解压树。"""
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

_COMPLETE = ".complete"


def _extract(pyz: Path, target: Path) -> None:
    tmp = target.with_name(target.name + f".tmp-{os.getpid()}")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(pyz) as zf:
        zf.extractall(tmp)
    (tmp / _COMPLETE).write_text("ok", encoding="utf-8")
    if (target / _COMPLETE).is_file():  # 并发对家先完成：弃 tmp 用现成
        shutil.rmtree(tmp)
        return
    if target.exists():  # 无标记残局（中途崩溃）：清掉重装
        shutil.rmtree(target)
    tmp.rename(target)


def main() -> int:
    here = Path(__file__).resolve().parent
    if (here / "vrp").is_dir():  # 解压树（真文件系统）：直接跑
        sys.path.insert(0, str(here))
        from vrp.cli import main as cli_main
        return cli_main()
    pyz = here  # zip 模式：__file__ = "<pyz>/__main__.py"（zipimport 惯例）
    digest = hashlib.sha256(pyz.read_bytes()).hexdigest()[:16]
    env = os.environ.get("VRP_PYZ_CACHE")
    base = Path(env) if env else Path.home() / ".vrp" / "pyz"
    target = base / f"vrp-{digest}"
    if not (target / _COMPLETE).is_file():
        try:
            base.mkdir(parents=True, exist_ok=True)
            _extract(pyz, target)
        except OSError as exc:
            print(f"vrp.pyz: extract failed: {exc}", file=sys.stderr)
            return 1
    return subprocess.run(
        [sys.executable, str(target / "__main__.py"), *sys.argv[1:]]
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
'''


def _entries() -> list[tuple[str, Path]]:
    """(arcname, 源路径) 收集：vrp/ 包全量（含数据目录），排除缓存产物。"""
    entries: list[tuple[str, Path]] = []
    for path in sorted(_SRC_PACKAGE.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(_REPO_ROOT)
        if any(part in _EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.suffix in _EXCLUDE_SUFFIXES:
            continue
        entries.append((rel.as_posix(), path))
    return entries


def build_pyz(dest: Path) -> dict[str, object]:
    """确定性构建 vrp.pyz 到 dest，返回摘要 {path/bytes/sha256/files}。"""
    entries = _entries()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".tmp-{__import__('os').getpid()}")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in entries:
            info = zipfile.ZipInfo(arcname, date_time=_FIXED_DATE)
            info.create_system = 0
            info.external_attr = 0
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
        launcher = zipfile.ZipInfo("__main__.py", date_time=_FIXED_DATE)
        launcher.create_system = 0
        launcher.external_attr = 0
        launcher.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(launcher, _LAUNCHER_SRC)
    tmp.replace(dest)
    payload = dest.read_bytes()
    return {
        "path": str(dest),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "files": len(entries) + 1,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vrp.build_pyz",
        description="构建零安装试用的 vrp.pyz（确定性 zipapp，自解压缓存运行）",
    )
    parser.add_argument(
        "--out",
        default=str(_REPO_ROOT / "dist" / "vrp.pyz"),
        help="输出路径（默认 dist/vrp.pyz）",
    )
    args = parser.parse_args(argv)
    summary = build_pyz(Path(args.out))
    print(
        f"vrp.pyz: {summary['path']} ({summary['files']} files,"
        f" {summary['bytes']} bytes, sha256 {summary['sha256'][:16]})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
