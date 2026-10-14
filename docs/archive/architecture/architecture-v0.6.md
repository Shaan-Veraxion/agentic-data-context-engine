# Agentic Dataset Context Engine: Architecture & Build Plan

Status: Draft v0.6 (2026-10-08). Open questions live in [`open-questions.md`](open-questions.md).
**Full proposal scope:** 29 agents in 5 groups plus an orchestrator (26 from the proposal + 3 BI source-adapter agents, CP-001), all six stores, Keycloak, Langfuse and every export format.
Based on the proposal "Agentic Dataset Context Engine - Proposal" (Sept 2026), with every key claim checked against current sources.
Audience: the engineering squads building the POC, plus the sponsor.

**Change log**
| Version | Change |
|---|---|
| v0.1 | First analysis of the proposal (reduced scope, superseded) |
| v0.2 | Full proposal scope restored; parallel squads |
| v0.3 | **CP-001 merged** (BI source adapters, Qlik first; two-mode Reverse-SQL). **ADR-12:** reuse the existing in-house KG engine (Brahmastra) instead of building the KG from scratch. |
| v0.4 | ADR-12 fit assessment against the latest Brahmastra docs (R1–R15 status), the **fact-log-as-source** integration design, and gateway memoization. BI scope confirmed: **QlikView + Qlik Sense** now, more BI tools later. |
| v0.5 | **ADR-12 revised:** Brahmastra is a starting point the team can change freely, so it is **evolved in place into the ADCE KG core** (imported into the monorepo, core/apps split), with no wrapper layer. R1–R15 become the engine's acceptance suite. |
| v0.6 | **Qlik scripts first** (server access later): Mode A split into A1 (scripts) and A2 (engine-backed). **ADR-13:** Dremio is the primary data source, with pushdown compute and Dremio SQL as the target dialect. **ADR-14:** models through Google Cloud (Vertex AI). **ADR-15:** local → staging → production, shippable to clients. Open questions moved to `open-questions.md`. |

---

## 1. Verdict on the proposal

The problem is real, the positioning ("feed the catalog, don't replace it") makes sense, and the core technical bet holds up: **statistics generate candidates and the LLM adjudicates and explains**. The research the proposal cites exists and backs this up.

Scope stays as proposed (plus CP-001). What changes is **how** it gets built and a few technical corrections:

| # | Issue | Why it matters | Fix |
|---|---|---|---|
| 1 | **Phases go one agent group at a time (horizontal)** | Nothing runs end to end until week 12, and squads would wait on each other. | All agent groups are built **in parallel** by dedicated squads against frozen contracts and a mock KG, with integration milestones every two weeks (section 7). |
| 2 | **Reverse-SQL, the go-to-market headline, is built last (weeks 11-12)** | If it slips, the POC fails to prove the claim we sell on. | Reverse-SQL squad starts in week 1. Mode A (BI translate) reaches end-to-end by M2, and Mode B (infer) also has its first end-to-end by M2. |
| 3 | **"Low temperature" as the determinism mitigation** | Current models reject `temperature`/`top_p` (HTTP 400 on Opus 5.5, Sonnet 5.5 and Sonnet 5). | Use structured outputs, `strict` tools, pinned model and prompt versions, per-route `effort`, and a gold-set regression run. |
| 4 | **"Redis not needed in POC"** | Self-hosted Langfuse v3 requires Postgres, **ClickHouse, Redis and S3/MinIO** anyway. | Run Redis and MinIO from day 1; Redis Streams is the event bus (ADR-6). |
| 5 | **Key-accuracy target ">90%" for all keys** | Published hybrid join inference gets 78-100% precision on well-structured schemas and notably worse on poorly normalized ones. | Separate targets: PK F1 ≥ 90%, FK F1 ≥ 80% within a source, cross-source matches reported per dataset with a normalization grade. |
| 6 | **Reverse-SQL only handles output-only reports (inference)** | The hardest path carries the headline alone, and BI estates whose logic could simply be translated are ignored. | Add a BI source-adapter layer and a **Mode A (translate)** path. Mode A shares the checker and reconciler with Mode B (ADR-11, CP-001). |
| 7 | **The KG is planned as a from-scratch build** | We already have an in-house KG engine (Brahmastra). Rebuilding it wastes the Platform squad's first weeks. | Evolve it in place into the ADCE KG core (it can be changed freely), with R1–R15 as its acceptance suite (ADR-12). |

Smaller corrections:
- **Models.** Sonnet 5 → **Sonnet 5.5** (same $2/$10 price). Opus 5.5 is $4/$20 and confirmed. On Opus 5.5 and Sonnet 5.5, thinking cannot be disabled and forced `tool_choice` returns a 400, so we use structured outputs instead. Opus 5.5's default effort is `medium`, so we set effort explicitly per route. The proposal's cost-table arithmetic is correct.
- **Neo4j "5".** Neo4j moved to calendar versioning (2025.01+). Pin **5.26 LTS** or a current 2026.x release. Community Edition has a single user database, so tenant isolation is a property or label. Multi-database per tenant needs Enterprise.
- **Graphiti.** The bi-temporal model is a good idea to borrow, but Graphiti itself is pre-1.0 and spends an LLM call on every write. Its ideas feed the R3 requirement on our KG engine (ADR-12).
- **Kuzu** (an embedded graph DB) was archived in Oct 2025. Do not adopt it.
- **ZDR.** Opus 5.5 and Sonnet 5.5 are fine under zero data retention. Fable-tier models need 30-day retention, so keep them out of the client-deployment plan.

---

## 2. Research check

| Claim in proposal | Verified | Design consequence |
|---|---|---|
| DBAutoDoc: statistics + iterative LLM refinement; human ground truth separate from seed context | ✅ arXiv 2603.23050. **The deterministic pipeline adds +23 F1 points over LLM-only FK detection.** | Key/FK agents are hybrid: deterministic gates first, LLM adjudication second. Two knowledge tiers: *confirmed* (immutable) and *seed* (soft). |
| LLM-FK: 4 agents (profiler/interpreter/refiner/verifier), F1 > 93% | ✅ arXiv 2603.07278, Findings of ACL 2026 | Unique-key-driven candidate pruning in the FK/IND detector; the global verifier enforces schema-wide consistency. |
| Magneto: cheap retrieval → LLM rerank | ✅ PVLDB 18(8); code at VIDA-NYU/magneto-matcher | Candidate finder + match reranker. |
| Join inference at scale | ✅ arXiv 2603.04176. PK candidates → LLM adjudication → IND detection → LLM adjudication; query history refines results. | Same pipeline shape. **Query logs and BI data models are the strongest join signals**; ingest both when available. |
| Query reverse engineering [5][6] | ✅ established research area (QRE, FastQRE, SQUARES) | Mode B: hypothesize, then bounded variant search, then execute-and-diff over several periods. |
| data.world KG accuracy studies [7][8] | ✅ | The query checker repairs against the ontology; "not resolved" is an allowed outcome. |
| BI logic (Qlik) can be extracted programmatically and converted to SQL (CP-001) | ✅ Qlik Engine API methods (`GetScript`, `GetTablesAndKeys`, `GetProperties`, `GetHyperCubeData`, `GetBaseBNF`); QlikView `-prj` export. Prior art cited by CP-001 (Sigma `qlik-to-sigma`, `countnazgul/qlik-script-parser`, Axis Vortex, ZS) still needs a hands-on review by the Mode A sub-team in M0. | BI adapters emit a normalized BI IR. Mode A translates instead of searching. The BI engine supplies ground truth for reconciliation. |
| Graphiti bi-temporal memory | ✅ (Thoughtworks Radar "Trial", Apr 2026; pre-1.0) | Copy the model, not the dependency. |
| LangGraph interrupts + Postgres checkpointer | ✅ with caveats: interrupts during parallel fan-out are poorly defined, and every step writes a checkpoint blob | HITL Gate/Queue are stored as data; only Ask uses `interrupt()` (ADR-5). |
| FastMCP | ✅ v3.x (Prefect), adds OAuth, OpenTelemetry and server composition | The `kg-mcp` tool surface is built on FastMCP 3.x (ADR-2, ADR-12). |

---

## 3. System architecture

Six layers, as in proposal Figure 1, plus the CP-001 additions:

| Layer | Components | Deployable services |
|---|---|---|
| Interfaces | Review workbench, API gateway, external MCP + catalog exports | `workbench` (Next.js), `api-gateway` (FastAPI), Keycloak |
| Ingestion agents | Intake router, Schema parser, Doc extractor, Entity linker, Scope registrar, **BI extractor (CP-001)** + HITL | `agents-ingestion`, **`agents-bi-adapters`** |
| Control | Model gateway, Orchestrator (LangGraph supervisor), HITL service | `model-gateway`, `orchestrator`, `hitl-service` |
| Agent groups | Profiling, Comparison, Definition, **Reverse-SQL (Mode A translate + Mode B infer)**, including **BI script translator** and **BI expression translator** (each group has a HITL gate) | `agents-profiling`, `agents-comparison`, `agents-definition`, `agents-reversesql`, `agents-bi-adapters` |
| Knowledge access | KG MCP server: typed tools, policy engine, validator, bi-temporal provenance, context assembler, read-only Cypher, **on top of the existing KG engine (ADR-12)** | `kg-mcp` (+ the KG engine as a library or service, see ADR-12) |
| Storage | Neo4j, Postgres, MinIO, DuckDB, Redis, Langfuse | managed containers |
| **Data sources** | **Dremio** (primary: tables, views, view definitions, wikis, query history) and file uploads, behind `data_port` (ADR-13) | read-only Dremio service account; local Dremio OSS for dev |
| **Model endpoint** | **Google Cloud Vertex AI** (Claude by default; Gemini optional), reached only through `model-gateway` (ADR-14) | dev / staging / client Vertex projects |

- **One monorepo, multiple deployables.** Each agent group is a separate worker service owned by one squad. All services share the `adce-core` library (agent contract, fact schemas, event schemas, BI IR, KG engine port, clients).
- Every agent connects two ways to `kg-mcp` over MCP streamable HTTP. Agents compute on data through DuckDB (reading Parquet from MinIO) and **record knowledge only through `kg-mcp`**.
- `agents-bi-adapters` is its own worker because it holds BI-server credentials and has different scaling needs (long-lived WebSocket sessions).
- **BI adapters connect to BI servers read-only** through a dedicated connector service account. BI-engine output (hypercube data) is treated as raw data: it goes to MinIO as Parquet and passes the ADR-8 privacy boundary before any LLM sees it.

---

## 4. Architecture decisions (ADRs)

### ADR-1: Contracts first so squads can work in parallel
In weeks 1-2 the Platform squad freezes six versioned contracts, all published as Pydantic models and JSON Schema in `adce-core`:
1. **Agent contract (`AgentSpec`).** Name, version, group, kind (LLM / hybrid / deterministic), subscribed event types, allowed KG tools, emitted fact kinds, HITL mode, model route, and budget.
2. **Fact payload schemas**, one per fact kind (ADR-3).
3. **Event schemas** (`upload.received`, `profile.ready`, `fact.proposed`, `fact.confirmed`, `conflict.flagged`, `bi_ir.ready`, …; full list in section 5.8).
4. **BI IR (`bi_ir` v1).** A tool-neutral representation of BI report logic, produced by every BI adapter and consumed by the translators, Metric mapper, Join planner and Reconciler (ADR-11). The mock KG ships BI IR fixtures (at least two Qlik apps covering tiers T1–T2), so the Reverse-SQL squad can build Mode A from week 1.
5. **KG engine port (`kg_port` v1).** The interface `kg-mcp` uses to reach the KG core (ADR-12). Both `kg-engine` (evolved Brahmastra) and the mock KG implement it, so squads build against the mock while the engine is refactored.
6. **Data-source port (`data_port` v1).** Catalog listing, schemas, view definitions, bounded pushdown queries, Arrow samples, query history (ADR-13). Implementations: `DremioSource`, `FileSource`, plus a fixture source for tests.

Platform also ships a **mock KG MCP server** with fixtures, so every squad builds and tests against it from week 1. Contract changes go through RFCs reviewed by the architecture group.

BI IR v1 sketch (indicative; the Platform squad finalizes it):

```python
class BIApp(BaseModel):
    app_id: str; tool: Literal["qlik_sense", "qlikview", "powerbi", "tableau"]
    name: str; reload_time: datetime | None          # snapshot timestamp, pins reconciliation
    variables: list[BIVariable]                      # name, definition, resolved_value (from reload log), source
    script: BIScript | None                          # ordered statements
    model: BIModel                                   # tables, fields, association keys, synthetic keys
    master_measures: list[BIMeasure]; master_dimensions: list[BIDimension]
    visuals: list[BIVisual]
    upstream_artifacts: list[ArtifactRef]            # QVDs / other apps this app reads

class BIScriptStatement(BaseModel):
    seq: int; kind: Literal["sql_select", "load", "resident_load", "preceding_load", "join", "keep",
                            "concatenate", "mapping_load", "applymap", "crosstable", "generic",
                            "intervalmatch", "store", "drop", "rename", "let_set", "control", "other"]
    target_table: str | None; source: SourceRef | None   # db.schema.table | qvd path | resident table | inline
    raw_text: str; source_dialect: str | None            # for sql_select
    fields: list[BIFieldExpr]                            # output field name ← expression AST
    where: ExprAST | None; group_by: list[str] | None
    span: TextSpan                                       # for evidence

class BIMeasure(BaseModel):
    id: str; label: str; description: str | None
    expression_raw: str; expression_ast: ExprAST | None  # set modifiers, TOTAL, Aggr, Above as typed nodes
    construct_tier: Literal["T1", "T2", "T3"]            # see section 9

class BIVisual(BaseModel):
    id: str; sheet: str; type: str; title: str | None
    dimensions: list[DimRef]; measures: list[MeasureRef]  # inline or master-item references
    sort: list[SortSpec]; suppress_nulls: bool; totals_mode: str | None
```

`ExprAST` is the **shared intermediate form for both Reverse-SQL modes**: Mode A translators and the Mode B synthesizer both target it before SQL generation, so Query-checker rules apply once.

### ADR-2: The KG MCP server is the only writer of knowledge
- `kg-mcp` is a FastMCP 3.x app on the `kg-engine` core (evolved Brahmastra) through `kg_port` (ADR-12). Each write runs **policy → validator → confidence/impact gate → commit + audit + event**, as in proposal Fig. 2.
- **Policy engine:** per-agent tool allow-lists come from `AgentSpec` and are enforced through Keycloak service-account tokens; scope and tenant isolation are enforced on every query; **only `hitl-service` can call `record_feedback`** and promote a fact to `confirmed`.
- **Validator:** checks that evidence is present, the entity exists, and nothing contradicts a confirmed fact, then re-runs a DuckDB check for data claims.
- **Context assembler:** subgraph + vector hits → filter by domain/trust → rank and trim to a token budget, with highest-trust facts first. Packs are deterministic, so prompt caching works.
- **Read-only Cypher:** only for the join planner and Q&A; parsed allow-list, row and time limits.
- **Tool catalog (16 tools, as in proposal section 7.2):**
  - Read: `search_context`, `get_context_pack`, `get_entity`, `get_neighbors`, `get_source_context`, `get_lineage`, `find_join_paths`, `get_feedback_history`
  - Write: `register_entity`, `add_context`, `link_entities`, `propose_fact`, `update_fact`, `flag_conflict`
  - Feedback: `record_feedback` (HITL service only)
  - Events: `subscribe_events` (orchestrator only)
  - Plus `run_readonly_cypher`, restricted to the join planner and Q&A.

### ADR-3: Facts are reified nodes; "current view" edges are materialized
Every fact must carry trust, evidence, version, validity and feedback. That metadata does not fit on a plain edge, so:

```
(:Tenant)-[:OWNS]->(:Source)-[:HAS_SCHEMA]->(:Schema)-[:HAS_DATASET]->(:Dataset)-[:HAS_COLUMN]->(:Column)
(:Document)-[:DESCRIBES]->(any entity)        (:Upload)-[:PRODUCED]->(:Dataset|:Document|:Report)
(:Term)-[:IN_DOMAIN]->(:Domain)               (:Report)-[:HAS_METRIC]->(:Metric)

// BI (CP-001)
(:Source {kind:"bi_server"})-[:HAS_APP]->(:BIApp {tool, app_id, reload_time})
(:BIApp)-[:HAS_VISUAL]->(:Visual)-[:SHOWS]->(:Metric)   // Visual ≈ Report section; reuse :Report where a visual is a report
(:BIApp)-[:READS]->(:Dataset)                           // source tables and QVDs
(:BIApp)-[:WRITES]->(:Dataset {kind:"qvd"})             // QVD chains: extractor app → QVD → transform app → dashboard

(:Fact {id, kind, payload_json, trust: confirmed|documented|validated|inferred,
        confidence, impact, valid_from, valid_to,      // when true in the data
        recorded_at, invalidated_at,                   // when we believed it
        produced_by: "agent@version", prompt_version, model, run_id, tenant_id})
  -[:ABOUT {role}]->(entity)
  -[:EVIDENCED_BY]->(:Evidence {kind, ref})
  -[:SUPERSEDES]->(:Fact)        -[:CONFLICTS_WITH]->(:Fact)
  -[:HAS_FEEDBACK]->(:Feedback {decision, edit, reason, reviewer, at})

Materialized for traversal (maintained by kg-mcp from current best-trust facts):
(:Column)-[:JOINS_TO {fact_id, trust, cardinality}]->(:Column)
(:Column)-[:SAME_AS|DERIVED_FROM {fact_id, trust}]->(:Column)
(:Column)-[:DEFINED_BY {fact_id}]->(:Term)
(:Metric)-[:COMPUTED_FROM {fact_id}]->(:Column)
```

- **Fact kinds:** `file_type`, `schema_def`, `profile`, `semantic_type`, `sensitivity`, `primary_key`, `foreign_key`, `match`, `domain`, `insight`, `definition`, `lineage`, `metric_mapping`, `report_structure`, `report_sql`, `scope_link`. CP-001 adds `bi_model_link` (association key between two BI tables, mapped to source columns), `bi_transform` (one script statement mapped to a SQL fragment) and `bi_metric_def` (a measure expression and its translated SQL expression).
- **Evidence kinds:** `dq_check`, `doc_span`, `sample_stat`, `human`, `query_log`. CP-001 adds `bi_script` (app_id + statement span), `bi_model` (app_id + association key) and `bi_engine_output` (app_id + visual_id + selection state + reload_time + MinIO Parquet path).
- **QVD files** are modeled as `:Dataset {kind:"qvd"}`. Lineage must traverse multi-app QVD chains back to source tables; otherwise Mode A SQL stops at a QVD file.

**Rules:**
- Confirmed facts are immutable; they can only be superseded by another confirmed fact.
- A disagreement across trust levels creates `CONFLICTS_WITH` plus a review item, never an overwrite.
- Nothing is deleted; old facts are invalidated.
- Point-in-time queries filter on `recorded_at`/`invalidated_at`.
- Embeddings (for Column, Term, Document chunks, Metric) live in the vector index.
- **BI trust rule:** BI-extracted logic enters as `documented`. It describes what the report *does*, not that the logic is correct or matches source semantics. It moves to `validated` only after Reconciler parity, and to `confirmed` only via HITL.

### ADR-4: All 29 agents share one runtime contract
Every agent, including the deterministic ones, is a class implementing `BaseAgent` from `adce-core`:

```
on_event(evt) → ctx = kg.get_context_pack(task, budget)
              → result = self.work(ctx, duckdb, llm)       # stats, LLM, or both
              → for p in result.proposals: kg.propose_fact(p, evidence)
              → emit trace (Langfuse), metrics, budget usage
```

So every agent is versioned, traced, budgeted, policy-checked and replayable in the same way. Deterministic agents never call `llm`. Hybrid agents run their deterministic gate first, and the LLM only sees candidates that survived the gate.

### ADR-5: HITL service with five modes; HITL is stored as data
The `hitl-service` owns Seed, Ask, Gate, Queue and Correct (proposal section 9), backed by Postgres.
- **Seed:** user-entered facts are written as `confirmed` before a run.
- **Gate/Queue:** routing uses `confidence × impact`, where impact is the number of dependent facts. The queue is sorted by impact × uncertainty. Gated facts stay `proposed`; downstream agents may read them, but marked as untrusted. A decision emits `fact.confirmed`/`fact.rejected`, which re-triggers the affected steps.
- **Ask:** the only mode that uses LangGraph `interrupt()`, and never inside a parallel fan-out.
- **Correct:** a user edit becomes a new confirmed fact that supersedes the old one.
- **Learning:** every `(proposal, decision, edit, reason)` is embedded and retrieved as few-shot examples for that agent and scope (`get_feedback_history`).

### ADR-6: Stores, consistency and events
| Store | Role | Notes |
|---|---|---|
| Neo4j | Entities, facts, materialized edges, vectors | Managed through the KG engine (ADR-12); the only writer is `kg-mcp`. |
| Postgres | **`fact_log` (append-only, bi-temporal; the system of record for all knowledge, ADR-12)**, HITL queue, outbox, LangGraph checkpoints, users/roles mirror; the engine's other source tables (workspaces, uploads) | Insert-only for `fact_log`. Neo4j is a projection rebuildable from it. |
| MinIO | Raw uploads (versioned), normalized Parquet, original documents, BI IR artifacts, BI engine outputs, published dictionaries | Raw data never enters Neo4j. |
| DuckDB | Embedded compute in every agent worker over Parquet in MinIO | Nothing persistent. |
| Redis | **Event bus (Redis Streams with one consumer group per agent)**, context-pack cache, rate limits | Also required by Langfuse. |
| Langfuse | Traces, cost, prompt versions, evaluation datasets | Self-hosted v3. |

- **Consistency:** `kg-mcp` writes the `fact_log` row and the outbox row in one Postgres transaction with an idempotent `fact_id`, then projects it into the graph through the KG engine, then a relay publishes the outbox to Redis Streams. A nightly job checks the graph against the log, and any drift is fixed by replaying the log.
- **Event storms:** with 29 consuming agents, events are batched per upload and debounced per entity; the global verifier runs debounced per scope.

### ADR-7: Model gateway service
`model-gateway` is a small internal service that wraps the Anthropic SDK. It handles:
- route → model + effort
- Presidio masking (ADR-8)
- prompt caching
- **response memoization** keyed by (input hash, model, prompt version) on every fact-writing route, so a re-run reproduces the same facts. Brahmastra measured only 21% triple overlap between two un-memoized runs (ADR-12).
- structured outputs
- bulk work as rate-limited concurrent calls (the Anthropic Batch API is not available on Vertex, ADR-14)
- per-run and per-task budgets with retry caps
- refusal fallback (client-side SDK middleware on Vertex)
- **provider adapters**: Claude on Vertex AI (default), Gemini on Vertex, Anthropic API/Bedrock for client installs, record/replay for tests (ADR-14)
- Langfuse tracing with prompt and model version stamping

Bedrock (Mantle), Vertex and Foundry clients are selected by config per deployment.

| Route | Model | Effort |
|---|---|---|
| Intake tie-breaks, semantic types, domain tags | Haiku 4.5 | none |
| Doc extraction, linker/key/FK adjudication, match rerank, insights, glossary, refinement, conflicts, lineage, report parsing, metric mapping, **BI expression translation (T1–T2), BI script long-tail translation** | Sonnet 5.5 | low-medium |
| Global verifier, SQL synthesis, query repair, mismatch diagnosis, **BI T3 expression translation (Aggr, nested set analysis), Mode A mismatch diagnosis** | Opus 5.5 | medium-high |
| Bulk glossary drafting, bulk doc extraction | Sonnet 5.5, rate-limited bulk calls (Vertex batch prediction evaluated in M0; no discount assumed) | low |

BI extraction itself is deterministic and has no route. **Every LLM call in the system goes through this gateway, including any LLM use inside the KG engine** (ADR-12, R11).

### ADR-8: Privacy boundary
- Presidio (plus custom recognizers) runs **inside `model-gateway`**, so no code path can send unmasked samples. Only schemas, statistics and masked samples leave the environment.
- A `sensitive` column sends statistics only. The PII classifier agent runs before any LLM agent sees a dataset. Downgrading a sensitivity tag requires a human.
- **BI-engine outputs** (`GetHyperCubeData`) contain real values. They are written to MinIO as Parquet and handled exactly like dataset samples: the PII classifier runs first, and only masked samples or aggregates reach `model-gateway`.
- BI connector credentials live in the secret store and are scoped read-only. The BI extractor never writes to the BI server, never triggers reloads, and never saves apps (ADR-11).

### ADR-9: Identity and access
- **Keycloak** handles SSO (OIDC/SAML) for users and issues service-account tokens for each agent service.
- **User roles:** viewer, reviewer, data owner, admin.
- **Agent permissions:** each agent's client scope maps to its `AgentSpec` tool allow-list, and `kg-mcp` enforces it.
- **Edge:** the API gateway validates tokens. External MCP access uses OAuth.
- **BI (CP-001):** `agents-bi-adapters` has a Keycloak service account **plus** a separate BI-server credential (Qlik: a read-only service user or API key per tenant). Access to a BI app is a scope-registration action that requires the data-owner role (ADR-11).

### ADR-10: Evaluation is a product feature from day 1
- The gold set (keys, joins, definitions, matches, reports with known SQL, **BI gold set**) and an eval harness exist before agents ship.
- Every agent has a per-agent eval, and every prompt or model change runs the regression suite in CI.
- Results go to Langfuse datasets.
- **Report ranges across repeated runs, never single runs.**
- **KG engine acceptance (R1–R15)** (ADR-12) is part of the same harness.

### ADR-11: BI source adapters and two-mode Reverse-SQL (CP-001)
**Context.** Reverse-SQL v0.2 infers SQL from report outputs. When a report comes from a BI tool, its logic and a computation engine are available, and translation is far more reliable than inference.

**Inputs, phased (decided 2026-10-08).** For now we receive **Qlik scripts** (load scripts; QlikView `-prj` folders and app files where available; reload logs if provided). Access to the Qlik servers may come later (Q-07, Q-13 in `open-questions.md`). Mode A is therefore split:
- **A1: scripts (POC).** Translate load scripts and, where measure definitions are provided, chart expressions into **Dremio SQL** (ADR-13). Validate structurally (sqlglot + Query-checker rules), execute in Dremio, and reconcile against **exported report outputs or owner-provided figures** where they exist. Without any ground truth, a result is reported as **`translated, not validated`** and never counted as exact.
- **A2: engine-backed (when server access arrives).** The Engine API extraction and `GetHyperCubeData` ground truth across ≥3 slices, as originally designed in CP-001.

**Decision.**
1. Introduce a BI source-adapter layer that outputs the BI IR contract (ADR-1 #4). Qlik Sense and QlikView are the first adapter. Power BI (DAX/M) and Tableau (calculated fields) are later adapters behind the same IR, with no downstream agent changes.
2. Reverse-SQL runs in two modes, selected per report by the orchestrator:
   - **Mode A (translate):** BI IR available → script translator (28) + expression translator (29) → Join planner validates the BI model against KG joins → SQL synthesizer composes (no variant search, or a minimal bounded one for ambiguous semantics) → Query checker → Reconciler using engine ground truth.
   - **Mode B (infer):** unchanged v0.2 path.
   - **Hybrid:** a PDF or Excel export of a BI report whose app is also registered runs Mode A, and its output is used as an extra reconciliation slice.
3. BI-extracted facts are published to the KG for use by all groups (FK/IND, lineage, glossary, metric mapping), with `bi_*` evidence and `documented` trust.
4. Disagreement between the BI model and confirmed KG joins is a **conflict** (`flag_conflict`), never a silent preference.

**Consequences.**
- Three new agents (#27–#29), a new deployable, a new contract, new fact and evidence kinds.
- Mode A becomes the earliest end-to-end Reverse-SQL path.
- BI translation semantics (grain-aware aggregation, set-analysis override semantics) become Query-checker rules.

**Alternatives considered.**
- (a) Export BI reports to PDF and use Mode B only: this throws away the logic and is strictly worse.
- (b) Adopt a commercial converter: these target Power BI/Fabric and are not KG-integrated. Benchmarks only.
- (c) Qlik-specific agents per group: this duplicates parsing and prevents Power BI/Tableau reuse.

**Resolved questions (CP-001 section 8):**
1. **Group placement.** BI adapters stay inside the Reverse-SQL group for the POC (`AgentSpec.group = "reversesql"`, sub-group tag `bi_adapters`), owned by the Mode A sub-team, in their own deployable. Promote them to a 6th group when a Power BI or Tableau adapter is scheduled; the tag makes that a configuration change.
2. **`ExprAST` for Mode B.** Adopted for both modes (ADR-1).
3. **Global verifier and `bi_model_link`.** The global verifier sees BI joins only after the FK/IND detector (13) turns them into `foreign_key` proposals. That keeps one path for join facts.

### ADR-12: Evolve Brahmastra into the ADCE knowledge-graph core
**Context.** The team already has an in-house knowledge-graph engine, Brahmastra (repo `vaultgraph-qy`, branch `brahmastra-v3`, ~950 tests). It is a shared, persistent knowledge graph for AI assistants:
- Postgres + pgvector holds the system of record; Neo4j holds the graph.
- 12 MCP tools and a REST API.
- Entity resolution, contradiction detection, hybrid search, graph-first `/ask`, and meeting/session/code ingestion.

It is a **starting point, not a fixed dependency**: the team has confirmed it can be changed in whatever way the ADCE architecture needs. It was built for a different domain (notes and meetings → concepts, not datasets → keys and joins), so the fit below is a starting assessment from its latest documentation (README, HOW_IT_WORKS, DEPLOYMENT, PLATFORMS, ROADMAP, GETTING_STARTED; 2026-10-08), to be confirmed against its code in week 1.

**Decision.**
1. **Brahmastra becomes the ADCE KG core and is changed in place.**
   - What fits is kept: store layer, three-layer isolation, hybrid search, entity resolution, ownership ledger, contradiction detection, diagnostics, tests.
   - What doesn't fit is redesigned inside the engine, not wrapped from outside: the fact model, bi-temporal history, trust, authority tiers, the LLM layer, auth and the MCP surface.
   - There is no "upstream vs ADCE layer" split and no workaround layer.
2. **It moves into the ADCE monorepo as `packages/kg-engine`**, imported with its git history (`git subtree`), so a contract change and the engine change it needs land in one PR. Its existing test suite keeps running and stays green through the refactor.
3. **Core and apps are separated.** The engine is split into:
   - `kg-engine` **core**: stores, fact model, isolation, search, resolution, traversal, conflict policy, projection.
   - **Apps** on top: ADCE's `kg-mcp` is one. Brahmastra's personal and team-memory features (notes, meetings, sessions, code index, Notion) become optional apps on the same core, or stay in the old repo (open decision 10).

   ADCE deployments ship the core only.
4. **`kg_port` stays**, as a decoupling and testing seam rather than a defensive boundary. Agents and `kg-mcp` code against `kg_port`, the real engine and `MockKG` both implement it, and squads keep building against the mock from week 1 while the engine is refactored.
5. **R1–R15 become the engine's acceptance suite** (`eval/kg_acceptance/`). They define "done" for the refactor and guard against regressions afterwards.

**Requirements (R1–R15): current state and what changes in the engine**

Current state from the docs: ✅ fits as-is · 🟡 extend · 🔴 redesign. Every row is confirmed against code in week 1.

| # | Requirement | Brahmastra today (per docs) | State | Change in the engine |
|---|---|---|---|---|
| R1 | **Ontology profiles.** The data-catalog labels and relations in ADR-3, including BI nodes, are a closed, validated vocabulary | Closed vocabulary (18 relations, 12+ types) for extraction; meeting artifacts written as **typed nodes declared directly** (`graph_record.py`) | 🟡 | Ontology becomes a **profile** loaded per workspace. The ADCE profile is declared by agents, never LLM-extracted. The notes profile keeps working for the memory app. |
| R2 | **Reified `:Fact` nodes** with trust, evidence, supersedes, conflicts, feedback; materialized current-view edges | Facts are direct typed edges with provenance (confidence, quote, source note). Reification deliberately rejected "unless we need statements about statements". | 🔴 | **Redesign the fact model** around the fact log below. ADCE needs statements about statements (trust, conflicts, feedback). Typed edges remain, now as the materialized current view. |
| R3 | **Bi-temporal**: valid + transaction time; invalidate, never delete; point-in-time queries | Notes carry `created_at`/`updated_at`; the derived graph is rebuilt and deleted to match notes; no fact history | 🔴 | **Fact log** (below) carries both timelines. Point-in-time reads are a core query. |
| R4 | **Authority tiers**: uploads, documents, human decisions, confirmed/documented facts survive any rebuild | SOURCE vs DERIVED split enforced by a test; a human rejection is already SOURCE (`artifact_rejections`); ownership ledger (write new / update changed / delete gone, intent logged first) | 🟡 | The fact log, HITL decisions and uploads become SOURCE kinds. The ownership ledger gains **invalidate-gone** (the ADCE default) next to delete-gone. |
| R5 | **Tenant + scope isolation, failing closed** | Workspaces with three-layer isolation (bound stores, verified binding, Cypher guard) | ✅ | Workspace = tenant, unchanged. **Scope** (Source/Schema) is added as a second bound dimension in the same guard, so scope isolation also fails closed in the store, not just in the policy engine. |
| R6 | **Hybrid search** over entities, documents, terms, metrics | Lexical + vector RRF (K=60), identical on Postgres and Neo4j; notes, statements, sessions, code; entity search by name and meaning | 🟡 | Index ADCE node types (Column, Term, Metric, Document chunk, BI expression). Keep the fusion. |
| R7 | **Traversal**: neighbours, trust-weighted paths, lineage across QVD chains | 1–3 hop neighbourhood, shortest path (`/paths`) | 🟡 | Add **trust-weighted path search** and **lineage traversal** as core queries. |
| R8 | **MCP tool surface** (16 ADCE tools + read-only Cypher), per-caller allow-lists, OAuth | 12 note-centric tools over stdio/HTTP, behind one API key | 🔴 | The ADCE `kg-mcp` app exposes the 16 tools on **FastMCP 3.x**. The memory app's tools are ported to the same framework so there is one MCP stack. Auth moves to **Keycloak OIDC** with per-agent scopes; the API key is kept only for local dev. |
| R9 | **Write path**: policy → validator → gate → commit + audit + outbox | Pipeline stages, ownership intent logging; no policy/validator/gate | 🟡 | A **write-path hook chain** in the core, which `kg-mcp` fills with ADCE's policy, validator and gate. |
| R10 | **Audit + outbox**, idempotent fact IDs | Postgres system of record; intent logged before writes; content-derived stable IDs | 🟡 | The fact log is the audit trail. Add an outbox table in the same transaction. |
| R11 | **No LLM calls to non-approved providers** | `llm.py` provider registry, **Groq by default** (+ OpenAI, Anthropic, Gemini, Ollama); LLM used in extraction, merge judge, summaries, `/ask`, meetings, checkpoints | 🔴 | In the core, **every LLM call goes through a single client that talks to `model-gateway`**. The direct provider registry and Groq key pool move to the memory app (or are dropped). The core cannot reach a provider directly; the acceptance suite asserts no other outbound LLM traffic. |
| R12 | **Scale**: ≥1M fact versions per tenant; p95 context pack < 500 ms; join paths < 1 s at 10k columns | Measured at hundreds to thousands of entities; resolution to 10k mentions; PageRank/Louvain in-memory NetworkX; 461 MB RSS with the embedding model | 🔴 unknown | Load test in M0 week 2 (synthetic catalog: 5k datasets, 100k columns, 1M fact versions). Index design for `:Fact` by (tenant, kind, subject, current). Graph analytics (PageRank/Louvain) move off the hot path and become optional jobs. Embedding inference moves out of the API process into a shared service, so workers don't each carry 461 MB. |
| R13 | **Entity resolution / linking** | Strong: five candidate signals, syntax safeguards, LLM judge with usage sentences, Union-Find + coherence, stable IDs, incremental cache | ✅ | Kept. Add **value-based signals** (MinHash, containment) as pluggable candidate sources for column matching. Its principle is adopted project-wide: **rules on syntax beat rules on model output**. |
| R14 | **Conflict detection** on functional fact kinds | Five functional relations; a second value is a contradiction, **resolved to the newest note**, all values kept as evidence | 🟡 | Detection kept. The resolution policy becomes **pluggable**: ADCE uses trust-ordered, never-auto-by-recency, HITL-routed (ADR-5); the memory app keeps newest-wins. |
| R15 | **Configurable embeddings** | `all-MiniLM-L6-v2`, local, 384-dim, baked in; `EMBEDDINGS_ENABLED=0` fallback | 🟡 | Model chosen per deployment (Voyage / bge-m3 / local) behind the embedding service; index rebuild per model. |

**Target design of the KG core**

1. **The fact log is SOURCE data.**
   - All facts, versions, evidence, trust changes, conflicts and HITL decisions go into an **append-only, bi-temporal `fact_log` in Postgres**.
   - Neo4j holds a **projection**: `:Fact` nodes for current and recent versions, plus typed edges as the materialized current view. It can always be rebuilt by replaying the log.
   - This keeps Brahmastra's own principle (Postgres holds what cannot be recomputed; the graph is derived) and fixes R2, R3, R4 and R10 at once.
2. **Two bound isolation dimensions:** workspace (tenant) and scope, both enforced by the store's guard and failing closed.
3. **Declared vocabularies per profile**, with LLM extraction available only to apps that need it (the memory app). ADCE knowledge is always declared by agents.
4. **One LLM client, one route out** (via `model-gateway`), with **response memoization** keyed by (input hash, model, prompt version). Brahmastra measured only **21%** triple overlap between two un-memoized runs at temperature 0, and its memo cache is what keeps the graph stable (ADR-7).
5. **Pluggable policies**: conflict resolution, ownership semantics (invalidate vs delete), write-path hooks, candidate signals.
6. **Kept as-is:**
   - entity resolution and its safeguards
   - hybrid search arithmetic
   - the ownership ledger's intent-first writes
   - fail-closed defaults
   - diagnostics with "will retry / will fail again" verdicts
   - the running-version fingerprint in `/health`
   - the sandboxed scratch runner
   - measured-adoption evaluation reported as ranges

**Dropped from ADCE deployments:**
- The Aura keepalive (ADCE runs self-hosted Neo4j 5.26 LTS) and Groq-specific code.
- Notion sync and write-back, meeting/lecture ingestion, session checkpoints and code indexing. These live on as memory-app features if kept.

The ADCE engineering team can still run the memory app for its **own engineering memory** during the build, in a workspace that never holds client data.

**Consequences.**
- The Platform & KG squad's M0–M2 work is a **planned refactor of a working engine**, not a greenfield build.
- The R2/R3 redesign (fact log + projection) is the critical path. It is scheduled first and isolated behind `kg_port`, so squads are not blocked.
- Brahmastra's tests and the R1–R15 acceptance suite must both stay green. Regressions in kept features (resolution, search, isolation) are caught by its existing tests.
- One MCP framework, one auth model and one LLM route across the codebase.

**Alternatives considered.**
- (a) Build from scratch: throws away proven resolution, search and isolation work.
- (b) Keep Brahmastra as a separate pinned dependency and wrap it in an ADCE layer: rejected now that it can change freely. The wrapper would duplicate the fact model and fight the engine's "graph is derived from notes" semantics.
- (c) Call its HTTP API as a service: no fact/trust semantics, and isolation enforced in two places.

### ADR-13: Dremio is the primary data source
**Context.** The client's tables and views live in **Dremio**, which federates and maintains them. Agents must read data and metadata from Dremio, not from file uploads alone. Dremio also holds knowledge we would otherwise have to infer:
- **view SQL definitions**
- **wikis and tags**
- **query history**

**Decision.**
1. **A data-source port** (`data_port` in `adce-core`) with these operations: list catalog, get schema, get view definition, run a bounded pushdown query, fetch a sample as Arrow, and read query history. Implementations:
   - **`DremioSource`**:
     - **Arrow Flight SQL** through the ADBC Flight SQL driver for queries and metadata (Dremio ≥ 21).
     - **REST catalog API (v3)** for view definitions, wikis and tags.
     - **`sys.jobs_recent`** for query history.
   - **`FileSource`**: CSV, Excel and Parquet uploads, unchanged.
   - **Local Dremio OSS** (`dremio-local` compose profile) seeded with sample data for development (ADR-15).
2. **Push computation down to Dremio.** Full-data statistics, uniqueness, composite-key checks and inclusion-dependency (containment) checks run **as SQL in Dremio**, where the data lives. Because Dremio federates sources, cross-source containment checks also run there.
   - **DuckDB stays** for uploaded files, for mixing file data with Dremio extracts, and for Polars-side processing of small results.
   - **Samples** pulled for LLM context are small, masked (ADR-8) and never persisted outside MinIO.
3. **Reverse-SQL targets Dremio SQL and executes in Dremio.** sqlglot has a Dremio dialect, used to parse view definitions, transpile Qlik-embedded SQL into Dremio SQL, and validate generated SQL. The Reconciler (26) runs candidate SQL in Dremio.
4. **Dremio metadata becomes knowledge:**

   | Dremio artifact | Becomes | Trust | Consumed by |
   |---|---|---|---|
   | Spaces / folders / sources | `Source` and `Schema` nodes | documented | Scope registrar (5) |
   | Physical and virtual datasets | `Dataset {kind: table \| view}` + columns | documented | Schema parser (2) |
   | **View SQL definitions** (parsed with sqlglot) | `lineage` (`DERIVED_FROM`) facts; JOIN clauses as `foreign_key` evidence | documented | Lineage inferrer (19), FK/IND detector (13) |
   | Wikis, descriptions, tags | definitions and context | documented | Doc extractor (3), Glossary drafter (16) |
   | **Query history** (`sys.jobs_recent`) | `query_log` evidence (joins actually used, frequent filters and aggregates) | evidence only | FK/IND detector (13), Metric mapper (22), Join planner (23) |

   New evidence kind: `view_definition` (ref = dataset path + view version). `Dataset.kind` is now `table | view | qvd | file`.
5. **Catalog sync, not upload.** A registered Dremio connection is crawled by the Scope registrar (5) and Schema parser (2).
   - Each dataset emits `dataset.registered`.
   - A schema or view-definition change found on the next crawl emits `dataset.changed`, which re-triggers only the affected agents.
   - No new agents; the agent count stays 29.
6. **Load governance on the client's Dremio** (limits are Q-10 in `open-questions.md`):
   - read-only service account
   - a cap on concurrent queries
   - per-query timeout and row limits
   - profiling scheduled off-peak
   - bounded profiling queries with sampling for very large tables
   - every query labelled so the client can see ADCE's load in their job history
   - no Reflections without client approval
7. **Qlik ↔ Dremio linking.** Sources in Qlik scripts (`SQL SELECT … FROM`, `LOAD … FROM`) are linked to Dremio datasets by the Entity linker (4), using names, paths and column overlap. If scripts query Dremio directly (Q-11), the link is near-exact.

**Consequences.**
- Profiling and key checks scale with Dremio, not with worker memory.
- View definitions turn much of lineage into **extraction** instead of inference.
- Query history gives the strongest join signal from day 1.
- Client Dremio load is a real operational risk, so it is governed and visible.

### ADR-14: Model access through Google Cloud (Vertex AI)
**Context.** LLM access goes through **Google** (Q-03 decides whether that means Claude on Vertex AI, Gemini, or both). The product must also ship to clients who bring their own approved endpoint.

**Decision.**
1. **`model-gateway` keeps a provider-adapter interface.**
   - **Default adapter: Claude on Vertex AI** (`AnthropicVertex` client; Opus 5.5, Sonnet 5.5, Haiku 4.5; model IDs without a prefix; region per data-residency choice, Q-04).
   - **Gemini on Vertex** sits behind the same interface for routes where it measures better.
   - Additional adapters (Anthropic API direct, Bedrock) exist for client installs.
   - Agents never know which provider served them.
2. **Feature differences on Vertex** (vs the Anthropic API) are handled in the gateway:

   | Feature | On Vertex | Gateway handling |
   |---|---|---|
   | Messages, tool use, structured outputs, PDF input, prompt caching, citations, adaptive thinking/effort, token counting | ✅ supported | Used as designed |
   | **Anthropic Message Batches API** | ❌ not supported | Bulk routes (glossary drafting, bulk doc extraction) run as rate-limited concurrent calls. Vertex's own batch prediction is evaluated in M0. **The 50% batch discount is not assumed in cost estimates.** |
   | **Server-side refusal fallbacks** | ❌ | Client-side refusal-fallback middleware in the SDK, plus gateway retry policy |
   | **Task budgets** | ❌ | Budgets enforced by the gateway (per run, per task, retry caps) |
   | Files API, Models API | ❌ | PDFs sent inline; model list kept in config |
   | Web search/fetch, code execution | limited / ❌ | Not used by ADCE |

3. **Pricing** is Google partner pricing, not Anthropic list prices. Section 14 of the proposal and Q-25 (budget) are re-estimated on Vertex rates in M0.
4. **Data handling.** Requests stay in the chosen GCP project and region. In client installs the gateway points at the **client's own** Vertex project (or their approved endpoint), so prompts never leave their cloud account. The ADR-8 masking boundary is unchanged.
5. **Local development** uses a dev Vertex project. The gateway also supports a **record/replay mode** (responses memoized by input hash, ADR-7), so tests and CI run offline and deterministically without spending tokens.

### ADR-15: Environments: local first, then production, shippable to clients
**Context.** Development starts in local environments and moves to production later. The product will also be **shipped to clients**, so everything must deploy outside our own infrastructure from the start.

**Decision.**
1. **Three environments, one set of artifacts.** The same container images and the same Helm chart / Compose bundle are used everywhere. Only configuration differs.

   | Environment | Runs on | Data | Models |
   |---|---|---|---|
   | **Local** (every developer) | Docker Compose with profiles: `core` (Postgres, Neo4j, Redis, MinIO, `kg-mcp`, `model-gateway`, API), `agents`, `ui`, `auth` (Keycloak), `obs` (Langfuse + ClickHouse), `dremio-local` (Dremio OSS + sample data), `bi-fixtures` (sample Qlik scripts). Windows (Docker Desktop/WSL2), macOS and Linux supported. | Local Dremio OSS sample dump, fixtures | Dev Vertex project, or record/replay |
   | **Staging** (shared) | GCP: GKE (or one VM with Compose at first), Cloud SQL for Postgres with pgvector, Memorystore (Redis), GCS through the S3-compatible API, Neo4j 5.26 LTS self-hosted on GKE, Secret Manager | Non-prod Dremio (read-only) | Staging Vertex project |
   | **Production / client** | **(a) Hosted by us:** a dedicated GCP project per client. **(b) Client-installed:** their cloud or on-prem via the Helm chart, their Dremio, their identity provider, their approved LLM endpoint | Client Dremio (read-only) | Client's Vertex or approved endpoint |

2. **Rules that make it shippable (apply from day 1):**
   1. **Every external dependency sits behind an interface**, chosen by config:
      - object store (S3 API: MinIO, GCS, S3)
      - Postgres (self-hosted or managed)
      - secrets (env, Secret Manager, Vault)
      - LLM endpoint (ADR-14)
      - embeddings
      - data source (ADR-13)
      - identity (Keycloak federating to the client's IdP)
   2. **Twelve-factor configuration.** No hard-coded hosts or cloud services; one documented config schema, validated at startup, failing closed.
   3. **Forward-only, automatic migrations:** Alembic for Postgres, versioned Cypher migrations for Neo4j. They are tested on the upgrade path from the previous release, not only on fresh installs.
   4. **Single-tenant per client deployment by default.** In-store tenant isolation (ADR-12) still applies, so multi-tenant hosting stays possible.
   5. **Air-gap capable.** Self-hostable embeddings, models baked into images (no runtime downloads), no required outbound calls except the configured LLM endpoint, and telemetry off unless the client opts in.
   6. **Release engineering:**
      - semver releases with release notes and an upgrade guide
      - signed images with an SBOM
      - vulnerability scanning in CI
      - a **support bundle** (diagnostics, versions and config with secrets redacted, never data)
   7. **Observability works without our services.** OpenTelemetry everywhere. Langfuse is bundled but optional in client installs.
3. **Promotion path:** local → staging → production.
   - Promotion is gated by the gold-set evaluation, the KG acceptance suite, contract tests, and security and vulnerability scans.
   - Production config changes go through the same pipeline as code.
4. **Local-first sequencing.** M0–M2 run entirely in local environments plus a shared dev Vertex project. Staging on GCP comes up at M3, and the first production-shaped install (Helm, managed services) at M4 (section 7.2).

**Consequences.**
- Some early work (interfaces, config schema, migrations, packaging) that a pure internal POC would skip. It is cheaper now than retrofitting before the first client.
- Developer machines need roughly 16–24 GB RAM for the full stack. Compose profiles allow subsets (Q-22).

---

## 5. Agent catalog (all 29 + orchestrator)

Kind: **D** = deterministic (no LLM), **H** = hybrid (deterministic gate, then LLM), **L** = LLM-led. "Writes" is the fact kind. HITL modes as in proposal section 9.

### 5.1 Ingestion group
| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 1 | Intake router | H: python-magic, charset detection, pandas/openpyxl sheet layout; Haiku for ambiguous types. Classes: data, schema, dictionary, document, report, **`bi_app`** (.qvf/.qvw upload, `-prj` folder archive, or registered BI-server connection) | `upload.received` | `get_source_context`, `register_entity` | Upload node, `file_type` | Confirm type; user sets scope tag |
| 2 | Schema parser | D: sqlglot (DDL, many dialects), dbt YAML, JSON Schema, Avro | `upload.classified[schema]` | `register_entity`, `add_context` | `schema_def` (documented) | Review parse warnings |
| 3 | Doc extractor | L: Sonnet 5.5, PDF input, structured outputs; every extracted fact must cite a supporting span (grounding check) | `upload.classified[document,dictionary]` | `add_context`, `search_context` | `definition`, `primary_key`, `foreign_key`, rules (documented) | Spot-check samples |
| 4 | Entity linker | H: name similarity, embeddings, value patterns (KG engine resolution primitives, R13) → Sonnet adjudication | `dataset.registered`, `context.added` | `search_context`, `get_source_context`, `link_entities` | `DESCRIBES` links, `scope_link` | Confirm low-confidence links |
| 5 | Scope registrar | D: rules | `upload.scoped`, `context.added` | `register_entity`, `get_source_context` | Source/Schema nodes, placeholders, inheritance | Confirm new source vs merge |

### 5.2 Profiling group
| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 6 | Stats profiler | D: DuckDB/Polars full-data stats, patterns, histograms, drift vs previous load | `dataset.registered`, `dataset.reloaded` | `get_entity`, `propose_fact` | `profile` (versioned) | Exclude columns |
| 7 | Semantic typer | H: regex/rule library → Haiku for the rest | `profile.ready` | `get_context_pack`, `search_context` | `semantic_type` | Correct types |
| 8 | PII classifier | D: Presidio + custom recognizers; runs before any LLM agent (also on BI engine outputs) | `profile.ready` (priority) | `propose_fact` | `sensitivity` | Any downgrade needs a human |
| 9 | Key discoverer | H: documented/confirmed keys first → uniqueness, naming, type and position scoring incl. composites up to width 3 → Sonnet validation | `profile.ready`, `context.added[keys]` | `get_context_pack`, `get_feedback_history`, `propose_fact`, `flag_conflict` | `primary_key` | Gate: confirm PK |
| 10 | Domain classifier | H: embeddings vs domain vocabularies → Haiku | `semantic_types.ready` | `search_context`, `propose_fact` | `domain` | Confirm when unsure |

### 5.3 Comparison group
| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 11 | Candidate finder | D: RapidFuzz names, embeddings, MinHash LSH value sketches; skips confirmed matches | `profile.ready` | `search_context`, `get_neighbors` | candidate sets (Postgres, not facts) | None |
| 12 | Match reranker | L: Sonnet 5.5 over top-k with profiles + context; labels relation type | `candidates.ready` | `get_context_pack`, `get_feedback_history`, `propose_fact` | `match` | Queue: batch approve |
| 13 | FK / IND detector | H: unique-key-driven pruning → DuckDB containment with deterministic gates → Sonnet adjudication. **Consumes `bi_model_link` facts as `foreign_key` evidence** (the only path by which BI joins become join facts) | `primary_key.*`, `match.confirmed`, `bi_model_link.*` | `find_join_paths`, `get_context_pack`, `propose_fact` | `foreign_key` (+ cardinality) | Queue; gate across sources |
| 14 | Global verifier | H: constraint checks (cycles, one-to-one violations, broken chains, type clashes) → Opus 5.5 holistic resolution | `scope.changed` (debounced) | `get_source_context`, `get_neighbors`, `flag_conflict` | conflicts | Resolve conflicts |
| 15 | Insight writer | H: computed overlap, coverage, orphan rows, drift, duplicates → Sonnet narrative | `foreign_key.*`, `match.*`, `profile.drift` | `get_feedback_history`, `propose_fact` | `insight` | Rate or dismiss |

### 5.4 Definition and lineage group
| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 16 | Glossary drafter | L: Sonnet 5.5 (Batch for bulk). Source order: documented text (incl. **BI master-item labels/descriptions**) → confirmed definitions of matched columns → profiles | `semantic_types.ready`, `context.added`, `bi_metric_def.*` | `get_context_pack`, `get_feedback_history`, `propose_fact` | `definition`, Terms | Correct: edit |
| 17 | Refinement loop | H: propagates along join paths, DBAutoDoc-style, until convergence; Sonnet re-drafts; never touches confirmed facts | `definition.*`, `fact.confirmed` | `get_neighbors`, `update_fact` | revised `definition` versions | Approve material changes |
| 18 | Conflict resolver | L: Sonnet 5.5; proposes scoped resolutions (e.g. per department) | `conflict.flagged` | `get_entity`, `get_feedback_history`, `propose_fact` | resolution proposals | Gate: human decides |
| 19 | Lineage inferrer | H: value-derivation checks, load timestamps, documents, reverse-SQL results, **`bi_transform` facts and QVD chains** → Sonnet | `foreign_key.*`, `report_sql.validated`, `bi_transform.*` | `get_lineage`, `propose_fact` | `lineage` (`DERIVED_FROM`) | Confirm lineage |
| 20 | Dictionary publisher | D: Excel, Markdown, PDF, dbt YAML, OpenLineage; Great Expectations suggested rules | `publish.requested`, `fact.confirmed` | `get_source_context` (validated subgraph) | version stamp | Gate: owner sign-off |

### 5.5 Reverse-SQL group (Mode B infer, plus Mode A deltas from CP-001)
| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 21 | Report parser | L: Sonnet 5.5 PDF + vision (Opus for complex layouts); openpyxl for Excel. **Mode A:** if the report is an export of a registered BI app (title, sheet name, visual match), link it to the `:Visual` and hand off to Mode A (hybrid). | `upload.classified[report]` | `get_entity`, `register_entity` | Report/Metric nodes, `report_structure` | Check parsed structure |
| 22 | Metric mapper | H: vector search over columns/terms → Sonnet ranking. **Mode A:** mappings come from `bi_metric_def` facts (BI field → source column via script lineage); vector search only for BI fields with no traced source. | `report.parsed`, `bi_metric_def.ready` | `search_context`, `get_feedback_history`, `propose_fact` | `metric_mapping` | Ask when ambiguous |
| 23 | Join planner | D: trust-weighted path search. **Mode A:** start from `bi_model_link` facts, verify each against confirmed/validated `JOINS_TO`, flag disagreements. | `mapping.ready` | `find_join_paths`, `run_readonly_cypher`, **`flag_conflict`** | join plan (ephemeral), conflicts | **Conflicts routed to Global verifier/queue** (Mode B: none) |
| 24 | SQL synthesizer | H: Opus 5.5 drafts SQL in `ExprAST` → bounded variant generator. **Mode A:** composes SQL from translated script CTEs + translated measure expressions + the join plan; variant search disabled except for flagged ambiguities (e.g. NULL handling of dual values). | `plan.ready` | `get_context_pack` | candidate SQL set | None |
| 25 | Query checker | H: sqlglot validation + KG rules (joins exist, grain, units, fan-out/double counting) → Opus repair. **BI semantics rules:** (1) grain-aware aggregation, so an aggregated field is aggregated at its native table grain before joining (pre-aggregate CTE), or the query is rejected for fan-out; (2) set-modifier override semantics vs the report's default selection state; (3) `TOTAL`/`Aggr` scope; (4) dual-value/NULL behaviour; (5) `DISTINCT` across associated tables. | `sql.candidates` | `get_entity` | checked SQL | None |
| 26 | Reconciler | H: Dremio execution (DuckDB for file-only data) → cell-level diff → Opus mismatch diagnosis; may return "not resolved". **Mode B:** ≥2 periods/slices from report uploads. **Mode A1:** executes in **Dremio** and compares with exported report outputs or owner-provided figures where available; otherwise status `translated, not validated`. **Mode A2:** ground truth = `GetHyperCubeData` for the visual, across **≥3 engine-applied selection slices** + the unfiltered default; runs SQL on data as of the app's `reload_time` (or the closest snapshot) and records any snapshot gap as a tolerance reason. All candidate SQL runs in Dremio (ADR-13). | `sql.checked` | `propose_fact` | `report_sql` + validated report lineage | Gate: approve or pick SQL |

Query checker (25) and Reconciler (26) are **shared by both modes** and owned by one named lead.

### 5.6 Orchestrator
- A LangGraph 1.x supervisor with one subgraph per group, checkpointed in Postgres.
- It plans runs, fans comparison out in parallel per dataset pair, **selects Mode A / Mode B / hybrid per report**, consumes Redis Streams events so it re-runs only affected steps, enforces per-run budgets, and hosts Ask interrupts.

### 5.7 BI source-adapter agents (CP-001)
Deployable `agents-bi-adapters`; Reverse-SQL group, sub-group `bi_adapters`; owned by the Mode A sub-team.

| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 27 | BI extractor | D. **A1 (now):** parsers for Qlik Sense load scripts (text/`.qvs`), QlikView `-prj` XML (script + per-object expressions), reload logs (resolved `$(variables)`, executed statements), and app files where they can be read offline. **A2 (later):** Qlik Engine API client (Sense/Cloud: `OpenDoc`, `GetScript`, `GetTablesAndKeys`, `GetAllInfos`→`GetProperties`, MeasureList/DimensionList, `GetVariables`, `GetHyperCubeData`), `-prj` XML parser (QlikView), reload-log parser for variable resolution; emits BI IR; exports hypercube data for registered visuals to MinIO Parquet. Read-only: never writes, reloads or saves. | `upload.classified[bi_app]` (script files, `-prj` archives, reload logs), `bi_app.reloaded` (poll `reload_time`) | `register_entity`, `add_context`, `get_source_context` | BIApp/Visual/Metric nodes, QVD `:Dataset` nodes, `READS`/`WRITES`, BI IR artifact, `bi_model_link` (documented) | Confirm app scope; confirm cross-app QVD linkage |
| 28 | BI script translator | H: sqlglot transpile of embedded `SQL SELECT` (source → target dialect); rule-based mapping of Qlik transforms to CTEs (table below); Sonnet for unmapped statements; every LLM output re-parsed by sqlglot; unparseable statements → `kind: other` + LLM, flagged `untranslated` if still unresolved | `bi_ir.ready` | `get_context_pack`, `get_lineage`, `propose_fact` | `bi_transform`, `lineage` (`DERIVED_FROM`, documented), source-column mappings | Review statements marked `untranslated` |
| 29 | BI expression translator | H: parse measure/dimension expressions to `ExprAST`; rule mapping for T1–T2; Sonnet (T2 edge cases) / Opus (T3); assigns `construct_tier` | `bi_ir.ready`, `bi_transform.*` | `get_context_pack`, `get_feedback_history`, `propose_fact` | `bi_metric_def`, `definition` seeds from master items (documented) | Ask when an expression depends on unresolved variables |

**Qlik → SQL rule mapping (minimum supported set for agents 28/29):**

| Qlik | SQL |
|---|---|
| Preceding `LOAD` / `RESIDENT` load | CTE chain |
| `CONCATENATE` | `UNION ALL` (align columns, NULL-fill) |
| `LEFT/INNER/OUTER JOIN` | Same join on all same-named fields |
| `LEFT KEEP` / `INNER KEEP` | `WHERE EXISTS` semi-join |
| `MAPPING LOAD` + `ApplyMap(map, key, default)` | `LEFT JOIN` mapping CTE + `COALESCE(value, default)` |
| `WHERE Exists(Field)` | `WHERE Field IN (SELECT Field FROM <prior loads>)` (order-dependent; preserve load order) |
| `Peek()` / `Previous()` | `LAG()` over load order (needs an explicit ordering key; otherwise T3) |
| `CROSSTABLE` / `GENERIC` | `UNPIVOT` / `PIVOT` (or conditional aggregation) |
| `IntervalMatch` | Range join (`BETWEEN`) |
| `Sum({<F={v}, G-={w}>} X)` | `SUM(CASE WHEN F = v AND G <> w THEN X END)` |
| `Sum(TOTAL X)` / `Sum(TOTAL <D> X)` | `SUM(SUM(X)) OVER ()` / `OVER (PARTITION BY D)` |
| `Aggr(Sum(X), D)` inside an outer aggregate | Subquery grouped by D, outer aggregate |
| `Above(expr)` / `RangeSum(Above(expr,0,n))` | `LAG` / windowed `SUM … ROWS BETWEEN n-1 PRECEDING AND CURRENT ROW` |
| `Count(DISTINCT F)` | `COUNT(DISTINCT F)` at F's native table grain |
| `$(var)` | Resolved before translation from reload log or `LET/SET`; unresolved → Ask |
| Section Access | Not translated to SQL; exported as an RLS requirement on `scope_link` |

**Key semantic hazard:** Qlik aggregates at the grain of the table holding the field, so a 1:n association does not double-count. A naive SQL join followed by `SUM` does. Query-checker rule (1) and engine reconciliation on ≥3 slices exist for exactly this reason.

### 5.8 Contracts and events added by CP-001
| Type | Name | Notes |
|---|---|---|
| Contract | `bi_ir` v1 | ADR-1 #4 |
| Event | `upload.classified[bi_app]` | From Intake router |
| Event | `bi_ir.ready` | From BI extractor; payload = app_id, BI IR MinIO ref, reload_time |
| Event | `bi_app.reloaded` | From BI extractor poller; re-triggers extraction + reconciliation |
| Event | `bi_transform.ready` / `bi_metric_def.ready` | From translators |
| Fact kinds | `bi_model_link`, `bi_transform`, `bi_metric_def` | ADR-3 |
| Evidence kinds | `bi_script`, `bi_model`, `bi_engine_output` | ADR-3 |
| Tool allow-list | Join planner (23) += `flag_conflict` | 5.5 |
| AgentSpec | 3 new specs (#27–29), group `reversesql`, sub-group `bi_adapters` | ADR-11 |

This is a minor version bump of the fact and event schemas. No existing v1 contract field changes.

---

## 6. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 (backend/agents), TypeScript (workbench) | `uv`, `ruff`, `pyright`, `pytest`; `pnpm`, ESLint, Playwright |
| Workbench UI | **Next.js + React Flow** from day 1; Streamlit only for internal debug consoles | Reuse the KG engine's existing graph-view components where they fit (assess with its latest version) |
| API gateway & auth | FastAPI + **Keycloak** | |
| Orchestration | LangGraph 1.x + `langgraph-checkpoint-postgres` | |
| Knowledge graph | **`kg-engine`: Brahmastra evolved in place** (fact log in Postgres + Neo4j 5.26 LTS projection, APOC) behind `kg_port` | R1–R15 acceptance suite (ADR-12) |
| KG tool server | **FastMCP 3.x** for `kg-mcp` | Align with the MCP framework the engine uses (open decision 11) |
| LLMs | `model-gateway` → **Claude on Google Vertex AI** (`anthropic[vertex]`: Haiku 4.5, Sonnet 5.5, Opus 5.5); Gemini via `google-genai` behind the same adapter interface | ADR-14; Q-03 |
| **Data source** | **Dremio** via Arrow Flight SQL (ADBC Flight SQL driver), REST catalog API v3, `sys.jobs_recent`; sqlglot `dremio` dialect | ADR-13 |
| Embeddings | Self-hosted bge-m3 / Vertex AI embeddings / Voyage / engine's local model, behind one interface | R15; Q-16 |
| Relational | Postgres 16 (pgvector image) | |
| Object store | MinIO | |
| Compute | **Pushdown SQL in Dremio** for full-data checks; DuckDB 1.5.x + Polars for files and small extracts | ADR-13 |
| Matching & keys | datasketch, RapidFuzz, DuckDB checks | |
| SQL | sqlglot | Also Mode A dialect transpile |
| **BI (CP-001)** | **A1:** Qlik script grammar + `-prj` XML (lxml) + reload-log parser. **A2 (later):** Qlik Engine API over WebSocket (JSON-RPC; e.g. `websockets` + typed client or `enigma`-style wrapper), `qlik-cli` (Cloud only), lxml for `-prj` XML, **a Qlik script grammar** (Lark/PEG, using `GetBaseBNF` as reference) | Read-only service user per tenant |
| Documents | Claude PDF/vision, openpyxl, python-docx, WeasyPrint | |
| Privacy | Microsoft Presidio | |
| Data quality | Great Expectations | |
| Events | Redis Streams (outbox relay from Postgres) | |
| Observability | Langfuse v3 self-hosted + OpenTelemetry → Grafana/Prometheus | |
| Exports | OpenLineage, dbt YAML, Excel, Markdown, PDF | |
| Deployment | **Local:** Docker Compose with profiles (incl. `dremio-local`). **Staging/production:** GCP (GKE, Cloud SQL + pgvector, Memorystore, GCS, Secret Manager) via the **same Helm chart** used for client installs | ADR-15 |
| Migrations & release | Alembic (Postgres), versioned Cypher migrations (Neo4j), semver, signed images + SBOM, support bundle | ADR-15 |
| CI | GitHub Actions: lint, type, unit, contract tests vs mock KG, **KG acceptance suite + the engine's existing tests**, gold-set eval on prompt/model changes | |

---

## 7. Team structure and plan (parallel squads, 12 weeks)

### 7.1 Squads
| Squad | Owns | Suggested size |
|---|---|---|
| **Platform & KG core** | `kg-engine` refactor (fact log + projection, profiles, scope isolation, pluggable policies, LLM client, embedding service), `kg_port`, acceptance suite, `kg-mcp` (tools, policy, validator, context assembler), stores, outbox/events, Keycloak, Compose + Helm, CI | **4-5** (the R2/R3 redesign is the critical path) |
| **Orchestration, HITL & Model gateway** | LangGraph supervisor + subgraphs (incl. Mode A/B selection), `hitl-service`, `model-gateway`, Langfuse | 3 |
| **Ingestion** | Agents 1-5 | 2-3 |
| **Profiling** | Agents 6-10 | 2-3 |
| **Comparison** | Agents 11-15 | 3 |
| **Definition & Lineage** | Agents 16-20 + all exports | 2-3 |
| **Reverse-SQL** | **Mode A sub-team:** agents 27–29 + Mode A paths in 22–26. **Mode B sub-team:** agents 21–24. **Shared:** 25 and 26, one named lead. | **4-5** |
| **Workbench & API** | Next.js workbench, API gateway, external MCP access | 2-3 |
| **Evaluation & Domain** | Gold set, **BI gold set**, eval harness, per-agent evals, domain experts, acceptance | 1-2 engineers + domain experts |
| Architecture group (1 per squad) | Contracts, RFCs, KG gap decisions (ADR-12), cross-cutting reviews (security, privacy, cost) | rotating |

### 7.2 Timeline
All squads start in week 1 and build against the mock KG. Integration milestones are every two weeks.

| Weeks | Milestone | What is true at the end |
|---|---|---|
| 1-2 | **M0: Contracts & foundation** | `AgentSpec`, fact/event schemas, **BI IR v1** and **`kg_port` v1** frozen. Mock KG + fixtures released (incl. ≥2 Qlik apps, T1–T2). **Week 1: Brahmastra imported into `packages/kg-engine` with history, existing tests green in ADCE CI; R1–R15 acceptance suite written and run to baseline the ADR-12 current-state column; core/apps split started; gateway-only LLM client in place (R11).** **Week 2: `fact_log` + Neo4j projection design merged and `propose_fact` writing through it with outbox; scope added to the isolation guard; R12 load test with a synthetic catalog.** Compose stack up (Neo4j, Postgres, MinIO, Redis, Langfuse, Keycloak). Model gateway with Presidio. Gold set v1. BI connector credentials and read-only access agreed with the client (or file-only fallback decided). Agent skeletons pass contract tests. |
| 3-4 | **M1: First real slice** | Ingestion 1-5 and Profiling 6-10 live on the real KG (`kg-engine` with the fact log and projection, behind `kg_port`); memory-app features moved under `apps/memory` with their tests green. HITL Gate/Queue/Seed live. Workbench review queue + entity view. Upload → profile → PK gated → confirmed, visible in graph and audit. **BI extractor (27) live on real Qlik scripts: BI IR + KG registration incl. QVD chains. Script translator (28) translating embedded SQL + T1 transforms into Dremio SQL. Dremio catalog crawl (spaces, datasets, view definitions) and pushdown profiling live against local Dremio OSS + sample dump.** Mode B agents 21-23 working on fixtures. |
| 5-6 | **M2: Relate & compound** | Comparison 11-15 live. Glossary drafter + dictionary publisher (MD/Excel). Documents before and after data both work. Event-driven re-runs live. **Reverse-SQL Mode A1 end-to-end: real Qlik scripts → Dremio SQL → executed in Dremio, with ≥3 report figures validated against exported outputs (T1–T2).** View-definition lineage and query-history join evidence live. **Mode B first end-to-end on 2 gold reports.** `bi_model_link` facts consumed by FK/IND detector (13). Workbench graph explorer. KG core: fact log + projection, scope isolation and gateway-only LLM client complete; R1–R11, R13–R15 acceptance green. |
| 7-8 | **M3: Full pipeline** | Global verifier, refinement loop, conflict resolver, lineage inferrer. Mode B 24-26 with multi-period validation. **Expression translator (29) T2 complete; BI semantics rules in the Query checker; BI lineage consumed by (19); BI master-item definitions consumed by (16); hybrid mode (BI export PDF → Mode A).** Ask + Correct modes. Feedback-as-examples. Keycloak roles enforced everywhere. **Staging on GCP up (ADR-15), connected to non-prod Dremio and the staging Vertex project.** |
| 9-10 | **M4: Complete outputs** | All exports. External MCP access with OAuth. Impact-ranked queue. Budgets and cost dashboards. **First production-shaped install from the Helm chart (managed Postgres, GCS, Secret Manager) and an upgrade-path migration test.** **BI T3 attempts; Mode A2 starts if Qlik server access exists (Q-13); BI-adapter interface documented for Power BI/Tableau (no implementation).** KG scale test against R12 targets. |
| 11-12 | **M5: Hardening & evaluation** | Full gold-set evaluation, **Mode A and Mode B reported separately**, worked example (proposal section 10.2) replayed, security/privacy review, load test (event storms with 29 consumers), final report with cost per dataset and per report. |

### 7.3 Success metrics
- PK F1 ≥ 90%; FK F1 ≥ 80% within a source; cross-source match precision reported per dataset with a normalization grade.
- Review items per new dataset from the same source decline across M1 → M5.
- Auto-accepted facts later reversed by humans stay below 5%.
- Late-arriving documents update affected facts with no manual re-run.
- **Reverse-SQL Mode B:** share of gold reports *exact*, *within tolerance* and *not resolved*.
- **Reverse-SQL Mode A1:** share of in-scope report figures (T1–T2) *exact* / *within tolerance* / *not resolved* / *translated, not validated*, against exported outputs. **Target ≥ 70% exact-or-within-tolerance among figures that have ground truth, by M5.** A2 (engine ground truth across ≥3 slices) is reported separately once server access exists.
- **Never blend Mode A and Mode B into one Reverse-SQL number.**
- **BI script coverage:** share of script statements translated without the `untranslated` flag (target ≥ 90%), per app.
- **Cross-group reuse:** count of `bi_model_link` facts accepted as `foreign_key` evidence, and lineage facts sourced from `bi_transform`.
- **KG acceptance:** R1–R15 green, with the engine's existing tests green; R12 latency targets met at M4.
- Cost per dataset and per report from Langfuse; per-agent latency and cost.

**BI gold set** (owned by Evaluation & Domain): ≥2 Qlik apps, ≥10 visuals spanning T1–T3, hand-verified SQL for at least the T1–T2 visuals, and engine outputs captured at a fixed `reload_time`.

---

## 8. Proposed repository layout

```
agentic-data-context-engine/
  docs/                      architecture.md, adr/, rfcs/, change-proposals/ (CP-001…), eval-reports/
  packages/
    adce-core/               AgentSpec, BaseAgent, fact & event schemas, clients
      bi_ir/                 BI IR models, ExprAST (shared by Mode A and Mode B)
      kg_port/               KG engine port (interface) + MockKG
      data_port/             data-source port: DremioSource (Flight SQL/ADBC, catalog API, sys.jobs_recent), FileSource, FixtureSource
    kg-engine/               Brahmastra imported via git subtree and evolved: core/ (stores, facts, projection, isolation, search, resolution, traversal, policies) + apps/memory/ (optional notes/meetings/sessions/code features)
  services/
    kg-mcp/                  FastMCP app on kg-engine: tools/, policy/, validator/, gate/, context/
    model-gateway/           routes, budgets, privacy (Presidio), batch, tracing
    hitl-service/            modes, routing, queue, feedback learning
    orchestrator/            LangGraph supervisor + subgraphs, Mode A/B selection, event consumers
    api-gateway/             FastAPI, auth, uploads, exports
    agents-ingestion/        intake_router, schema_parser, doc_extractor, entity_linker, scope_registrar
    agents-profiling/        stats_profiler, semantic_typer, pii_classifier, key_discoverer, domain_classifier
    agents-comparison/       candidate_finder, match_reranker, fk_ind_detector, global_verifier, insight_writer
    agents-definition/       glossary_drafter, refinement_loop, conflict_resolver, lineage_inferrer, dictionary_publisher
    agents-reversesql/       report_parser, metric_mapper, join_planner, sql_synthesizer, query_checker, reconciler
    agents-bi-adapters/      bi_extractor/ (qlik_engine/, qlikview_prj/, reload_logs/), bi_script_translator/, bi_expression_translator/
  apps/workbench/            Next.js + React Flow
  prompts/                   versioned prompts per agent (synced to Langfuse)
  eval/
    gold/                    datasets, keys, joins, definitions, reports
    gold/bi/                 apps/, expected_sql/, engine_outputs/
    kg_acceptance/           R1–R15 executable acceptance suite for kg-engine
    harness/                 per-agent suites, range reporting
  deploy/
    compose/                 profiles: core, agents, ui, auth, obs, dremio-local (+ sample dump loader), bi-fixtures
    helm/                    one chart for staging, our hosted production and client installs
    config/                  config schema (validated at startup), env examples per environment
    keycloak/                realm export, IdP federation templates
  migrations/                alembic/ (Postgres), cypher/ (Neo4j), upgrade-path tests
```

---

## 9. Risks (additions to proposal section 16)

| Risk | Mitigation |
|---|---|
| Parallel squads drift from each other | Frozen v1 contracts, contract tests in CI against the mock KG, an architecture group that owns RFCs, integration milestones every 2 weeks |
| Graph and Postgres drift out of sync | Outbox + idempotent fact IDs + nightly reconciliation |
| Gold set late | Blocks evaluation for every squad. Domain experts committed in week 1; Evaluation squad owns delivery by M0. |
| Thinking-always-on models cost more than estimated (and no Anthropic Batch discount on Vertex) | Per-route `effort`, Haiku for simple routes, prompt caching, memoization, per-agent budgets, weekly cost review; re-estimate on Vertex pricing in M0 |
| Reverse-SQL search explodes on complex reports | Mode B: bounded variant budgets; POC scope is single-statement aggregate SQL. **Mode A scope is set by construct tiers. T1:** plain aggregates, filters, simple script transforms. **T2:** set analysis with literal modifiers, `TOTAL`, `Above`, `ApplyMap`, `IntervalMatch`. **T3:** `Aggr`, nested or dynamic set expressions, dollar-expansion-driven logic, `Peek`-dependent load-order logic. **The POC commits to T1–T2; T3 is attempted and reported "not resolved" when it fails.** |
| Event storms with 29 consumers | Per-upload batching, per-entity debounce, consumer-group backpressure, priority lanes (PII first) |
| Keycloak/MCP auth complexity slows squads | Dev realm with pre-provisioned service accounts from M0; enforcement on at M3 |
| **Silent double counting from Qlik's grain-aware aggregation** | Query-checker grain rule + pre-aggregation, plus engine-based reconciliation on ≥3 slices |
| **QVD chains span several apps**, so a single app's SQL stops at a QVD | QVD as `:Dataset`; BI extractor registers `READS`/`WRITES` across all apps in scope; scope registration covers the whole chain; an unresolved upstream QVD → "not resolved", with the missing app named |
| **BI snapshot staleness** vs live source data | Pin reconciliation to `reload_time`; record the gap as a tolerance reason |
| **No Engine API access** at the client (files only) | Degrade: QlikView `-prj` or exported `.qvf` gives logic without ground truth, so Mode A translation + Mode B-style reconciliation against exported report files |
| **Undocumented Qlik syntax variants** break the parser | Grammar based on `GetBaseBNF`; statement-level fallback to `kind: other` + LLM; untranslated-statement rate tracked as a metric |
| **BI model encodes wrong or legacy joins** | `documented` trust only; disagreements with confirmed joins → conflict, never overwrite |
| **KG engine built for a different domain and scale** (notes → concepts) | Planned refactor with R1–R15 as acceptance; R12 load test in M0 week 2 and again at M4; squads isolated from the refactor by `kg_port` + MockKG |
| **Refactor breaks what already works** (resolution, search, isolation) | Import with history; the engine's ~950 existing tests stay green on every PR; the fact-model change lands behind `kg_port` with a projection-replay test |
| **Engine LLM calls leak client data to a non-approved provider** | R11: the core has one LLM client that only talks to `model-gateway`; direct providers live only in the memory app; acceptance suite asserts no other outbound LLM traffic |
| **Rebuilds wipe human decisions** | R4: the fact log is SOURCE and Neo4j is only a projection; rebuild = replay; tested in acceptance |

---

## 10. Open decisions

All unanswered questions are tracked in **[`open-questions.md`](open-questions.md)**, with a default, a deadline group and what each one blocks. Answered questions move to its *Resolved* table.

Blocking before week 1:
- **Q-01** pilot domain and data
- **Q-02** Brahmastra code
- **Q-03** what "Google API" means (Claude on Vertex vs Gemini)
- **Q-04** GCP project, region and quota
- **Q-05** Dremio access for development
- **Q-06** Dremio edition and permissions
- **Q-07** which Qlik inputs we receive

---

## 11. Non-goals (binding, from CP-001)
- Building Power BI or Tableau adapters in the POC. Only the IR and the adapter interface are designed for them.
- Recreating Qlik dashboards or layouts in another BI tool. The output is SQL + KG facts, not visuals.
- Writing back to, reloading or modifying any client BI app.
- Translating Section Access into executable SQL. It is captured as an RLS requirement only.
- Guaranteeing T3 constructs.

---

## Sources
- DBAutoDoc: https://arxiv.org/pdf/2603.23050
- LLM-FK: https://arxiv.org/html/2603.07278
- Magneto: https://arxiv.org/abs/2412.08194 · https://github.com/VIDA-NYU/magneto-matcher
- Scalable Join Inference: https://arxiv.org/html/2603.04176v1
- Graphiti (Thoughtworks Radar): https://www.thoughtworks.com/en-us/radar/platforms/graphiti
- LangGraph Postgres checkpointer: https://pypi.org/project/langgraph-checkpoint-postgres/1.0.6 · interrupt caveats: https://rentierdigital.xyz/blog/human-in-the-loop-langgraph · checkpoint cost: https://pub.towardsai.net/langgraph-checkpointing-is-not-free-a-production-postmortem-398bc86861f4
- FastMCP: https://www.agenticwire.news/article/fastmcp-vs-mcp-python-sdk
- Langfuse self-hosting: https://langfuse.com/self-hosting
- Neo4j versioning: https://neo4j.com/docs/operations-manual/current/introduction/
- Kuzu archival / LadybugDB: https://www.puppygraph.com/blog/what-is-kuzudb
- DuckDB releases: https://en.wikipedia.org/wiki/DuckDB
- Atlan/Collibra 2026 positioning: https://www.modern-datatools.com/compare/atlan-vs-collibra
- Claude model pricing and API behavior: Anthropic API docs (models table, cached 2026-09-25)
- CP-001: BI Source Adapters (Qlik first) and Two-Mode Reverse-SQL (docs/change-proposals/CP-001-bi-source-adapters-qlik.md)
