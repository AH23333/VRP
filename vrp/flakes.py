"""L15（构想v11 甜点）：flake 登记表——复发追踪、抖王名单、隔离候选。

轮子裁决（构想v11）：GitHub flaky bot 形态借鉴（报告自动化），pytest-rerunfailures
**不引入**——重试即掩盖，本表只记录不掩盖。

登记纪律：
- **首观测不进表**——交接文档 prose 记实（判例：#120 观察记实
  test_s7_enforce::test_execute_timeout_tree_killed_returns_none 全量首跑高负载窗
  单发红、单跑绿，"首次观测，复发再立候选"）；
- **复发进表 count+1**——两度独立观测同测试红即登记，count>=2 升
  隔离候选（isolation_candidate）；
- **定案即出表**——判例 fd0d7db（F 批 2026-11-06）："rawpack determinism
  过断言"时代首现未定性放行、二度出现**定案设计缺陷**（坑 #87：跨会话
  pre_sha 比较非不变量，修复=同会话两建自证）——误判 flake 的反面教材：
  复发优先找根因，找实了不是 flake 就修，不留在本表；
- **环境类单跑绿不修**——判例 fd0d7db "testsig rust PermissionError"
  （Windows 全量磁盘高压文件锁瞬态，单跑 10 绿）：不修只记档；
- **门禁红先查环境负载**——判例 #170 push pre-push 3 红（负载诱发时序，
  本地重跑全绿后重推通过）：勿急修代码。

validate() 漂移守卫：active 条目测试文件+函数名必须在树（改名/删除=漂移红）；
resolved/renamed 条目豁免（历史证据不出树）。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

CATEGORIES = frozenset({"timing", "environment", "platform", "harness"})
STATUSES = frozenset({"active", "resolved", "renamed"})

# 名序抖动免疫：count 降序 → test_id 升序（模块级常量，测试锁序）
FLAKES: tuple[dict, ...] = (
    {
        "test_id": "tests/test_s7_enforce.py::test_execute_timeout_tree_killed_returns_none",
        "category": "timing",
        "status": "resolved",
        "count": 1,
        "first_seen": "2026-11-15",
        "last_seen": "2026-11-15",
        "isolation_candidate": False,
        "note": (
            "#120 观察记实：全量首跑高负载窗 488.66s 单发红、单跑 8.71s 绿；"
            "_execute 只消费 timed_out，本批改动严格不可能使其失败——R-W 负载 flake 族；"
            "#232 定案出表：断言界 15s<杀路径预算 47s（2s timeout+taskkill 30s R-Y 阀"
            "+15s AU-1 drain）静态不一致=测试界缺陷非产品 flake——界 15→50+垫片"
            "sleep 30→90 判别力保持（AU-1 回归悬到 90s>50 红）"
        ),
    },
)


def registry() -> list[dict]:
    """登记表全量（count 降序→test_id 升序既序，直接拷贝返回）。"""
    return [dict(entry) for entry in FLAKES]


def shake_kings(entries: list[dict] | None = None) -> list[dict]:
    """抖王名单：count >= 2（两度独立观测）——隔离候选；count 降序→id 升序自足定序。"""
    if entries is None:
        entries = registry()
    kings = [entry for entry in entries if entry["count"] >= 2]
    return sorted(kings, key=lambda e: (-e["count"], e["test_id"]))


def validate(entries: list[dict] | None = None) -> list[str]:
    """登记表自洽+漂移守卫：问题清单（空=绿）。

    - schema：category/status 在册、test_id 形如 path::name、count>=1、
      首末见日期形如 YYYY-MM-DD、无重复 test_id、count>=2 必 isolation_candidate
    - 漂移：active 条目文件存在且函数名在文件内（resolved/renamed 豁免）
    """
    if entries is None:
        entries = registry()
    problems: list[str] = []
    seen_ids: set[str] = set()
    for entry in entries:
        test_id = str(entry.get("test_id", ""))
        if "::" not in test_id:
            problems.append(f"test_id not in 'path::name' form: {test_id!r}")
        if entry.get("category") not in CATEGORIES:
            problems.append(f"unknown category: {entry.get('category')!r} ({test_id})")
        status = entry.get("status")
        if status not in STATUSES:
            problems.append(f"unknown status: {status!r} ({test_id})")
        count = entry.get("count")
        if not isinstance(count, int) or count < 1:
            problems.append(f"count must be int >= 1: {count!r} ({test_id})")
        if entry.get("isolation_candidate") is not (count is not None and count >= 2):
            problems.append(f"isolation_candidate must equal (count >= 2) ({test_id})")
        if test_id in seen_ids:
            problems.append(f"duplicate test_id: {test_id}")
        seen_ids.add(test_id)
        for date_key in ("first_seen", "last_seen"):
            date = str(entry.get(date_key, ""))
            if len(date) != 10 or date[4] != "-" or date[7] != "-":
                problems.append(f"{date_key} not YYYY-MM-DD: {date!r} ({test_id})")
        if status == "active" and "::" in test_id:
            file_part, name_part = test_id.split("::", 1)
            test_file = REPO_ROOT / file_part
            if not test_file.is_file():
                problems.append(f"active entry test file missing: {file_part}")
            elif f"def {name_part}(" not in test_file.read_text(encoding="utf-8"):
                problems.append(f"active entry test function not found: {test_id} (renamed? update registry)")
    return problems


def render_text(entries: list[dict] | None = None) -> str:
    if entries is None:
        entries = registry()
    lines = ["vrp flakes - flake 登记表（复发追踪·只记录不掩盖）"]
    if not entries:
        lines.append("  (登记表空——首观测记交接 prose，复发再进表)")
    for entry in entries:
        mark = "[抖王]" if entry["count"] >= 2 else "[观测]"
        lines.append(
            f"  {mark} {entry['test_id']}"
            f"  {entry['category']}/{entry['status']}"
            f"  count={entry['count']}"
            f"  {entry['first_seen']}..{entry['last_seen']}"
        )
        if entry.get("note"):
            lines.append(f"         {entry['note']}")
    kings = shake_kings(entries)
    lines.append(
        f"  summary: {len(entries)} 条目  抖王 {len(kings)}"
        f"  隔离候选 {sum(1 for e in kings if e['isolation_candidate'])}"
    )
    problems = validate(entries)
    for problem in problems:
        lines.append(f"  [drift] {problem}")
    return "\n".join(lines)


def flakes_rc(entries: list[dict] | None = None) -> int:
    """报告非门但漂移是门：登记表自洽失败 rc=1（改名未同步=漂移红）。"""
    return 1 if validate(entries) else 0
