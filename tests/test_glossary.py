"""Q24/W21 文档锚点锁（#177）：glossary/理论出处引用的码路径与表名必须真实。

#153 纪律的文档面落点——glossary 是 reference（Q24 构想判定"纯文档工程"），
但引用指针会漂移：模块改名/表改名/文件移动后 glossary 里的 详处 列即悬空。
两 Face 守卫（宁缺勿错：锁的是"引用物存在"，不锁定义正确性——定义正确性
由人工 review 承载）：
- Face A 路径面：两文档内 `vrp/…` 引用（含 glob 形态）逐个展开，必须命中
  ≥1 真实文件（悬空指针=红）。
- Face B 表名面：glossary 反引号内联引用的 facts 表名（`xxx` 表 / `xxx`/`yyy`
  表 / `xxx.field` 三形态提取）必须都在 facts_schema.sql CREATE TABLE 集——
  glossary 点名的表改名未同步=红。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY = (ROOT / "docs" / "glossary.md").read_text(encoding="utf-8")
THEORY = (ROOT / "docs" / "理论出处.md").read_text(encoding="utf-8")
SCHEMA = (ROOT / "vrp" / "schema" / "facts_schema.sql").read_text(encoding="utf-8")

SCHEMA_TABLES = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))


def test_cited_vrp_paths_resolve():
    """Face A：两文档引用的 vrp/… 路径（含 glob）必须命中真实文件/目录。

    边界 `(?<![.\\w])` 排除 `.vrp/…` 运行时路径（工作目录非源码面——
    .vrp/verdicts/ 等是管道运行产物，不属于"引用物存在"守卫域）。"""
    tokens = set(re.findall(r"(?<![.\w])vrp/[A-Za-z0-9_*./-]+", GLOSSARY + THEORY))
    assert tokens, "路径提取面为空——glossary 至少引用 schema 一处（提取器失效=红）"
    dangling: list[str] = []
    for token in sorted(tokens):
        probe = token.rstrip("/.")
        if "*" in probe:
            if not list(ROOT.glob(probe)):
                dangling.append(token)
        elif not (ROOT / probe).exists():
            dangling.append(token)
    assert not dangling, f"glossary/理论出处 悬空指针（Face A）: {dangling}"


def test_cited_table_names_exist_in_schema():
    """Face B：glossary 反引号内联引用的表名 ⊆ facts_schema CREATE TABLE 集。"""
    cited = set(re.findall(r"`([a-z_]+)` 表", GLOSSARY))
    cited |= {m for pair in re.findall(r"`([a-z_]+)`/`([a-z_]+)` 表", GLOSSARY) for m in pair}
    cited |= set(re.findall(r"`([a-z_]+)\.[a-z_]+`", GLOSSARY))
    assert cited, "表名提取面为空——glossary 点名表（Q24：fid/OWNED_TABLES 等）提取器失效=红"
    unknown = sorted(cited - SCHEMA_TABLES)
    assert not unknown, f"glossary 引用了 schema 外的表名（Face B）: {unknown}"
