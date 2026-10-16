# Design review: the starting stage as built vs as designed

**Date:** 2026-10-16 · **Compared:** the code on `feature/real-scripts-and-llm-gateway` against
[architecture.md](architecture.md) v0.8, [architecture-alt-qlik-first.md](architecture-alt-qlik-first.md),
[CP-001](change-proposals/CP-001-bi-source-adapters-qlik.md), [implementation-plan.md](implementation-plan.md) and the five
proposals in [proposals/](proposals) (Overall; Starting Stage; Starting Stage Technical Design; Architecture and Workflow;
Phase 1 Technical Design, local-first).

Each finding says what the design asks for, what the code does, and where. Section 1 lists what we built differently from
the design or skipped; section 2 lists what was deferred on purpose (agreed, not a shortcut); section 3 is the plan.

---

## Summary

The deterministic core is solid: the Qlik reader, the rule translator, local validation V0 to V4, the fact log with its
outbox, kg-mcp, the gateway and the review loop all work, and the design's principles (rules first, no rows to models,
evidence on every fact, nothing dropped silently) hold in the code.

Where we fell short is everything that makes it an **agentic system with a supervisor**, and some correctness rules
the design calls essential:

1. **The orchestrator is a straight line**, not the LangGraph supervisor with subgraphs, per-table fan-out, repair and
   diagnosis loops, Ask interrupts and re-entry at the affected step that every design document describes.
2. **The query checker does not check Qlik semantics.** Grain and fan-out ("the single most common silent error",
   CP-001) and the other BI rules are missing.
3. **The contracts drifted.** AgentSpec, fact payload schemas, event schemas, fact kind names and the fact log columns
   differ from the frozen contracts, and there is no tenant isolation.
4. **kg-mcp is thin.** It has no validator, no trust gate, no conflicts, 11 of the 16 tools are missing, and agents do not
   read a context pack first.
5. **Review has no automatic acceptance.** No Correct mode, impact is not computed, and Ask does not block what depends on it.
6. **Some T2 constructs the stage commits to are not translated**: IntervalMatch, Peek/Previous, and loops over files
   resolved from the reload log.

---

## 1. Built differently from the design, or skipped

### 1.1 Orchestration (the biggest gap)

**Status: fixed in step 1 (2026-10-16)**; re-runs validate only the affected tables and those downstream (scope,
added the same day). See [code-guide.md](code-guide.md) section 4.

| Design | Code | Where |
|---|---|---|
| A **LangGraph supervisor** with one subgraph per group; first subgraph `qlik_pipeline` (Arch §5.6, TD §6.4) | One linear `StateGraph` of 8 nodes; no supervisor, no subgraph | [pipeline.py](../services/orchestrator/src/orchestrator/pipeline.py) |
| **Fan-out per Qlik table with `Send`**; tables translate, check and reconcile in parallel (A&W §5.4, §8.2) | All tables in one pass inside each agent | pipeline.py |
| **Bounded loops in the graph**: check → compose → repair at most twice; reconcile → diagnose → route to the likely cause (re-plan, re-translate, re-resolve) (A&W §8.2, §9) | Loops are Python inside agent 28 (`translate_with_ai`, `repair_with_ai`); LangGraph cannot see, checkpoint or resume them | [ai_loop.py](../packages/adce-reversesql/src/adce_reversesql/ai_loop.py) |
| **Ask uses `interrupt()`**: it pauses only the affected table and resumes on the answer (ADR-5, A&W §8.2) | No interrupts; Ask items are data only, and the table carries on with the unresolved variable | — |
| **Re-entry at the affected node, for the affected tables** on `fact.confirmed`, `dataset.changed`, new inputs (A&W §10) | Event consumer re-runs the **whole application** | [events.py](../services/orchestrator/src/orchestrator/events.py) |
| **Per-run budgets** enforced by the orchestrator and gateway | The gateway's default budget is infinite and nothing sets one | [gateway.py:139](../packages/adce-gateway/src/adce_gateway/gateway.py#L139) |
| The orchestrator is its own worker | Runs inside the API gateway process (noted as open in the implementation plan) | [app.py](../services/api-gateway/src/api_gateway/app.py) |

### 1.2 Agent responsibilities moved or not done

**Status: fixed in step 1** for agents 25 (repair, route `repair_sql`) and 26 (diagnosis from classified
mismatches; per-member grouped comparisons come with step 2). `classify_file`, `parse_export` and sending `kind:
other` statements to a model remain open.

| Design | Code |
|---|---|
| **Agent 25 repairs** SQL that fails a rule (route `repair_sql`, Opus, at most twice, then queue) | Declares `repair_sql`, never calls it ([agents.py](../services/agents-reversesql/src/agents_reversesql/agents.py)) |
| **Agent 26 diagnoses** mismatches from **grouped aggregate differences** that pinpoint the dimension members, and routes to the cause | Diagnosis runs inside agent 28 on whole-table comparisons; agent 26 declares the route but makes no calls; no grouped comparisons |
| Mismatch table (A&W §9): more rows → re-plan; fewer rows → extractor/Ask; totals differ → re-translate; recent rows only → tolerance; another dataset → equivalence | Every mismatch goes to one route (`diagnose_mismatch`), with no classification first |
| Routes `classify_file` (agent 1, ambiguous files) and `parse_export` (agent 21, PDF exports) | Prompts exist, no callers |
| `kind: other` statements go to the translator's AI route (A&W step 3) | They are counted and flagged only; never sent to a model |

### 1.3 Query checker: the BI semantic rules are missing

**Status: step 2 (2026-10-16)** added `fan_out` and `set_fields` (blocking), `distinct_across_tables`, `totals` and
`join_fields` (warnings) in `semantics.py`. Still open: grain facts (so pre-aggregated many-to-one joins pass),
dual-value and units rules, `EXPLAIN` in Dremio (PD).

The design names these rules as the reason agent 25 exists (Arch §5.5, TD §6.2, CP-001 §3). The code checks syntax, known
fields, determinism and inputs only ([check.py](../packages/adce-reversesql/src/adce_reversesql/check.py)).

| Rule | Status |
|---|---|
| (1) **Grain and fan-out**: aggregate at the field's own table grain before joining, or reject | Missing as a check. Chart SQL is built grain-aware ([charts.py](../packages/adce-reversesql/src/adce_reversesql/charts.py), DISTINCT bridge), but nothing independently verifies it, and there are no grain (key uniqueness) facts, even from local data |
| (2) Set-analysis override vs the default selection state | Missing |
| (3) TOTAL / Aggr scope | Missing (Aggr is reported as T3) |
| (4) Dual values and NULL behaviour | Missing as a check (handled in translation for `&`) |
| (5) DISTINCT across associated tables | Missing |
| (6) Joins exist; units match | Missing |
| V1 via Dremio `EXPLAIN` | Not possible until phase PD; sqlglot only |

### 1.4 Contracts drifted from the frozen versions (ADR-1)

| Contract | Design | Code |
|---|---|---|
| `AgentSpec` | name, version, **group**, kind, subscribes, **allowed_tools**, emits_fact_kinds, **hitl_modes**, model_route, **budget** | No group, allowed_tools, hitl_modes or budget ([agent.py](../packages/adce-core/src/adce_core/agent.py)) |
| Fact payload schemas | One schema per fact kind, validated on every write | `value` is a free dict; nothing validates it |
| Event schemas | Versioned schemas for every event | Event names are strings; no schemas |
| BI IR | `BIApp{tool, reload_time, model, master_measures, visuals, upstream_artifacts}`, statement kinds incl. `resident_load`, `preceding_load`, `applymap`, ...; `BIMeasure.construct_tier` | Different shape ([bi_ir.py](../packages/adce-core/src/adce_core/bi_ir.py)): no `tool`, no `reload_time`, no `upstream_artifacts`; statement kinds coarser |
| Fact kinds (ADR-3, CP-001) | `scope_link`, `bi_model_link`, `bi_metric_def`, `lineage` (DERIVED_FROM), `metric_mapping`, `report_structure` | We write `reads_source`, `associates`, `measure_translation`, `export_maps_to`, `report_export`; no `lineage` facts |
| Evidence kinds | `bi_script`, `bi_model`, `qvd_header`, `reload_log`, `view_definition`, `report_export`, `human` | `script_span`, `rule_check`, `artifact`, `bi_ir`, ... |
| `kg_port` | `supersede`, `invalidate`, `get_entity(as_of)`, `lineage(ref, direction, depth)`, `context_pack(task, token_budget)` | `register_entity, entity, propose_fact, decide, facts, search` ([ports.py](../packages/adce-core/src/adce_core/ports.py)) |
| `data_port` | `list_catalog, get_schema, get_view_definition, query(at=snapshot, limits)` | `run_query, has_table` |

### 1.5 Fact log and isolation

The design's `fact_log` (TD §11.1, ADR-Q5) has `tenant_id`, `scope_id`, `version`, `impact`, `valid_to`,
`invalidated_at`, `prompt_version`, `model`, `run_id`. The migration
([0001_fact_log.py](../migrations/alembic/versions/0001_fact_log.py)) has none of these.

- **No tenant or scope isolation.** The design requires each client's knowledge isolated in the store, failing closed
  (Overall §14, TD §18, ADR-12 R5). Nothing binds a tenant today.
- **No audit of model and prompt versions on facts** (Overall §14: "every proposal, decision, prompt version and model
  version"). AI-assisted facts do not record which model or prompt produced them.
- **No invalidation time**: point-in-time reads use `recorded_at` only.

### 1.6 Knowledge service (kg-mcp, ADR-2)

| Design | Code |
|---|---|
| Write path: **policy → validator → confidence×impact gate → commit + audit + event** | Policy and commit + event only |
| **Validator**: evidence present, entity exists, nothing contradicts a confirmed fact, re-run a data check for data claims | Missing |
| **Trust gate**: sends facts needing review to hitl-service | Missing; the review service polls facts instead |
| **Conflicts**: `CONFLICTS_WITH` + review item, never overwrite; `flag_conflict` | Missing entirely |
| **16 tools** | 11 tools, several of them batch variants; missing `get_neighbors`, `get_source_context`, `get_lineage`, `find_join_paths`, `add_context`, `link_entities`, `update_fact`, `flag_conflict`, `record_feedback`, read-only Cypher |
| **Context packs** ranked by trust and trimmed to a **token budget**, deterministic | `get_context_pack(key, limit)`: count limit, no budget |
| **Runtime loop: every agent reads a context pack first** (ADR-4) | No agent does; only model tools can fetch one |
| Per-agent tool allow-lists from Keycloak service-account tokens | The caller names itself |

### 1.7 Review (HITL)

| Design | Code |
|---|---|
| **Automatic acceptance** of validated, high-confidence, low-impact facts, reversible (Overall §12; target: auto-accepted later reversed < 5%) | Missing: every proposal waits for a person or stays proposed |
| **Correct**: a reviewer edits any output; the edit becomes a confirmed fact | Only "provide SQL" for untranslated statements; translated SQL cannot be corrected |
| **Impact** = number of dependent facts | Fixed constants per item kind ([service.py](../services/hitl-service/src/hitl_service/service.py)) |
| **Ask blocks the dependent statements** (marked `blocked`) | The run continues with the unresolved value |
| Gates: **confirm application scope**, **confirm cross-app QVD linkage** | Missing |
| Decisions reused as examples by every agent | Agent 28 only |
| Workbench: overview, review queue, lineage, SQL next to the script | The Next.js workbench ([apps/workbench](../apps/workbench)) has the three screens but none of what was added later (gaps, Dremio source mapping, model runs); those went into the internal test bench instead |

### 1.8 Qlik coverage the stage commits to

**Status: step 2** translated IntervalMatch (keyed too), Previous()/Peek() over the load's ORDER BY, Above() and
RangeSum(Above()) in charts, unrolled file-list loops from the reload log, recorded Section Access as an RLS
requirement, and applied QUALIFY. QlikView `-prj` folders are read (script, charts, variables; checked on synthetic
samples only). GENERIC, Hierarchy and recursive Peek stay T3.

| Construct | Commitment | Code |
|---|---|---|
| **IntervalMatch** | T2, committed | Classified and flagged T2; **no translation** (range join) |
| **Peek() / Previous()** | LAG over load order with an ordering key; otherwise T3 | **No rule**; goes to the model |
| **Loops over file lists** | Resolved from the reload log where it shows the iterations (TD §7.4, ADR-Q3) | Modelled once and flagged `loop_not_unrolled`. This is the `$(vLoadfile)` problem in the raroc script |
| GENERIC | T3 | Flagged, not translated (acceptable) |
| Section Access | Recorded as an RLS requirement on `scope_link` | Classified only; no requirement recorded |
| QUALIFY | Applied to field names | Recorded, not applied (implementation plan P1.3) |
| QlikView `-prj` XML | Script, objects, expressions, master items | Not read ([frontend.py](../packages/adce-qlik/src/adce_qlik/frontend.py) says so) |
| Above / RangeSum(Above()) in charts | T2 | Not translated |

### 1.9 Source resolution and QVD chains

- **Cross-application QVD chains are not followed.** The design inlines the producer application's CTE chain so the
  consumer's SQL reads only Dremio datasets, and names a missing producer in the pack (TD §9.3, ADR-Q4). The code
  handles QVDs stored and read **within one application**; the QVD header's `CreatorDoc` is recorded but never used to
  link applications, and there is no multi-application scope.
- The **connection mapping** (Seed mode, drafted from the scripts in week 1) does not exist as such; mappings are entered
  one source at a time (`resolves_to`), and from 2026-10-16 in the bench's Dremio sources panel.

### 1.10 Validation

**Status: step 2** added the V2 column check, null counts in V4, the drift tolerance with its labels, and a result
cache. Still open: grouped comparisons per dimension member, hash of a sorted key sample.

| Design | Code |
|---|---|
| V2 checks the **schema equals the BI IR table definition** (columns and types) | V2 = the query runs |
| **Drift tolerance** (default 0.5%), labelled "within tolerance (drift)", never "exact" | Exact comparison only; no tolerance labels |
| **Grouped comparisons** identify the dimension members that differ | Per-table and per-field aggregates only |
| Results **cached** by SQL fingerprint, dataset version and snapshot | No cache |
| V4 includes null counts, sums and a hash of a sorted key sample | Count, distinct, min/max, value shapes and fingerprints; no null counts or key-sample hash |

### 1.11 Migration pack (ADR-Q7, TD §17)

| Design | Code ([agents.py](../services/agents-reversesql/src/agents_reversesql/agents.py) `Publisher`) |
|---|---|
| `<app>__<reload_time>__pack-v<N>/` | `apps/<app>/packs/<run_id>/` |
| `sql/tables/*.sql`, `sql/measures/*.sql` | `sql/*.sql`, `sql/charts/*.sql` |
| `lineage/lineage.json` + `lineage.md` | `lineage.json` only |
| `validation/report.md` + `report.xlsx` with tolerances and drift reasons | `validation.md` |
| `review/untranslated.csv`, `decisions.csv`, `open-items.md` | `review.md` |
| Manifest: inputs, **software and model versions**, run id | Inputs and software version; no model or prompt versions |
| "Regenerates identically" verified | Not tested |

### 1.12 Gateway, evaluation and operations

| Design | Code |
|---|---|
| Claude on Vertex by default | No Vertex project yet (Q-04): development used Groq and, locally, the Claude Code bridge. Our model results are not on the target models |
| Gold set: 2 to 3 **real** applications with hand-verified SQL; every prompt, model or rule change runs it in CI; ranges over repeated runs | Synthetic cases only in the repo (the real ones are private); **CI runs pytest but not the bench or the gold-set evaluation**; results not in Langfuse |
| Langfuse on every AI call; OpenTelemetry across services; diagnostics | Langfuse optional and only wired where the gateway is built from settings; no OpenTelemetry |
| Model gateway as a service; events over LISTEN/NOTIFY, then Redis Streams | Gateway is a library; the event consumer polls |

### 1.13 Shortcuts from the recent sessions

- **The Claude Code bridge** was built as a local experiment and stays off the feature branch.
- **UI work went into the internal test bench**, not the product workbench (see 1.7).
- **Diagnosis ran on missing local data** and rewrote correct SQL. Fixed on 2026-10-16 (`MissingData`).
- **The code guide left out the workbench**. Fixed in [code-guide.md](code-guide.md).

---

## 2. Deferred on purpose (agreed, not shortcuts)

These wait for inputs or phases that have not started:

| Item | Waits for |
|---|---|
| Catalog crawl (agents 2, 5), source resolution to datasets (4-lite), grain in Dremio (9-lite), copies (30-lite), execution and time travel in Dremio, `EXPLAIN` | Phase PD: Dremio access (Q-05, Q-06) |
| Brahmastra as the knowledge engine (Graphiti on Neo4j for now) | Q-02 |
| Claude on Vertex AI | Q-04 |
| Mode A2 and V6 (Qlik engine), Mode B (inference from outputs) | Server access; phase P3 |
| Presidio masking | Agents that send samples (later stages) |
| Profiling, comparison, estate, definition agents; data model designer | Platform stages |

Some items in section 1 can be built now even though Dremio is missing: grain checks on QVD and extract data, the drift
tolerance, the connection mapping, cross-application QVD chains, all of 1.1, 1.3 to 1.8, 1.11 and the CI evaluation.

---

## 3. What to do, in order

Each step leaves the system working and tested; the bench and the gold set run after every step.

### Step 1. The orchestrator as designed (agentic)

**Done 2026-10-16.** Supervisor graph with a translate subgraph (one node per step), `Send` fan-out for check and
reconcile, repair by agent 25 (`repair_sql` v2) and diagnosis by agent 26 (mismatch classes, back through
synthesize), an LLM supervisor (`plan_next`) with rules as fallback, `interrupt()` for owner questions with an
answers endpoint, re-entry at extract/resolve/translate, per-run budgets (`RunGateway`), the orchestrator worker
(queue table `run_request`, Helm `worker`), and the run timeline on the bench.
- Supervisor graph over a `qlik_pipeline` subgraph; `Send` fan-out per table after resolve.
- A translation subgraph per untranslated statement: memory → propose (LLM) → verify (code) → retry / accept / give up,
  replacing the Python loop in `ai_loop.py`.
- Repair goes back to **agent 25** (route `repair_sql`, at most twice). Diagnosis goes back to **agent 26**: grouped
  comparisons first, then the mismatch is classified and routed to its cause (re-plan, re-translate, re-resolve, Ask, or
  tolerance), once per new piece of evidence.
- A supervisor LLM node decides the next action from the run state, within a step limit and a per-run budget.
- `interrupt()` for Ask, which pauses that table only. Re-entry at the affected node for the affected tables.
- The orchestrator as its own worker. A run timeline view showing nodes, decisions and model calls.

### Step 2. Correctness rules

**Done 2026-10-16** (see the status notes in 1.3, 1.8 and 1.10 for what stays open).
- Query-checker rules (1) to (6), with grain facts computed from QVD and extract data now, and from Dremio in PD.
- V2 schema check; drift tolerance labels; null counts and key-sample hash in V4; result cache.
- IntervalMatch, Peek/Previous with an ordering key, Above/RangeSum, file-list loops from reload-log iterations,
  Section Access as an RLS requirement, QUALIFY.

### Step 3. Contracts and knowledge
- AgentSpec fields (group, allowed tools, HITL modes, budget); fact payload and event schemas validated on write; fact
  and evidence kinds renamed to the design's (with a migration of existing facts).
- Fact log migration: tenant, scope, version, impact, valid_to, invalidated_at, prompt version, model, run id; tenant
  binding that fails closed.
- kg-mcp: validator, trust gate, conflicts, the missing tools, token-budgeted context packs; agents read their context
  pack first.

### Step 4. Review that lets the system decide
- Automatic acceptance policy: validated, high-confidence, low-impact facts are accepted by an auto-review agent,
  recorded as its decision and reversible. The reversal rate is tracked against the < 5% target.
- Correct mode on any output; impact from dependent facts; Ask blocks its dependents; scope and QVD-linkage gates.
- The workbench gets what the bench has (gaps, Dremio source mapping, SQL next to the script).

### Step 5. Pack, evaluation, operations
- Pack layout as in ADR-Q7, with model and prompt versions in the manifest, and a test that it regenerates identically.
- CI runs the bench and a replayed gold-set evaluation; results into Langfuse; Langfuse and OpenTelemetry wired in
  every service.
- Cross-application QVD chains with a multi-application scope; QlikView `-prj` reading; PDF exports through
  `parse_export`; agents 21, 22, 23, 29 inside the graph.

Steps 1 and 2 matter most: step 1 is what makes the system agentic, and step 2 is what stops wrong numbers. They can
run in parallel because the checker rules become nodes in the new graph.
