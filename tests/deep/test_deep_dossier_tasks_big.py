import json
from pathlib import Path

import pytest

from tests.deep.conftest import ROOT, run_vrp

pytestmark = pytest.mark.deep

SECTION_KEYS = {"architecture", "repo_map_brief", "repomap", "sql_views", "evidence", "summary", "raw_pack"}
VIEW_KEYS = {"hotspots", "mutual_cycles", "strongly_connected", "dead_code", "dup_clusters", "coupling"}
SKIPPED_SECTIONS = ("architecture", "repo_map_brief", "raw_pack")


@pytest.fixture(scope="module")
def big_dossiers(big_repo):
    dossiers = {}
    for task in ("T1", "T3", "T4"):
        out = run_vrp(
            ["dossier", str(big_repo), "--task", task, "--skip-external"],
            cwd=ROOT,
            timeout=1800,
        )
        summary = json.loads(out.split("== S5 dossier ==")[1].strip())
        doc = json.loads((big_repo / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
        dossiers[task] = {"summary": summary, "doc": doc}
    return dossiers


def test_dossier_summary_shape(big_dossiers, big_repo):
    for task, entry in big_dossiers.items():
        summary = entry["summary"]
        assert summary["task"] == task
        assert summary["project"] == "tree-sitter-analyzer"
        assert summary["budget"] == 32000
        assert Path(summary["out"]) == big_repo / ".vrp" / "dossier.json"
        assert summary["generated_at"]


def test_dossier_doc_sections_complete(big_dossiers):
    for task, entry in big_dossiers.items():
        doc = entry["doc"]
        assert doc["task"] == task
        assert doc["project"] == "tree-sitter-analyzer"
        assert set(doc["views"]) == SECTION_KEYS


def test_dossier_sql_views_six_keys_nonempty(big_dossiers):
    for _task, entry in big_dossiers.items():
        sql_views = entry["doc"]["views"]["sql_views"]
        assert set(sql_views) == VIEW_KEYS
        for name in VIEW_KEYS:
            assert len(sql_views[name]) >= 1


def test_dossier_hotspots_top1_stable_across_tasks(big_dossiers):
    tops = {}
    for task, entry in big_dossiers.items():
        top = entry["doc"]["views"]["sql_views"]["hotspots"][0]
        assert top["score"] >= 5000
        tops[task] = (top["score"], top["qualified_name"])
    assert len(set(tops.values())) == 1


def test_dossier_repomap_pure_on_big_repo(big_dossiers):
    for _task, entry in big_dossiers.items():
        repomap = entry["doc"]["views"]["repomap"]
        assert set(repomap) == {
            "files_included",
            "files_total",
            "map",
            "symbols_included",
            "symbols_total",
            "truncated",
        }
        assert repomap["symbols_total"] > 0
        text = repomap["map"]
        assert isinstance(text, str)
        first = next((line for line in text.splitlines() if line.strip()), "")
        assert not first.startswith("<")


def test_dossier_external_sections_skipped(big_dossiers):
    for _task, entry in big_dossiers.items():
        for name in SKIPPED_SECTIONS:
            section = entry["doc"]["views"][name]
            assert set(section) == {"reason", "status"}
            assert section["status"]
            assert section["reason"]


def test_dossier_summary_section_shape(big_dossiers):
    for _task, entry in big_dossiers.items():
        summary_section = entry["doc"]["views"]["summary"]
        assert set(summary_section) == {"annotation_status", "counts", "findings"}
