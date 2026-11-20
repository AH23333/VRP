"""B4 件：测试覆盖对账 vrp testcov（批⑤，#203；traces #144 独立表+对账分类
模式第三次复用；学 coverage.py JSON 报告出口 + diff-cover 行区间映射）。

外部生产者纪律（零第三方运行时依赖红线）：VRP 不跑 coverage——只导入
外部已产出的 coverage.py JSON 报告（pytest --cov --cov-report=json /
coverage json 同形）；与 traces 记录外部观测、freeze 记录命令输出同哲学。

对账语义（test_linkage 静态关联 vs 实际行覆盖互查）：test_linkage 说
"这个符号有测试"，覆盖运行说"这些行真的执行过"——两面对账把"静态说有
测试但从不执行"的假关联现形（B4 立项核心动机）。

符号执行判定（diff-cover 行区间映射的符号级适配）：body 区间
(start_line, end_line] 命中 executed_lines 即 executed、命中 missing_lines
即 missed；start_line（def/class 声明行）本身不计——coverage.py 对未
调用函数恒报 def 行 executed（import 时刻执行非测试执行），直用全区间
会把"导入过"误判"测过"；end_line <= start_line（单行符号 body 空）→
两判定恒 False → 落 undetermined 桶（诚实未定不猜）。

五桶（对象=test_linkage 静态关联目标符号）+悬挂计数：
- linked_and_covered（关联真：测试确实执行了目标 body）
- linked_not_covered_measured（**假关联强信号**——文件被测、body 有
  missing 行零 executed 行，"静态说有测试但从不执行"现形）
- linked_not_covered_unmeasured（文件不在覆盖运行——范围信号，弱于
  measured 不混桶夸大信号强度）
- linked_undetermined（文件被测、body 区间既无 executed 也无 missing
  ——单行符号/全 excluded，诚实未定）
- covered_not_linked（符号 body 执行但无静态关联——静态漏关联，
  test_linkage 假阴性面）
- linked_unresolved（test_linkage 指向无现行符号行的 fid——etl 后应
  恒 0，非零即悬挂锚诚实计数；样本=fid 回显）

存储：test_coverage 独立表（label/file_path 键——路径跨 etl 稳定=
co_changes/runtime_calls qn 键同哲学；per label 整批替换=traces 同型；
跨 etl 经 OWNED_TABLES 快照回灌存续）。观测证据独立表，不并入静态
facts（test_linkage/CALLS 边/视图零动——静态分析本体红线）。

已知局限（诚实记档，receipt note 明示）：class 符号 body 含方法 def 行
——import 时刻执行，class 符号"被覆盖"仅证明类体导入过而非方法被测
（Function 符号语义最准）；路径精确匹配（归一化 \\ → /）不做前缀剥离
——未匹配文件入 unresolved_files 诚实上报不猜。
"""

from __future__ import annotations

import bisect
import json
import sqlite3
from pathlib import Path

UNRESOLVED_SAMPLE_CAP = 10
AGREEMENT_SAMPLE_CAP = 5
DEFAULT_LABEL = "unlabeled"

# coverage.py JSON 报告逐文件消费面（totals/excluded 不入存储——对账不需要）
_LINE_FIELDS = ("executed_lines", "missing_lines")

# 对账桶（输出键序稳定；linked_unresolved 样本=fid 非 qn）
_BUCKETS = (
    "linked_and_covered",
    "linked_not_covered_measured",
    "linked_not_covered_unmeasured",
    "linked_undetermined",
    "covered_not_linked",
)


def _load_cov_file(file_path: Path) -> dict[str, dict[str, list[int]]]:
    """读 coverage.py JSON 报告；逐层形状守卫（R-AD：loads 消费必 isinstance）。"""
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"testcov: {file_path} is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        raise SystemExit(f"testcov: {file_path} must be a JSON object with a 'files' object")
    files = raw.get("files")
    if not isinstance(files, dict):
        raise SystemExit(f"testcov: {file_path} 'files' must be an object")
    normalized: dict[str, dict[str, list[int]]] = {}
    for path, entry in files.items():
        if not isinstance(path, str) or not path:
            raise SystemExit("testcov: 'files' keys must be non-empty strings")
        if not isinstance(entry, dict):
            raise SystemExit(f"testcov: files[{path!r}] must be an object")
        bucket: dict[str, list[int]] = {}
        for field in _LINE_FIELDS:
            lines = entry.get(field)
            if not isinstance(lines, list):
                raise SystemExit(
                    f"testcov: files[{path!r}] '{field}' must be an array of line numbers"
                )
            for ln in lines:
                # bool 是 int 子类（True 会混进行号）——显式排除
                if isinstance(ln, bool) or not isinstance(ln, int) or ln < 1:
                    raise SystemExit(
                        f"testcov: files[{path!r}] '{field}' must contain positive integers"
                    )
            bucket[field] = sorted(set(lines))
        normalized[path.replace("\\", "/")] = bucket
    return normalized


def _body_hit(sorted_lines: list[int], start_line: int, end_line: int) -> bool:
    """body 区间 (start_line, end_line] 命中检测（bisect；def 行排除——
    coverage.py 对未调用函数恒报 def 行 executed（import 执行非测试执行），
    start_line 本身不计入判定；单行符号 body 空恒 False）。"""
    if end_line <= start_line:
        return False
    idx = bisect.bisect_right(sorted_lines, start_line)
    return idx < len(sorted_lines) and sorted_lines[idx] <= end_line


def ingest_testcov(con: sqlite3.Connection, file_path: Path, label: str | None = None) -> dict:
    """导入 coverage.py JSON 报告 → test_coverage（per label 整批替换）+ 对账收据。"""
    file_path = Path(file_path)
    if not file_path.is_file():
        raise SystemExit(f"testcov: file not found: {file_path}")
    measured = _load_cov_file(file_path)
    run_label = label or DEFAULT_LABEL
    # per label 整批替换（幂等——traces 同型：同 label 重导入 = DELETE 后 INSERT）
    con.execute("DELETE FROM test_coverage WHERE label = ?", (run_label,))
    con.executemany(
        "INSERT INTO test_coverage (label, file_path, executed_lines_json, missing_lines_json)"
        " VALUES (?,?,?,?)",
        [
            (
                run_label,
                path,
                json.dumps(bucket["executed_lines"], ensure_ascii=False),
                json.dumps(bucket["missing_lines"], ensure_ascii=False),
            )
            for path, bucket in sorted(measured.items())
        ],
    )
    con.commit()
    return _reconcile(con, measured, run_label, file_path)


def _reconcile(
    con: sqlite3.Connection,
    measured: dict[str, dict[str, list[int]]],
    run_label: str,
    file_path: Path,
) -> dict:
    """test_linkage 静态关联 vs 实际行覆盖五桶对账 + 悬挂锚计数。"""
    linked_targets = {
        row[0] for row in con.execute("SELECT DISTINCT target_fid FROM test_linkage")
    }
    counts = {bucket: 0 for bucket in _BUCKETS}
    candidates: dict[str, list[str]] = {bucket: [] for bucket in _BUCKETS}
    facts_files: set[str] = set()
    resolved_targets: set[str] = set()
    for fid, qn, raw_file, start_line, end_line in con.execute(
        "SELECT fid, qualified_name, file_path, start_line, end_line FROM symbols"
    ):
        path = (raw_file or "").replace("\\", "/")
        if path:
            facts_files.add(path)
        cov = measured.get(path)
        if fid in linked_targets:
            resolved_targets.add(fid)
            if cov is None:
                bucket = "linked_not_covered_unmeasured"
            elif _body_hit(cov["executed_lines"], start_line, end_line):
                bucket = "linked_and_covered"
            elif _body_hit(cov["missing_lines"], start_line, end_line):
                bucket = "linked_not_covered_measured"
            else:
                bucket = "linked_undetermined"
            counts[bucket] += 1
            candidates[bucket].append(qn)
        elif cov is not None and _body_hit(cov["executed_lines"], start_line, end_line):
            counts["covered_not_linked"] += 1
            candidates["covered_not_linked"].append(qn)
    unresolved_targets = sorted(linked_targets - resolved_targets)
    unresolved_files = sorted(set(measured) - facts_files)
    return {
        "ok": True,
        "label": run_label,
        "file": str(file_path),
        "files_in_run": len(measured),
        "files_matched": len(set(measured) & facts_files),
        "lines_executed_total": sum(len(m["executed_lines"]) for m in measured.values()),
        "lines_missing_total": sum(len(m["missing_lines"]) for m in measured.values()),
        "agreement": {
            "linkage_targets": len(linked_targets),
            "linked_and_covered": counts["linked_and_covered"],
            "linked_not_covered_measured": counts["linked_not_covered_measured"],
            "linked_not_covered_unmeasured": counts["linked_not_covered_unmeasured"],
            "linked_undetermined": counts["linked_undetermined"],
            "linked_unresolved": len(unresolved_targets),
            "covered_not_linked": counts["covered_not_linked"],
            "samples": {
                bucket: sorted(candidates[bucket])[:AGREEMENT_SAMPLE_CAP]
                for bucket in _BUCKETS
            },
            "linked_unresolved_sample": unresolved_targets[:AGREEMENT_SAMPLE_CAP],
        },
        "unresolved_files_total": len(unresolved_files),
        "unresolved_files": unresolved_files[:UNRESOLVED_SAMPLE_CAP],
        "note": "test coverage ingested (file-path-keyed OWNED table, per-label whole-run "
        "replace); body interval (start_line, end_line] vs executed/missing lines - "
        "def/class declaration line excluded (import-time execution is not test "
        "execution); linked_not_covered_measured = static test_linkage says tested but "
        "coverage shows body never executed (false-linkage signal); "
        "covered_not_linked = executed without static linkage (false-negative face); "
        "class symbols: import-time class-body execution counts (function symbols "
        "most precise)",
    }
