---
name: vrp-curation
description: vrp curation production guide - how the AI produces slices/structure artifacts, submits them through curate validation, handles rejection codes, and re-curates drifted artifacts. Use when curating business lines (slices), the overall structure (module partition + layer DAG), handling curate receipts or rejection codes, running enforce over layer rules, or re-curating artifacts that drifted after transactions landed - also when the user asks to map business flows, partition the codebase into modules, define layering rules, or says "carve this repo up" / "trace the checkout flow".
---

# vrp-curation: curation production guide

Operation protocol for the VRP main-flow curation loop. Prerequisite reading: `docs/契约/梳理工件契约.md` (schema authority).

> Zero-agent alternative: `vrp curate-auto <repo>` runs the same six-step flow with a configured LLM (`.vrp/config.json`, contract §6) - this skill documents the agent path over MCP/CLI.

## Purpose

Persist the AI's understanding of the codebase as **verifiable data**:
- **slices** (lines): business threads - step sequences, each step anchored to a real symbol, adjacent steps joined by real call edges
- **structure** (surface): overall shape - module partition + layer DAG + architecture rules (the raw material for S7 enforcement)

Validation is **deterministic** (machine-checked; everything passes or nothing ingests); **value judgments belong to the user** (rejections are logged). You (the AI) produce, curate validates, the user decides.

**Default output language is English** for curated content (module `name`/`responsibility`, slice `name`/`summary`, act names), unless the user explicitly requests another language - the user-facing report renders this content verbatim.

## Production flow (six steps)

```
1. Gather      dossier / vrp_query(search) to locate the target business
2. Verify      vrp_query(callers) / vrp_query(callees) for genuinely adjacent
               symbols (no skipping ahead, no invented fids)
3. Produce     write slices.json / structure.json per the contract schema
4. Submit      curate_submit(kind, payload_json), or drop into .vrp/ai/
               and run `vrp curate <repo>`
5. Read receipt accepted -> done; rejected -> fix per errors[].code,
               resubmit the whole file
6. Drill down  vrp_query(slice, target=slice_id) to double-check the ingested result
```

After a structure is ingested, run `enforce()` (S7): broken layer contracts become P9 findings - report them to the user and fold them into refactor targets.

## Worked example: one slice, end to end

Goal: persist the checkout flow as a slice (three steps, two acts).

1. Gather: `vrp_query(search, "checkout")` -> fid `S101` (`POST /checkout` handler), plus nearby order/persist symbols
2. Verify adjacency: `vrp_query(callers, "S101")` and `vrp_query(callees, "S101")` confirm real edges `S101 -> S205` (CALLS) and `S205 -> S312` (CALLS) - no gaps, nothing invented
3. Produce `slices.json` per the contract:

```json
[{
  "slice_id": "checkout-flow",
  "name": "checkout flow",
  "summary": "POST /checkout validates the cart and persists the order",
  "steps": [
    {"fid": "<S101 sha>", "role": "entry", "note": "POST /checkout route"},
    {"fid": "<S205 sha>", "role": "process", "note": "cart validation"},
    {"fid": "<S312 sha>", "role": "persist", "note": "order persistence"}
  ],
  "acts": [
    {"name": "entry", "from_step": 0, "to_step": 1},
    {"name": "persist", "from_step": 2, "to_step": 2}
  ]
}]
```

4. `curate_submit("slices", payload_json)` -> receipt `accepted: ["checkout-flow"]`
5. `vrp_query(slice, target="checkout-flow")` -> three steps, two acts, edges present - matches what was written

If step 4 had returned `SLICE_MISSING_EDGE` on step 1->2, the edge was wishful thinking: re-verify with `vrp_query(callers/callees)`, shorten the slice scope, resubmit the whole file.

## Validation red lines (common rejection causes)

| Mistake | Receipt code |
|---|---|
| no real edge between adjacent steps (skipping/wishful thinking) | `SLICE_MISSING_EDGE` |
| stale fid (transactions landed, no fresh vrp_query(search)) | `SLICE_UNKNOWN_FID` |
| same symbol assigned to two modules | `STRUCT_FID_OVERLAP` |
| layer dependency cycle | `STRUCT_LAYER_CYCLE` |
| module.layer / rule references a layer not in layers | `STRUCT_UNKNOWN_LAYER` / `STRUCT_RULE_LAYER` |
| acts do not join seamlessly / do not cover all steps | `SLICE_ACT_BOUNDS` |

## Rules

1. **fids come only from vrp_query (nav-family types)** - invented fids never pass existence validation
2. **Adjacent steps need a real edge**: `CALLS`/`CALL_REFERENCE`/`HTTP_CALLS` in either direction; verify with vrp_query(callers)/vrp_query(callees) before writing steps
3. **Atomicity**: one bad entry rejects the whole file - fix everything, then resubmit the file
4. **Idempotent**: resubmitting the same slice_id overwrites; structure is single-row overwrite - iterate boldly
5. **Honesty**: if a business line cannot be drawn with real edges, shorten its scope; never pad steps to force a shape
6. A user rejection (status=rejected) is final - never resubmit the same id against the user's decision; re-curating under a new slice_id requires explicit user consent

## Drift and remap

Transactions invalidate fids. `vrp_query(drift)` (read-only) lists per-artifact `fresh`/`missing_fids`/`missing_edges` plus relocation candidates from the fid ledger:
- Unique qualified_name candidate -> direct `remap` (old fid -> new fid): apply it, re-curate, `curate_submit` again (full re-validation, no bypass)
- Qualified_name gone (renamed/deleted) -> same-file candidates are listed (capped) for judgment
- Artifacts marked `stale` are re-validation failures - re-curate before relying on them

## Rejection code quick reference

slices: `SLICE_SCHEMA` / `SLICE_EMPTY_NAME` / `SLICE_EMPTY_SUMMARY` / `SLICE_UNKNOWN_FID` / `SLICE_MISSING_EDGE` / `SLICE_ACT_BOUNDS`
structure: `STRUCT_SCHEMA` / `STRUCT_EMPTY_NAME` / `STRUCT_EMPTY_RESPONSIBILITY` / `STRUCT_UNKNOWN_FID` / `STRUCT_FID_OVERLAP` / `STRUCT_LAYER_CYCLE` / `STRUCT_UNKNOWN_LAYER` / `STRUCT_RULE_LAYER`
file level: `FILE_NOT_JSON` / `FILE_WRONG_SHAPE`

## Tool surface (MCP 0.2.0: ten tools - single read tool vrp_query + write/execution verbs)

| Tool | Use |
|---|---|
| `vrp_query(query_type, target, options_json)` | single read tool, 17 types: search/architecture/module/symbol/facts/callers/callees/slice/path/seams/coverage/impact/drift/curate_slices/curate_structure/curate_receipt/tx_verdicts (target carries the primary key; options_json carries secondary params, e.g. `{"limit":50}`) |
| `dossier(task, budget, skip_external)` | S5 intake briefing over facts.db (task-typed budgets T1-T4; default skip_external=True = fast, deterministic) |
| `curate_submit(kind, payload_json)` | submit for validation and ingest (kind=slices/structure) |
| `curate_reject(kind, target_id, note)` | relay a user rejection (conservative direction, agent may relay) |
| `enforce()` | S7 architecture enforcement over the ingested structure (P9 findings) |
| CLI `vrp curate <repo>` / `--reject <id> --note ...` | manual entry points |

Output budget: tool outputs are capped at 24K chars with deterministic JSON-aware shrinking (a `_truncated` note records what was cut - narrow the query or pass a lower `limit` option instead of retrying blindly).

## Related

- Restructuring loop (transactions) -> `skills/vrp-loop/`
- Read-first scenarios (understand/rot history) -> `skills/vrp-archaeology/`
- Evidence-based review -> `skills/vrp-review/`
