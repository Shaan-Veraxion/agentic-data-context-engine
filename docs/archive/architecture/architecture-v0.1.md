# Agentic Dataset Context Engine: Architecture & Build Plan

Status: Draft v0.1 (2026-10-08). Based on the proposal "Agentic Dataset Context Engine - Proposal" (Sept 2026), with every key claim checked against current sources.
Audience: the engineers building the POC, plus the sponsor.

---

## 1. Verdict on the proposal

The problem is real, the positioning ("feed the catalog, don't replace it") makes sense, and the core technical bet holds up: **statistics generate candidates and the LLM adjudicates and explains**. The research the proposal cites exists and backs this up.

The proposal has five problems we need to fix before building:

| # | Issue | Why it matters | Fix |
|---|---|---|---|
| 1 | **26 "agents" + 6 stores + Keycloak + Langfuse for 2 engineers in 12 weeks** | The phases are split by agent group (horizontal). Integration risk lands in week 12. | About 12 LLM agents plus deterministic services. Build a thin end-to-end slice first (section 6). |
| 2 | **Reverse-SQL, the go-to-market headline, is built last (weeks 11-12)** | If it slips, the POC fails to prove the claim we sell on. | Run a feasibility spike in weeks 4-5 and narrow the scope to aggregate reports (section 6). |
| 3 | **"Low temperature" as the determinism mitigation** | Current models reject `temperature`/`top_p` (HTTP 400 on Opus 5.5, Sonnet 5.5 and Sonnet 5). | Use structured outputs, `strict` tools, pinned model and prompt versions, per-route `effort`, and a gold-set regression run. |
| 4 | **"Redis not needed in POC"** | Self-hosted Langfuse v3 itself requires Postgres, **ClickHouse, Redis and S3/MinIO**. | Accept Redis and MinIO in compose (Langfuse needs them anyway), or use Langfuse Cloud for the internal POC if policy allows. |
| 5 | **Key-accuracy target ">90%" for all keys** | Published hybrid join inference gets 78-100% precision on well-structured schemas and notably worse on poorly normalized ones. | Set separate targets: PK F1 ≥ 90%, FK F1 ≥ 80% within a source, cross-source matches reported but no hard target. |

Smaller corrections:
- **Models.** Sonnet 5 → **Sonnet 5.5** (same $2/$10 price). Opus 5.5 is $4/$20 and confirmed. On Opus 5.5 and Sonnet 5.5, thinking cannot be disabled and forced `tool_choice` returns a 400, so we use structured outputs instead. Opus 5.5's default effort is `medium`, so we set effort explicitly per route. The proposal's cost-table arithmetic is correct.
- **Neo4j "5".** Neo4j moved to calendar versioning (2025.01+). Pin **5.26 LTS** or a current 2026.x release. Community Edition has a single user database, so tenant isolation in the POC is a property or label, not a separate database.
- **Graphiti.** The bi-temporal model is a good idea to borrow, but Graphiti itself is pre-1.0 and spends an LLM call on every write to extract facts from text. Our facts are already structured, so we **implement the bi-temporal fact model ourselves** and do not depend on Graphiti.
- **Kuzu** (an embedded graph DB) was archived in Oct 2025 after Apple acquired it. Do not adopt it.
- **ZDR.** Opus 5.5 and Sonnet 5.5 are fine under zero data retention. Fable-tier models need 30-day retention, so keep them out of the client-deployment plan.

---

## 2. Research check (what the evidence actually supports)

| Claim in proposal | Verified | Design consequence |
|---|---|---|
| DBAutoDoc: statistics + iterative LLM refinement; human ground truth separate from seed context | ✅ arXiv 2603.23050. **The deterministic pipeline adds +23 F1 points over LLM-only FK detection.** | Statistics-first key/FK discovery is mandatory, not optional. Two knowledge tiers: *confirmed* (immutable) and *seed* (soft). |
| LLM-FK: 4 agents (profiler/interpreter/refiner/verifier), F1 > 93% | ✅ arXiv 2603.07278, Findings of ACL 2026. Cuts the candidate space by 2-3 orders of magnitude. | Unique-key-driven decomposition to prune candidates, plus a **global verifier** for schema-wide consistency. |
| Magneto: cheap retrieval → LLM rerank | ✅ PVLDB 18(8); code at VIDA-NYU/magneto-matcher | Two-stage matcher. Can reuse their code or prompts for the reranker. |
| Join inference at scale | ✅ arXiv 2603.04176 (Tursio). PK candidates → LLM adjudication → IND detection → LLM adjudication; refines with query history. 78-100% precision on clean schemas. | Same pipeline shape. **If any query logs exist, ingest them**: they are the strongest join signal. |
| Graphiti bi-temporal memory | ✅ (Thoughtworks Radar "Trial", Apr 2026; pre-1.0) | Copy the model (valid time + transaction time, invalidate rather than delete). Skip the dependency. |
| LangGraph interrupts + Postgres checkpointer | ✅ but with caveats: interrupts during parallel fan-out are poorly defined, and every step writes a checkpoint blob | **HITL is stored as data, not as a paused graph** (ADR-4). |
| FastMCP | ✅ v3.x (Prefect), adds OAuth, OpenTelemetry and server composition | Define the KG tools once in FastMCP. Internal agents use the in-memory client and external tools use HTTP. |

---

## 3. Architecture decisions (ADRs)

### ADR-1: Modular monolith in Python, not microservices
One Python codebase (`uv`-managed) with clear module boundaries: `kg`, `ingest`, `profiling`, `matching`, `definitions`, `reverse_sql`, `hitl`, `orchestration`, `api`. Two processes: **api** (FastAPI + MCP endpoint) and **worker** (job runner). Split into services only when load or tenancy forces it.
*Why:* two engineers. Network boundaries between 26 agents would cost the whole schedule.

### ADR-2: The KG service is the only writer of knowledge
- All knowledge writes go through `kg.service`, which runs the **policy → validator → trust gate → commit + audit** path from proposal Fig. 2.
- The same functions are exposed as **FastMCP tools**. Internal agents call them through FastMCP's in-memory client, so there is one code path and one place where policy is enforced. External AI tools reach the same server over streamable HTTP.
- No raw write Cypher from any LLM. Read-only Cypher is allowed only for the join planner and Q&A, with a parsed allow-list and a row limit.

### ADR-3: Facts are reified nodes; "current view" edges are materialized
The proposal wants every fact to carry trust, evidence, version, validity and feedback. That metadata does not fit on a plain edge, so:

```
(:Source)-[:HAS_SCHEMA]->(:Schema)-[:HAS_DATASET]->(:Dataset)-[:HAS_COLUMN]->(:Column)
(:Document)-[:DESCRIBES]->(any entity)

(:Fact {id, kind, payload_json, trust: confirmed|documented|validated|inferred,
        confidence, valid_from, valid_to,          // when true in the data
        recorded_at, invalidated_at,               // when we believed it
        produced_by: agent@version, prompt_version, model, run_id})
  -[:ABOUT {role:"subject"|"object"}]->(:Column|:Dataset|...)
  -[:EVIDENCED_BY]->(:Evidence {kind: dq_check|doc_span|sample_stat|human, ref})
  -[:SUPERSEDES]->(:Fact)
  -[:CONFLICTS_WITH]->(:Fact)

Materialized for traversal (rebuilt from the current, best-trust facts):
(:Column)-[:JOINS_TO {fact_id, trust, cardinality}]->(:Column)
(:Column)-[:SAME_AS|DERIVED_FROM {fact_id, trust}]->(:Column)
(:Column)-[:DEFINED_BY {fact_id}]->(:Term)
(:Report)-[:HAS_METRIC]->(:Metric)-[:COMPUTED_FROM {fact_id}]->(:Column)
```

Fact kinds: `primary_key`, `foreign_key`, `semantic_type`, `sensitivity`, `definition`, `match`, `lineage`, `metric_mapping`, `report_sql`, `domain`.
**Rules:** confirmed facts are immutable (they can only be superseded by a new confirmed fact). A disagreement across trust levels creates a `CONFLICTS_WITH` edge and a review item, never an overwrite. Point-in-time queries filter on `recorded_at`/`invalidated_at`.

### ADR-4: HITL is stored as data, not as a paused graph
- Agents **propose** facts. The KG service routes each proposal by `confidence × impact` to: **auto-accept** (validated, high confidence, low impact), **queue**, or **gate**. The run continues or ends; nothing sits blocked on a human.
- A reviewer's decision is written to Postgres, applied via `record_feedback`, and emits an event that re-runs the affected steps.
- **Only "Ask" mode** (an agent that cannot continue) uses a LangGraph `interrupt()`, and never inside a parallel fan-out.
- Every correction is stored as `(proposal, decision, edit, reason)` and retrieved later as a few-shot example for that agent and scope.

*Why:* long-lived paused graphs are fragile and costly to checkpoint. Queue semantics match how reviewers actually work (batch approval).

### ADR-5: Two stores of record, kept consistent with an outbox
- **Neo4j** holds knowledge. **Postgres** holds the HITL queue, audit log, jobs, checkpoints and users.
- Write order: Postgres transaction (audit row + outbox event, idempotent `fact_id`) → Neo4j write → mark outbox row done. A relay retries anything pending.
- Events use the same outbox with `LISTEN/NOTIFY` for wakeups. Jobs use a Postgres-backed queue (Procrastinate or plain `SKIP LOCKED`). No Kafka.

### ADR-6: Deterministic first, LLM second
Anything that statistics or parsing can answer never goes to an LLM: profiling, uniqueness, inclusion dependencies, MinHash overlap, DDL parsing, SQL validity, report diffs. LLMs do classification of ambiguous inputs, adjudication, naming and definitions, document extraction, and SQL hypotheses. Every LLM proposal that makes a data claim is **re-verified in DuckDB** before it can reach the `validated` trust level.

### ADR-7: Call the Anthropic SDK directly behind a thin model gateway
- `llm.gateway` is our own module (~300 lines) that does route → model + effort, prompt caching on context packs, structured outputs, per-run token/cost budgets, retries, refusal fallback, and Langfuse tracing. It is **not** LiteLLM or LangChain chat models, which add abstraction over features we need (effort, caching, structured outputs, batches).
- Portability: the Anthropic SDK ships Bedrock (Mantle), Vertex and Foundry clients, and the gateway picks the client from config.
- Routing defaults:

| Route | Model | Effort |
|---|---|---|
| File-type tie-breaks, domain tags, semantic types | Haiku 4.5 | none |
| Key/FK adjudication, match rerank, doc extraction, definitions, conflict proposals | Sonnet 5.5 | low-medium |
| Global verifier, SQL synthesis, mismatch diagnosis | Opus 5.5 | medium-high |
| Bulk definition drafting | Sonnet 5.5 via **Batch API** (−50%) | low |

### ADR-8: Privacy boundary at the gateway
Presidio runs **inside the gateway**, so no code path can send unmasked samples. Only schemas, statistics and masked samples (≤ N rows per column) leave the environment. A column tagged `sensitive` sends no sample values at all, only statistics. Downgrading a sensitivity tag requires a human.

### ADR-9: Evaluation is a product feature from day 1
The gold set (keys, joins, definitions, 3-5 reports with known SQL) and a `make eval` harness come before the first agent. Every prompt or model change runs against it. Results go to Langfuse datasets. This is also how we answer the "model upgrades change behavior" risk.

### ADR-10: Defer what doesn't prove the hypothesis
Not in the POC: Keycloak (use a single OIDC or basic-auth user), Next.js, Kubernetes/Helm, multi-tenant isolation beyond a `tenant_id` property, Great Expectations export, OpenLineage export (stretch).

---

## 4. Agent consolidation (26 → 12 LLM agents + deterministic services)

| Proposal agent | Becomes | LLM? |
|---|---|---|
| Intake router | `ingest.sniff` (python-magic, pandas, openpyxl) with Haiku only for ambiguous cases | Rarely |
| Schema parser | `ingest.ddl` (sqlglot, dbt YAML, JSON Schema, Avro) | No |
| Doc extractor | **DocExtractor agent** (Claude PDF input, structured output) | Yes |
| Entity linker | `link.candidates` (names, embeddings, value patterns) + **Linker agent** adjudicates | Yes |
| Scope registrar | `kg.scope` (service code) | No |
| Stats profiler | `profiling.stats` (DuckDB/Polars) | No |
| Semantic typer + Domain classifier | **Typer agent** (rules first, then Haiku) | Yes |
| PII classifier | `privacy.presidio` (in the gateway) | No |
| Key discoverer | `keys.candidates` (DuckDB scoring) + **KeyJudge agent** | Yes |
| Candidate finder | `match.candidates` (RapidFuzz, embeddings, MinHash) | No |
| Match reranker | **MatchJudge agent** | Yes |
| FK / IND detector | `keys.ind` (DuckDB containment) + KeyJudge | Shared |
| Global verifier | **Verifier agent** | Yes |
| Insight writer | `insights` (computed) + short Sonnet summary | Light |
| Glossary drafter + Refinement loop | **Definer agent** with a deterministic propagation loop | Yes |
| Conflict resolver | **ConflictJudge agent** | Yes |
| Lineage inferrer | `lineage.rules` + Definer | Light |
| Dictionary publisher | `publish` (Jinja → MD/XLSX/dbt YAML) | No |
| Report parser | **ReportParser agent** (PDF/vision) | Yes |
| Metric mapper | **MetricMapper agent** | Yes |
| Join planner | `kg.join_paths` (Cypher shortest paths weighted by trust) | No |
| SQL synthesizer | **SqlSynth agent** (Opus) plus bounded variant generator | Yes |
| Query checker | `sql.check` (sqlglot + KG rules) + repair through SqlSynth | Partial |
| Reconciler | `reconcile` (DuckDB execution and diff) + **Diagnoser agent** | Yes |

That leaves 12 LLM roles, each with a role-scoped tool subset (which also handles the "too many tools" risk).

---

## 5. Tech stack (POC choices)

| Layer | Choice | Notes / rejected alternatives |
|---|---|---|
| Language | Python 3.12, `uv`, `ruff`, `pyright`, `pytest` | |
| API | FastAPI + Pydantic v2 | |
| KG tool server | **FastMCP 3.x** | Official MCP SDK is lower-level. FastMCP gives in-memory client, OTel and auth. |
| Orchestration | **LangGraph 1.x** (one subgraph per pipeline) + `langgraph-checkpoint-postgres` | LLM calls inside nodes use the Anthropic SDK directly. Alternatives considered: Pydantic AI (good typing, weaker durable workflows), Claude Agent SDK (filesystem/coding harness, wrong shape here). |
| Jobs / events | Postgres outbox + `LISTEN/NOTIFY` + Procrastinate | Redis Streams/Kafka later. |
| LLM | Anthropic SDK; Haiku 4.5 / Sonnet 5.5 / Opus 5.5 | Structured outputs, prompt caching, Batch API, task budgets. |
| Embeddings | Voyage (POC) behind an interface; **bge-m3 self-hosted** option | Stored in Neo4j vector index. |
| Knowledge graph | **Neo4j Community 5.26 LTS** (or 2026.x) + APOC | GDS optional. Fallback if Neo4j is vetoed: Postgres + pgvector + recursive CTEs. Fine at POC scale, weaker for exploration. |
| Relational | Postgres 16 | HITL, audit, jobs, checkpoints, Langfuse metadata. |
| Compute | **DuckDB 1.5.x** (+ excel extension), Polars | Parquet as the canonical internal format: every upload is normalized to Parquet once. |
| Matching | datasketch (MinHash/LSH), RapidFuzz | |
| SQL | sqlglot | DDL parsing, SQL validation, dialect transpile. |
| Documents | Claude PDF/vision input, openpyxl, python-docx | |
| Privacy | Microsoft Presidio (+ custom recognizers for account numbers, etc.) | |
| Object store | MinIO (Langfuse needs it anyway) | |
| Observability | Langfuse v3 self-hosted (needs ClickHouse + Redis + MinIO) + OpenTelemetry | Size it at 4 vCPU / 8 GB minimum. |
| UI | Streamlit (POC) on top of the same REST API | Next.js + React Flow for v1. Keep the UI thin so it can be thrown away. |
| Deploy | Docker Compose | |

Compose footprint: `api`, `worker`, `postgres`, `neo4j`, `minio`, `langfuse-web`, `langfuse-worker`, `clickhouse`, `redis`, `streamlit`. One 8 vCPU / 32 GB VM is plenty.

---

## 6. Phased plan (vertical slices)

Each phase ends with a demo on real data and a gold-set eval run.

**Phase 0: Foundations (weeks 1-2)**
- Repo skeleton, compose stack, CI, `make eval`.
- KG schema (ADR-3), KG service with ~10 tools (`register_entity`, `propose_fact`, `get_context_pack`, `get_entity`, `get_neighbors`, `search_context`, `find_join_paths`, `link_entities`, `flag_conflict`, `record_feedback`), policy, validator, audit, and outbox.
- Model gateway with Presidio, budgets and Langfuse.
- HITL queue tables and minimal Streamlit queue.
- **Gold set assembled** with the domain expert.
- *Exit:* a stub agent proposes a fact, it is gated, a human approves it, and the change shows up in the graph and audit log with full provenance.

**Phase 1: Thin end-to-end slice (weeks 3-5)**
- Ingest CSV/Parquet/XLSX + DDL, normalize to Parquet, apply scope tags.
- Stats profiler, key candidates + KeyJudge, IND/FK within one source, Typer.
- Review queue with batch approve.
- Markdown/XLSX dictionary export (raw definitions are fine at this stage).
- **Reverse-SQL spike (weeks 4-5, about 30% of one engineer):** on 2 gold reports, using known joins, can ReportParser → MetricMapper → SqlSynth → DuckDB diff reproduce the figures? The result is a go/no-go on how much of Phase 4 to scope.
- *Exit:* 3+ datasets end-to-end; PK/FK precision, recall and F1 measured.

**Phase 2: Context & compounding (weeks 6-7)**
- DocExtractor (dictionaries, PDFs), entity linker, document-before-data placeholders, document-after-data relinking.
- Documented-vs-observed conflict detection, event-driven re-runs, feedback-as-examples.
- *Exit:* the worked example from proposal section 10.2 runs end-to-end without manual re-runs; review items per new dataset from the same source go down.

**Phase 3: Cross-source matching & definitions (weeks 8-9)**
- Candidate finder + MatchJudge, global Verifier, insights, Definer with propagation loop, ConflictJudge.
- Dictionary in the dictionary's own wording; owner sign-off flow.
- *Exit:* second source matched; dictionary signed off with light edits; less than 5% of auto-accepted facts reversed.

**Phase 4: Reverse-SQL + lineage + hardening (weeks 10-12)**
- Full reverse-SQL pipeline scoped by the spike result, with multi-period validation and "not resolved" as an allowed outcome.
- Lineage edges and dbt YAML export; OpenLineage as stretch.
- Final eval report with cost per dataset and per report.
- *Exit:* the success metrics from proposal section 13, using the revised targets in section 1.

**Reverse-SQL scope for the POC:** single-statement aggregate SQL (SELECT … JOIN … WHERE … GROUP BY with SUM/COUNT/AVG, date bucketing, simple CASE). Out of scope: window functions, multi-step ETL, and procedural logic. Score each report as *exact*, *within tolerance*, or *not resolved*.

---

## 7. Proposed repository layout

```
agentic-data-context-engine/
  docs/                    architecture.md, adr/, eval-reports/
  deploy/compose/          docker-compose.yml, .env.example
  src/adce/
    api/                   FastAPI routers, MCP mount
    kg/                    schema.cypher, service.py, policy.py, validator.py, tools.py (FastMCP)
    llm/                   gateway.py, routes.py, prompts/ (versioned), privacy.py
    ingest/  profiling/  keys/  matching/  definitions/  reverse_sql/  publish/
    hitl/                  queue.py, routing.py, feedback_examples.py
    orchestration/         graphs/ (LangGraph), jobs.py, events.py
    storage/               postgres.py, neo4j.py, objects.py, duck.py
  ui/streamlit/
  eval/                    gold/, harness.py, metrics.py
  tests/
```

---

## 8. Updated risk list (additions to proposal section 16)

| Risk | Mitigation |
|---|---|
| Neo4j and Postgres drift out of sync | Outbox + idempotent fact IDs + nightly reconciliation job |
| Reverse-SQL spike shows low feasibility | Re-scope Phase 4 to "metric-to-column lineage only" and keep the SQL search as a v1 item |
| Gold set not ready by week 2 | It blocks everything. Sponsor commits the domain expert's time in week 1. |
| Thinking-always-on models cost more than estimated | Per-route `effort`, Haiku for simple routes, prompt caching, Batch API, weekly cost review in Langfuse |
| Langfuse self-host is heavier than planned | Use Langfuse Cloud for the internal POC if data policy permits (traces contain masked samples only) |
| Messy real data has no clean keys | Report results per dataset "normalization grade"; composite-key search limited to width 3 |

---

## 9. Open decisions (need an owner)

1. **Pilot domain and the two sources.** Do the reports with known SQL actually exist?
2. **Neo4j vs Postgres-only** for the KG (recommendation: Neo4j).
3. **Hosting for the POC.** Internal VM vs cloud account; is Anthropic API direct allowed, or Bedrock/Vertex from day 1?
4. **Langfuse self-hosted vs Cloud.**
5. **Embeddings provider.** Voyage vs self-hosted bge-m3.
6. **UI.** Streamlit vs going straight to Next.js (depends on whether either engineer is comfortable in React).

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
- Atlan/Collibra 2026 positioning: https://www.modern-datatools.com/compare/atlan-vs-collibra · https://atlan.com/collibra-alternatives
- Claude model pricing and API behavior: Anthropic API docs (models table, cached 2026-09-25)
