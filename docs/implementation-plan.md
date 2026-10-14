# Implementation plan: starting stage (Qlik → SQL), without Dremio

**Status:** active · **Started:** 2026-10-14 · **Last updated:** 2026-10-15 (third build session; images built, phase 1 design written) · **Design:** `proposals/ADCE - Phase 1 Technical Design - Local-first Qlik to Dremio SQL.pdf` (source `_src/build_phase1_design.py`) · **Applies to:** the starting stage described in `architecture-alt-qlik-first.md` (v0.3) and the three starting-stage PDFs.

## 1. What changed

**Dremio is not available in the first phase.** The catalog crawl and everything else that needs a live Dremio (source resolution to real datasets, grain checks, copy detection, execution and time travel) moves to a separate phase **PD: Dremio connection**, started when access arrives.

What stays the same:

- **Target output is still Dremio SQL.** The translator emits the `dremio` dialect.
- The architecture, contracts and agents do not change. Dremio sits behind `data_port`, so connecting it later swaps an adapter; nothing upstream changes.

What we use instead, for now:

| Needed for | Without Dremio we use | Labelled as |
|---|---|---|
| Source identity | **Logical sources** taken from the scripts: Qlik connection + table path (e.g. `Dremio_Sales:sales.erp.orders_v2`). They map to Dremio datasets in PD. | `scope_link` facts with trust `documented` (from script), resolution pending |
| Execution (V2) | **DuckDB**. The Dremio SQL is transpiled to DuckDB by sqlglot and run on local tables. | venue `local` |
| Source data | **QVD files** received with the apps (Qlik's own extracts) and synthetic fixtures | evidence `qvd_snapshot` / `fixture` |
| Row counts (V3) | Reload logs and QVD headers | unchanged |
| Data checks (V4) | QVD contents loaded into DuckDB | venue `local` |
| Figures (V5) | Exported reports | unchanged |

Results validated locally are **never reported as validated in Dremio**. In PD they are re-run in Dremio, and the level is recorded per venue.

## 2. Phases

| Phase | Weeks | Goal | Exit criteria |
|---|---|---|---|
| **P0 Foundations** | 1–2 | Repo, tooling, CI, contracts, ports, first Qlik readers, fixture corpus | `uv run pytest` green in CI; the fixture apps split into statements with spans; reload logs and QVD headers parsed |
| **P1 Script → BI IR** | 2–4 | Full extraction: grammar, statement kinds, variables, includes, loops, QVD chains, coverage | Every fixture/sample app parsed into BI IR; coverage reported per app; nothing silently dropped |
| **P2 BI IR → SQL (tables)** | 4–6 | Rule translator (T1), CTE composer, Dremio dialect output, grain/fan-out checks, local execution and V0–V4 | ≥ 90% of statements translated on the samples; ≥ 80% of tables at V3 (local) |
| **P3 Measures, review, lineage** | 6–9 | ExprAST + expression translator, model-gateway (Vertex) for the long tail, kg-mcp on the fact log, hitl-service + workbench (overview, queue), lineage, export parsing (V5) | ≥ 3 real figures match exported reports; review loop working end to end |
| **P4 Pack and harden** | 9–12 | Orchestrator (LangGraph), migration pack, Compose stack, Helm/staging, gold-set eval in CI | A pack accepted by a report owner; identical regeneration; same build in staging |
| **PD Dremio connection** | when access arrives | Catalog crawl (agents 2, 5), resolution of logical sources to datasets (4-lite), grain in Dremio (9-lite), copies (30-lite), execution and time travel in Dremio, re-validation | Every logical source resolved or queued; levels re-earned in venue `dremio` |

PD can start in any week; it needs only a read-only Dremio service account (Q-05, Q-06). Until then, P0–P4 do not wait on it.

## 3. Work packages

### P0 Foundations
| ID | Work package | Output | Status |
|---|---|---|---|
| P0.1 | Monorepo, uv workspace, ruff, pyright, pytest, CI workflow | `pyproject.toml`, `.github/workflows/ci.yml` | done |
| P0.2 | `adce-core` contracts: BI IR, facts and evidence, trust levels, events | `packages/adce-core` | done (first version) |
| P0.3 | Ports: `KGPort` + in-memory `MemoryKG`; `DataPort` + `DuckDBSource`; `DremioSource` placeholder | `adce_core.ports`, `adce_core.data` | done (first version) |
| P0.4 | Qlik statement splitter with source spans (strings, brackets, comments, control statements) | `adce_qlik.splitter` | done |
| P0.5 | Statement classifier (kinds) + variable resolution (`SET`/`LET`, `$(…)` expansion) | `adce_qlik.classify`, `adce_qlik.variables` | done |
| P0.6 | Reload log parser (tables, lines fetched, resolved statements, errors) | `adce_qlik.reload_log` | done |
| P0.7 | QVD header reader (table, creator doc, records, fields, lineage) | `adce_qlik.qvd` | done |
| P0.8 | Fixture corpus: synthetic apps with scripts, reload logs, QVD headers | `eval/gold/bi/apps/` | started (1 app; real samples needed) |
| P0.9 | Local Compose: Postgres 16 + pgvector, Neo4j 5.26, S3 (SeaweedFS); profiles `obs` (Langfuse v3) and `auth` (Keycloak) | `deploy/compose/` | done (core profile verified; obs/auth defined, not yet exercised) |

### P1 Script → BI IR
| ID | Work package | Status |
|---|---|---|
| P1.1 | Lark grammar (LALR, contextual lexer): LOAD field list, aliases, preceding LOAD, WHERE/WHILE, GROUP BY, DISTINCT, RESIDENT, FROM file/QVD with format spec, INLINE, AUTOGENERATE; expressions as ExprAST | done (`adce_qlik/load.lark`, `load_parser.py`) |
| P1.2 | Prefixes: JOIN/LEFT/RIGHT/INNER/OUTER, KEEP, CONCATENATE/NOCONCATENATE, MAPPING; CROSSTABLE, Generic, Hierarchy, IntervalMatch flagged with tier | done |
| P1.3 | Table identity: labels, Qlik default names (flagged), auto-concatenation, DROP/RENAME of tables and fields | done; QUALIFY recorded but not applied to names yet |
| P1.4 | Control flow: IF/ELSE, FOR/NEXT, FOR EACH, SUB/CALL, SWITCH, EXIT SCRIPT, $(Include) with spans | done (`interpreter.py`): includes spliced, SUBs run at CALL with parameters, loops unrolled when bounds are constant, conditions evaluated when constant; otherwise every branch is kept and flagged |
| P1.5 | BI IR builder + logical source registry (connection + path, via sqlglot `dremio` for embedded SQL) + QVD chains + `LOAD *` resolved from QVD headers | done (`adce_qlik/model.py`) |
| P1.6 | Coverage report and `adce` CLI | done: `adce parse`, `adce translate`, `adce validate` |

### P2 BI IR → SQL
| ID | Work package | Status |
|---|---|---|
| P2.1 | Expression translation (ExprAST → sqlglot) with Qlik semantics made explicit: NULL-safe `&`, division by zero → NULL, -1/0 truth values, `LIKE` wildcards, formatting functions pass values through, ApplyMap as a left join | done (`expressions.py`) |
| P2.2 | Load steps → versioned CTEs: CREATE, CONCATENATE (explicit and automatic), JOIN on shared fields, KEEP, MAPPING, RESIDENT at the right version, DROP, STORE snapshots; statement map in comments | done (`compose.py`) |
| P2.3 | Emit `dremio`, transpile to `duckdb` for local runs | done |
| P2.4 | Checker: translated, Dremio syntax, transpiles, known fields, determinism, inputs to resolve, AI-assisted marker | done (`check.py`); grain/fan-out for measures comes with P3 |
| P2.5 | Local venue: source extracts and QVD contents (PyQvd) in DuckDB, logical sources mapped on the syntax tree | done (`local.py`) |
| P2.6 | Reconciler V0–V4: row counts per step (rows read or produced, basis recorded) and per stored QVD, per-field aggregates against QVD contents | done (`reconcile.py`); both `orders_local` tables reach V4 against an independent oracle |
| P2.7 | Agents 28 (with AI route and reviewer SQL as overrides), 24, 25, 26, 19-lite, 20-lite (migration pack) and the orchestrator nodes | done |

### Brought forward from P3/P4 (built in session 2, 2026-10-14)
| ID | Work package | Output | Status |
|---|---|---|---|
| P3.a | Knowledge service adapter behind `KGPort`: Postgres fact log (append-only, enforced by trigger) + outbox, projected into **Graphiti** (graphiti-core 0.30, Apache-2.0) on Neo4j 5.26 | `packages/adce-kg`, `migrations/alembic` | done; integration-tested on the local stack |
| P3.b | kg-mcp on FastMCP 3.4 (streamable HTTP): register_entity, propose_fact, get_entity, get_facts, search_context, get_context_pack | `services/kg-mcp` | done; tested through a real MCP client |
| P3.c | model-gateway library: routes, privacy guard (no rows), memoization, record/replay, budgets, Claude on Vertex AI (`anthropic[vertex]` 1.13, structured outputs) | `packages/adce-gateway`, `prompts/` | done; tested with a fake client (no Vertex project yet) |
| P3.d | Agent runtime contract (AgentSpec, BaseAgent, fact-kind policy) + artifact store over S3 | `adce_core.agent`, `adce_core.artifacts` | done |
| P3.e | Agents: Intake router (1), BI extractor (27) | `services/agents-ingestion`, `services/agents-bi-adapters` | done |
| P3.f | Orchestrator: LangGraph 1.2 `qlik_pipeline` (intake → extract → resolve → summarize) with the Postgres checkpointer | `services/orchestrator` | done; grain/translate/compose/check/reconcile nodes come with P2 |
| P3.g | API gateway: FastAPI uploads, runs, tables, facts; Keycloak bearer tokens (realm roles) | `services/api-gateway`, `deploy/keycloak` | done; Keycloak path unit-tested, not yet run against a live realm |
| P3.h | hitl-service: Ask / Queue / Gate / Seed items generated from facts, ranked by impact × uncertainty; decisions become confirmed facts the next run uses (reviewer SQL, variable answers, Dremio mappings) | `services/hitl-service`, migration 0002 | done |
| P3.i | Review workbench: Next.js 16.4 + React Flow 12.12 (overview, review queue, lineage), runtime API proxy | `apps/workbench` | done; checked in a headless browser against the live API |
| P4.a | Container images (one Python image for all services, workbench image), Helm chart with staging and client values, Terraform for Google Cloud, CI jobs | `deploy/docker`, `deploy/helm/adce`, `deploy/terraform/gcp`, `.github/workflows/ci.yml` | Helm lint + render and Terraform validate pass; both images built and smoke-tested locally (python 1.04 GB, workbench 324 MB) |
| — | Still open: Langfuse tracing hook-up, model-gateway as its own HTTP service, Keycloak login in the workbench, orchestrator as a separate worker (it runs inside the API gateway for now), export parsing and figures (V5) | | to do |

P4 and PD are broken into packages at the start of each phase.

### Decisions taken while building (2026-10-14)
| Decision | Why | Revisit if |
|---|---|---|
| **Knowledge service = Graphiti on Neo4j, behind `KGPort`**, with the Postgres fact log as the record | Open source (Apache-2.0), bi-temporal edges, runs on the Neo4j 5.26 already in the stack. Brahmastra (Q-02) plugs in later as another `KGPort` implementation | Brahmastra's latest version is available, or Graphiti's model limits us |
| Structured facts are written to Graphiti **directly** (nodes, edges, episodes), not through `add_episode` / `add_triplet` | Those paths call an LLM to deduplicate; ADCE writes must be deterministic and reproducible | Later stages ingest documents: those use Graphiti's LLM extraction through the model gateway |
| Graphiti telemetry is **off** (`GRAPHITI_TELEMETRY_ENABLED=false`) | Client installs must not send data out | Never |
| **SeaweedFS** replaces MinIO for local S3 | MinIO images are no longer publicly available (pull fails on Docker Hub and quay.io); code uses only the S3 API | — (staging and production use GCS) |
| FastMCP pinned to **3.x** (latest 3.4.8) though 4.x exists | The architecture specifies 3.x; move deliberately | Planned upgrade review |
| Langfuse SDK pinned to **3.x** | Matches the self-hosted Langfuse v3 server | Server upgrade |
| Haiku route stays on **claude-haiku-4-5** | The architecture's choice; newer Haiku models get evaluated on the gold set before switching | Gold-set results |
| Embeddings: **Gemini embeddings on Vertex** when a project is configured; deterministic **hash embedder** offline | Matches the Google requirement; tests stay offline (hash vectors are not semantic) | Q-16 decided |
| The S3 client ignores the machine's `AWS_PROFILE` | Developer machines with unrelated AWS SSO profiles broke the client | — |
| Qlik's "lines fetched" is matched against both the rows read and the rows produced by a step, and the basis is recorded | Which one Qlik logs differs by statement shape; recording the basis keeps V3 honest | Real reload logs show one basis consistently |
| ApplyMap keeps Qlik's first match as MIN(value) per key, and mapping-key uniqueness is checked locally | SQL has no load order; with unique keys the result is exact | Duplicate mapping keys found in real data |
| The translation is a pure function of (BI IR, overrides); agents exchange only artifact keys | Every agent recomputes identical SQL; packs regenerate identically | — |
| sqlglot's Dremio dialect writes GROUP_CONCAT; Concat() is built as LISTAGG | Dremio uses LISTAGG | sqlglot fixes the dialect |
| Disk space: building images needs several GB free; on 2026-10-15 the C: drive filled during the first image build; after freeing space both images built | Builds also run in CI (images job) | — |

## 4. Repository layout (as built)

```
packages/
  adce-core/        contracts (BI IR, ExprAST, facts, entities), ports (KGPort, DataPort), agent runtime, artifacts, settings
  adce-qlik/        Qlik readers: splitter, classifier, variables, reload logs, QVD headers, LOAD grammar, BI IR builder
  adce-kg/          knowledge service adapter: Postgres fact log + Graphiti projection on Neo4j
  adce-gateway/     model gateway: routes, privacy guard, memo/replay, budgets, Vertex AI client
  adce-reversesql/  translation, composition, checks, local validation (P2)
services/
  agents-ingestion/    intake router (1)
  agents-bi-adapters/  BI extractor (27); translators (28, 29) in P2
  kg-mcp/              FastMCP server over KGPort
  orchestrator/        LangGraph qlik_pipeline
  api-gateway/         FastAPI
prompts/            versioned prompts per model-gateway route
migrations/         Alembic (Postgres schema)
eval/gold/bi/apps/  fixture and sample Qlik apps with expected outputs
eval/recordings/    model-gateway recordings (replay fixtures)
deploy/compose/     local stack; deploy/keycloak/ dev realm
deploy/docker/      python.Dockerfile (all Python services), workbench.Dockerfile
deploy/helm/adce/   one chart: values.yaml, values-staging.yaml, values-client.yaml
deploy/terraform/gcp/  VPC, GKE Autopilot, Cloud SQL, GCS, Artifact Registry, Secret Manager, workload identity
apps/workbench/     Next.js + React Flow review workbench
services/hitl-service/, services/agents-reversesql/  review service; agents 24, 25, 26, 19-lite, 20-lite
```

Libraries come first, and services wrap them later. An agent in `services/agents-bi-adapters` is a thin runtime around `adce-qlik`, which keeps the logic testable without infrastructure.

## 5. Working rules

- Every Qlik construct gets a fixture and a test before it counts as supported.
- Nothing is dropped silently. Unknown input becomes `kind: other` with its span and is counted in coverage.
- Dremio-dependent code goes only behind `DataPort`. No module imports a Dremio client directly.
- Real client scripts and QVDs go in `eval/gold/bi/apps/<app>/` and are **git-ignored** unless cleared for the repo.
