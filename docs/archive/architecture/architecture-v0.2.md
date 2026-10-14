# Agentic Dataset Context Engine: Architecture & Build Plan

Status: Draft v0.2 (2026-10-08). **Full proposal scope:** 26 agents in 5 groups plus an orchestrator, all six stores, Keycloak, Langfuse and every export format. Based on the proposal "Agentic Dataset Context Engine - Proposal" (Sept 2026), with every key claim checked against current sources.
Audience: the engineering squads building the POC, plus the sponsor.

---

## 1. Verdict on the proposal

The problem is real, the positioning ("feed the catalog, don't replace it") makes sense, and the core technical bet holds up: **statistics generate candidates and the LLM adjudicates and explains**. The research the proposal cites exists and backs this up.

Scope stays as proposed. What changes is **how** it gets built and a few technical corrections:

| # | Issue | Why it matters | Fix |
|---|---|---|---|
| 1 | **Phases go one agent group at a time (horizontal)** | Nothing runs end to end until week 12, and squads would wait on each other. | All five agent groups are built **in parallel** by dedicated squads against frozen contracts and a mock KG server, with integration milestones every two weeks (section 7). |
| 2 | **Reverse-SQL, the go-to-market headline, is built last (weeks 11-12)** | If it slips, the POC fails to prove the claim we sell on. | The Reverse-SQL squad starts in week 1 and reaches its first end-to-end run on gold reports by week 6. |
| 3 | **"Low temperature" as the determinism mitigation** | Current models reject `temperature`/`top_p` (HTTP 400 on Opus 5.5, Sonnet 5.5 and Sonnet 5). | Use structured outputs, `strict` tools, pinned model and prompt versions, per-route `effort`, and a gold-set regression run. |
| 4 | **"Redis not needed in POC"** | Self-hosted Langfuse v3 itself requires Postgres, **ClickHouse, Redis and S3/MinIO**. | Run Redis and MinIO from day 1 and use Redis Streams as the event bus (ADR-6). |
| 5 | **Key-accuracy target ">90%" for all keys** | Published hybrid join inference gets 78-100% precision on well-structured schemas and notably worse on poorly normalized ones. | Separate targets: PK F1 ≥ 90%, FK F1 ≥ 80% within a source, cross-source matches reported per dataset with a normalization grade. |

Smaller corrections:
- **Models.** Sonnet 5 → **Sonnet 5.5** (same $2/$10 price). Opus 5.5 is $4/$20 and confirmed. On Opus 5.5 and Sonnet 5.5, thinking cannot be disabled and forced `tool_choice` returns a 400, so we use structured outputs instead. Opus 5.5's default effort is `medium`, so we set effort explicitly per route. The proposal's cost-table arithmetic is correct.
- **Neo4j "5".** Neo4j moved to calendar versioning (2025.01+). Pin **5.26 LTS** or a current 2026.x release. Community Edition has a single user database, so tenant isolation in the POC is a property or label. Multi-database per tenant needs Enterprise (decision 3 in section 10).
- **Graphiti.** The bi-temporal model is a good idea to borrow, but Graphiti itself is pre-1.0 and spends an LLM call on every write to extract facts from text. Our facts are already structured, so we **implement the bi-temporal fact model ourselves**.
- **Kuzu** (an embedded graph DB) was archived in Oct 2025. Do not adopt it.
- **ZDR.** Opus 5.5 and Sonnet 5.5 are fine under zero data retention. Fable-tier models need 30-day retention, so keep them out of the client-deployment plan.

---

## 2. Research check (what the evidence actually supports)

| Claim in proposal | Verified | Design consequence |
|---|---|---|
| DBAutoDoc: statistics + iterative LLM refinement; human ground truth separate from seed context | ✅ arXiv 2603.23050. **The deterministic pipeline adds +23 F1 points over LLM-only FK detection.** | Key/FK agents are hybrid: deterministic gates first, LLM adjudication second. Two knowledge tiers: *confirmed* (immutable) and *seed* (soft). |
| LLM-FK: 4 agents (profiler/interpreter/refiner/verifier), F1 > 93% | ✅ arXiv 2603.07278, Findings of ACL 2026. Cuts the candidate space by 2-3 orders of magnitude. | Unique-key-driven candidate pruning in the FK/IND detector; the global verifier enforces schema-wide consistency. |
| Magneto: cheap retrieval → LLM rerank | ✅ PVLDB 18(8); code at VIDA-NYU/magneto-matcher | Candidate finder + match reranker. Reuse their prompts and benchmark. |
| Join inference at scale | ✅ arXiv 2603.04176. PK candidates → LLM adjudication → IND detection → LLM adjudication; query history refines results. | Same pipeline shape. **If query logs exist, ingest them as a context input**: they are the strongest join signal. |
| Query reverse engineering [5][6] | ✅ established research area (QRE, FastQRE, SQUARES) | Hypothesize, then bounded variant search, then execute-and-diff over several periods. |
| data.world KG accuracy studies [7][8] | ✅ | The query checker repairs against the ontology; "not resolved" is an allowed outcome. |
| Graphiti bi-temporal memory | ✅ (Thoughtworks Radar "Trial", Apr 2026; pre-1.0) | Copy the model, not the dependency. |
| LangGraph interrupts + Postgres checkpointer | ✅ with caveats: interrupts during parallel fan-out are poorly defined, and every step writes a checkpoint blob | HITL Gate/Queue are stored as data; only Ask uses `interrupt()` (ADR-5). |
| FastMCP | ✅ v3.x (Prefect), adds OAuth, OpenTelemetry and server composition | The KG MCP server is built on FastMCP 3.x. |

---

## 3. System architecture (as proposed, made concrete)

Six layers, unchanged from proposal Figure 1:

| Layer | Components | Deployable services |
|---|---|---|
| Interfaces | Review workbench, API gateway, external MCP + catalog exports | `workbench` (Next.js), `api-gateway` (FastAPI), Keycloak |
| Ingestion agents | Intake router, Schema parser, Doc extractor, Entity linker, Scope registrar + HITL | `agents-ingestion` |
| Control | Model gateway, Orchestrator (LangGraph supervisor), HITL service | `model-gateway`, `orchestrator`, `hitl-service` |
| Agent groups | Profiling, Comparison, Definition, Reverse-SQL (each with a HITL gate) | `agents-profiling`, `agents-comparison`, `agents-definition`, `agents-reversesql` |
| Knowledge access | KG MCP server: typed tools, policy engine, validator, bi-temporal provenance, context assembler, read-only Cypher | `kg-mcp` |
| Storage | Neo4j, Postgres, MinIO, DuckDB, Redis, Langfuse | managed containers |

**One monorepo, multiple deployables.** Each agent group is a separate worker service owned by one squad. All services share the `adce-core` library (agent contract, fact schemas, event schemas, clients). Every agent connects two ways to `kg-mcp` over MCP streamable HTTP. Agents compute on data through DuckDB (reading Parquet from MinIO) and **record knowledge only through `kg-mcp`**.

---

## 4. Architecture decisions (ADRs)

### ADR-1: Contracts first so squads can work in parallel
In weeks 1-2 the Platform squad freezes three versioned contracts, all published as Pydantic models and JSON Schema in `adce-core`:
1. **Agent contract (`AgentSpec`).** Name, version, group, kind (LLM / hybrid / deterministic), subscribed event types, allowed KG tools, emitted fact kinds, HITL mode, model route, and budget.
2. **Fact payload schemas**, one per fact kind (section 4, ADR-3).
3. **Event schemas** (`upload.received`, `profile.ready`, `fact.proposed`, `fact.confirmed`, `conflict.flagged`, …).

Platform also ships a **mock KG MCP server** with fixtures, so every squad builds and tests against it from week 1. Contract changes go through RFCs reviewed by the architecture group.

### ADR-2: The KG MCP server is the only writer of knowledge
- `kg-mcp` is a FastMCP 3.x service. Each write runs **policy → validator → confidence/impact gate → commit + audit + event**, as in proposal Fig. 2.
- **Policy engine:** per-agent tool allow-lists come from `AgentSpec` and are enforced through Keycloak service-account tokens; scope and tenant isolation are enforced on every query; **only `hitl-service` can call `record_feedback`** and promote a fact to `confirmed`.
- **Validator:** checks that evidence is present, the entity exists, and nothing contradicts a confirmed fact, then re-runs a DuckDB check for data claims (uniqueness, containment, value ranges).
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

(:Fact {id, kind, payload_json, trust: confirmed|documented|validated|inferred,
        confidence, impact, valid_from, valid_to,      // when true in the data
        recorded_at, invalidated_at,                   // when we believed it
        produced_by: "agent@version", prompt_version, model, run_id, tenant_id})
  -[:ABOUT {role}]->(entity)
  -[:EVIDENCED_BY]->(:Evidence {kind: dq_check|doc_span|sample_stat|human|query_log, ref})
  -[:SUPERSEDES]->(:Fact)        -[:CONFLICTS_WITH]->(:Fact)
  -[:HAS_FEEDBACK]->(:Feedback {decision, edit, reason, reviewer, at})

Materialized for traversal (maintained by kg-mcp from current best-trust facts):
(:Column)-[:JOINS_TO {fact_id, trust, cardinality}]->(:Column)
(:Column)-[:SAME_AS|DERIVED_FROM {fact_id, trust}]->(:Column)
(:Column)-[:DEFINED_BY {fact_id}]->(:Term)
(:Metric)-[:COMPUTED_FROM {fact_id}]->(:Column)
```

Fact kinds: `file_type`, `schema_def`, `profile`, `semantic_type`, `sensitivity`, `primary_key`, `foreign_key`, `match`, `domain`, `insight`, `definition`, `lineage`, `metric_mapping`, `report_structure`, `report_sql`, `scope_link`.

**Rules:**
- Confirmed facts are immutable; they can only be superseded by another confirmed fact.
- A disagreement across trust levels creates `CONFLICTS_WITH` plus a review item, never an overwrite.
- Nothing is deleted; old facts are invalidated.
- Point-in-time queries filter on `recorded_at`/`invalidated_at`.
- Embeddings (for Column, Term, Document chunks and Metric) live in the Neo4j vector index.

### ADR-4: All 26 agents share one runtime contract
Every agent, including the deterministic ones, is a class implementing `BaseAgent` from `adce-core`:

```
on_event(evt) → ctx = kg.get_context_pack(task, budget)
              → result = self.work(ctx, duckdb, llm)       # stats, LLM, or both
              → for p in result.proposals: kg.propose_fact(p, evidence)
              → emit trace (Langfuse), metrics, budget usage
```

So every agent is versioned, traced, budgeted, policy-checked and replayable in the same way. Deterministic agents simply never call `llm`. Hybrid agents run their deterministic gate first, and the LLM only sees candidates that survived the gate (DBAutoDoc, LLM-FK).

### ADR-5: HITL service with five modes; HITL is stored as data
The `hitl-service` owns Seed, Ask, Gate, Queue and Correct (proposal section 9), backed by Postgres.
- **Seed:** user-entered facts are written as `confirmed` before a run.
- **Gate/Queue:** routing uses `confidence × impact`, where impact is the number of dependent facts (computed by `kg-mcp`). The queue is sorted by impact × uncertainty. Gated facts stay `proposed`; downstream agents may read them, but marked as untrusted. The run continues, and a decision emits `fact.confirmed`/`fact.rejected`, which re-triggers the affected steps.
- **Ask:** the only mode that uses LangGraph `interrupt()`, and never inside a parallel fan-out. The question appears in the workbench and the graph resumes on the answer.
- **Correct:** a user edit becomes a new confirmed fact that supersedes the old one.
- **Learning:** every `(proposal, decision, edit, reason)` is embedded and retrieved as few-shot examples for that agent and scope (`get_feedback_history`). Agents improve without retraining.

### ADR-6: Stores, consistency and events
| Store | Role | Notes |
|---|---|---|
| Neo4j | Entities, facts, materialized edges, vectors | Single writer: `kg-mcp`. |
| Postgres | HITL queue, audit log (append-only), outbox, LangGraph checkpoints, users/roles mirror | Audit table is insert-only (revoke UPDATE/DELETE). |
| MinIO | Raw uploads (versioned buckets), normalized Parquet, original documents, published dictionaries | Raw data never enters Neo4j. |
| DuckDB | Embedded compute in every agent worker over Parquet in MinIO (httpfs) | Nothing persistent. |
| Redis | **Event bus (Redis Streams with one consumer group per agent)**, context-pack cache, rate limits | Also required by Langfuse. |
| Langfuse | Traces, cost, prompt versions, evaluation datasets | Self-hosted v3 (ClickHouse + Redis + MinIO). |

**Consistency:** `kg-mcp` writes the audit and outbox rows in one Postgres transaction with an idempotent `fact_id`, then writes to Neo4j, then a relay publishes the outbox to Redis Streams. Failures are retried; a nightly job reconciles Neo4j against the audit log.
**Event storms:** events are batched per upload and debounced per entity (proposal risk table). The global verifier runs debounced per scope.

### ADR-7: Model gateway service
`model-gateway` is a small internal service that wraps the Anthropic SDK. It does not use LiteLLM or LangChain chat models, which hide the effort, caching, structured-output and batch features we need. It handles:
- route → model + effort
- Presidio masking (ADR-8)
- prompt caching
- structured outputs
- Batch API for bulk work
- per-run and per-task token/cost budgets with retry caps
- refusal fallback
- Langfuse tracing with prompt and model version stamping

Portability: the Anthropic SDK's Bedrock (Mantle), Vertex and Foundry clients are selected by config per deployment.

Routing defaults (tuned against the gold set):

| Route | Model | Effort |
|---|---|---|
| Intake tie-breaks, semantic types, domain tags | Haiku 4.5 | none |
| Doc extraction, linker/key/FK adjudication, match rerank, insights, glossary, refinement, conflicts, lineage, report parsing, metric mapping | Sonnet 5.5 | low-medium |
| Global verifier, SQL synthesis, query repair, mismatch diagnosis | Opus 5.5 | medium-high |
| Bulk glossary drafting, bulk doc extraction | Sonnet 5.5 via Batch API (−50%) | low |

### ADR-8: Privacy boundary
Presidio (plus custom recognizers for account numbers, national IDs, etc.) runs **inside `model-gateway`**, so no code path can send unmasked samples. Only schemas, statistics and masked samples leave the environment. A `sensitive` column sends statistics only. The PII classifier agent runs before any LLM agent sees a dataset. Downgrading a sensitivity tag requires a human.

### ADR-9: Identity and access
**Keycloak** handles SSO (OIDC/SAML) for users and issues service-account tokens for each agent service.
- **User roles:** viewer, reviewer, data owner, admin.
- **Agent permissions:** each agent gets a client scope that maps to its `AgentSpec` tool allow-list, and `kg-mcp` enforces it.
- **Edge:** the API gateway validates tokens. External MCP access uses OAuth (FastMCP 3 auth).

### ADR-10: Evaluation is a product feature from day 1
- The gold set (keys, joins, definitions, matches, 3-5 reports with known SQL) and an eval harness exist before agents ship.
- Every agent has a per-agent eval, and every prompt or model change runs the regression suite in CI.
- Results go to Langfuse datasets. Before switching model versions, the gold set is re-run (proposal risk table).

---

## 5. Agent catalog (all 26 + orchestrator)

Kind: **D** = deterministic (no LLM), **H** = hybrid (deterministic gate, then LLM), **L** = LLM-led. "Writes" is the fact kind. HITL modes as in proposal section 9.

### 5.1 Ingestion group
| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 1 | Intake router | H: python-magic, charset detection, pandas/openpyxl sheet layout; Haiku for ambiguous types | `upload.received` | `get_source_context`, `register_entity` | Upload node, `file_type` | Confirm type; user sets scope tag |
| 2 | Schema parser | D: sqlglot (DDL, many dialects), dbt YAML, JSON Schema, Avro | `upload.classified[schema]` | `register_entity`, `add_context` | `schema_def` (documented) | Review parse warnings |
| 3 | Doc extractor | L: Sonnet 5.5, PDF input, structured outputs; python-docx/openpyxl preprocessing | `upload.classified[document,dictionary]` | `add_context`, `search_context` | `definition`, `primary_key`, `foreign_key`, rules (documented) | Spot-check samples |
| 4 | Entity linker | H: name similarity, embeddings, value patterns → Sonnet adjudication | `dataset.registered`, `context.added` | `search_context`, `get_source_context`, `link_entities` | `DESCRIBES` links, `scope_link` | Confirm low-confidence links |
| 5 | Scope registrar | D: rules | `upload.scoped`, `context.added` | `register_entity`, `get_source_context` | Source/Schema nodes, placeholders, inheritance | Confirm new source vs merge |

### 5.2 Profiling group
| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 6 | Stats profiler | D: DuckDB/Polars full-data stats, patterns, histograms, drift vs previous load | `dataset.registered`, `dataset.reloaded` | `get_entity`, `propose_fact` | `profile` (versioned) | Exclude columns |
| 7 | Semantic typer | H: regex/rule library → Haiku for the rest | `profile.ready` | `get_context_pack`, `search_context` | `semantic_type` | Correct types |
| 8 | PII classifier | D: Presidio + custom recognizers; runs before any LLM agent | `profile.ready` (priority) | `propose_fact` | `sensitivity` | Any downgrade needs a human |
| 9 | Key discoverer | H: documented/confirmed keys first → uniqueness, naming, type and position scoring incl. composites up to width 3 → Sonnet validation | `profile.ready`, `context.added[keys]` | `get_context_pack`, `get_feedback_history`, `propose_fact`, `flag_conflict` | `primary_key` | Gate: confirm PK |
| 10 | Domain classifier | H: embeddings vs domain vocabularies → Haiku | `semantic_types.ready` | `search_context`, `propose_fact` | `domain` | Confirm when unsure |

### 5.3 Comparison group
| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 11 | Candidate finder | D: RapidFuzz names, embeddings, MinHash LSH value sketches (datasketch); skips confirmed matches | `profile.ready` | `search_context`, `get_neighbors` | candidate sets (Postgres, not facts) | None |
| 12 | Match reranker | L: Sonnet 5.5 over top-k with profiles + context; labels relation type | `candidates.ready` | `get_context_pack`, `get_feedback_history`, `propose_fact` | `match` | Queue: batch approve |
| 13 | FK / IND detector | H: unique-key-driven pruning → DuckDB containment with deterministic gates → Sonnet adjudication; LLM proposals verified statistically | `primary_key.*`, `match.confirmed` | `find_join_paths`, `get_context_pack`, `propose_fact` | `foreign_key` (+ cardinality) | Queue; gate across sources |
| 14 | Global verifier | H: constraint checks (cycles, one-to-one violations, broken chains, type clashes) → Opus 5.5 holistic resolution | `scope.changed` (debounced) | `get_source_context`, `get_neighbors`, `flag_conflict` | conflicts | Resolve conflicts |
| 15 | Insight writer | H: computed overlap, coverage, orphan rows, drift, duplicates → Sonnet narrative | `foreign_key.*`, `match.*`, `profile.drift` | `get_feedback_history`, `propose_fact` | `insight` | Rate or dismiss |

### 5.4 Definition and lineage group
| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 16 | Glossary drafter | L: Sonnet 5.5 (Batch for bulk). Source order: documented text → confirmed definitions of matched columns → profiles | `semantic_types.ready`, `context.added` | `get_context_pack`, `get_feedback_history`, `propose_fact` | `definition`, Terms | Correct: edit |
| 17 | Refinement loop | H: propagates along join paths, DBAutoDoc-style, until convergence (max N passes, change threshold); Sonnet re-drafts; never touches confirmed facts | `definition.*`, `fact.confirmed` | `get_neighbors`, `update_fact` | revised `definition` versions | Approve material changes |
| 18 | Conflict resolver | L: Sonnet 5.5; proposes scoped resolutions (e.g. per department) | `conflict.flagged` | `get_entity`, `get_feedback_history`, `propose_fact` | resolution proposals | Gate: human decides |
| 19 | Lineage inferrer | H: value-derivation checks, load timestamps, documents, reverse-SQL results → Sonnet | `foreign_key.*`, `report_sql.validated` | `get_lineage`, `propose_fact` | `lineage` (`DERIVED_FROM`) | Confirm lineage |
| 20 | Dictionary publisher | D: renders Excel (openpyxl), Markdown, PDF (WeasyPrint), dbt YAML, OpenLineage; Great Expectations suggested rules | `publish.requested`, `fact.confirmed` (auto-republish draft) | `get_source_context` (validated subgraph) | version stamp | Gate: owner sign-off |

### 5.5 Reverse-SQL group
| # | Agent | Kind / model | Triggered by | KG tools | Writes | HITL |
|---|---|---|---|---|---|---|
| 21 | Report parser | L: Sonnet 5.5 PDF + vision (Opus for complex layouts); openpyxl for Excel | `upload.classified[report]` | `get_entity` (earlier versions), `register_entity` | Report/Metric nodes, `report_structure` | Check parsed structure |
| 22 | Metric mapper | H: vector search over columns/terms → Sonnet ranking of columns or expressions | `report.parsed` | `search_context`, `get_feedback_history`, `propose_fact` | `metric_mapping` | Ask when ambiguous |
| 23 | Join planner | D: trust-weighted path search (confirmed joins preferred) | `mapping.ready` | `find_join_paths`, `run_readonly_cypher` | join plan (ephemeral) | None |
| 24 | SQL synthesizer | H: Opus 5.5 drafts SQL → bounded variant generator (filters, aggregation, date logic, NULL handling) | `plan.ready` | `get_context_pack` | candidate SQL set | None |
| 25 | Query checker | H: sqlglot validation + KG rules (joins exist, grain, units, fan-out/double counting) → Opus repair | `sql.candidates` | `get_entity` (keys, cardinalities, definitions) | checked SQL | None |
| 26 | Reconciler | H: DuckDB execution → cell-level diff vs report → Opus mismatch diagnosis → validation across ≥2 periods/slices; may return "not resolved" | `sql.checked` | `propose_fact` | `report_sql` + validated report lineage | Gate: approve or pick SQL |

### 5.6 Orchestrator
- A LangGraph 1.x supervisor with one subgraph per group, checkpointed in Postgres.
- It plans runs, fans comparison out in parallel per dataset pair, consumes events from Redis Streams so it re-runs only affected steps, enforces per-run budgets, and hosts Ask interrupts.
- Individual agents run in their group's worker service; the orchestrator dispatches them with job messages.

---

## 6. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 (backend/agents), TypeScript (workbench) | `uv`, `ruff`, `pyright`, `pytest`; `pnpm`, ESLint, Playwright |
| Workbench UI | **Next.js + React Flow** from day 1 (dedicated squad); Streamlit only for internal debug consoles | Review queue, graph explorer, dictionary view, Ask inbox, conflict resolution |
| API gateway & auth | FastAPI + **Keycloak** | SSO, roles, service accounts |
| Orchestration | LangGraph 1.x + `langgraph-checkpoint-postgres` | LLM calls go through `model-gateway`, not LangChain models |
| Agent ↔ KG | **FastMCP 3.x** server; MCP client in `adce-core` (or `langchain-mcp-adapters` inside LangGraph) | Typed tools, OAuth, OTel |
| LLMs | Anthropic SDK: Haiku 4.5, Sonnet 5.5, Opus 5.5; Bedrock/Vertex/Foundry clients for client deployments | Structured outputs, caching, Batch API, task budgets |
| Embeddings | Voyage AI (default) / self-hosted bge-m3 (strict clients) behind one interface | Neo4j vector index |
| Knowledge graph | Neo4j 5.26 LTS (or 2026.x) + APOC + GDS | Community for POC; Enterprise if multi-DB tenancy is needed |
| Relational | Postgres 16 | |
| Object store | MinIO (S3 API) | Versioned buckets |
| Compute | DuckDB 1.5.x (httpfs, excel extensions) + Polars | Every upload is normalized to Parquet |
| Matching & keys | datasketch, RapidFuzz, DuckDB checks | |
| SQL | sqlglot | |
| Documents | Claude PDF/vision, openpyxl, python-docx, WeasyPrint | |
| Privacy | Microsoft Presidio | |
| Data quality | Great Expectations | Suggested-rule export |
| Events | Redis Streams (outbox relay from Postgres) | Kafka later if volume demands |
| Observability | Langfuse v3 self-hosted + OpenTelemetry → Grafana/Prometheus for services | |
| Exports | OpenLineage, dbt YAML, Excel, Markdown, PDF | |
| Deployment | Docker Compose (dev/POC), **Helm charts built in parallel** by Platform | Kubernetes targeted for pilot |
| CI | GitHub Actions: lint, type, unit, contract tests vs mock KG, gold-set eval on prompt/model changes | |

---

## 7. Team structure and plan (parallel squads, 12 weeks)

### 7.1 Squads
| Squad | Owns | Suggested size |
|---|---|---|
| **Platform & KG** | `kg-mcp` (tools, policy, validator, bi-temporal store, context assembler), Neo4j/Postgres/MinIO/Redis, outbox/events, Keycloak, Compose + Helm, CI | 3-4 |
| **Orchestration, HITL & Model gateway** | LangGraph supervisor + subgraphs, `hitl-service` (5 modes, routing, feedback learning), `model-gateway` (routing, budgets, Presidio, Batch), Langfuse | 3 |
| **Ingestion** | Agents 1-5 | 2-3 |
| **Profiling** | Agents 6-10 | 2-3 |
| **Comparison** | Agents 11-15 | 3 |
| **Definition & Lineage** | Agents 16-20 + all exports | 2-3 |
| **Reverse-SQL** | Agents 21-26 | 3-4 |
| **Workbench & API** | Next.js workbench, API gateway, external MCP access | 2-3 |
| **Evaluation & Domain** | Gold set, eval harness, per-agent evals, domain experts, acceptance | 1-2 engineers + domain experts |
| Architecture group (1 per squad) | Contracts, RFCs, cross-cutting reviews (security, privacy, cost) | rotating |

### 7.2 Timeline
All squads start in week 1 and build against the mock KG. Integration milestones are every two weeks.

| Weeks | Milestone | What is true at the end |
|---|---|---|
| 1-2 | **M0: Contracts & foundation** | `AgentSpec`, fact and event schemas frozen (v1). Mock `kg-mcp` + fixtures released. Compose stack up (Neo4j, Postgres, MinIO, Redis, Langfuse, Keycloak). Real `kg-mcp` with read tools + `propose_fact` + audit/outbox. Model gateway with Presidio. Gold set v1. Each squad has agent skeletons passing contract tests. |
| 3-4 | **M1: First real slice** | Ingestion 1-5 and Profiling 6-10 live against the real KG. HITL Gate/Queue/Seed live. Workbench review queue + entity view. Upload → profile → PK gated → confirmed, visible in graph and audit. Reverse-SQL agents 21-23 working on fixtures. |
| 5-6 | **M2: Relate & compound** | Comparison 11-15 live (within-source and cross-source). Glossary drafter + dictionary publisher (MD/Excel). Documents before and after data both work. Event-driven re-runs live. **Reverse-SQL first end-to-end on 2 gold reports.** Workbench graph explorer. |
| 7-8 | **M3: Full pipeline** | Global verifier, refinement loop, conflict resolver, lineage inferrer. Reverse-SQL 24-26 with query checker + multi-period validation. Ask + Correct modes. Feedback-as-examples retrieval. Keycloak roles enforced on every service. |
| 9-10 | **M4: Complete outputs** | All exports (Excel, MD, PDF, dbt YAML, OpenLineage, GE rules). External MCP access with OAuth. Impact-ranked queue. Budgets and cost dashboards. Helm deploy to a staging cluster. |
| 11-12 | **M5: Hardening & evaluation** | Full gold-set evaluation, worked example (proposal section 10.2) replayed, security/privacy review, load test (event storms), final report with cost per dataset and per report. |

### 7.3 Success metrics (proposal section 13, refined)
- PK F1 ≥ 90%; FK F1 ≥ 80% within a source; cross-source match precision reported per dataset with a normalization grade.
- Review items per new dataset from the same source decline across M1 → M5.
- Auto-accepted facts later reversed by humans stay below 5%.
- Late-arriving documents update affected facts with no manual re-run.
- Reverse-SQL: share of gold reports *exact*, *within tolerance* and *not resolved*, reported separately.
- Cost per dataset and per report from Langfuse; per-agent latency and cost.

---

## 8. Proposed repository layout

```
agentic-data-context-engine/
  docs/                      architecture.md, adr/, rfcs/, eval-reports/
  packages/
    adce-core/               AgentSpec, BaseAgent, fact & event schemas, KG/LLM/DuckDB clients
  services/
    kg-mcp/                  FastMCP server: tools/, policy/, validator/, context/, store/ (neo4j, postgres)
    model-gateway/           routes, budgets, privacy (Presidio), batch, tracing
    hitl-service/            modes, routing, queue, feedback learning
    orchestrator/            LangGraph supervisor + subgraphs, event consumers
    api-gateway/             FastAPI, auth, uploads, exports
    agents-ingestion/        intake_router, schema_parser, doc_extractor, entity_linker, scope_registrar
    agents-profiling/        stats_profiler, semantic_typer, pii_classifier, key_discoverer, domain_classifier
    agents-comparison/       candidate_finder, match_reranker, fk_ind_detector, global_verifier, insight_writer
    agents-definition/       glossary_drafter, refinement_loop, conflict_resolver, lineage_inferrer, dictionary_publisher
    agents-reversesql/       report_parser, metric_mapper, join_planner, sql_synthesizer, query_checker, reconciler
  apps/workbench/            Next.js + React Flow
  prompts/                   versioned prompts per agent (synced to Langfuse)
  eval/                      gold/, harness/, per-agent suites
  deploy/                    compose/, helm/, keycloak realm export
```

---

## 9. Risks (additions to proposal section 16)

| Risk | Mitigation |
|---|---|
| Parallel squads drift from each other | Frozen v1 contracts, contract tests in CI against the mock KG, an architecture group that owns RFCs, integration milestones every 2 weeks |
| Neo4j and Postgres drift out of sync | Outbox + idempotent fact IDs + nightly reconciliation |
| Gold set late | It blocks evaluation for every squad. Domain experts are committed in week 1; Evaluation squad owns delivery by M0. |
| Thinking-always-on models cost more than estimated | Per-route `effort`, Haiku for simple routes, prompt caching on context packs, Batch API, per-agent budgets, weekly cost review |
| Reverse-SQL search explodes on complex reports | Bounded variant budgets; POC scope is single-statement aggregate SQL (joins, filters, GROUP BY, SUM/COUNT/AVG, date buckets, CASE); report "not resolved" honestly |
| Event storms with 26 consumers | Per-upload batching, per-entity debounce, consumer-group backpressure, priority lanes (PII first) |
| Keycloak/MCP auth complexity slows squads | Dev realm with pre-provisioned service accounts from M0; auth enforcement turned on at M3 |

---

## 10. Open decisions (need an owner)

1. **Pilot domain and the two sources.** Do the reports with known SQL actually exist?
2. **Hosting.** Which cloud or on-prem; Anthropic API direct, or Bedrock/Vertex from day 1?
3. **Neo4j Community vs Enterprise.** Enterprise is needed for per-tenant databases and RBAC.
4. **Embeddings.** Voyage vs self-hosted bge-m3.
5. **Squad sizes and leads**, mapped onto section 7.1.

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
