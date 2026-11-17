"""R2 vrp cochange：co-change 隐式耦合边（构想v12 亮点，设计稿已批准 D1-D4）。

守卫重点：公式手算锚定（support/confidence/changes 逐对核）、行规范
（file_a < file_b 字典序 + 同 commit 去重）、幂等（DELETE+INSERT 重跑
零膨胀，#62 同型）、阈值边界（>= 含等）、--no-merges 锁（merge commit
的冲突消解文件集不计入——commits 计数与 confidence 双锁）、非 git 仓/
空历史诚实空结果且清残、确定性（同仓双跑同产物）、OWNED 存续
（snapshot/restore 含 co_changes）、freshness 硬拒（CLI 层 stale 拒）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from vrp import cochange as coc
from vrp import etl as etl_mod
from vrp import procs
from vrp.cli import build_parser, cmd_cochange
from vrp.cli import main as cli_main

SCHEMA = Path(coc.__file__).parent / "schema" / "facts_schema.sql"


# ---------- 合成 git 仓 fixture（设计稿 §7：tmp git init + 脚本化多 commit） ----------


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


def _commit(repo: Path, files: dict[str, str], msg: str, date: str | None = None) -> None:
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    env = None
    if date:
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    _git(repo, "commit", "-q", "-m", msg, env=env)


# ---------- 纯函数层 ----------


def test_parse_log_forms():
    """解析：US 头/空行分隔不敏感；同 commit 去重；规范化（去空白+反斜杠
    转正斜杠）；per-commit 排序（combinations 键序的前提）。
    """
    raw = (
        "\x1fh2\x1f2026-02-01T10:00:00+08:00\n"
        "\n"
        "b.py\n"
        "  a.py  \n"
        "b.py\n"
        "src\\x.py\n"
        "\n"
        "\x1fh1\x1f2026-01-01T10:00:00+08:00\n"
        "\n"
        "a.py\n"
    )
    commits = coc.parse_log(raw)
    assert len(commits) == 2
    assert commits[0] == {
        "date": "2026-02-01T10:00:00+08:00",
        "files": ["a.py", "b.py", "src/x.py"],
    }
    assert commits[1] == {"date": "2026-01-01T10:00:00+08:00", "files": ["a.py"]}


def test_mine_hand_computed():
    """度量手算锚定（设计稿 §7：3 commit 小仓逐对手算）：

    c3: a,b    c2: a,b,c    c1: a,b
    support(a,b)=3；changes a=3 b=3 c=1；
    conf(a->b)=conf(b->a)=1.0；conf(a->c)=1/3，conf(c->a)=1.0；
    first/last_co_change 取共现区间的最旧/最新 commit 日期。
    """
    raw = (
        "\x1fh3\x1f2026-03-01T10:00:00+08:00\n\na.py\nb.py\n"
        "\x1fh2\x1f2026-02-01T10:00:00+08:00\n\na.py\nb.py\nc.py\n"
        "\x1fh1\x1f2026-01-01T10:00:00+08:00\n\na.py\nb.py\n"
    )
    mined = coc.mine(raw)
    assert mined["commits"] == 3
    assert mined["changes"] == {"a.py": 3, "b.py": 3, "c.py": 1}
    p_ab = mined["pairs"][("a.py", "b.py")]
    assert p_ab["support"] == 3
    assert p_ab["changes_a"] == 3
    assert p_ab["changes_b"] == 3
    assert p_ab["confidence_ab"] == 1.0
    assert p_ab["confidence_ba"] == 1.0
    assert p_ab["first_co_change"] == "2026-01-01T10:00:00+08:00"
    assert p_ab["last_co_change"] == "2026-03-01T10:00:00+08:00"
    p_ac = mined["pairs"][("a.py", "c.py")]
    assert p_ac["support"] == 1
    assert p_ac["confidence_ab"] == pytest.approx(1 / 3)
    assert p_ac["confidence_ba"] == 1.0
    assert p_ac["first_co_change"] == "2026-02-01T10:00:00+08:00"
    assert p_ac["last_co_change"] == "2026-02-01T10:00:00+08:00"
    assert all(a < b for a, b in mined["pairs"])  # 行规范：无序对字典序唯一行


def test_persist_idempotent(facts_db):
    """幂等红线（#62）：重跑 DELETE+INSERT 零膨胀，行集不变。"""
    raw = (
        "\x1fh2\x1f2026-02-01T10:00:00+08:00\n\na.py\nb.py\n"
        "\x1fh1\x1f2026-01-01T10:00:00+08:00\n\na.py\nb.py\n"
        "\x1fh0\x1f2025-12-01T10:00:00+08:00\n\na.py\nb.py\n"
    )
    mined = coc.mine(raw)
    n1 = coc.persist(facts_db, mined)
    n2 = coc.persist(facts_db, mined)
    assert n1 == n2 == 1
    rows = facts_db.execute("SELECT file_a, file_b, support FROM co_changes").fetchall()
    assert rows == [("a.py", "b.py", 3)]


def test_query_threshold_boundary(facts_db):
    """阈值边界（>= 含等）：support==3 / confidence==0.3 幸存，差一即滤；
    排序 (support DESC, confidence_max DESC, file_a, file_b)；limit 截断
    展示面但幸存数为真值（诚实计数）。
    """
    facts_db.executemany(
        "INSERT INTO co_changes VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("a.py", "b.py", 3, 10, 10, 0.3, 0.3, "2026-01-01", "2026-03-01"),
            ("a.py", "c.py", 2, 10, 10, 0.9, 0.9, "2026-01-01", "2026-03-01"),
            ("a.py", "d.py", 5, 10, 10, 0.29, 0.29, "2026-01-01", "2026-03-01"),
            ("b.py", "c.py", 5, 10, 10, 0.9, 0.9, "2026-01-01", "2026-03-01"),
            ("x.py", "y.py", 3, 10, 10, 0.5, 0.5, "2026-01-01", "2026-03-01"),
        ],
    )
    facts_db.commit()
    shown, surviving = coc.query_pairs(facts_db, 3, 0.3)
    assert surviving == 3
    assert [(e["file_a"], e["file_b"]) for e in shown] == [
        ("b.py", "c.py"),  # support 5 领先
        ("x.py", "y.py"),  # support 3 内 confidence 0.5 > 0.3
        ("a.py", "b.py"),  # 边界幸存（support==3 且 confidence==0.3）
    ]
    shown, surviving = coc.query_pairs(facts_db, 2, 0.9, limit=1)
    assert surviving == 2
    assert len(shown) == 1


# ---------- 挖掘层（真实 git 仓） ----------


def test_run_end_to_end_real_git(tmp_path, facts_db):
    """全链：真 git 仓 3 commit → 挖掘落库 → 阈值过滤呈现。

    c0: a    c1: a,b    c2: a,b
    support(a,b)=2；conf(a->b)=2/3，conf(b->a)=1.0；
    默认阈值 min-support 3 → 幸存 0（诚实空呈现，表非空）。
    """
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, {"a.py": "1"}, "c0", "2026-01-01T10:00:00+08:00")
    _commit(repo, {"a.py": "2", "b.py": "1"}, "c1", "2026-02-01T10:00:00+08:00")
    _commit(repo, {"a.py": "3", "b.py": "2"}, "c2", "2026-03-01T10:00:00+08:00")
    result = coc.run_cochange(repo, facts_db)
    s = result["summary"]
    assert s["commits"] == 3
    assert s["files"] == 2
    assert s["pairs"] == 1
    row = facts_db.execute(
        "SELECT file_a, file_b, support, changes_a, changes_b, confidence_ab,"
        " confidence_ba, first_co_change, last_co_change FROM co_changes"
    ).fetchone()
    assert row == (
        "a.py",
        "b.py",
        2,
        3,
        2,
        pytest.approx(2 / 3),
        1.0,
        "2026-02-01T10:00:00+08:00",
        "2026-03-01T10:00:00+08:00",
    )
    assert s["surviving_pairs"] == 0  # 默认阈值下诚实空呈现
    assert result["pairs"] == []
    shown, surviving = coc.query_pairs(facts_db, 1, 0.1)
    assert surviving == 1  # 阈值只过滤展示面，表存全量对
    assert shown[0]["confidence_max"] == 1.0


def test_merge_commit_excluded(tmp_path, facts_db):
    """--no-merges 双锁：merge 的冲突消解文件集不计入。

    base: a+m 共变 → side 改 m → master 改 m（分叉）→ merge 冲突消解 m。
    非 merge commit = 3；merge 若被计入则 changes_m 3→4、commits 3→4——
    commits==3 与 confidence(m->a)==1/3（而非 1/4）双断言锁死旗标。
    """
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, {"a.py": "1", "m.py": "base"}, "base")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, {"m.py": "side"}, "side")
    _git(repo, "checkout", "-q", "master")
    _commit(repo, {"m.py": "master"}, "master")
    res = _git(repo, "merge", "side", ok=False)
    assert res["returncode"] != 0  # 冲突（设计如此）
    (repo / "m.py").write_text("merged", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "merge")
    merges = _git(repo, "rev-list", "--count", "--merges", "HEAD")["stdout"].strip()
    assert merges == "1"  # merge commit 确实存在（排除才有意义）
    result = coc.run_cochange(repo, facts_db)
    assert result["summary"]["commits"] == 3  # --no-merges：merge 不扫描
    row = facts_db.execute(
        "SELECT support, changes_a, changes_b, confidence_ab, confidence_ba"
        " FROM co_changes WHERE file_a = 'a.py' AND file_b = 'm.py'"
    ).fetchone()
    assert row == (1, 1, 3, 1.0, pytest.approx(1 / 3))


def test_non_git_honest_empty(tmp_path, facts_db):
    """非 git 仓：诚实空结果且清残（预置残行被清——表与真实状态一致）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    facts_db.execute(
        "INSERT INTO co_changes VALUES ('a.py', 'b.py', 3, 3, 3, 1.0, 1.0, 'x', 'y')"
    )
    facts_db.commit()
    result = coc.run_cochange(repo, facts_db)
    assert result["summary"]["commits"] == 0
    assert result["summary"]["pairs"] == 0
    assert facts_db.execute("SELECT COUNT(*) FROM co_changes").fetchone()[0] == 0


def test_empty_history_honest_empty(tmp_path, facts_db):
    """空历史（git init 无 commit）：诚实空结果。"""
    repo = _init_repo(tmp_path / "repo")
    result = coc.run_cochange(repo, facts_db)
    assert result["summary"]["commits"] == 0
    assert result["summary"]["pairs"] == 0


def test_deterministic_rerun(tmp_path, facts_db):
    """复现性红线：同仓（同 HEAD）双跑，结果 dict 与表行集逐字相等。"""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, {"a.py": "1", "b.py": "1"}, "c0", "2026-01-01T10:00:00+08:00")
    _commit(repo, {"a.py": "2", "b.py": "2"}, "c1", "2026-02-01T10:00:00+08:00")
    r1 = coc.run_cochange(repo, facts_db)
    rows1 = facts_db.execute("SELECT * FROM co_changes").fetchall()
    r2 = coc.run_cochange(repo, facts_db)
    rows2 = facts_db.execute("SELECT * FROM co_changes").fetchall()
    assert r1 == r2
    assert rows1 == rows2


# ---------- OWNED 存续与 freshness ----------


def test_owned_snapshot_restore(tmp_path):
    """OWNED 存续：co_changes 在 OWNED_TABLES 内，snapshot/restore 往返
    行集不变（etl 重跑后行集不变的单元层等价——D2）。
    """
    src_path = tmp_path / "facts.db"
    con = sqlite3.connect(src_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO co_changes VALUES ('a.py', 'b.py', 3, 3, 3, 1.0, 1.0,"
        " '2026-01-01', '2026-03-01')"
    )
    con.commit()
    con.close()
    snap = etl_mod.snapshot_owned(src_path)
    assert "co_changes" in snap  # OWNED_TABLES 成员锁（漏登即红）
    dst = sqlite3.connect(tmp_path / "new.db")
    dst.executescript(SCHEMA.read_text(encoding="utf-8"))
    warnings = etl_mod.restore_owned(dst, snap)
    dst.commit()
    row = dst.execute("SELECT file_a, file_b, support FROM co_changes").fetchone()
    dst.close()
    assert warnings == []
    assert row == ("a.py", "b.py", 3)


def test_freshness_stale_rejected(tmp_path, capsys):
    """freshness（M4）：stale facts.db（git_head 不匹配）挖掘被硬拒——
    CLI 层 _open_facts_rw → rc 1 + stderr 含 stale。
    """
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, {"a.py": "1"}, "c0")
    facts = repo / ".vrp" / "facts.db"
    facts.parent.mkdir()
    con = sqlite3.connect(facts)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', 'deadbeef')")
    con.commit()
    con.close()
    rc = cli_main(["cochange", str(repo)])
    assert rc == 1
    assert "stale" in capsys.readouterr().err


def test_old_db_schema_auto_upgrade(tmp_path, capsys):
    """老库自升级：pre-co_changes schema（表缺失）经 CLI cochange 的
    ensure_schema 幂等补建（CREATE IF NOT EXISTS），挖掘正常落库——
    verify 仓冒烟暴露的缺口（老 facts.db 无 co_changes 直接 OperationalError）。
    """
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, {"a.py": "1", "b.py": "1"}, "c0", "2026-01-01T10:00:00+08:00")
    _commit(repo, {"a.py": "2", "b.py": "2"}, "c1", "2026-02-01T10:00:00+08:00")
    facts = repo / ".vrp" / "facts.db"
    facts.parent.mkdir()
    con = sqlite3.connect(facts)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("DROP TABLE co_changes")  # 模拟老 schema
    head = _git(repo, "rev-parse", "HEAD")["stdout"].strip()
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', ?)", (head,))
    con.commit()
    con.close()
    rc = cli_main(["cochange", str(repo), "--min-support", "1"])
    assert rc == 0
    con = sqlite3.connect(facts)
    assert con.execute("SELECT COUNT(*) FROM co_changes").fetchone()[0] == 1
    con.close()
    assert "a.py <-> b.py" in capsys.readouterr().out


# ---------- 呈现与 CLI 面 ----------


def test_render_text_shape():
    result = {
        "repo": "D:/x/repo",
        "summary": {
            "commits": 3,
            "files": 2,
            "pairs": 1,
            "surviving_pairs": 1,
            "min_support": 2,
            "min_confidence": 0.3,
        },
        "limit": 50,
        "shown": 1,
        "pairs": [
            {
                "file_a": "a.py",
                "file_b": "b.py",
                "support": 2,
                "changes_a": 3,
                "changes_b": 2,
                "confidence_ab": 0.6667,
                "confidence_ba": 1.0,
                "confidence_max": 1.0,
                "first_co_change": "2026-02-01T10:00:00+08:00",
                "last_co_change": "2026-03-01T10:00:00+08:00",
            }
        ],
    }
    text = coc.render_text(result)
    assert text.startswith("vrp cochange - repo")
    assert "commits     : 3 scanned (no merges)" in text
    assert "files       : 2 distinct" in text
    assert "pairs       : 1 stored" in text
    assert "thresholds  : min-support 2, min-confidence 0.3 -> 1 surviving, showing 1" in text
    assert "a.py <-> b.py" in text
    assert "[2026-02-01 .. 2026-03-01]" in text


def test_cli_parser_accepts_cochange():
    args = build_parser().parse_args(["cochange", "somerepo"])
    assert args.func is cmd_cochange
    assert args.repo == "somerepo"
    assert args.min_support == coc.DEFAULT_MIN_SUPPORT
    assert args.min_confidence == coc.DEFAULT_MIN_CONFIDENCE
    assert args.json is False
    args = build_parser().parse_args(
        ["cochange", "somerepo", "--json", "--min-support", "5", "--min-confidence", "0.5"]
    )
    assert args.json is True
    assert args.min_support == 5
    assert args.min_confidence == 0.5
