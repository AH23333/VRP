"""F15+BB3 golden 审计：对独立真值的双向计数矩阵（CGC 循环完美门防线）。

- 真值=committed JSON（vrp-golden-1 格式）——与被审计库物理分离；
  「手工改过的 golden 不落盘=自欺」防线=provenance 必填+守卫测试钉死。
- violations 面（F15 查准查全）：findings 按 source 过滤，truth 条目全含
  importer/imported 时键含两者——多集匹配（同键多条按 min 计 matched，
  重复违例诚实计数不折叠）。
- symbols 面（BB3 ETL 准确率）：域=truth 出现的 file 集（有界语料完整
  枚举——域外符号不触碰），严格精确匹配 (qn, label, file, start, end)——
  近失（同 qn 错行号）两侧成对计数，解析不精度诚实现形。
- precision/recall 分母零→null 诚实（「期望零且得零」靠计数面呈现，
  不造 1.0）。
- rc 恒 0（度量非门禁，testcov/pitindex 同口径；红面归测试 pin/jq）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import dbpath

GOLDEN_FORMAT = "vrp-golden-1"
SAMPLE_CAP = 5
SCHEMA_PATH = Path(__file__).parent / "schema" / "facts_schema.sql"

_VIOLATION_REQUIRED = ("kind", "file", "line")
_VIOLATION_OPTIONAL = ("importer", "imported", "pathology")
_SYMBOL_KEYS = ("qn", "label", "file", "start", "end")
_SYMBOL_ROW_LEN = 9
_TOP_KEYS = ("format", "name", "language", "provenance", "fixture", "truth")
_FIXTURE_KEYS = ("repo_name", "files", "meta", "symbols", "structure")
_TRUTH_KEYS = ("violations", "symbols", "findings_source")


def _die(msg: str) -> None:
    raise SystemExit(f"golden: {msg}")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value) -> bool:
    return isinstance(value, str) and bool(value)


def _check_keys(entry: dict, allowed: tuple, where: str) -> None:
    for key in entry:
        if key not in allowed:
            _die(f"{where}: unknown key {key!r} (allowed: {', '.join(allowed)})")


def _validate_violation(entry, where: str) -> dict:
    if not isinstance(entry, dict):
        _die(f"{where}: violation entry must be an object")
    _check_keys(entry, _VIOLATION_REQUIRED + _VIOLATION_OPTIONAL, where)
    for key in _VIOLATION_REQUIRED:
        if key not in entry:
            _die(f"{where}: violation missing required key {key!r}")
    if not _is_str(entry["kind"]) or not _is_str(entry["file"]):
        _die(f"{where}: kind/file must be non-empty strings")
    if not _is_int(entry["line"]) or entry["line"] < 1:
        _die(f"{where}: line must be a positive int (bool excluded)")
    for key in _VIOLATION_OPTIONAL:
        if key in entry and not _is_str(entry[key]):
            _die(f"{where}: {key} must be a non-empty string when present")
    return entry


def _validate_symbol(entry, where: str) -> dict:
    if not isinstance(entry, dict):
        _die(f"{where}: symbol entry must be an object")
    _check_keys(entry, _SYMBOL_KEYS, where)
    for key in _SYMBOL_KEYS:
        if key not in entry:
            _die(f"{where}: symbol missing required key {key!r}")
    if not _is_str(entry["qn"]) or not _is_str(entry["label"]) or not _is_str(entry["file"]):
        _die(f"{where}: qn/label/file must be non-empty strings")
    if not _is_int(entry["start"]) or not _is_int(entry["end"]):
        _die(f"{where}: start/end must be ints (bool excluded)")
    if entry["start"] < 1 or entry["end"] < 1:
        _die(f"{where}: start/end must be positive (1-based lines)")
    return entry


def _validate_seed_row(row, where: str) -> list:
    if not isinstance(row, list) or len(row) != _SYMBOL_ROW_LEN:
        _die(f"{where}: seed row must be a list of {_SYMBOL_ROW_LEN} fields"
             " [fid, project, qn, label, name, file_path, start_line, end_line, content_hash]")
    for i, value in enumerate(row[:6]):
        if not isinstance(value, str):
            _die(f"{where}: seed field {i} must be a string")
    for i in (6, 7):
        if not _is_int(row[i]) or row[i] < 1:
            _die(f"{where}: seed start/end must be positive ints")
    if not isinstance(row[8], str):
        _die(f"{where}: seed content_hash must be a string")
    return row


def _validate_fixture_rel(rel: str, where: str) -> None:
    if not _is_str(rel):
        _die(f"{where}: file keys must be non-empty strings")
    if "\\" in rel or rel.startswith("/"):
        _die(f"{where}: file paths must be relative with '/' separators: {rel!r}")
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _die(f"{where}: invalid relative path {rel!r}")


def _validate_fixture(fixture, where: str) -> dict:
    if not isinstance(fixture, dict):
        _die(f"{where}: fixture must be an object")
    _check_keys(fixture, _FIXTURE_KEYS, where)
    for key in ("repo_name", "files"):
        if key not in fixture:
            _die(f"{where}: fixture missing required key {key!r}")
    repo_name = fixture["repo_name"]
    if not _is_str(repo_name) or "/" in repo_name or "\\" in repo_name or repo_name in (".", ".."):
        _die(f"{where}: repo_name must be a plain directory name")
    files = fixture["files"]
    if not isinstance(files, dict) or not files:
        _die(f"{where}: files must be a non-empty object {{relpath: content}}")
    for rel in files:
        _validate_fixture_rel(rel, f"{where}.files")
        if not isinstance(files[rel], str):
            _die(f"{where}.files: content for {rel!r} must be a string")
    meta = fixture.get("meta", {})
    if not isinstance(meta, dict):
        _die(f"{where}: meta must be an object")
    for key, value in meta.items():
        if not _is_str(key) or not isinstance(value, str):
            _die(f"{where}.meta: keys and values must be strings")
    seeds = fixture.get("symbols", [])
    if not isinstance(seeds, list):
        _die(f"{where}: symbols (seed rows) must be a list")
    for i, row in enumerate(seeds):
        _validate_seed_row(row, f"{where}.symbols[{i}]")
    structure = fixture.get("structure")
    if structure is not None and not isinstance(structure, dict):
        _die(f"{where}: structure must be an object when present")
    return fixture


def load_golden(path: Path) -> dict:
    """加载并全量校验 golden 真值文件（形状违例整文件拒绝，零审计）。"""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _die(f"cannot read golden file: {exc}")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        _die(f"golden file is not valid JSON: {exc}")
    if not isinstance(data, dict):
        _die("golden top level must be a JSON object")
    _check_keys(data, _TOP_KEYS, "golden")
    if data.get("format") != GOLDEN_FORMAT:
        _die(f"unsupported golden format {data.get('format')!r} (expected {GOLDEN_FORMAT!r})")
    for key in ("name", "language", "provenance"):
        if not _is_str(data.get(key)):
            _die(f"{key} must be a non-empty string"
                 + (" (provenance: 手工改过的 golden 不落盘=自欺)" if key == "provenance" else ""))
    truth = data.get("truth")
    if not isinstance(truth, dict):
        _die("truth must be an object")
    _check_keys(truth, _TRUTH_KEYS, "truth")
    violations = truth.get("violations")
    if violations is not None:
        if not isinstance(violations, list):
            _die("truth.violations must be a list (empty list = valid 'expect zero' face)")
        violations = [
            _validate_violation(entry, f"truth.violations[{i}]") for i, entry in enumerate(violations)
        ]
    symbols = truth.get("symbols")
    if symbols is not None:
        if not isinstance(symbols, list):
            _die("truth.symbols must be a list (empty list = valid 'expect zero' face)")
        symbols = [
            _validate_symbol(entry, f"truth.symbols[{i}]") for i, entry in enumerate(symbols)
        ]
    if violations is None and symbols is None:
        _die("truth must declare at least one of 'violations'/'symbols'")
    findings_source = truth.get("findings_source", "s7-enforce")
    if not _is_str(findings_source):
        _die("truth.findings_source must be a non-empty string when present")
    fixture = data.get("fixture")
    if fixture is not None:
        fixture = _validate_fixture(fixture, "fixture")
    return {
        "format": GOLDEN_FORMAT,
        "name": data["name"],
        "language": data["language"],
        "provenance": data["provenance"],
        "fixture": fixture,
        "truth": {
            "violations": violations,
            "symbols": symbols,
            "findings_source": findings_source,
        },
    }


def write_fixture_files(golden: dict, base_dir: Path) -> Path:
    """按 fixture 节落真源文件（字节=JSON 内容，newline pin 保跨平台确定）。"""
    fixture = golden.get("fixture")
    if fixture is None:
        _die("golden has no fixture section (truth-only file for CLI audits)")
    repo = Path(base_dir) / fixture["repo_name"]
    if repo.exists():
        _die(f"fixture repo already exists: {repo}")
    for rel in sorted(fixture["files"]):
        target = repo / Path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(fixture["files"][rel], encoding="utf-8", newline="")
    return repo


def seed_fixture_db(golden: dict, repo: Path) -> Path:
    """按 fixture 节建 facts.db（schema+symbols 种子+meta+structure）。

    quick 审计用（s7 引擎消费 facts 面）；BB3 ETL 深度审计不走本函数
    （真链 index+etl 自建 facts.db——被审计面必须被审计者自产）。
    """
    fixture = golden["fixture"]
    db_path = Path(repo) / ".vrp" / "facts.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for row in fixture.get("symbols", []):
            con.execute(
                "INSERT INTO symbols (fid, project, qualified_name, label, name,"
                " file_path, start_line, end_line, content_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(row),
            )
        for key in sorted(fixture.get("meta", {})):
            con.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (key, fixture["meta"][key]),
            )
        structure = fixture.get("structure")
        if structure is not None:
            con.execute(
                "INSERT INTO structure (payload_json, status, created_at)"
                " VALUES (?, 'active', 't')",
                (json.dumps(structure, ensure_ascii=False),),
            )
        con.commit()
    finally:
        con.close()
    return db_path


def _ratio(numerator: int, denominator: int):
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _face(truth_keys: list, actual_keys: list, fmt) -> dict:
    """多集匹配双向计数：matched=min 计/missing=truth 余/spurious=actual 余。"""
    truth_counts: dict = {}
    for key in truth_keys:
        truth_counts[key] = truth_counts.get(key, 0) + 1
    actual_counts: dict = {}
    for key in actual_keys:
        actual_counts[key] = actual_counts.get(key, 0) + 1
    matched = 0
    missing = 0
    for key, count in truth_counts.items():
        have = actual_counts.get(key, 0)
        matched += min(count, have)
        missing += max(count - have, 0)
    spurious = 0
    for key, count in actual_counts.items():
        spurious += max(count - truth_counts.get(key, 0), 0)
    missing_samples = sorted(
        fmt(key) for key, count in truth_counts.items() if count > actual_counts.get(key, 0)
    )
    spurious_samples = sorted(
        fmt(key) for key, count in actual_counts.items() if count > truth_counts.get(key, 0)
    )
    return {
        "truth": len(truth_keys),
        "actual": len(actual_keys),
        "matched": matched,
        "missing": missing,
        "spurious": spurious,
        "precision": _ratio(matched, matched + spurious),
        "recall": _ratio(matched, matched + missing),
        "missing_samples": missing_samples[:SAMPLE_CAP],
        "spurious_samples": spurious_samples[:SAMPLE_CAP],
    }


def audit_violations(con, truth_violations: list, source: str) -> dict:
    """F15 面：findings（source 过滤）vs truth.violations 多集匹配。

    键=kind/file/line +（truth 条目全含时的）importer/imported——payload
    提取逐层守卫（R-AD：loads 拒绝值非对象/字段非 str 时键位 None）。
    """
    rows = con.execute(
        "SELECT kind, file_path, start_line, payload_json FROM findings"
        " WHERE source = ? ORDER BY file_path, start_line, kind, payload_json",
        (source,),
    ).fetchall()
    use_importer = all("importer" in entry for entry in truth_violations)
    use_imported = all("imported" in entry for entry in truth_violations)
    truth_keys = []
    for entry in truth_violations:
        key = [entry["kind"], entry["file"], entry["line"]]
        if use_importer:
            key.append(entry["importer"])
        if use_imported:
            key.append(entry["imported"])
        truth_keys.append(tuple(key))
    actual_keys = []
    for kind, file_path, start_line, payload_json in rows:
        importer = None
        imported = None
        try:
            parsed = json.loads(payload_json)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            candidate = parsed.get("importer")
            if isinstance(candidate, str):
                importer = candidate
            candidate = parsed.get("imported")
            if isinstance(candidate, str):
                imported = candidate
        key = [kind, file_path, start_line]
        if use_importer:
            key.append(importer)
        if use_imported:
            key.append(imported)
        actual_keys.append(tuple(key))

    def fmt(key) -> str:
        arrow = " -> ".join(str(part) for part in key[3:])
        return f"{key[0]} {key[1]}:{key[2]}" + (f" {arrow}" if arrow else "")

    return _face(truth_keys, actual_keys, fmt)


def audit_symbols(con, truth_symbols: list) -> dict:
    """BB3 面：域=truth file 集（有界语料完整枚举），严格精确匹配。

    近失（同 qn 错行号）两侧成对计数——解析不精度不掩埋。
    """
    scope = {entry["file"] for entry in truth_symbols}
    rows = con.execute(
        "SELECT qualified_name, label, file_path, start_line, end_line FROM symbols"
        " ORDER BY qualified_name, file_path, start_line, end_line, label"
    ).fetchall()
    actual_keys = [
        (row[0], row[1], row[2], row[3], row[4]) for row in rows if row[2] in scope
    ]
    truth_keys = [
        (entry["qn"], entry["label"], entry["file"], entry["start"], entry["end"])
        for entry in truth_symbols
    ]

    def fmt(key) -> str:
        return f"{key[0]} [{key[1]}] {key[2]}:{key[3]}-{key[4]}"

    return _face(truth_keys, actual_keys, fmt)


def run_audit(repo: Path, golden_path: Path) -> dict:
    """CLI 面：对既有 facts.db（ro）按 truth 声明的面审计——记分卡 receipt。"""
    golden = load_golden(golden_path)
    db_path = Path(repo) / ".vrp" / "facts.db"
    if not db_path.exists():
        _die(f"facts.db missing - run 'vrp run' first: {db_path}")
    con = dbpath.ro_connect(db_path)
    try:
        faces: dict = {}
        truth = golden["truth"]
        if truth["violations"] is not None:
            faces["violations"] = audit_violations(
                con, truth["violations"], truth["findings_source"]
            )
        if truth["symbols"] is not None:
            faces["symbols"] = audit_symbols(con, truth["symbols"])
    finally:
        con.close()
    return {
        "ok": True,
        "file": str(golden_path),
        "name": golden["name"],
        "language": golden["language"],
        "faces": faces,
        "note": "audit vs committed truth; rc is always 0 (measurement, not a gate)"
        " - red faces belong to test pins / jq consumers",
    }
