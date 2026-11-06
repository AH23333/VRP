---
name: vrp-loop
description: AI restructuring loop over the vrp MCP tools - navigate, verify, produce transactions, preview, run, read verdicts, fix-and-retry (max 3 per transaction). Use for any agent connected to a vrp MCP server (`vrp mcp <repo>`).
---

# vrp-loop: AI restructuring loop protocol v2

## Prerequisites

- MCP server connected (`vrp mcp <repo>`, stdio, per-repo binding; `pip install vrp[mcp]`)
- Fact layer built (facts.db exists; if nav_* reports facts.db missing, ask the user to run `vrp run <repo>` first)
- Transaction contract: `docs/契约/事务契约.md` (schema / gate codes / production guide - read before producing)

## The 8-step loop

1. **Locate**: `nav_search(query)` for candidate symbols (fid handles + file_path)
   - For the global map first call `nav_architecture()` (prefers the AI-curated structure; directory aggregation fallback)
2. **Drill down and verify**: `nav_module(module_id=file_path)` -> `nav_symbol(fid)`
   - **Full-chain drilling**: keep drilling caller/callee fids until the blast radius is closed
   - Do not read raw source as a source of cognition - use the layered interfaces (`query_facts`, dossier)
3. **Produce the transaction**: assemble tx_json per the contract (`{"id","type","summary","declared_moves","edits"}`)
   - **Minimal transactions**: one intent per transaction; edits are full-file overwrites (fetch current content first)
   - T1 declared_moves must be complete (base them on nav_symbol/query_facts qualified_names, never memory)
4. **Preview**: `tx_preview(tx_json)` - read the unified diff, confirm the change surface matches the declared intent
5. **Run**: `tx_run(tx_json)`
   - T4 returns needs_confirmation - **do not retry**; summarize the diff and hand it to the user for manual release (CLI `vrp transact --confirm`)
6. **Read the verdict**: `tx_verdicts()`; on failure read `failed_gate` + `failed_codes`
7. **Fix and retry**: repair tx_json per the failure codes, preview -> run again; **max 3 retries per transaction** - after 3 failures change approach or drop the target and tell the user why
8. **Regression**: when the batch is done, ask the user to run `vrp run <repo>` to rebuild the fact layer + `vrp dossier` to re-check pathology (open verdicts feed into the dossier evidence section automatically)

## Enforcement loop (S7)

Once a structure is ingested (`curate_submit` kind=structure), run `enforce()`:
- Broken layer contracts become P9 findings (idempotent replace-all) - feed them back into the loop as refactor targets
- Honestly skipped when no structure or status is not active

## Drift handling

Curated artifacts go stale as transactions land. `drift_report()` (read-only) reports per-artifact `fresh` / `missing_fids` / `missing_edges` plus relocation candidates from the fid ledger:
- A unique qualified_name candidate yields a direct `remap` (old fid -> new fid); re-curate with the remap and `curate_submit` again (full re-validation, no bypass)
- Non-empty `summary.active_but_drifted` means a hole in the revalidation chain - report it to the user immediately

## Failure code quick reference (contract §4)

| Code | Meaning | Fix direction |
|---|---|---|
| `gate/G0/apply-edits` | target missing/already exists | check file path and op; nav_module for current state |
| `gate/G1/parse-errors` | edit introduced syntax errors | regenerate full content |
| `gate/G2/static-check` | lint/typecheck failed | read detail, fix lint |
| `gate/G3/test` | tests failed | read detail.failed test names; transaction bug or stale test? |
| `gate/G4/undeclared-change` | T1 has undeclared symbol changes | complete declared_moves or split the transaction |
| `gate/G4/dangling-reference` | T2 removed a still-referenced symbol | shrink the deletion scope |
| `gate/G4/edge-mismatch` | T1 usage edges not preserved | missed a caller; verify via nav_symbol callees |
| `gate/G4/verify-broken` | code-atlas verify_changes BROKEN | structural breakage, redesign |
| `gate/G4/structural-change` | undeclared content change (difftastic) | revert the sneak edit or declare it honestly and change tier |
| `gate/G5/deadcode-increase` | T2/T4 introduced new dead code | delete the new dead paths |

## Trust modes (design doc v7 §3.3)

| Mode | Behavior | Deliverable |
|---|---|---|
| Supervised (default) | user approves each decision point (curate reject / T4 confirm / render material) | per-round verdicts + briefing material |
| Trust | zero interruptions across multi-round refactors | initial-vs-final comparison |

**Trust-mode flow**: `session_begin(note)` -> run the 8-step loop as usual (multiple rounds, uninterrupted) -> `session_end(note)` -> `session_report()` (count diff over four groups + side-by-side L0 graphs + verdict timeline within the window, written to the session directory report.html).
- The T4 safety gate does not open in trust mode: `tx_run` still always returns needs_confirmation - avoid T4 during the session, or leave T4 for manual handling after session_end
- Initial-vs-final comparison is the honest "side-by-side + counts" version; no graph diff (fid content is sensitive and always changes after refactors)

## Red lines

- **Never read raw source as cognition** (only fall back to query_facts when interfaces cannot answer)
- **Never release T4** (needs_confirmation means stop and hand over to a human)
- **Max 3 retries** per transaction
- **Never invent fids/qualified_names** (if the interfaces cannot find it, it does not exist)
- **Default output language is English** for everything you produce through vrp (curated artifacts, summaries, responsibilities), unless the user explicitly requests another language

## Related

- Curation loop (slices/structure) -> `skills/vrp-curation/` (curate_submit validation -> nav_slice drill-down -> drift remap)
- Reporting loop -> render tools: `render_verdicts()` (T4 briefing), `render_slice(slice_id)` (business-line Mermaid source), `render_report()` (full static report at `.vrp/report/` - the user-facing product: how-to-read guide, AI-curated overview, pathology checkup in plain language, deep data folded for agents)
- Transaction schema authority: `docs/契约/事务契约.md`; curated-artifact schema authority: `docs/契约/梳理工件契约.md`
