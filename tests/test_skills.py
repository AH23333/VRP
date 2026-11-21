"""E14 场景 skill 包守卫（批⑨ #207）。

skills/ 下每件 SKILL.md 的形状纪律：frontmatter（name+description）、
name==目录名、description 单行非空 ASCII ≤1024（Anthropic skills 生态
上限）、正文非空、LF 纯（md 纪律）、skills/<name>/ 交叉链指向真实目录
（Related 节防烂链）。只守本仓 skills/——verify/ 参照仓不在此列。
"""

from __future__ import annotations

import re
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)
DESC_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)
LINK_RE = re.compile(r"`skills/([a-z0-9-]+)/`")

MAX_DESC_CHARS = 1024


def _skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontmatter(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    assert match is not None, "missing '---' frontmatter block"
    return match.group(1)


def test_every_skill_dir_has_skill_md() -> None:
    dirs = sorted(d for d in SKILLS_DIR.iterdir() if d.is_dir())
    assert len(dirs) >= 4, "expected at least 4 scenario skills (loop/curation/archaeology/review)"
    for d in dirs:
        assert (d / "SKILL.md").is_file(), f"{d.name}/ has no SKILL.md"


def test_frontmatter_has_name_and_description() -> None:
    for path in _skill_files():
        fm = _frontmatter(_read(path))
        name = NAME_RE.search(fm)
        desc = DESC_RE.search(fm)
        assert name is not None, f"{path.parent.name}: frontmatter missing name"
        assert desc is not None, f"{path.parent.name}: frontmatter missing description"


def test_name_equals_directory() -> None:
    for path in _skill_files():
        fm = _frontmatter(_read(path))
        name = NAME_RE.search(fm)
        assert name is not None
        assert name.group(1) == path.parent.name, (
            f"{path.parent.name}: frontmatter name {name.group(1)!r} != dir name"
        )


def test_description_shape() -> None:
    for path in _skill_files():
        fm = _frontmatter(_read(path))
        desc = DESC_RE.search(fm)
        assert desc is not None
        value = desc.group(1)
        assert 0 < len(value) <= MAX_DESC_CHARS, (
            f"{path.parent.name}: description length {len(value)} not in (0, {MAX_DESC_CHARS}]"
        )
        assert value.isascii(), f"{path.parent.name}: description must be ASCII (English discipline)"
        assert "\n" not in value


def test_body_non_empty() -> None:
    for path in _skill_files():
        text = _read(path)
        body = FRONTMATTER_RE.sub("", text).strip()
        assert len(body) > 0, f"{path.parent.name}: empty body under frontmatter"


def test_lf_only_no_crlf() -> None:
    for path in _skill_files():
        raw = path.read_bytes()
        assert b"\r" not in raw, f"{path}: contains CR (LF-only discipline)"


def test_cross_links_resolve() -> None:
    known = {d.name for d in SKILLS_DIR.iterdir() if d.is_dir()}
    for path in _skill_files():
        for ref in LINK_RE.finditer(_read(path)):
            assert ref.group(1) in known, (
                f"{path.parent.name}: links to nonexistent skill dir {ref.group(1)!r}"
            )


def test_pack_membership_pinned() -> None:
    names = {d.name for d in SKILLS_DIR.iterdir() if d.is_dir()}
    expected = {"vrp-loop", "vrp-curation", "vrp-archaeology", "vrp-review"}
    assert expected <= names, f"skill pack incomplete: missing {expected - names}"
