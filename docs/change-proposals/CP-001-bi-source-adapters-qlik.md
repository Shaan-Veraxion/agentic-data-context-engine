# CP-001: BI Source Adapters (Qlik first) and Two-Mode Reverse-SQL

| Field | Value |
|---|---|
| Change proposal | CP-001 |
| Target document | `docs/architecture.md`: "Agentic Dataset Context Engine: Architecture & Build Plan", Draft v0.2 (2026-10-08) |
| Proposed by | Shaan |
| Date | 2026-10-08 |
| Status | **Merged into `architecture.md` v0.3 (2026-10-08).** Open questions in section 8 are resolved in ADR-11. |
| Type | Additive: new ADR, 3 new agents, schema/contract additions, edits to existing agents, plan, risks and open decisions |
| Breaking changes | None to existing v1 contracts. Adds new contract `BI IR v1`, new fact kinds and new evidence kinds (minor version bump of the fact/event schemas). |

---

## 0. Instructions for the merging agent

- Apply each change in section 4 to the named section of `architecture.md`. Every change is tagged **ADD**, **MODIFY** or **REPLACE** and quotes the anchor it attaches to.
- Keep numbering consistent. New agents are #27–#29, and the agent count becomes **29 + orchestrator**. Update every place that says "26 agents" (title block, section 5 heading, ADR-4 heading, section 9 event-storm risk).
- Do not change the original Mode B behaviour of agents 21–26. This proposal adds a parallel Mode A path and shares the checker and reconciler.
- Where this proposal conflicts with a decision the architecture group has already made, surface the conflict as an open decision rather than silently overriding it.
- Sections 6 (acceptance criteria) and 7 (non-goals) are binding scope for this change.

---

## 1. Summary

Today the Reverse-SQL group assumes we only see a report's **output** (PDF or Excel) and must infer its SQL through bounded hypothesis search (QRE-style). Many client estates also have **BI applications** (Qlik Sense, QlikView, later Power BI and Tableau) where the report **logic itself** can be extracted programmatically:

- the load script, including embedded source SQL,
- the data model (tables and their association keys),
- measure and dimension expressions,
- a live engine that returns exact numbers for any filter combination.

This proposal adds:

1. **A BI source-adapter layer** (3 new agents) that extracts BI logic into a normalized, tool-neutral **BI IR** contract. Qlik is the first adapter; Power BI and Tableau plug in later without agent changes.
2. **Two-mode Reverse-SQL:**
   - **Mode A, translate.** Used when logic is available.
   - **Mode B, infer.** Used for output-only reports; this is the existing design.

   Query checker (25) and Reconciler (26) are shared by both modes.
3. **Cross-group reuse.** Extracted BI logic feeds Ingestion, Comparison (FK/IND), Definition and Lineage as evidenced facts, so it is not a Reverse-SQL-only feature.
4. **Engine-backed reconciliation.** The Reconciler validates Mode A SQL across many slices by applying selections in the BI engine, instead of needing several report uploads.

## 2. Motivation

| Problem in v0.2 | How this change addresses it |
|---|---|
| Reverse-SQL is the go-to-market headline, but Mode B (PDF → SQL search) is the hardest path and the most likely to slip. | Mode A (translation from real logic) is more deterministic and can reach end-to-end on real client apps by M2, which de-risks the headline. |
| Open decision #1: "Do the reports with known SQL actually exist?" The gold set is a blocking risk. | BI apps provide reports with known logic **and** queryable ground-truth outputs. They form a ready-made gold set for Mode A. |
| Join inference relies on statistics; the doc already notes query logs as "the strongest join signal" when available. | BI data models are hand-built join graphs that already run in production: high-value `foreign_key` evidence. |
| Lineage inference (19) has weak inputs for derived tables. | BI load scripts and QVD chains describe transformations explicitly, so lineage becomes an extraction problem rather than an inference problem. |
| Multi-period validation needs ≥2 report instances. | The BI engine can compute any slice on demand. |

## 3. Background: what a Qlik app contains (for agents unfamiliar with Qlik)

A Qlik app is not a set of saved queries. It has three layers:

1. **Load script (ETL).**
   - It contains `SQL SELECT …` statements sent to source databases, written in the source dialect.
   - Around those are Qlik-only transforms: preceding `LOAD`, `RESIDENT` loads, `JOIN`/`KEEP`, `CONCATENATE`, `ApplyMap` (lookup), `CROSSTABLE` (unpivot), `GENERIC` (pivot), `IntervalMatch` (range join), `Peek()`/`Previous()` (row-wise lookback), and `WHERE Exists()`.
   - It also includes `$(variable)` dollar-sign expansion, and `STORE`/`LOAD … FROM *.qvd`, Qlik's intermediate file format.
2. **Associative data model.** Tables are linked automatically on **same-named fields**; there are no declared join types. Synthetic keys and circular references can occur.
3. **Chart objects (hypercubes).**
   - Each chart is a set of dimensions plus measure expressions, e.g. `Sum({<Year={2025}, Region-={'APAC'}>} Sales)`.
   - Measures may be inline in the chart or reference reusable **master items**.
   - Set analysis `{<…>}` **overrides** the user's current selections on the named fields.

Key semantic difference from SQL: Qlik evaluates an aggregation **at the grain of the table holding the aggregated field**, so a 1:n association does not double-count. A naive SQL join followed by `SUM` does. This is the single most common silent error in Qlik-to-SQL translation.

Extraction surfaces:

| Product | Mechanism | Yields |
|---|---|---|
| Qlik Sense (client-managed or Cloud) | Engine API (JSON-RPC over WebSocket): `OpenDoc`, `GetScript`, `GetTablesAndKeys`, `GetAllInfos` → `GetProperties` per object, MeasureList/DimensionList session objects with `qData` paths for master items, `GetVariables`, `GetHyperCubeData` | Script, model, all expressions, variables, computed outputs |
| Qlik Cloud | Engine API, or `qlik-cli` (`qlik app script get`, measure/dimension listing). `qlik-cli` is Cloud-only. | Same |
| QlikView (.qvw) | `<App>-prj` folder: QlikView writes the script and per-object XML on save. Variable *values* are not exported; resolve them from the script or reload logs. | Script, layout/expressions (no data) |
| All | Script reload logs | Statements as executed, after variable expansion |

Prior art to study:
- Sigma's open-source `qlik-to-sigma` skill (an end-to-end discover → convert → parity pipeline).
- `countnazgul/qlik-script-parser` (a PEG.js work-in-progress grammar).
- The engine's `GetBaseBNF` method (the official script and chart-function grammar).
- Commercial accelerators: Axis Group Vortex, ZS Qlik→Power BI. Both target Fabric/Power BI, but generate SQL layers from load scripts.

---

## 4. Changes to `architecture.md`

All changes in this section were applied in `architecture.md` v0.3. Where they landed:

| CP section | Landed in v0.3 |
|---|---|
| 4.1 Title block | Title block |
| 4.2 Verdict row 6 | Section 1, row 6 |
| 4.3 Research row | Section 2 |
| 4.4 System architecture | Section 3 |
| 4.5 BI IR contract | ADR-1 #4 |
| 4.6 Graph schema, fact/evidence kinds, trust rule | ADR-3 |
| 4.7 ADR-4 heading | ADR-4 |
| 4.8 Routing | ADR-7 |
| 4.9 Privacy | ADR-8 |
| 4.10 Identity | ADR-9 |
| 4.11 ADR-11 | ADR-11 (with section 8 questions resolved) |
| 4.12 Agent catalog | Sections 5.1, 5.5, 5.7 |
| 4.13 Risks / construct tiers | Section 9 |
| 4.14 Squads | Section 7.1 |
| 4.15 Timeline | Section 7.2 |
| 4.16 Metrics, BI gold set | Section 7.3 |
| 4.17 Repo layout | Section 8 |
| 4.18 Open decisions | Section 10, items 6–9 |
| 5 Contracts/events | Section 5.8 |
| 7 Non-goals | Section 11 |

This copy records where each change landed rather than repeating the change text. The full content now lives in `architecture.md`, which is the source of truth. The original proposal text was supplied by Shaan on 2026-10-08.

## 5. Contract and event additions (consolidated)

| Type | Name | Notes |
|---|---|---|
| Contract | `bi_ir` v1 | ADR-1 #4 |
| Event | `upload.classified[bi_app]` | From Intake router |
| Event | `bi_ir.ready` | From BI extractor; payload = app_id, BI IR MinIO ref, reload_time |
| Event | `bi_app.reloaded` | From BI extractor poller; re-triggers extraction + reconciliation |
| Event | `bi_transform.ready` / `bi_metric_def.ready` | From translators |
| Fact kinds | `bi_model_link`, `bi_transform`, `bi_metric_def` | ADR-3 |
| Evidence kinds | `bi_script`, `bi_model`, `bi_engine_output` | ADR-3 |
| Tool allow-list change | Join planner (23) += `flag_conflict` | 4.12 |
| AgentSpec | 3 new specs (#27–29) | Group `reversesql`, sub-group `bi_adapters` (resolved in ADR-11) |

## 6. Acceptance criteria for this change

1. `architecture.md` reflects every change in section 4, and the agent count is consistent (29) everywhere.
2. BI IR v1 is specified as Pydantic + JSON Schema in `adce-core`, with fixtures in the mock KG.
3. ADR-11 is present and cross-referenced from ADR-1, ADR-3, ADR-8 and ADR-9.
4. The agent catalog has rows #27–#29 with all columns filled, and agents 21–26 carry their Mode A deltas.
5. Timeline, squads, risks, metrics, repo layout and open decisions include the additions above.
6. No change alters Mode B behaviour or existing v1 contract fields.

Status: 1, 3, 4, 5 and 6 are met in `architecture.md` v0.3. Criterion 2 is an implementation task for M0.

## 7. Non-goals

- Building Power BI or Tableau adapters in the POC. Only the IR and the adapter interface are designed for them.
- Recreating Qlik dashboards or layouts in another BI tool. The output is SQL + KG facts, not visuals.
- Writing back to, reloading or modifying any client BI app.
- Translating Section Access into executable SQL. It is captured as an RLS requirement only.
- Guaranteeing T3 constructs.

## 8. Open questions for the planning agent (resolved in ADR-11)

1. **Group placement.** Resolved: Reverse-SQL group with sub-group tag `bi_adapters` and its own deployable; promote to a 6th group when a Power BI or Tableau adapter is scheduled.
2. **`ExprAST` for Mode B.** Resolved: adopted for both modes.
3. **Global verifier and `bi_model_link`.** Resolved: only via FK/IND detector (13) `foreign_key` proposals.
