"""S7 TS/JS 执法引擎单元 + mocked E2E（真 depcruise E2E 见 tests/deep/test_deep_s7_enforce_ts.py）。"""

import json
import sqlite3
from pathlib import Path

from vrp import s7_enforce, s7_enforce_ts

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "core-mod", "name": "核心", "responsibility": "算", "symbol_fids": ["t1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["t2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    },
    ensure_ascii=False,
)

CANNED_TS_JSON = json.dumps(
    {
        "modules": [
            {
                "source": "src/core/a.ts",
                "dependencies": [
                    {
                        "resolved": "src/iface/b.ts",
                        "rules": [{"severity": "error", "name": "layer: core must not import interface"}],
                    }
                ],
            },
            {
                "source": "src/iface/b.ts",
                "dependencies": [
                    {
                        "resolved": "src/core/a.ts",
                        "rules": [{"severity": "warn", "name": "advice-only"}],  # warn 不算违例
                    }
                ],
            },
        ],
        "summary": {"error": 1, "warn": 1, "totalCruised": 2},
    }
)


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('t1', 'p', 'src.core.a', 'Function', 'a', 'src/core/a.ts', 1, 3, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('t2', 'p', 'src.iface.b', 'Function', 'b', 'src/iface/b.ts', 1, 2, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "src" / "core").mkdir(parents=True)
    (repo / "src" / "core" / "a.ts").write_text(
        'import { b } from "../iface/b";\nexport const a = b + 1;\n', encoding="utf-8"
    )
    (repo / "src" / "iface").mkdir(parents=True)
    (repo / "src" / "iface" / "b.ts").write_text("export const b = 41;\n", encoding="utf-8")
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


def _mock_exec(rc, stdout, calls=None, stderr=""):
    def fake(bin_path, repo, config, targets, timeout):
        if calls is not None:
            calls.append({"config": config, "cwd": str(repo), "targets": list(targets), "bin": str(bin_path)})
        return rc, stdout, stderr

    return fake


# ------------------------------------------------------------------ unit


def test_prefix_variants():
    assert s7_enforce_ts._prefix("src/core/a.ts") == "src/core"
    assert s7_enforce_ts._prefix("src\\x\\b.tsx") == "src/x"
    assert s7_enforce_ts._prefix("packages/core/src/m.ts") == "packages/core/src"
    assert s7_enforce_ts._prefix("app.js") is None  # 根级文件无目录前缀
    assert s7_enforce_ts._prefix("src/a.vue") is None  # 非 TS/JS
    assert s7_enforce_ts._prefix("node_modules/x/y.js") is None
    assert s7_enforce_ts._prefix(".vrp/x.ts") is None


def test_derive_ts_containers(tmp_path):
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    try:
        payload = json.loads(STRUCTURE)
        out = s7_enforce_ts.derive_ts_containers(con, payload)
    finally:
        con.close()
    assert out["containers"] == {"core-mod": ["src/core"], "iface-mod": ["src/iface"]}
    assert out["extensions"] == []


def test_build_config_shape():
    contracts = [
        {
            "name": "layer: core must not import interface",
            "source_modules": ["src/core", "src/deep.dir"],
            "forbidden_modules": ["src/iface"],
        },
        {
            "name": "layer: core must not import app",
            "source_modules": ["src/core"],
            "forbidden_modules": ["src/app"],
        },
    ]
    text = s7_enforce_ts.build_config(contracts)
    assert "module.exports" in text
    assert "forbidden: [" in text and 'severity: "error"' in text
    assert '"layer: core must not import interface"' in text  # 中文契约名 JSON 转义可回读
    assert '"^(?:src/core|src/deep\\.dir)(?:/|$)"' in text  # regex 转义 + 前缀边界
    assert '"^(?:src/iface)(?:/|$)"' in text
    # 多契约逗号分隔回归（真实冒烟实证：漏逗号 → Node SyntaxError → depcruise rc=1）
    assert "    }\n    {\n" not in text.replace(",\n", "\n") or "},\n" in text
    assert text.count("},\n    {") == len(contracts) - 1  # 每对相邻规则恰好一个逗号


def test_parse_violations_error_only_and_dedup():
    dup = json.loads(CANNED_TS_JSON)
    dup["modules"][0]["dependencies"].append(dict(dup["modules"][0]["dependencies"][0]))
    violations, summary = s7_enforce_ts.parse_violations(json.dumps(dup))
    assert violations == [
        {
            "contract": "layer: core must not import interface",
            "importer": "src/core/a.ts",
            "imported": "src/iface/b.ts",
            "line": None,
        }
    ]  # warn 不算 + 同 (rule, importer, imported) 去重
    assert summary["totalCruised"] == 2 and summary["error"] == 1


# ------------------------------------------------------------ run_ts mocked


def test_run_ts_violation_mock(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(s7_enforce_ts, "find_depcruise", lambda: Path("fake-depcruise.cmd"))
    monkeypatch.setattr(s7_enforce_ts, "_execute", _mock_exec(0, CANNED_TS_JSON, calls))
    con = _con(repo)
    try:
        out = s7_enforce_ts.run_ts(repo, con, json.loads(STRUCTURE), "active", {"core-mod": ["src/core"], "iface-mod": ["src/iface"]})
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "dependency-cruiser"
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["total_cruised"] == 2
    assert calls[0]["config"] == ".vrp/dependency-cruiser.cjs"
    assert calls[0]["cwd"] == str(repo)
    assert calls[0]["targets"] == ["src/core", "src/iface"]
    assert not (repo / ".vrp" / "dependency-cruiser.cjs").exists()  # 运行后清理
    row = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT pathology, kind, source, anchor_fid, file_path, start_line, end_line, payload_json"
        " FROM findings"
    ).fetchone()
    assert row[0] == "P9"
    assert row[1] == "layer-violation"
    assert row[2] == "s7-enforce"
    assert row[3] == "t1"  # 锚定文件首符号
    assert row[4] == "src/core/a.ts"
    assert row[5] == 1 and row[6] == 1  # 行号取锚符号 start_line
    payload = json.loads(row[7])
    assert payload["source_layer"] == "core"
    assert payload["forbidden_layer"] == "interface"
    assert payload["importer"] == "src/core/a.ts"
    assert payload["imported"] == "src/iface/b.ts"
    assert payload["line"] is None  # depcruise 无行号，诚实记录
    assert payload["structure_status"] == "active"


def test_run_ts_idempotent_mock(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_ts, "find_depcruise", lambda: Path("fake-depcruise.cmd"))
    monkeypatch.setattr(s7_enforce_ts, "_execute", _mock_exec(0, CANNED_TS_JSON))
    con = _con(repo)
    try:
        first = s7_enforce_ts.run_ts(repo, con, json.loads(STRUCTURE), "active", {"core-mod": ["src/core"], "iface-mod": ["src/iface"]})
        second = s7_enforce_ts.run_ts(repo, con, json.loads(STRUCTURE), "active", {"core-mod": ["src/core"], "iface-mod": ["src/iface"]})
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # replace-all：不膨胀


def test_run_ts_compliant_clears_old_findings(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 't1', '{}')"
    )
    con.commit()
    con.close()
    clean = json.dumps({"modules": [], "summary": {"error": 0, "warn": 0, "totalCruised": 2}})
    monkeypatch.setattr(s7_enforce_ts, "find_depcruise", lambda: Path("fake-depcruise.cmd"))
    monkeypatch.setattr(s7_enforce_ts, "_execute", _mock_exec(0, clean))
    con = _con(repo)
    try:
        out = s7_enforce_ts.run_ts(repo, con, json.loads(STRUCTURE), "active", {"core-mod": ["src/core"], "iface-mod": ["src/iface"]})
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 0  # 合规重跑清掉旧违例


def test_run_ts_enforcer_failed_keeps_old_findings(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, payload_json)"
        " VALUES ('P9', 'layer-violation', 's7-enforce', 't1', '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(s7_enforce_ts, "find_depcruise", lambda: Path("fake-depcruise.cmd"))
    monkeypatch.setattr(s7_enforce_ts, "_execute", _mock_exec(1, "", stderr="ERROR: Can't open config file"))
    con = _con(repo)
    try:
        out = s7_enforce_ts.run_ts(repo, con, json.loads(STRUCTURE), "active", {"core-mod": ["src/core"], "iface-mod": ["src/iface"]})
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "rc=1" in out["reason"]
    assert "Can't open config" in out["stderr_tail"]
    count = sqlite3.connect(repo / ".vrp" / "facts.db").execute(
        "SELECT COUNT(*) FROM findings WHERE source = 's7-enforce'"
    ).fetchone()[0]
    assert count == 1  # 执法器失败不误删既有记录


def test_run_ts_timeout(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_ts, "find_depcruise", lambda: Path("fake-depcruise.cmd"))
    monkeypatch.setattr(s7_enforce_ts, "_execute", lambda bin_path, repo, config, targets, timeout: None)
    con = _con(repo)
    try:
        out = s7_enforce_ts.run_ts(repo, con, json.loads(STRUCTURE), "active", {"core-mod": ["src/core"], "iface-mod": ["src/iface"]}, timeout=7)
    finally:
        con.close()
    assert out["status"] == "enforcer-failed"
    assert "timed out after 7s" in out["reason"]
    assert not (repo / ".vrp" / "dependency-cruiser.cjs").exists()


def test_run_ts_not_found(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_ts, "find_depcruise", lambda: None)
    con = _con(repo)
    try:
        out = s7_enforce_ts.run_ts(repo, con, json.loads(STRUCTURE), "active", {"core-mod": ["src/core"], "iface-mod": ["src/iface"]})
    finally:
        con.close()
    assert out["status"] == "enforcer-not-found"
    assert "VRP_DEPCRUISE_BIN" in out["reason"]
    assert out["contracts"] == 1


def test_run_ts_targets_missing_dirs_not_enforceable(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_ts, "find_depcruise", lambda: Path("fake-depcruise.cmd"))
    con = _con(repo)
    try:
        out = s7_enforce_ts.run_ts(repo, con, json.loads(STRUCTURE), "active", {"core-mod": ["ghost/core"], "iface-mod": ["ghost/iface"]})
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "no container directories" in out["reason"]


# ------------------------------------------------- run() 语言路由（s7_enforce）


def test_run_routes_to_ts_engine(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(s7_enforce_ts, "find_depcruise", lambda: Path("fake-depcruise.cmd"))
    monkeypatch.setattr(s7_enforce_ts, "_execute", _mock_exec(0, CANNED_TS_JSON, calls))
    con = _con(repo)
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "dependency-cruiser"  # 路由命中 TS 引擎
    assert out["violations"] == 1
    assert calls[0]["targets"] == ["src/core", "src/iface"]


def test_run_mixed_language_not_enforceable(tmp_path):
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    # 加一个 .py 符号进 core 模块 → py 容器与 ts 容器并存
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('t3', 'p', 'legacy.mod', 'Module', 'mod', 'legacy/mod.py', 1, 2, 'h')"
    )
    mixed = json.loads(STRUCTURE)
    mixed["modules"][0]["symbol_fids"].append("t3")
    con.execute("UPDATE structure SET payload_json = ?", (json.dumps(mixed, ensure_ascii=False),))
    con.commit()
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "not-enforceable"
    assert "mixed-language" in out["reason"]


def test_run_ts_cli_enforce_wiring(tmp_path, monkeypatch, capsys):
    from vrp import cli

    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s7_enforce_ts, "find_depcruise", lambda: Path("fake-depcruise.cmd"))
    monkeypatch.setattr(s7_enforce_ts, "_execute", _mock_exec(0, CANNED_TS_JSON))
    rc = cli.main(["enforce", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["engine"] == "dependency-cruiser"
    assert out["findings_written"] == 1
