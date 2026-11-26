---
name: vrp-review
description: vrp evidence-based review protocol - back a code review or repo audit with deterministic static evidence. Fresh facts, findings ranked by the open score formula, change blast radius with suggested tests, coverage reconciliation (fake test linkage exposed), runtime trace import, SARIF review comments. Advisory, never a gate - the human decides. Use for "review this repo/PR/changeset", "is the test story real", "audit before release/refactor", pre-merge sanity check, or a due-diligence pass where every claim must cite its evidence.
---

# vrp-review: evidence-based review protocol

Scenario skill: review with evidence, not opinions. Every claim you make in
the review must be traceable to a deterministic face (findings, impact BFS,
coverage buckets, golden truth match). The review itself never mutates the
repo - findings feed fixpropose/transact, but the human pulls the trigger.

## Prerequisites

- Fact layer fresh: `vrp status <repo>` -> `vrp run <repo>` (first time) or
  `vrp refresh <repo>` (after code changes; full recovery chain)
- CLI on PATH; `vrp doctor` when in doubt

## The 6-step review

1. **Inventory**: `vrp findings <repo>` - S6-scored pathology list; unknown
   codes via `vrp explain <code>`. Rank follows the open score formula
   (impact/evidence/span, components shown - recompute by hand if disputed).
2. **Cross-engine agreement**: `vrp xcheck <repo>` - engine-vs-engine
   agreement matrix; disagreement is a review finding in itself (which engine
   is right is a drill-down question, not an assumption).
3. **Change blast radius** (PR/changeset review): `vrp impact <repo>`
   - Diff-hunk seeding against HEAD (or `--base`), multi-source BFS over
     CALLS/CALL_REFERENCE, depth-capped with honest saturation notes
   - Suggested tests come from test_linkage aggregation - **suggested is not
     sufficient**; the full-test decision belongs to the consumer
4. **Is the test story real?**: `vrp testcov <repo> --file coverage.json
   --label <run>` - import an external coverage.py JSON (producer stays
   outside vrp), reconcile static test linkage against executed lines:
   - `linked_not_covered_measured` = "statically claims a test, never
     executed" - the fake-linkage bucket this face exists to expose
   - `covered_not_linked` = coverage without static linkage - linkage gap
   - Runtime evidence: `vrp traces <repo> --file traces.json` ->
     agreed / observed_only (static under-report) / static_only (dead-code
     corroboration)
5. **Review comments**: `vrp review <repo>` - SARIF (default
   `.vrp/report/findings.sarif`) -> `review-comments.json` beside it, ready
   to paste into a PR review; `--sarif` for an alternate SARIF source.
   Architecture layer: `vrp enforce <repo>` - P9 findings over the curated
   structure (per-language routers); no active structure = honest skip.
6. **Audit-grade claims** (optional, when the review must defend its numbers):
   `vrp golden <repo> --file truth.json` - precision/recall against committed
   golden truth; missing (under-report) and spurious (over-report) counted
   both directions; rc is always 0 - a bad number is a finding, not a failure.
   SARIF hand-off for CI consumers: `docs/评估/SARIF-CI消费指南.md`.

## Worked example: PR review with a test-story check

Input: a 12-file PR touching `pay/` + a claim that "tests cover the changes".

1. `vrp status <repo>` -> stale -> `vrp refresh <repo>` (recovery chain, rc=0)
2. `vrp findings <repo>` -> 2 new P8 hub findings on `pay/v2/adapter.py` since the PR branch
3. `vrp impact <repo> --base main` -> diff-hunk BFS: 34 symbols reached, 5 suggested tests (from test_linkage), saturation note at depth 3
4. CI produced coverage.json -> `vrp testcov <repo> --file coverage.json --label pr-123`:
   `linked_not_covered_measured` lists `test_pay_adapter.py::test_refund` - statically linked to the changed code but **never executed** in the run - the "tests cover it" claim is half fake; report it
5. `vrp review <repo>` -> `review-comments.json` beside findings.sarif - paste into the PR
6. Verdict to the user: merge-blocking evidence = fake test linkage + 2 new hub findings; advisory only, human decides

## Honesty rules

- **Advisory, never a gate**: review output informs; nothing here blocks,
  mutates, or auto-fixes. Gate semantics live in transact verdicts (vrp-loop)
- **Every number traceable**: cite the face that produced it (findings id,
  impact path, coverage bucket, golden missing/spurious counts)
- **Dismiss is a reweight, not a delete**: `vrp findings --dismiss <id>`
  reorders with a recorded reason; the finding stays in the list forever
- **Agreement is not truth**: xcheck agreement means engines agree; golden
  recall against committed truth is the only "vs ground truth" number
- **Unknown is a valid answer**: coverage `no_recorded_issue` is not proof of
  completeness; single-line symbols are honestly undetermined in testcov

## Related

- Fixing what the review found (single finding) -> `vrp fixpropose` +
  `skills/vrp-loop/` (transaction gate)
- Curation (structure for enforce) -> `skills/vrp-curation/`
- Read-first scenarios -> `skills/vrp-archaeology/`
