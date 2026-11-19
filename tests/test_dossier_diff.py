"""A2（构想v10）：dossier diff 测试——递归对账/身份键对齐/排除面/CLI。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vrp import cli, dossier_diff


def _wf(tmp_path: Path, name: str, payload: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_diff_scalars_added_removed_changed():
    old = {"a": 1, "b": 2, "c": 3}
    new = {"a": 1, "b": 99, "d": 4}
    changes = dossier_diff.diff_json(old, new)
    by_path = {c["path"]: c for c in changes}
    assert by_path["$.b"] == {"op": "changed", "path": "$.b", "old": 2, "new": 99}
    assert by_path["$.c"]["op"] == "removed" and by_path["$.c"]["value"] == 3
    assert by_path["$.d"]["op"] == "added" and by_path["$.d"]["value"] == 4


def test_diff_nested_dict_recursion():
    old = {"views": {"summary": {"hotspots": 1}}}
    new = {"views": {"summary": {"hotspots": 2}, "extra": True}}
    changes = dossier_diff.diff_json(old, new)
    paths = {c["path"] for c in changes}
    assert paths == {"$.views.summary.hotspots", "$.views.extra"}


def test_diff_type_change_is_changed():
    changes = dossier_diff.diff_json({"x": 1}, {"x": {"deep": 1}})
    assert changes == [{"op": "changed", "path": "$.x", "old": 1, "new": {"deep": 1}}]


def test_diff_identical_zero_changes():
    payload = {"a": [1, 2], "b": {"c": "x"}}
    assert dossier_diff.diff_json(payload, payload) == []


def test_diff_generated_at_ignored():
    changes = dossier_diff.diff_json(
        {"generated_at": "2026-11-18T01:00:00Z", "x": 1},
        {"generated_at": "2026-11-19T02:00:00Z", "x": 1},
    )
    assert changes == []


def test_diff_list_identity_key_alignment_no_false_changed_on_prepend():
    """头插病理只报 added 不产生整列假 changed（release notes 语义核心）。"""
    old = [{"symbol": "b", "score": 2}, {"symbol": "c", "score": 3}]
    new = [{"symbol": "a", "score": 1}, {"symbol": "b", "score": 2}, {"symbol": "c", "score": 3}]
    changes = dossier_diff.diff_json(old, new)
    assert changes == [
        {"op": "added", "path": "$.a", "value": {"symbol": "a", "score": 1}}
    ]


def test_diff_list_identity_key_removal_and_inner_change():
    old = [{"fid": "f1", "v": 1}, {"fid": "f2", "v": 2}]
    new = [{"fid": "f1", "v": 9}]
    changes = dossier_diff.diff_json(old, new)
    by_path = {c["path"]: c for c in changes}
    assert by_path["$.f1.v"] == {"op": "changed", "path": "$.f1.v", "old": 1, "new": 9}
    assert by_path["$.f2"]["op"] == "removed"


def test_diff_list_identity_key_preference_order():
    """id 优先于 symbol（偏好序 deterministic）。"""
    old = [{"symbol": "s", "id": "i1"}]
    new = [{"symbol": "t", "id": "i1"}]
    changes = dossier_diff.diff_json(old, new)
    assert len(changes) == 1
    assert changes[0]["path"] == "$.i1.symbol"


def test_diff_list_scalar_fallback_index_aligned():
    changes = dossier_diff.diff_json({"n": [1, 2]}, {"n": [1, 2, 3]})
    assert changes == [{"op": "added", "path": "$.n[2]", "value": 3}]


def test_diff_list_no_common_id_key_falls_back():
    """双列表 dict 无公共身份键→逐位对齐回退。"""
    old = [{"a": 1}]
    new = [{"b": 2}]
    changes = dossier_diff.diff_json(old, new)
    assert {c["op"] for c in changes} == {"added", "removed"}


def test_diff_list_id_key_requires_all_elements():
    """部分元素缺身份键→不按键对齐（全元素具备才启用）。"""
    old = [{"fid": "f1"}, {"nokey": 1}]
    new = [{"fid": "f1"}, {"nokey": 2}]
    changes = dossier_diff.diff_json(old, new)
    assert any(c["path"] == "$[1].nokey" for c in changes)


def test_run_dossier_diff_end_to_end(tmp_path):
    old_p = _wf(tmp_path, "old.json", {"views": {"summary": {"n": 1}}, "generated_at": "t1"})
    new_p = _wf(tmp_path, "new.json", {"views": {"summary": {"n": 2}}, "generated_at": "t2"})
    result = dossier_diff.run_dossier_diff(old_p, new_p)
    assert result["summary"] == {"added": 0, "removed": 0, "changed": 1}
    assert result["changes"][0]["path"] == "$.views.summary.n"


def test_run_dossier_diff_bad_json_exit(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    good = _wf(tmp_path, "good.json", {})
    with pytest.raises(SystemExit, match="cannot load"):
        dossier_diff.run_dossier_diff(bad, good)


def test_run_dossier_diff_missing_file_exit(tmp_path):
    good = _wf(tmp_path, "good.json", {})
    with pytest.raises(SystemExit, match="cannot load"):
        dossier_diff.run_dossier_diff(tmp_path / "nope.json", good)


def test_run_dossier_diff_non_object_top_level_exit(tmp_path):
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2]", encoding="utf-8")
    good = _wf(tmp_path, "good2.json", {})
    with pytest.raises(SystemExit, match="top level must be a JSON object"):
        dossier_diff.run_dossier_diff(arr, good)


def test_render_text_groups_by_op(tmp_path):
    old_p = _wf(tmp_path, "o.json", {"a": 1, "b": 2})
    new_p = _wf(tmp_path, "n.json", {"a": 9, "c": 3})
    text = dossier_diff.render_text(dossier_diff.run_dossier_diff(old_p, new_p))
    assert "1 added, 1 removed, 1 changed" in text
    assert "ADDED (1):" in text and "REMOVED (1):" in text and "CHANGED (1):" in text
    assert "$.a: 1 -> 9" in text


def test_cli_dossier_diff_end_to_end(tmp_path, capsys):
    old_p = _wf(tmp_path, "o.json", {"x": 1})
    new_p = _wf(tmp_path, "n.json", {"x": 2})
    rc = cli.main(["dossier-diff", str(old_p), str(new_p), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["summary"]["changed"] == 1


def test_cli_parser_accepts_dossier_diff():
    parser = cli.build_parser()
    args = parser.parse_args(["dossier-diff", "a.json", "b.json", "--json"])
    assert args.old == "a.json" and args.new == "b.json" and args.json


ROOT = Path(__file__).resolve().parents[1]


def test_dogfood_self_diff_zero_changes():
    """dogfood：自仓 dossier.json 自 diff 零变更（同一份文件的诚实底线）。"""
    d = ROOT / ".vrp" / "dossier.json"
    if not d.exists():
        pytest.skip("no local dossier.json (run `vrp dossier` first)")
    result = dossier_diff.run_dossier_diff(d, d)
    assert result["summary"] == {"added": 0, "removed": 0, "changed": 0}
