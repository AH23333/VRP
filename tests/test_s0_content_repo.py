"""P-2 内容仓库探针：HomeWork 类仓库（JSON/HTML/PDF>90%、零源码、无构建
系统）对代码分析不适用——precheck 提前判定，'vrp run' 跳过注定失败的大
内存索引尝试（提示语义，非硬门：单命令仍可手动执行）。
"""

from __future__ import annotations

from vrp import cli, s0_precheck


def _mk_content_repo(root, n: int = 30) -> None:
    root.mkdir(parents=True)
    exts = (".json", ".html", ".pdf", ".md")
    for i in range(n):
        (root / f"data-{i:02d}{exts[i % 4]}").write_text("{}", encoding="utf-8")


def test_content_repo_probe_detects_content_only_repo(tmp_path):
    repo = tmp_path / "homework"
    _mk_content_repo(repo)
    probe = s0_precheck.content_repo_probe(repo)
    assert probe["detected"] is True
    assert probe["source_files"] == 0
    assert probe["content_share_pct"] == 100.0
    assert probe["build_markers"] == []


def test_content_repo_probe_spared_by_any_source_file(tmp_path):
    """最强守卫：任何一个真源码文件即豁免（防误杀代码仓）。"""
    repo = tmp_path / "proj"
    _mk_content_repo(repo)
    (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")
    probe = s0_precheck.content_repo_probe(repo)
    assert probe["detected"] is False
    assert probe["source_files"] == 1


def test_content_repo_probe_spared_by_build_marker(tmp_path):
    repo = tmp_path / "docsite"
    _mk_content_repo(repo)
    (repo / "package.json").write_text("{}", encoding="utf-8")
    probe = s0_precheck.content_repo_probe(repo)
    assert probe["detected"] is False
    assert "package.json" in probe["build_markers"]


def test_content_repo_probe_csproj_marker(tmp_path):
    repo = tmp_path / "slides"
    _mk_content_repo(repo)
    (repo / "Deck.csproj").write_text("<Project />", encoding="utf-8")
    probe = s0_precheck.content_repo_probe(repo)
    assert probe["detected"] is False
    assert "Deck.csproj" in probe["build_markers"]


def test_content_repo_probe_empty_repo_not_detected(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    probe = s0_precheck.content_repo_probe(repo)
    assert probe["detected"] is False
    assert probe["total_files"] == 0


def test_content_repo_probe_ignores_ignored_dirs(tmp_path):
    """IGNORE_DIRS（.git/node_modules 等）内文件不计入——与 census 口径一致。"""
    repo = tmp_path / "content"
    _mk_content_repo(repo)
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.py").write_text("x = 1\n", encoding="utf-8")
    probe = s0_precheck.content_repo_probe(repo)
    assert probe["detected"] is True
    assert probe["source_files"] == 0


def test_precheck_includes_content_repo(tmp_path, monkeypatch):
    repo = tmp_path / "homework"
    _mk_content_repo(repo)
    monkeypatch.setattr(s0_precheck, "census_scc", lambda repo: None)
    result = s0_precheck.precheck(repo)
    assert result["content_repo"]["detected"] is True
    import json

    persisted = json.loads((repo / ".vrp" / "precheck.json").read_text(encoding="utf-8"))
    assert persisted["content_repo"]["detected"] is True


def test_cmd_run_skips_pipeline_for_content_repo(tmp_path, monkeypatch, capsys):
    """P-2 接线：detected 时 cmd_run 跳过 S1/S1.5/xcheck 并返回 rc=0。"""
    from types import SimpleNamespace

    repo = tmp_path / "homework"
    _mk_content_repo(repo)
    monkeypatch.setattr(s0_precheck, "census_scc", lambda repo: None)

    def boom(*a, **k):
        raise AssertionError("S1 must not run for a content repo")

    monkeypatch.setattr("vrp.s1_cbm.index_repository", boom)
    args = SimpleNamespace(repo=str(repo), cbm_name=None, reindex=False)
    rc = cli.cmd_run(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "not applicable" in out
    assert "content repository" in out
    assert "== S1 cbm index ==" not in out


def test_cmd_run_proceeds_for_code_repo(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")
    monkeypatch.setattr(s0_precheck, "census_scc", lambda repo: None)
    monkeypatch.setattr("vrp.s1_cbm.index_repository", lambda repo, name, reindex=False: {"status": "ok"})
    monkeypatch.setattr("vrp.etl.run_etl", lambda repo, name: {"symbols": 1})
    monkeypatch.setattr("vrp.xcheck.run_xcheck", lambda repo, facts_db, reindex=False: {"ledger_rows": 0})
    args = SimpleNamespace(repo=str(repo), cbm_name=None, reindex=False)
    rc = cli.cmd_run(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "S1 cbm index" in out
    assert "S1.5 etl" in out
