---
name: vrp-archaeology
description: vrp archaeology protocol - understand an unfamiliar or legacy repo before touching it. Build the fact layer, read the intake briefing, replay the rot timeline (when did it start to decay), mine implicit coupling, triage pathology findings, then navigate by fid. Use when the task is "understand this codebase" / "where did it go wrong" / "onboard me onto this repo" - not restructuring (see vrp-loop).
---

# vrp-archaeology: repo archaeology protocol

Scenario skill: read-heavy, zero-write. You are reconstructing *why a repo looks
the way it does* - rot history, implicit coupling, pathology inventory - and
producing an honest picture for the user. Restructuring (transactions) is
vrp-loop's job; curation (slices/structure) is vrp-curation's job.

## Prerequisites

- CLI on PATH (`vrp --help` works); `vrp doctor` if anything below misbehaves
- MCP optional: `vrp mcp <repo>` (stdio) for navigation queries; CLI covers everything else
- git history present = timeline/cochange have something to mine (non-git repos fail loudly and honestly)

## The 7-step dig

1. **Freshness first**: `vrp status <repo>` (facts.db snapshot; missing = never built).
   Stale or missing -> `vrp run <repo>` (full pipeline S0->S1->ETL->xcheck).
   Migration-flavored question -> `vrp assess <repo>` (read-only verdict run/skip).
2. **Intake briefing**: `dossier(task=T1)` over MCP, or `vrp dossier <repo> --task T1`
   (T1-T4 task-typed budgets; five views assembled from facts.db). This is the
   map of the territory - read it before any drill-down.
3. **Rot timeline** (the archaeology core): `vrp timeline <repo>`
   - Sampled replay of git history (default `--samples 20`, first+last commit
     always included) -> `.vrp/timeline.json` + turning-point narrative
   - Turning points name the *commit pair and date* of the largest jumps in
     dead code, cycle count, coupling - answer "when did it start to decay"
     with evidence, not vibes
   - Single-sample failure aborts the whole replay (a gap in the curve is a
     lie); non-git / no commits raise loudly - never present an empty curve
4. **Implicit coupling**: `vrp cochange <repo>` - file co-change pairs mined
   from git history (support/confidence, defaults 3/0.3 filter the display
   only). Divergence between the co-change graph and the CALLS graph is the
   discovery: files that change together but do not call each other.
5. **Pathology inventory**: `vrp findings <repo>` (S6-scored, open formula -
     every score shows its components), `vrp score <repo>` for the formula
     applied, `vrp graph <repo>` (PageRank + LPA communities vs curated
     structure Jaccard - disagreement between AI curation and graph theory is
     a finding), `vrp s2 <repo>` for the full engine sweep.
   Unknown P-code -> `vrp explain <P9>`.
6. **Navigate by fid** (MCP `vrp_query`): search / architecture / module /
   symbol / callers / callees / path / seams / coverage / impact.
   - **Never read raw source as cognition** - the fact layer is the source of
     truth; raw files only for verbatim quoting after locating via fids
   - Never invent fids; if search cannot find it, it does not exist
   - Before trusting graph answers on a file: `vrp coverage <repo> --path P`
     (six-state index completeness - "no recorded issue" is not proof)
7. **Deliverables**: `vrp report <repo>` (`.vrp/report/` - how-to-read guide,
   AI-curated overview, pathology checkup, fusion.html single-page; SARIF
   included) for the human; `vrp tour <repo>` for onboarding order (needs a
   curated structure). Optional deep audits: `vrp xcheck` (cross-engine
   agreement), `vrp golden` (precision/recall vs committed truth), 
   `vrp testcov` (coverage reconciliation), `vrp pitindex` (pit -> test index).

## Command quick reference

| Question | Command |
|---|---|
| Is the fact layer fresh? | `vrp status <repo>` |
| One-paragraph map of the repo | `vrp dossier <repo> --task T1` |
| When did it start to decay? | `vrp timeline <repo>` |
| What changes together secretly? | `vrp cochange <repo>` |
| What is rotten, ranked? | `vrp findings <repo>` / `vrp score <repo>` |
| AI curation vs graph theory | `vrp graph <repo>` |
| What does P-code mean? | `vrp explain <code>` |
| Human-facing report | `vrp report <repo>` |
| Reading order for a newcomer | `vrp tour <repo>` |

## Red lines

- **Zero-write scenario**: no tx_preview / tx_run / transact / curate_submit
  here - archaeology observes; if the dig produces a refactor target, hand it
  to vrp-loop (or `vrp fixpropose` for single-finding fixes) explicitly
- **Never read raw source as cognition**; never invent fids
- **Fresh facts before conclusions** - M4 freshness gate is a feature, not an
  obstacle; conclusions drawn from stale facts.db are archaeology fiction
- **Honest absence**: timeline on a non-git repo / cochange with no history
  fail loudly; an empty curve or empty pair list is a lie - report the error,
  do not improvise
- Turning points are *candidates* for the "when it went wrong" story, not
  verdicts - correlate with the user's own memory of the project

## Related

- Restructuring loop (transactions) -> `skills/vrp-loop/`
- Curation loop (slices/structure) -> `skills/vrp-curation/`
- Evidence-based review -> `skills/vrp-review/`
