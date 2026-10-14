# Agentic Dataset Context Engine: Architecture & Build Plan

Status: Draft v0.3 (2026-10-08).
**Full proposal scope:** 29 agents in 5 groups plus an orchestrator (26 from the proposal + 3 BI source-adapter agents, CP-001), all six stores, Keycloak, Langfuse and every export format.
Based on the proposal "Agentic Dataset Context Engine - Proposal" (Sept 2026), with every key claim checked against current sources.
Audience: the engineering squads building the POC, plus the sponsor.

**Change log**
| Version | Change |
|---|---|
| v0.1 | First analysis of the proposal (reduced scope, superseded) |
| v0.2 | Full proposal scope restored; parallel squads |
| v0.3 | **CP-001 merged** (BI source adapters, Qlik first; two-mode Reverse-SQL). **ADR-12:** reuse the existing in-house KG engine (Brahmastra) instead of building the KG from scratch. |

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
| 7 | **The KG is planned as a from-scratch build** | We already have an in-house KG engine (Brahmastra). Rebuilding it wastes the Platform squad's first weeks. | Integrate it behind a KG engine port and validate it with a conformance suite in week 1 (ADR-12). |

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

- **One monorepo, multiple deployables.** Each agent group is a separate worker service owned by one squad. All services share the `adce-core` library (agent contract, fact schemas, event schemas, BI IR, KG engine port, clients).
- Every agent connects two ways to `kg-mcp` over MCP streamable HTTP. Agents compute on data through DuckDB (reading Parquet from MinIO) and **record knowledge only through `kg-mcp`**.
- `agents-bi-adapters` is its own worker because it holds BI-server credentials and has different scaling needs (long-lived WebSocket sessions).
- **BI adapters connect to BI servers read-only** through a dedicated connector service account. BI-engine output (hypercube data) is treated as raw data: it goes to MinIO as Parquet and passes the ADR-8 privacy boundary before any LLM sees it.

---

## 4. Architecture decisions (ADRs)

### ADR-1: Contracts first so squads can work in parallel
In weeks 1-2 the Platform squad freezes five versioned contracts, all published as Pydantic models and JSON Schema in `adce-core`:
1. **Agent contract (`AgentSpec`).** Name, version, group, kind (LLM / hybrid / deterministic), subscribed event types, allowed KG tools, emitted fact kinds, HITL mode, model route, and budget.
2. **Fact payload schemas**, one per fact kind (ADR-3).
3. **Event schemas** (`upload.received`, `profile.ready`, `fact.proposed`, `fact.confirmed`, `conflict.flagged`, `bi_ir.ready`, …; full list in section 5.8).
4. **BI IR (`bi_ir` v1).** A tool-neutral representation of BI report logic, produced by every BI adapter and consumed by the translators, Metric mapper, Join planner and Reconciler (ADR-11). The mock KG ships BI IR fixtures (at least two Qlik apps covering tiers T1–T2), so the Reverse-SQL squad can build Mode A from week 1.
5. **KG engine port (`kg_port` v1).** The interface `kg-mcp` uses to reach the KG engine (ADR-12). Both the Brahmastra adapter and the mock KG implement it.

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
- `kg-mcp` is a FastMCP 3.x service sitting on the existing KG engine through `kg_port` (ADR-12). Each write runs **policy → validator → confidence/impact gate → commit + audit + event**, as in proposal Fig. 2.
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
| Postgres | HITL queue, append-only audit log, outbox, LangGraph checkpoints, users/roles mirror | Audit table is insert-only. |
| MinIO | Raw uploads (versioned), normalized Parquet, original documents, BI IR artifacts, BI engine outputs, published dictionaries | Raw data never enters Neo4j. |
| DuckDB | Embedded compute in every agent worker over Parquet in MinIO | Nothing persistent. |
| Redis | **Event bus (Redis Streams with one consumer group per agent)**, context-pack cache, rate limits | Also required by Langfuse. |
| Langfuse | Traces, cost, prompt versions, evaluation datasets | Self-hosted v3. |

- **Consistency:** `kg-mcp` writes the audit and outbox rows in one Postgres transaction with an idempotent `fact_id`, then writes the graph through the KG engine, then a relay publishes the outbox to Redis Streams. A nightly job reconciles the graph against the audit log.
- **Event storms:** with 29 consuming agents, events are batched per upload and debounced per entity; the global verifier runs debounced per scope.

### ADR-7: Model gateway service
`model-gateway` is a small internal service that wraps the Anthropic SDK. It handles:
- route → model + effort
- Presidio masking (ADR-8)
- prompt caching
- structured outputs
- Batch API for bulk work
- per-run and per-task budgets with retry caps
- refusal fallback
- Langfuse tracing with prompt and model version stamping

Bedrock (Mantle), Vertex and Foundry clients are selected by config per deployment.

| Route | Model | Effort |
|---|---|---|
| Intake tie-breaks, semantic types, domain tags | Haiku 4.5 | none |
| Doc extraction, linker/key/FK adjudication, match rerank, insights, glossary, refinement, conflicts, lineage, report parsing, metric mapping, **BI expression translation (T1–T2), BI script long-tail translation** | Sonnet 5.5 | low-medium |
| Global verifier, SQL synthesis, query repair, mismatch diagnosis, **BI T3 expression translation (Aggr, nested set analysis), Mode A mismatch diagnosis** | Opus 5.5 | medium-high |
| Bulk glossary drafting, bulk doc extraction | Sonnet 5.5 via Batch API (−50%) | low |

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
- **KG engine conformance** (ADR-12) is part of the same harness.

### ADR-11: BI source adapters and two-mode Reverse-SQL (CP-001)
**Context.** Reverse-SQL v0.2 infers SQL from report outputs. When a report comes from a BI tool, its logic and a computation engine are available, and translation is far more reliable than inference.

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

### ADR-12: Reuse the existing KG engine (Brahmastra) behind a port
**Context.** The team already has an in-house knowledge-graph engine, Brahmastra, built as a separate project. It is being updated, and its latest state will be shared later. Building the KG layer from scratch would duplicate it. Brahmastra was built for a different domain (notes to concepts), though, so how well it fits ADCE has to be **measured, not assumed**.

**Decision.**
1. **`kg-mcp` is built on Brahmastra, not from scratch.** ADCE-specific behaviour (data-catalog ontology, fact/trust/bi-temporal layer, policy, validator, HITL hooks, context assembler) is either **contributed upstream** to Brahmastra, if it is generic, or lives in an **ADCE layer** inside `kg-mcp`. The architecture group decides each gap.
2. **ADCE depends on a port, not on engine internals.** `kg_port` (ADR-1 #5) is a narrow interface in `adce-core` (entity CRUD, fact propose/supersede/invalidate, traversal, hybrid search, point-in-time read, isolation-bound session). It has two implementations: `BrahmastraAdapter` and `MockKG`. Agents never import Brahmastra.
3. **A conformance suite decides fitness.** It is an executable version of requirements R1–R15 below, run against the latest Brahmastra in week 1. It produces a gap report: each requirement is *met*, *met via ADCE layer*, *upstream change needed*, or *blocker*. The suite then runs in **both** repos' CI, so an upstream change cannot silently break ADCE.
4. **The engine's version is pinned**, and upgrades happen only after a green conformance run.

**Requirements on the KG engine (R1–R15):**

| # | Requirement | Used by |
|---|---|---|
| R1 | **Pluggable ontology profile.** The data-catalog labels and relations in ADR-3, including BI nodes, are a closed, validated vocabulary per profile. | All |
| R2 | **Reified `:Fact` nodes** with trust, evidence, supersedes, conflicts and feedback, plus materialized current-view edges | All writers, HITL |
| R3 | **Bi-temporal.** Valid and transaction time; invalidate, never delete; point-in-time queries | Audit, refinement loop, reconciliation |
| R4 | **Authority tiers.** Uploads, documents, human decisions and confirmed/documented facts are system-of-record and can **never be wiped by a rebuild or reprocess**. Only caches (scores, clusters, context packs) are rebuildable. | HITL, publisher |
| R5 | **Tenant + scope isolation enforced in the store, failing closed** (a query without a tenant binding is refused) | Policy engine, security |
| R6 | **Hybrid search** (lexical + vector) over entities, documents, terms and metrics | `search_context`, linker, metric mapper |
| R7 | **Traversal.** Neighbours, trust-weighted path finding, lineage traversal across QVD chains | Join planner, lineage, context assembler |
| R8 | **MCP tool surface** for the 16 tools + read-only Cypher, with per-caller allow-lists and OAuth (FastMCP 3.x or a decided equivalent) | All agents, external tools |
| R9 | **Write-path hooks**: policy → validator → gate → commit + audit + outbox event | `kg-mcp` |
| R10 | **Postgres-backed audit and outbox** with idempotent fact IDs | Consistency (ADR-6) |
| R11 | **No direct LLM calls to non-approved providers.** Any LLM use inside the engine (extraction, summaries, GraphRAG) must route through `model-gateway`, or be disabled in ADCE deployments | Privacy (ADR-8) |
| R12 | **Scale.** Targets to confirm: ≥ 1M fact versions per tenant; p95 `get_context_pack` < 500 ms; p95 `find_join_paths` < 1 s at 10k columns | All |
| R13 | **Entity resolution and linking primitives** (mentions → entities, with merge evidence) | Entity linker, candidate finder |
| R14 | **Conflict detection** on functional fact kinds (e.g. one current PK per dataset per scope) | Global verifier, conflict resolver |
| R15 | **Configurable embeddings** (Voyage / bge-m3 / local), with vector index rebuild tooling | Search, matching |

**Consequences.**
- The Platform & KG squad's M0 work changes from building a KG to **integrate, run conformance and close gaps**.
- The mock KG and the real engine share one port, so squads are unaffected by gap-closing.
- Upstream contributions benefit Brahmastra as a product too.

**Alternatives considered.**
- (a) Build from scratch: wastes existing work.
- (b) Call Brahmastra's HTTP API as an opaque service: it has no fact/trust semantics, and isolation would be enforced in two places.
- (c) A hard fork: it diverges fast. Only fall back to this if upstream and ADCE needs conflict (open decision 10).

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
| 26 | Reconciler | H: DuckDB execution → cell-level diff → Opus mismatch diagnosis; may return "not resolved". **Mode B:** ≥2 periods/slices from report uploads. **Mode A:** ground truth = `GetHyperCubeData` for the visual, across **≥3 engine-applied selection slices** + the unfiltered default; runs SQL on data as of the app's `reload_time` (or the closest snapshot) and records any snapshot gap as a tolerance reason. | `sql.checked` | `propose_fact` | `report_sql` + validated report lineage | Gate: approve or pick SQL |

Query checker (25) and Reconciler (26) are **shared by both modes** and owned by one named lead.

### 5.6 Orchestrator
- A LangGraph 1.x supervisor with one subgraph per group, checkpointed in Postgres.
- It plans runs, fans comparison out in parallel per dataset pair, **selects Mode A / Mode B / hybrid per report**, consumes Redis Streams events so it re-runs only affected steps, enforces per-run budgets, and hosts Ask interrupts.

### 5.7 BI source-adapter agents (CP-001)
Deployable `agents-bi-adapters`; Reverse-SQL group, sub-group `bi_adapters`; owned by the Mode A sub-team.

| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 27 | BI extractor | D: Qlik Engine API client (Sense/Cloud: `OpenDoc`, `GetScript`, `GetTablesAndKeys`, `GetAllInfos`→`GetProperties`, MeasureList/DimensionList, `GetVariables`, `GetHyperCubeData`), `-prj` XML parser (QlikView), reload-log parser for variable resolution; emits BI IR; exports hypercube data for registered visuals to MinIO Parquet. Read-only: never writes, reloads or saves. | `upload.classified[bi_app]`, `bi_app.reloaded` (poll `reload_time`) | `register_entity`, `add_context`, `get_source_context` | BIApp/Visual/Metric nodes, QVD `:Dataset` nodes, `READS`/`WRITES`, BI IR artifact, `bi_model_link` (documented) | Confirm app scope; confirm cross-app QVD linkage |
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
| Knowledge graph | **Existing KG engine (Brahmastra) behind `kg_port`** on Neo4j 5.26 LTS (or 2026.x) + APOC | Conformance suite decides gaps (ADR-12) |
| KG tool server | **FastMCP 3.x** for `kg-mcp` | Align with the MCP framework the engine uses (open decision 11) |
| LLMs | Anthropic SDK via `model-gateway`: Haiku 4.5, Sonnet 5.5, Opus 5.5 | |
| Embeddings | Voyage AI / self-hosted bge-m3 / engine's local model, behind one interface | R15 |
| Relational | Postgres 16 (pgvector image) | |
| Object store | MinIO | |
| Compute | DuckDB 1.5.x + Polars | Parquet canonical |
| Matching & keys | datasketch, RapidFuzz, DuckDB checks | |
| SQL | sqlglot | Also Mode A dialect transpile |
| **BI (CP-001)** | Qlik Engine API over WebSocket (JSON-RPC; e.g. `websockets` + typed client or `enigma`-style wrapper), `qlik-cli` (Cloud only), lxml for `-prj` XML, **a Qlik script grammar** (Lark/PEG, using `GetBaseBNF` as reference) | Read-only service user per tenant |
| Documents | Claude PDF/vision, openpyxl, python-docx, WeasyPrint | |
| Privacy | Microsoft Presidio | |
| Data quality | Great Expectations | |
| Events | Redis Streams (outbox relay from Postgres) | |
| Observability | Langfuse v3 self-hosted + OpenTelemetry → Grafana/Prometheus | |
| Exports | OpenLineage, dbt YAML, Excel, Markdown, PDF | |
| Deployment | Docker Compose (dev/POC), **Helm charts built in parallel** | |
| CI | GitHub Actions: lint, type, unit, contract tests vs mock KG, **KG conformance suite**, gold-set eval on prompt/model changes | |

---

## 7. Team structure and plan (parallel squads, 12 weeks)

### 7.1 Squads
| Squad | Owns | Suggested size |
|---|---|---|
| **Platform & KG integration** | `kg_port`, Brahmastra adapter, conformance suite, ADCE KG layer (facts/trust/bi-temporal where not upstream), upstream contributions, `kg-mcp` (tools, policy, validator, context assembler), stores, outbox/events, Keycloak, Compose + Helm, CI | 3-4 |
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
| 1-2 | **M0: Contracts & foundation** | `AgentSpec`, fact/event schemas, **BI IR v1** and **`kg_port` v1** frozen. Mock KG + fixtures released (incl. ≥2 Qlik apps, T1–T2). **Week 1: latest Brahmastra received; conformance suite run; gap report.** **Week 2: gap decisions made (upstream vs ADCE layer vs blocker); Brahmastra adapter passes read tools + `propose_fact` + audit/outbox.** Compose stack up (Neo4j, Postgres, MinIO, Redis, Langfuse, Keycloak). Model gateway with Presidio. Gold set v1. BI connector credentials and read-only access agreed with the client (or file-only fallback decided). Agent skeletons pass contract tests. |
| 3-4 | **M1: First real slice** | Ingestion 1-5 and Profiling 6-10 live on the real KG (through the adapter). HITL Gate/Queue/Seed live. Workbench review queue + entity view. Upload → profile → PK gated → confirmed, visible in graph and audit. **BI extractor (27) live against a real Qlik app: BI IR + hypercube export + KG registration incl. QVD chains. Script translator (28) translating embedded SQL + T1 transforms.** Mode B agents 21-23 working on fixtures. |
| 5-6 | **M2: Relate & compound** | Comparison 11-15 live. Glossary drafter + dictionary publisher (MD/Excel). Documents before and after data both work. Event-driven re-runs live. **Reverse-SQL Mode A end-to-end on ≥3 real BI visuals (T1–T2) with engine-based reconciliation.** **Mode B first end-to-end on 2 gold reports.** `bi_model_link` facts consumed by FK/IND detector (13). Workbench graph explorer. KG conformance: all "upstream change needed" gaps closed or carried by the ADCE layer. |
| 7-8 | **M3: Full pipeline** | Global verifier, refinement loop, conflict resolver, lineage inferrer. Mode B 24-26 with multi-period validation. **Expression translator (29) T2 complete; BI semantics rules in the Query checker; BI lineage consumed by (19); BI master-item definitions consumed by (16); hybrid mode (BI export PDF → Mode A).** Ask + Correct modes. Feedback-as-examples. Keycloak roles enforced everywhere. |
| 9-10 | **M4: Complete outputs** | All exports. External MCP access with OAuth. Impact-ranked queue. Budgets and cost dashboards. Helm deploy to staging. **BI T3 attempts; BI-adapter interface documented for Power BI/Tableau (no implementation).** KG scale test against R12 targets. |
| 11-12 | **M5: Hardening & evaluation** | Full gold-set evaluation, **Mode A and Mode B reported separately**, worked example (proposal section 10.2) replayed, security/privacy review, load test (event storms with 29 consumers), final report with cost per dataset and per report. |

### 7.3 Success metrics
- PK F1 ≥ 90%; FK F1 ≥ 80% within a source; cross-source match precision reported per dataset with a normalization grade.
- Review items per new dataset from the same source decline across M1 → M5.
- Auto-accepted facts later reversed by humans stay below 5%.
- Late-arriving documents update affected facts with no manual re-run.
- **Reverse-SQL Mode B:** share of gold reports *exact*, *within tolerance* and *not resolved*.
- **Reverse-SQL Mode A:** share of in-scope BI visuals (T1–T2) *exact* / *within tolerance* / *not resolved* against engine ground truth across ≥3 slices. **Target ≥ 70% exact-or-within-tolerance on T1–T2 by M5.**
- **Never blend Mode A and Mode B into one Reverse-SQL number.**
- **BI script coverage:** share of script statements translated without the `untranslated` flag (target ≥ 90%), per app.
- **Cross-group reuse:** count of `bi_model_link` facts accepted as `foreign_key` evidence, and lineage facts sourced from `bi_transform`.
- **KG conformance:** R1–R15 green on the pinned engine version; R12 latency targets met at M4.
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
  integrations/
    brahmastra/              BrahmastraAdapter (implements kg_port), ADCE ontology profile, version pin
  services/
    kg-mcp/                  FastMCP server: tools/, policy/, validator/, context/, adce_layer/ (facts, trust, bi-temporal where not upstream)
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
    kg_conformance/          R1–R15 executable suite (also run in the Brahmastra repo's CI)
    harness/                 per-agent suites, range reporting
  deploy/                    compose/, helm/, keycloak realm export
```

---

## 9. Risks (additions to proposal section 16)

| Risk | Mitigation |
|---|---|
| Parallel squads drift from each other | Frozen v1 contracts, contract tests in CI against the mock KG, an architecture group that owns RFCs, integration milestones every 2 weeks |
| Graph and Postgres drift out of sync | Outbox + idempotent fact IDs + nightly reconciliation |
| Gold set late | Blocks evaluation for every squad. Domain experts committed in week 1; Evaluation squad owns delivery by M0. |
| Thinking-always-on models cost more than estimated | Per-route `effort`, Haiku for simple routes, prompt caching, Batch API, per-agent budgets, weekly cost review |
| Reverse-SQL search explodes on complex reports | Mode B: bounded variant budgets; POC scope is single-statement aggregate SQL. **Mode A scope is set by construct tiers. T1:** plain aggregates, filters, simple script transforms. **T2:** set analysis with literal modifiers, `TOTAL`, `Above`, `ApplyMap`, `IntervalMatch`. **T3:** `Aggr`, nested or dynamic set expressions, dollar-expansion-driven logic, `Peek`-dependent load-order logic. **The POC commits to T1–T2; T3 is attempted and reported "not resolved" when it fails.** |
| Event storms with 29 consumers | Per-upload batching, per-entity debounce, consumer-group backpressure, priority lanes (PII first) |
| Keycloak/MCP auth complexity slows squads | Dev realm with pre-provisioned service accounts from M0; enforcement on at M3 |
| **Silent double counting from Qlik's grain-aware aggregation** | Query-checker grain rule + pre-aggregation, plus engine-based reconciliation on ≥3 slices |
| **QVD chains span several apps**, so a single app's SQL stops at a QVD | QVD as `:Dataset`; BI extractor registers `READS`/`WRITES` across all apps in scope; scope registration covers the whole chain; an unresolved upstream QVD → "not resolved", with the missing app named |
| **BI snapshot staleness** vs live source data | Pin reconciliation to `reload_time`; record the gap as a tolerance reason |
| **No Engine API access** at the client (files only) | Degrade: QlikView `-prj` or exported `.qvf` gives logic without ground truth, so Mode A translation + Mode B-style reconciliation against exported report files |
| **Undocumented Qlik syntax variants** break the parser | Grammar based on `GetBaseBNF`; statement-level fallback to `kind: other` + LLM; untranslated-statement rate tracked as a metric |
| **BI model encodes wrong or legacy joins** | `documented` trust only; disagreements with confirmed joins → conflict, never overwrite |
| **KG engine built for a different domain and scale** (notes → concepts) | Conformance suite R1–R15 in week 1; gaps triaged in week 2; R12 scale test at M4; the port lets the ADCE layer carry gaps without blocking squads |
| **Engine changes upstream break ADCE** | Pinned version, conformance suite in both repos' CI, upgrades only on green |
| **Engine's own LLM features leak client data to a non-approved provider** | R11: route through `model-gateway` or disable in ADCE deployments; verified by the conformance suite (no outbound LLM calls except via the gateway) |
| **Engine rebuild semantics wipe human decisions** | R4 authority tiers: confirmed and documented facts are system-of-record and never rebuilt; tested in conformance |

---

## 10. Open decisions (need an owner)

1. **Pilot domain and the two sources.** Do the reports with known SQL actually exist? (Partly de-risked by the BI gold set.)
2. **Hosting.** Which cloud or on-prem; Anthropic API direct, or Bedrock/Vertex from day 1?
3. **Neo4j Community vs Enterprise.** Enterprise is needed for per-tenant databases and RBAC.
4. **Embeddings.** Voyage vs self-hosted bge-m3 vs the KG engine's current local model.
5. **Squad sizes and leads**, mapped onto section 7.1.
6. **BI estate inventory.** Which tools and versions (QlikView / client-managed Qlik Sense / Qlik Cloud), roughly how many apps, and are QVD extractor/transform apps in scope with the dashboards?
7. **BI access model.** Is read-only Engine API access available (service user / API key per tenant), or only exported files? This decides whether engine-based reconciliation is possible.
8. **Target SQL dialect(s)** for generated SQL (e.g. the client warehouse). Is DuckDB execution on Parquet extracts enough for the POC, or must SQL also run on the target engine?
9. **Default selection state per visual.** Does the report owner define what "the report" means with no selections (bookmarks, default filters)?
10. **KG engine consumption model.** Pinned package dependency (recommended), vendored copy, or fork? Who owns upstream Brahmastra, and how are ADCE-driven changes reviewed there?
11. **MCP framework alignment.** `kg-mcp` on FastMCP 3.x vs whatever the latest Brahmastra MCP server uses. Decide after reviewing the latest engine.

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
