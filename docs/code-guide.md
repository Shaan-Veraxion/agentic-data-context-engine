# Code guide: how ADCE works, file by file

This guide explains the code as it is today: what happens when you run the pipeline, which file does each step,
what every agent reads and writes, where LangGraph and the models come in, and where to look when something fails.
It is written to be read next to the code; every section links the files it describes.

For why the system is built this way, see [architecture.md](architecture.md); for where the code differs from that
design, see [design-review.md](design-review.md). For how to run things, see
[testing-guide.md](testing-guide.md).

Contents
1. [The whole thing in one page](#1-the-whole-thing-in-one-page)
2. [Repository map](#2-repository-map)
3. [Five ideas the code is built on](#3-five-ideas-the-code-is-built-on)
4. [The pipeline (LangGraph), node by node](#4-the-pipeline-langgraph-node-by-node)
5. [Inside translation (agent 28)](#5-inside-translation-agent-28)
6. [Validation: the V0 to V5 ladder](#6-validation-the-v0-to-v5-ladder)
7. [The model gateway: every LLM call](#7-the-model-gateway-every-llm-call)
8. [Knowledge: facts, kg-mcp, Postgres and Graphiti](#8-knowledge-facts-kg-mcp-postgres-and-graphiti)
9. [Review (human in the loop)](#9-review-human-in-the-loop)
10. [Learning and event-driven re-runs](#10-learning-and-event-driven-re-runs)
11. [Front-end agents (charts, exports)](#11-front-end-agents-charts-exports)
12. [Services: API gateway, hitl-service, kg-mcp, workbench](#12-services-api-gateway-hitl-service-kg-mcp-workbench)
13. [The test bench](#13-the-test-bench)
14. [Debugging playbook](#14-debugging-playbook)
15. [What is declared but not wired yet](#15-what-is-declared-but-not-wired-yet)
16. [Glossary](#16-glossary)

---

## 1. The whole thing in one page

**Input:** the files of one Qlik application: load scripts (`.qvs`), optionally a reload log, QVD files, sample
data for the sources, front-end objects (charts as JSON) and exported reports.

**Output:** a *migration pack*: Dremio SQL for every table the script builds (and every chart), a validation
report saying how far each table's SQL was proven, lineage, and a list of what a person still has to decide.

**How:** a LangGraph graph of agents. The front of the graph is a fixed sequence; after the SQL is checked and run,
a **supervisor** decides what to do next (fix a table, ask the owner, or publish) and the graph loops until nothing
more can be improved:

```
files ─▶ intake (1) ─▶ extract (27) ─▶ resolve ─▶ translate (28) ─▶ synthesize (24) ─▶ check (25) per table
                                                     subgraph                   ▲                │
                                                                                │       repair (25) once
                                                       correction ──────────────┤                ▼
                                                                                │      reconcile (26) per table
                                                     ask_owner ◀── supervisor ◀─┘◀───────────────┘
                                                     (pauses)        │  └──▶ diagnose (26) ──▶ back to synthesize
                                                                     ▼
                                                record (26) ─▶ publish (20) ─▶ summarize
```

| # | Node | Agent class | In one sentence |
|---|------|-------------|-----------------|
| 1 | intake | `IntakeRouter` | Decides what each file is (script, log, QVD, sample data, ...). |
| 27 | extract | `BIExtractor` | Parses the script into the BI IR: tables, load steps, fields, sources, variables. |
| – | resolve | (function) | Lists the database sources that still need a Dremio dataset. |
| 28 | translate | `ScriptTranslator` | Turns every load statement into SQL: rules first, then memory, then the LLM (a subgraph, one node per step). |
| 24 | synthesize | `Synthesizer` | Writes one `.sql` file per table, with sources mapped to Dremio names. |
| 25 | check, repair | `QueryChecker` | Rule checks per table; with a model, repairs a table that fails a blocking rule. |
| 26 | reconcile, diagnose, record | `ReconcilerAgent` | Runs each table's SQL on local data, compares with the reload log and QVDs (V0 to V5), classifies each mismatch, corrects a contradicted statement with a model, records the results. |
| – | supervisor | (orchestrator) | Picks the next action among those code allows; the model decides when it is on. |
| 20 | publish | `Publisher` | Writes the migration pack (SQL, validation.md, review.md, lineage, manifest). |

**Deterministic first, LLM second.** Most of the work is plain code (a Qlik parser and a translator built on
sqlglot). The LLM is used where rules run out, and always through the model gateway:

| Route | Agent | When |
|---|---|---|
| `translate_statement` | 28 | a statement the rules could not translate |
| `explain_gap` | 28 | what stays untranslated, explained for a person, with questions for the data owner |
| `repair_sql` | 25 | a table whose SQL fails a blocking rule (unknown field, invalid SQL) |
| `diagnose_mismatch` | 26 | a table whose SQL runs but whose data disagrees with the evidence |
| `plan_next` | supervisor | what the run does next, when several actions are possible |

Every LLM answer is checked by code before it is used (SQL by the checks and the data, the supervisor's choice
against the allowed options), and every accepted SQL answer is remembered (translation memory), so the next run of
any application reuses it without calling the model.

**Everything the agents learn is a fact** in the knowledge service (append-only, with evidence and a trust level).
A person reviews what is uncertain (the review queue). A decision is also a fact, and it triggers a re-run of the
applications it affects.

---

## 2. Repository map

`packages/` hold libraries (no servers, no I/O of their own). `services/` hold the agents and the processes that run.

| Folder | What lives there | Start reading at |
|--------|------------------|------------------|
| [packages/adce-core](../packages/adce-core/src/adce_core) | The contracts: agent base class, facts, ports, BI IR models, artifact store | [agent.py](../packages/adce-core/src/adce_core/agent.py), [facts.py](../packages/adce-core/src/adce_core/facts.py), [ports.py](../packages/adce-core/src/adce_core/ports.py), [bi_ir.py](../packages/adce-core/src/adce_core/bi_ir.py) |
| [packages/adce-qlik](../packages/adce-qlik/src/adce_qlik) | Reading Qlik: split, classify, run control flow, variables, parse LOADs, build the BI IR, reload logs, QVD headers, front end | [document.py](../packages/adce-qlik/src/adce_qlik/document.py), [model.py](../packages/adce-qlik/src/adce_qlik/model.py) |
| [packages/adce-reversesql](../packages/adce-reversesql/src/adce_reversesql) | BI IR → Dremio SQL; checks; local venue (DuckDB); validation ladder; the LLM loop; charts; source mapping | [compose.py](../packages/adce-reversesql/src/adce_reversesql/compose.py), [ai_loop.py](../packages/adce-reversesql/src/adce_reversesql/ai_loop.py), [reconcile.py](../packages/adce-reversesql/src/adce_reversesql/reconcile.py) |
| [packages/adce-gateway](../packages/adce-gateway/src/adce_gateway) | The only path to any model: routes, providers, privacy guard, recordings, tools, rate limits | [gateway.py](../packages/adce-gateway/src/adce_gateway/gateway.py), [routes.py](../packages/adce-gateway/src/adce_gateway/routes.py) |
| [packages/adce-kg](../packages/adce-kg/src/adce_kg) | The knowledge service: Postgres fact log + outbox, Graphiti projection on Neo4j | [service.py](../packages/adce-kg/src/adce_kg/service.py), [fact_log.py](../packages/adce-kg/src/adce_kg/fact_log.py) |
| [services/agents-ingestion](../services/agents-ingestion/src/agents_ingestion) | Agent 1 | [intake_router.py](../services/agents-ingestion/src/agents_ingestion/intake_router.py) |
| [services/agents-bi-adapters](../services/agents-bi-adapters/src/agents_bi_adapters) | Agents 27, 28, 29 | [bi_extractor.py](../services/agents-bi-adapters/src/agents_bi_adapters/bi_extractor.py), [script_translator.py](../services/agents-bi-adapters/src/agents_bi_adapters/script_translator.py) |
| [services/agents-reversesql](../services/agents-reversesql/src/agents_reversesql) | Agents 24, 25, 26, 20 and the front-end agents 21, 22, 23 | [agents.py](../services/agents-reversesql/src/agents_reversesql/agents.py), [frontend_agents.py](../services/agents-reversesql/src/agents_reversesql/frontend_agents.py) |
| [services/orchestrator](../services/orchestrator/src/orchestrator) | The LangGraph pipeline (graph, translate subgraph, supervisor, budget), the worker, scheduled re-runs, the event consumer | [pipeline.py](../services/orchestrator/src/orchestrator/pipeline.py), [supervisor.py](../services/orchestrator/src/orchestrator/supervisor.py), [worker.py](../services/orchestrator/src/orchestrator/worker.py), [events.py](../services/orchestrator/src/orchestrator/events.py), [learning.py](../services/orchestrator/src/orchestrator/learning.py) |
| [services/kg-mcp](../services/kg-mcp/src/kg_mcp) | The knowledge service as MCP tools; the client agents use | [server.py](../services/kg-mcp/src/kg_mcp/server.py), [client.py](../services/kg-mcp/src/kg_mcp/client.py) |
| [services/hitl-service](../services/hitl-service/src/hitl_service) | Review items and decisions | [service.py](../services/hitl-service/src/hitl_service/service.py) |
| [services/api-gateway](../services/api-gateway/src/api_gateway) | HTTP entry point: uploads, runs, reads; Keycloak auth | [app.py](../services/api-gateway/src/api_gateway/app.py) |
| [apps/workbench](../apps/workbench/src) | The review workbench for reviewers and owners (Next.js + React Flow): app overview, review queue, lineage; talks only to the API gateway | [page.tsx](../apps/workbench/src/app/apps/[appId]/page.tsx), [review/page.tsx](../apps/workbench/src/app/apps/[appId]/review/page.tsx), [lib/api.ts](../apps/workbench/src/lib/api.ts) |
| [services/testbench](../services/testbench/src/testbench) | The browser bench and `adce-bench` CLI | [runner.py](../services/testbench/src/testbench/runner.py), [server.py](../services/testbench/src/testbench/server.py), [static/index.html](../services/testbench/src/testbench/static/index.html) |
| [prompts/](../prompts) | One system prompt per model route and version (`prompts/<route>/<version>.md`) | [translate_statement/v3.md](../prompts/translate_statement/v3.md) |
| [eval/](../eval) | Synthetic gold apps, bench cases, recordings of model answers, eval results | [eval/cases](../eval/cases) |
| [deploy/](../deploy) | docker compose, Helm chart, gateway configs (`deploy/gateway/*.toml`) | [deploy/gateway](../deploy/gateway) |
| [migrations/](../migrations) | Alembic migrations for Postgres (fact log, outbox, review items, event cursor, run requests) | |

Local-only folders (git-ignored): `.adce/` (bench runs, recordings made live, memory), `eval/private/` (cases
with client scripts), `scripts.md`.

---

## 3. Five ideas the code is built on

### 3.1 The agent contract

Every agent is a subclass of `BaseAgent` ([agent.py](../packages/adce-core/src/adce_core/agent.py)) with:

- a `spec` (`AgentSpec`): number, name, version, kind (`D` deterministic, `H` hybrid, `L` LLM), the events that
  trigger it, the events it emits, the **fact kinds it may write** (`writes`) and the **model routes it may use**
  (`routes`);
- `async run(job, ctx) -> AgentResult`.

`job` is a plain dict (keys of artifacts, file lists). `ctx` (`AgentContext`) carries the knowledge port `kg`, the
artifact store, `run_id`, `app_id` and the model `gateway` (or `None` when AI is off).

`AgentResult` returns `events`, the ids of `facts` it proposed, `artifacts` it wrote, `metrics` and `outputs`
(the values the next node needs). `BaseAgent.__call__` adds `agent` and `latency_ms` to the metrics; that is
where the `latency_ms` numbers on the bench come from.

`self.propose(ctx, fact, result)` is the only way an agent writes knowledge. It refuses a fact kind that is not in
`spec.writes` (`PolicyViolation`) and stamps the agent's name and version on the fact.

### 3.2 Facts, trust and evidence

[facts.py](../packages/adce-core/src/adce_core/facts.py). A `Fact` is `{kind, subject, object?, value, trust,
confidence, evidence[], agent, status, supersedes}`.

- **subject** is a readable key: `app:<app>`, `app:<app>/table:<T>`, `source:<conn>:<path>`, `memory:<hash>`.
- **trust**: `documented` (read from a file), `validated` (checked against data), `inferred` (reasoned, including
  every LLM answer), `confirmed` (a person decided). Agents can never write `confirmed`.
- **evidence**: at least one is required (`script_span`, `reload_log`, `qvd_header`, `query_result`, ...).
- **id** is a hash of the content, so proposing the same fact twice is a no-op. That is what makes re-runs safe
  and stops event loops.
- **append-only**: nothing is updated. A decision is a new fact with `supersedes = <old id>`
  (`decision_fact` in [ports.py](../packages/adce-core/src/adce_core/ports.py)). `facts(current=True)` hides
  superseded ones.

The write policy (`check_policy` in ports.py) runs in every implementation: evidence required, agent named,
confidence in 0..1, only `proposed` status.

### 3.3 Ports

Agents never import Postgres, Neo4j or Dremio. They use protocols from
[ports.py](../packages/adce-core/src/adce_core/ports.py):

- `KGPort`: `register_entity, entity, propose_fact, decide, facts, search`. Implementations: `MemoryKG` (same
  file, used by tests and the bench), `KnowledgeService` (Postgres + Graphiti), `McpKG` (the same calls over MCP
  to kg-mcp; this is what agents get in real runs).
- `DataPort`: runs SQL. `DuckDBSource` now; `DremioSource` is a stub until Dremio access (phase PD).

### 3.4 Artifacts: big things go in the store, keys go in the state

[artifacts.py](../packages/adce-core/src/adce_core/artifacts.py). Uploaded files, the BI IR, the translation,
SQL files and packs are stored under keys like `apps/<app>/bi_ir/<hash>.json`. `LocalArtifactStore` is a folder
(the bench uses a temp folder), `S3ArtifactStore` is SeaweedFS locally or GCS in the cloud. Agents pass each
other **keys**, never the objects.

### 3.5 The translation is recomputed, never stored as SQL

[runtime.py](../packages/adce-reversesql/src/adce_reversesql/runtime.py) `load_translation(store, bi_ir_key,
translation_key)`. The translation artifact holds only the **overrides**: `statement number -> SQL` from the
reviewer, memory or LLM. Every agent after 28 loads the BI IR, re-runs `compose(app, overrides)` and gets exactly
the same SQL. So if you want to know "why does table X have this SQL", the answer is always: the rules in
`compose.py`/`expressions.py`, plus the overrides listed in the translation artifact.

---

## 4. The pipeline (LangGraph), node by node

### 4.1 How LangGraph is used

The graph lives in [pipeline.py](../services/orchestrator/src/orchestrator/pipeline.py); the supervisor's policy in
[supervisor.py](../services/orchestrator/src/orchestrator/supervisor.py); the run budget in
[budget.py](../services/orchestrator/src/orchestrator/budget.py). LangGraph features used:

| Feature | Where | What for |
|---|---|---|
| `StateGraph` with a typed state | `build_graph()` | the whole run as nodes and edges |
| **Subgraph** | `build_translate_graph()`, added as node `translate` | agent 28's steps as their own nodes |
| **Conditional edges** | `_route_start`, `_after_check`, `_after_supervisor`, `_tr_next`, ... | loops and branches decided from the state |
| **`Send` fan-out** | `_fan_check`, `_fan_reconcile` | one `check_one` and one `reconcile_one` branch per table, joined before the next node |
| **Reducers** | `events`, `trace`, `decisions` (appended), `metrics`, `checks`, `validations` (merged) | parallel branches and loops add to the state without overwriting each other |
| **`interrupt()` / `Command(resume=...)`** | `ask_owner`, `resume_pipeline` | pause the run for the data owner, continue with the answers |
| **Runtime context** | `Deps` (`context_schema=Deps`) | dependencies that cannot be checkpointed |
| **Checkpointer** | Postgres in the API and the worker; in-memory in tests | resume after a crash or an interrupt |

The **model** is never called through LangChain: every call goes through our model gateway (privacy guard,
recordings, budgets, tracing). LangGraph runs the steps; the gateway calls the models; code judges the answers.

**State** (`PipelineState`): plain data only. Artifact keys (`bi_ir_key`, `translation_key`, ...), the classified
files, table lists, `tr` (the translate subgraph's working state: overrides, tried statements, the queue, outcomes,
the current statement and its attempts), `checks` and `validations` (one entry per table), `loop` (counters: steps,
repairs, diagnosed tables, asked), `next` (the supervisor's last choice), `trace` and `decisions`.

**Deps** (runtime context): `kg`, `artifacts`, `gateway` (wrapped in `RunGateway` for the run's budget),
`llm_tools`, `supervisor` (`auto`/`llm`/`rules`), `interactive` (owner questions pause the run), `max_steps`,
`budget_usd`, `max_calls`, and `work` (the translation's in-memory objects: memory, validator; rebuilt when
missing, e.g. after a restart).

**The trace** is the run timeline: every node appends `{node, at, ...what it found}`. It is in the run summary and
on the bench (Agents tab, orchestrator step). **Read the trace first when a run does something unexpected.**

### 4.2 Entry, front and the translate subgraph

#### enter
Routes to `intake` (a new run) or, for a re-run with the previous run's summary, straight to `extract`, `resolve` or
`translate` (`start_at`). See 4.5.

#### intake → agent 1 `IntakeRouter`
[intake_router.py](../services/agents-ingestion/src/agents_ingestion/intake_router.py)

- Reads: the uploaded files (`{name, key}`).
- Does: `classify_file(name, first bytes)`, using the name, extension and a regex on the first bytes, never data
  rows. Classes: `script, reload_log, qvd, report_export, app_objects, connection_map, source_extract,
  app_binary, qlikview_project, other`. A file under `sources/<space>/.../<table>.csv` is a **source extract**:
  sample data for that database table, used by the local venue.
- Writes: `bi_input` facts (what the app was given, used by re-runs); output `files` with `class` added.
- Fails: a script named oddly is classed `other` and never parsed. Check the `classified` list (trace: `intake`).

#### extract → agent 27 `BIExtractor`
[bi_extractor.py](../services/agents-bi-adapters/src/agents_bi_adapters/bi_extractor.py), built on
[adce-qlik](../packages/adce-qlik/src/adce_qlik)

The Qlik reader, in order:

1. [splitter.py](../packages/adce-qlik/src/adce_qlik/splitter.py) splits the text into statements with exact
   line spans (handles strings, `[names]`, `$(...)`, comments, `lib://`).
2. [classify.py](../packages/adce-qlik/src/adce_qlik/classify.py) finds the label (`Orders:`), prefixes
   (`LEFT JOIN (X)`, `NOCONCATENATE`, `MAPPING`, `CROSSTABLE`) and statement kind.
3. [interpreter.py](../packages/adce-qlik/src/adce_qlik/interpreter.py) "runs" the control flow statically:
   splices `$(Include=...)`, expands `SUB`/`CALL`, unrolls `FOR` loops with constant bounds (max 200),
   evaluates constant `IF`s and keeps both branches otherwise (`branch_not_evaluated`). A loop whose items are
   not constant (`FOR EACH f IN FileList(...)`, bounds from data) is **unrolled with the values the reload log
   shows**: Qlik echoes the body once per iteration with the variable expanded, so each echo gives one value
   (`_items_from_log`, issue `loop_unrolled_from_log`). Without a log, and for `DO ... LOOP`, the body runs once
   and is flagged `loop_not_unrolled`. Calls into known libraries (QlikView Components `Qvc.*`) are replaced
   by modelled statements from [libraries.py](../packages/adce-qlik/src/adce_qlik/libraries.py); that is where
   `"__generated__"."Qvc.Calendar(...)"` comes from.
4. [variables.py](../packages/adce-qlik/src/adce_qlik/variables.py) resolves `SET`/`LET` and `$(var)`.
   Values only known at reload time are recovered from the reload log echo lines; **values a reviewer or owner
   confirmed** (`variable_value` facts) are used as seeds. If neither works, the statement keeps
   `unresolved_variables` and an `unresolved_variable` issue is raised (an Ask item, and an owner question the
   supervisor can ask). This is how `"__file__"."$(vLoadfile)"` ends up in SQL.
5. [load_parser.py](../packages/adce-qlik/src/adce_qlik/load_parser.py) parses each LOAD into fields
   (expressions as a small AST, `Expr`), source, WHERE, GROUP BY, INLINE rows.
6. [model.py](../packages/adce-qlik/src/adce_qlik/model.py) `build_app` builds the **BI IR** (`BIApp`): Qlik
   tables as a sequence of steps (create, concatenate, auto-concatenate, join, keep), preceding loads, mapping
   tables, drops, renames, stores, logical sources. **QUALIFY / UNQUALIFY** rules rename matching fields to
   `Table.Field` (`_qualify`; recorded on the step as `qualify`, applied by compose), so qualified tables no
   longer associate or auto-concatenate. Tables loaded between `Section Access` and `Section Application` are
   **not data tables**: they become `BIApp.section_access` (`AccessTable`: fields, reduction fields), an
   `rls_requirement` fact and a section of the pack's review record (CP-001: Section Access is never translated).
   A RESIDENT load's `ORDER BY` is kept (`LoadOp.order_by`), for Peek()/Previous(). Embedded `SQL SELECT` is parsed in the source database's
   dialect ([sql_dialect.py](../packages/adce-qlik/src/adce_qlik/sql_dialect.py)).
7. [reload_log.py](../packages/adce-qlik/src/adce_qlik/reload_log.py) and
   [qvd.py](../packages/adce-qlik/src/adce_qlik/qvd.py) read row counts and QVD headers.

- Writes: the BI IR artifact (`bi_ir_key`); facts `bi_table`, `contains`, `reads_source`, `stores_qvd`,
  `reads_qvd`, `uses_mapping`, `variable_value`, `bi_issue`, `rls_requirement`, row counts. Metrics: `statements`, `coverage`
  (share of statements understood), `tables`, `issues`.
- Fails: coverage < 1.0 means some statements are `OTHER` (not understood). No script: the run ends with status
  `needs_inputs`.

#### resolve (function in pipeline.py)
For every logical database source, look for a confirmed `resolves_to` fact. Without one, the source is
**pending** with `waits_for: dremio_catalog (phase PD)`. Nothing blocks; a run with pending sources ends with
status `waiting_for_dremio`. A re-run for a source mapping only (`reuse_translation`) goes from here straight to
`synthesize`.

#### translate → agent 28, as a subgraph
`build_translate_graph()`. Each node is one step of `ai_loop` (section 5), so every model attempt is a separate,
checkpointed node in the trace:

| Node | Calls | Does | Next |
|---|---|---|---|
| `translate.prepare` | `ScriptTranslator.prepare`, `ai_loop.apply_rule_fixes` | rules, reviewer SQL, memory and validator loaded; learned rule corrections applied | pick |
| `translate.pick` | `ai_loop.next_statements` | takes the next root statement still untranslated; when the round's queue is empty, composes again and starts the next round (at most 5) | memory, or finish |
| `translate.memory` | `ai_loop.from_memory` | an accepted gap, or a learned answer checked on this app's data | propose (ask the model), or pick |
| `translate.propose` | `ai_loop.statement_request`, `call_model`, `judge_statement` | one `translate_statement` call, judged by code; on problems, again with the problems (at most 4 answers) | propose, or pick |
| `translate.finish` | `ScriptTranslator.finish` | learned answers recorded, gaps explained (`explain_gap`), translation artifact and `bi_transform` facts written | (back to the graph) |

The round's statements are stored in `tr.queue` exactly as composed when the round started, so prompts are the
same as the plain loop's (`translate_with_ai`) and recorded answers replay. A `skipped` statement (provider not
answering, budget used) stops translation; the next run tries again.

- Writes: the translation artifact (`translation_key`: overrides, AI notes, AI outcomes, gap analyses); facts
  `bi_transform`, `translation_memory`, `memory_check`, `gap_analysis`. Metrics: `steps`, `untranslated`,
  `ai_overrides`, `gap_analyses`, `translated_ratio`.
- The subgraph has its own input schema (`TranslateIn`: no trace, events or metrics), so it returns only what it
  adds and nothing is counted twice.

### 4.3 Synthesize, check per table, repair

#### synthesize → agent 24 `Synthesizer`
[agents.py](../services/agents-reversesql/src/agents_reversesql/agents.py)

- For each output table (still in the data model, or stored to a QVD) it renders the SQL, then
  `apply_mappings` ([resolve.py](../packages/adce-reversesql/src/adce_reversesql/resolve.py)) swaps
  placeholders (`__file__`, `__qvd__`, `__generated__`, bare database tables) for Dremio datasets from confirmed
  `resolves_to` facts or the case's `dremio_sources`. The SQL starts with a comment listing what is still
  unmapped.
- Writes: `apps/<app>/runs/<run>/sql/<table>.sql`, `sql_candidate` facts (`unmapped`, `unqualified`, `mapped`),
  chart SQL (`chart_sql`). Sets `out_tables` (the tables the pack covers) for the fan-out.
- Runs again after every accepted repair or diagnosis.

#### check_one ×N, check_join → agent 25 `QueryChecker`
`Send` gives each table its own `check_one` branch: `QueryChecker.check_one` runs
[check.py](../packages/adce-reversesql/src/adce_reversesql/check.py) `check_table`: `translated`, `composed`,
`dremio_syntax`, `transpiles_for_local_run`, `known_fields` (blocking), and `deterministic`, `inputs_in_dremio`,
`rules_only` (warnings), plus the script rule of
[semantics.py](../packages/adce-reversesql/src/adce_reversesql/semantics.py) `script_rules`: `join_fields` (warning: a
JOIN or KEEP whose sides share no field is a cartesian product). It proposes a `sql_checked` fact and returns
`{ok, failing, warnings}` (`failing`: blocking rules a repair can fix, `ai_loop.failed_rules`). `check_join` counts
the passes, runs the **chart rules** on every chart's SQL (`QueryChecker.check_charts` → `semantics.chart_rules`,
one `sql_checked` fact per visual) and emits `sql.checked`:

| Chart rule | Blocking | Checks |
|---|---|---|
| `fan_out` | yes | no aggregate is computed over a join that can repeat its records; allowed: one relation, or rows joined to a DISTINCT row-id bridge (`semantics.fan_out`, checked on the SQL itself, whoever wrote it) |
| `set_fields` | yes | every field a set modifier names is in the data model |
| `distinct_across_tables` | no | Count(DISTINCT F) where several tables hold F (Qlik counts across all of them) |
| `totals` | no | TOTAL / Aggr scope to review |

`fan_out` accepts a join only through the row-id bridge; a pre-aggregated many-to-one join is safe too but needs
key-uniqueness (grain) facts, which come with Dremio (phase PD).

#### repair → agent 25 (route `repair_sql`)
When a table fails a blocking rule (other than an untranslated statement) and the run has a model, `repair` runs
once per run: `QueryChecker.repair` → `ai_loop.repair_table` for each failing table: the model sees the statements
and the failed rules and replaces one statement's SQL (prompt
[repair_sql/v2.md](../prompts/repair_sql/v2.md), output `runtime.SqlRepair`); `judge_repair` accepts it only when
the table then passes every blocking rule (at most 2 answers per table). An accepted repair writes a new
translation artifact (outcome status `sql_repaired`) and the graph goes back to `synthesize`.

### 4.4 Reconcile per table, the supervisor, diagnose, ask the owner, record

#### reconcile_one ×N → agent 26 `ReconcilerAgent.validate`
One branch per table: the validation ladder (section 6) in the local venue, **no facts yet**. The entry keeps the
level, failed comparisons, errors, evidence, and a **mismatch classification**
([diagnose.py](../packages/adce-reversesql/src/adce_reversesql/diagnose.py) `classify`):

| Category | Symptom | Route |
|---|---|---|
| `no_data` | sources with no local data | none (the evidence is missing, not the SQL) |
| `execution_error` | the SQL fails to run | diagnose |
| `rows_more` | more rows than Qlik loaded (fan-out, duplicate keys) | diagnose |
| `rows_fewer` | fewer rows (a filter or a variable) | diagnose |
| `fields` | stored columns differ from the QVD's | diagnose |
| `values` | counts match, field aggregates differ | diagnose |
| `mapping_keys` | a mapping table has duplicate keys | review |
| `none` | nothing failed | none |

#### supervisor ([supervisor.py](../services/orchestrator/src/orchestrator/supervisor.py))
Code lists the **options** (`supervisor.options`): `diagnose:<table>` for each table whose mismatch route is
diagnose and that was not diagnosed yet (needs a model); `ask_owner` when the run is interactive and owner questions
are open; `publish` (always). Then:

- one option only → it is taken (`by: rules`, "the only option");
- step limit reached (`Deps.max_steps`, default 6) → publish;
- policy `llm` → route `plan_next` (prompt [plan_next/v1.md](../prompts/plan_next/v1.md), output `NextAction`):
  the model sees each table's level, mismatch and detail (aggregates only, tables keyed by name), the options, the
  steps left and the budget used, and picks one with a reason. An answer outside the options, or no answer,
  falls back to the rules, and the fallback is recorded;
- policy `rules` → diagnose in table order, then ask, then publish.

`auto` (the default) uses `llm` when the gateway calls a model (modes live and record) and `rules` otherwise, so
replayed runs and tests are deterministic. Every choice goes into `decisions` with `by` (`rules` or
`llm:<model>`), the reason and the options it had.

#### diagnose → agent 26 (route `diagnose_mismatch`)
`ReconcilerAgent.diagnose(job, ctx, result, [table])` → `ai_loop.diagnose_table`: the model sees the table's
statements and failed comparisons and replaces one statement's SQL; `judge_fix` accepts it only when every
comparison then passes (at most 3 answers). An accepted fix is remembered as a rule correction
(`translation_memory`, `replaces_rule`), a new translation artifact is written (status `ai_repaired`), and the graph
goes back to `synthesize` → check → reconcile with the fix. Each table is diagnosed at most once per run.

#### ask_owner (interrupt)
`interrupt({app_id, run_id, questions})` pauses the run; `run_pipeline` returns the state with `__interrupt__`. The
API records each answer as a review decision (a confirmed `variable_value` fact) and calls `resume_pipeline(...,
answer={"answered": [...]})`; the node then routes to `extract`, which reads the script again with the answers. Only
with `Deps.interactive` and a checkpointer; otherwise owner questions stay review items (Ask) and the run goes on.

#### record → agent 26 `ReconcilerAgent.record`
The final results only: one `report_sql` fact per table (the review Gate), V5 figure checks against exported
reports (`figures_validated`), `validation.json`, `figures.json`, levels; emits `report_sql.validated`. Recording
once at the end keeps one Gate item per table, whatever the loops did.

#### publish → agent 20 `Publisher`
Writes the pack under `apps/<app>/packs/<run>/`: `sql/<table>.sql`, `sql/charts/*.sql`, `lineage.json`
(OpenLineage-shaped), `validation.md`, `review.md`, `manifest.json`. Fact `pack_published`.

#### summarize
Writes `runs/<run>/summary.json` (status, artifact keys, levels, pending sources, events, metrics, decisions,
trace) and `apps/<app>/latest.json` (the pointer re-runs use), and sets the final `status`.

### 4.5 Re-entry, budgets, bounds

- **Re-entry**: `run_pipeline(start_at=..., previous=<last summary>, reuse_translation=...)` enters at `extract`,
  `resolve` or `translate` with the previous run's classified files, BI IR, translation, tables and sources. The
  event consumer uses it (section 10). Without a previous summary the run starts at `intake`.
- **Budget**: `RunGateway` counts the run's model calls and cost; past `budget_usd` or `max_calls` it raises
  `BudgetExceeded`, which agents treat like an unavailable provider (`skipped`). The supervisor sees the spend.
- **Bounds**: 5 translation rounds, 4 answers per statement, 1 repair pass (2 answers per table), 1 diagnosis per
  table (3 answers), `max_steps` supervisor decisions; LangGraph's recursion limit is set to 1000 node visits.
- **Workers**: in a deployment the API queues runs and `orchestrator.worker` runs them (section 12).

### 4.6 Adding a node

Write `async def mynode(state, runtime) -> dict` returning only the keys it changes (and a `trace` entry: `_t(name,
...)`), register it in `build_graph()`, connect it with `add_edge` or a conditional edge (a router reads the state
and returns the next node's name, or a list of `Send` for a fan-out). Keep the state plain data; put objects that
cannot be serialised in `Deps.work` and make them rebuildable.

---

## 5. Inside translation (agent 28)

Agent 28 is [script_translator.py](../services/agents-bi-adapters/src/agents_bi_adapters/script_translator.py)
with three parts: `prepare` (rules, reviewer SQL, memory, validator, reviewer decisions → a `TranslateWork`), the AI
steps in [ai_loop.py](../packages/adce-reversesql/src/adce_reversesql/ai_loop.py), and `finish` (record what was
learned, explain gaps, write the artifact and facts). `run()` does all three in one call (the bench, the CLI); the
orchestrator runs the AI steps as subgraph nodes (4.2). The order:

```
1 rules           compose(app, {})                         → SQL for everything the rules can translate
2 reviewer SQL    translation_override facts for this app  → overrides
3 memory          learned.load_memory: translation_memory facts + reviewer SQL from any app
4 rule fixes      apply_rule_fixes: learned corrections of rule output, re-checked on this app's data
5 statements      per untranslated root: from_memory → (statement_request → call_model → judge_statement)×≤4
6 record          learned.record_memory: new answers, re-checks, disproved answers
7 explain_gaps    what is still untranslated → LLM (explain_gap) → gap_analysis facts (review "Ask" items)
8 artifact + bi_transform facts
```

Correcting rule output that the data contradicts is **agent 26's** job (diagnose, 4.4), and repairing SQL that fails
a blocking rule is **agent 25's** (repair, 4.3); both use the same kind of steps (5.4, 5.7).

### 5.1 The rules: compose.py and expressions.py

[compose.py](../packages/adce-reversesql/src/adce_reversesql/compose.py): `Composer.compose()` walks the BI IR in
execution order and builds **one CTE per load step**. A Qlik table is a series of versions; each step makes a new
CTE: create = the chain's output; concatenate = `UNION ALL` with columns aligned; join = join on all shared
field names (Qlik semantics); keep = semi-join; `RESIDENT X` reads X's version current at that point.
`Translation.table_sql(T)` returns `WITH ... SELECT * FROM <final cte>`.

Constructs translated by the rules (beyond plain loads, joins, keeps, concatenation, mapping):

| Qlik | SQL | Where |
|---|---|---|
| `CROSSTABLE(attr, data[, n])` | `UNION ALL` of one SELECT per unpivoted column | `Composer._construct` |
| `IntervalMatch(F [, keys])` | values of F (from the tables loaded so far that hold F and the keys) `JOIN` the intervals `ON F BETWEEN Start AND End` (bounds included), `DISTINCT` | `Composer._intervalmatch` |
| `QUALIFY` | a projection renaming the step's fields to `Table.Field` | `Composer._step` |
| `Previous(x)` | `LAG(x) OVER (ORDER BY <the load's ORDER BY>)`; not in a load with WHERE (it reads rows the WHERE drops) | `Translator._lookback` |
| `Peek('F'[, -n / 0])` | `LAG(<F's expression>, n)` / `FIRST_VALUE(...)` over the load's ORDER BY | `Translator._lookback` |
| `Above(x[, n])`, `RangeSum(Above(x, 0, n))` (charts) | `LAG` / `SUM(...) OVER (ORDER BY dims ROWS BETWEEN n-1 PRECEDING AND CURRENT ROW)` over the chart's rows, in dimension order (the chart's sort is not known: tier T2, noted) | `ChartTranslator._value` |

Not translated, on purpose (T3): Previous()/Peek() without an ORDER BY (SQL has no load order), Peek() of a field
computed from Peek() (a running total: recursive), GENERIC, Hierarchy, Aggr().

When a step cannot be translated, compose does not guess. It records an `Untranslated` with `reason`, `tier`
(T1 = rules should handle, T2/T3 = harder), the statement `text`, its `input_columns` and `relations` (other tables
the statement names). Steps that depend on it are untranslated too (knock-on failures). `_roots()` in ai_loop
picks only the **root causes**.

[expressions.py](../packages/adce-reversesql/src/adce_reversesql/expressions.py): Qlik expression → SQL, with
Qlik's semantics made explicit (`&` treats NULL as '', division by zero → NULL, true = -1, `ApplyMap` → join to the
mapping table, `Date#` → `TO_DATE` with the format converted, and so on; the module docstring lists them all).
A function with no safe translation raises `Untranslatable`, which becomes the `Untranslated.reason`.

[dremio.py](../packages/adce-reversesql/src/adce_reversesql/dremio.py) finishes the sqlglot tree as Dremio SQL;
embedded SQL in Oracle/SQL Server is transpiled.

### 5.2 Translation memory

`TranslationMemory` in [ai_loop.py](../packages/adce-reversesql/src/adce_reversesql/ai_loop.py). Each `MemoryEntry`
is `{statement text, input columns, sql, status, app_id}`. The key is a hash of the normalised statement and its
columns (`memory_key`).

- `exact()` gives the answer for the same statement with the same columns; `similar()` gives examples for the
  prompt; `rule_fix()` gives a learned correction of a rule translation.
- Status: `ai_validated` (data confirmed it), `ai_checked` (checks passed, no data), `reviewer` (a person wrote it).
- In agents, the memory is rebuilt from facts each run
  ([learned.py](../packages/adce-reversesql/src/adce_reversesql/learned.py) `load_memory`; agents 26 and 28 both
  record into it with `record_memory`). An entry is **proven** when
  re-checks passed on 2+ apps and never failed, and **disproved** (no longer used) as soon as one app's data
  contradicts it.

### 5.3 The statement steps (route translate_statement)

The loop is a set of step functions in `ai_loop.py`; `translate_with_ai` runs them in a plain loop, the orchestrator
runs each as a node:

1. `apply_rule_fixes`: learned corrections (`rule_fix`), each re-checked on this app's data first.
2. `next_statements`: compose with the current overrides, take the root untranslated statements not tried yet (a
   round; at most `max_rounds`).
3. `from_memory(u)`: an accepted gap → `gap_accepted`; a memory hit → verified on this app's data → `memory`;
   a hit a reviewer rejected later is dropped and its SQL goes into `previous_attempts`; no model → `no_model`.
4. `statement_request(u)`: the prompt inputs: `statement`, `why_rules_failed`, `input_relation: "src"`,
   `input_columns`, `settings` (date formats, MonthNames ...), `examples` (similar memory entries),
   `previous_attempts` (earlier SQL with its problems), and `other_relations` when the statement names other
   tables; tools when on.
5. `call_model`: output invalid → try again; `PrivacyViolation` → `withheld`; provider/quota/replay miss/budget →
   `skipped` (stops; the next run retries); `GatewayRefusal` → `failed`.
6. `judge_statement`: model says not translatable → `declined`; otherwise `_verify()`: `static_problems` (parses as
   Dremio, reads only `src`, known columns), compose again, `check_table` blocking rules, then the **validator**
   (local data, section 6). Problems go back as `previous_attempts` (up to `max_attempts`). Pass →
   `ai_validated` (V3+) or `ai_checked`; stored in memory; becomes an override.

### 5.4 Diagnosis steps (agent 26, route diagnose_mismatch)

`mismatch(app, table, overrides, validator)` gives the level and failed comparisons of a table whose SQL is complete
but contradicted; `table_statements` the statements as the model sees them (text, input columns, SQL reading
`src`); `diagnosis_request` the inputs; `judge_fix` accepts a fix only when every comparison then passes, and
remembers it with `replaces_rule=True`. `diagnose_table` runs them (at most 3 answers); `repair_with_ai` runs it for
several tables (the CLI, tests).

**Sources with no local data** (`execution: no local data for: ...`) are not problems: they mean the check could
not run. The validator drops them ([local.py](../packages/adce-reversesql/src/adce_reversesql/local.py)
`MissingData`, `make_validator` in [runtime.py](../packages/adce-reversesql/src/adce_reversesql/runtime.py)), and the
classification says `no_data`, so such a table is never sent to the model.

### 5.5 explain_gaps (route explain_gap)

`gap_subjects()` collects what stays for a person: loops modelled once (`loop_not_unrolled`) and statements still
untranslated or declined. For each one, the model gets the script block and returns a `GapAnalysis`: summary,
pattern, how to do it in Dremio, questions for the data owner, proposed SQL with `<PLACEHOLDERS>`, confidence. These
become `gap_analysis` facts, which become **Ask** items in review. An owner's answer (`gap_answer`) is passed back
on the next run (`answers`).

### 5.6 Reviewer decisions the loop acts on (ADR-5)

`_decisions()`: spans a reviewer accepted as untranslated are skipped (`gap_accepted`). For tables whose SQL a
reviewer rejected, memory answers learned before the rejection are dropped, and the rejected SQL plus the reason
go into `previous_attempts`.

### 5.7 Repair steps (agent 25, route repair_sql)

`failed_rules(t, table)` lists the blocking rules a table fails (not `translated` or `composed`: those are the
translator's); `repair_request` the inputs (statements, failed rules, settings, earlier attempts); `judge_repair`
accepts a replacement only when the table then passes every blocking rule. `repair_table` runs them (at most 2
answers). The repaired SQL is still validated against the data by agent 26 afterwards.

### 5.8 The translation artifact

[runtime.py](../packages/adce-reversesql/src/adce_reversesql/runtime.py) `save_translation` / `translation_doc`:
`{bi_ir_key, overrides, ai_notes, ai_outcomes, gaps}`, keyed by the BI IR and the overrides. Agents 28, 25 and 26
each write the next version (25 and 26 add their outcomes to the list), so the latest `translation_key` in the
state always has every override that produced the SQL.

---

## 6. Validation: the V0 to V5 ladder

[reconcile.py](../packages/adce-reversesql/src/adce_reversesql/reconcile.py) `Reconciler.validate(table)`:

| Level | Means | How |
|-------|-------|-----|
| V0 | parses | no "does not parse" untranslated |
| V1 | checked | `check_table` passes |
| V2 | runs, with the right columns | the full SQL executes in the venue **and returns exactly the Qlik table's fields** (`fields` from the BI IR; tables whose fields are unknown, `["*"]`, skip the column check) |
| V3 | row counts match | each step vs the reload log's "lines fetched"; stored tables vs the QVD header |
| V4 | data matches | per-field aggregates (count, **nulls**, distinct, min/max, sum, value shapes, value fingerprints) vs the stored QVD's rows |
| V5 | figures match | chart SQL result vs an exported report (in `_figures`, [exports.py](../packages/adce-reversesql/src/adce_reversesql/exports.py)) |

The level reached is the last one passed. A failure at V2 stops the ladder there.

**Drift tolerance** (`Reconciler(tolerance=...)`, setting `ADCE_VALIDATION_TOLERANCE`, `Deps.tolerance`): when the
evidence (reload log, QVDs) and the data are not from the same moment, counts and sums within the relative
tolerance pass with basis `within tolerance (drift)`; the table is then **not exact** (`TableValidation.exact`,
`report_sql.exact`, "(within tolerance, drift)" in validation.md). Locally the tolerance is 0: the QVDs and the log
come from the same reload. Phase PD sets it (0.5% by default in the design) for Dremio data.

**Result cache**: `LocalVenue.run` caches results (up to 5,000 rows) by the data's fingerprint (the bytes of every
file loaded, plus today's date for generated inputs) and the SQL, so the graph's re-validation after a fix does not
run the same queries again. **Temporary files**: `local_evidence` extracts the uploads to a temp folder, loads them
into DuckDB (which copies them) and removes the folder; the bench removes its run folder after each run.

**The local venue** ([local.py](../packages/adce-reversesql/src/adce_reversesql/local.py), `LocalVenue`) is
DuckDB with:
- database tables read from source extracts (`sources/<space>/<folder>/<table>.csv|parquet`);
- `__qvd__` inputs read from uploaded QVDs (PyQvd);
- `__file__` inputs from `sources/files/<name>`;
- `__generated__` inputs (e.g. a calendar) generated in DuckDB.

`to_local()` rewrites the Dremio SQL onto those tables and transpiles to DuckDB. **A source with no local data
raises `MissingData`**: the table stays at V1 with an `execution: no local data for: ...` error. **That is
expected for any real script whose database tables you have not given sample data for.** It is the most common
reason for "nothing checked".

`make_validator` in [runtime.py](../packages/adce-reversesql/src/adce_reversesql/runtime.py) wraps the reconciler
for the AI loop: it returns `(level, problems)` with stable wording (the wording is part of the prompt, so
recordings replay).

---

## 7. The model gateway: every LLM call

[gateway.py](../packages/adce-gateway/src/adce_gateway/gateway.py) `ModelGateway.call(route, inputs, OutputModel)`:

```
privacy guard → memo key → recording? (replay / memo) → budget → prompt (system = prompts/<route>/<v>.md,
user = inputs as JSON) → provider (primary, then fallbacks) → validate against OutputModel → record → result
```

- **Routes** ([routes.py](../packages/adce-gateway/src/adce_gateway/routes.py)) name a tier
  (`strong/standard/light`), effort, max tokens and prompt version. Agents name a route, never a model. In use:
  `translate_statement` (v3), `explain_gap`, `repair_sql` (v2), `diagnose_mismatch` (v2), `plan_next`,
  `translate_expression`, `map_metric`; declared but not called: `classify_file`, `parse_export`.
- **Config** ([config.py](../packages/adce-gateway/src/adce_gateway/config.py), `deploy/gateway/*.toml`) maps tiers
  to `provider:model`, with fallbacks, prices and rate limits. The bench "Model" picker chooses one of these files.
- **Providers** ([providers.py](../packages/adce-gateway/src/adce_gateway/providers.py)): Anthropic API, Vertex,
  Bedrock, and any OpenAI-compatible API (Groq, a local server, the Claude Code bridge).
- **Modes**: `replay` (recordings only; a missing one is `ReplayMiss`), `record` (call and save), `live`.
  Recordings are content-addressed by `memo_key`: route, model, prompt version, system prompt, inputs, schema. Any
  change to a prompt or an input changes the key, so the call goes live again.
- **Privacy guard** ([privacy.py](../packages/adce-gateway/src/adce_gateway/privacy.py)): rejects inputs that look
  like data rows (`PrivacyViolation`). Models see script text, schemas, column names and aggregate differences,
  never rows.
- **Tools** ([tools.py](../packages/adce-gateway/src/adce_gateway/tools.py)): with `--tools`, the model can call
  read-only tools (`check_sql`, `get_statements`, kg-mcp lookups) before it answers through `submit_answer`.
- **Claude Code bridge** (local branch only, `tools/claude-code-bridge/bridge.py`): an OpenAI-compatible server on
  port 8790 that answers by running `claude -p`. It is slow (often 20 to 120 s per call) and has a timeout. A
  timeout shows as `ProviderError ... 504 claude timed out` and the statement becomes `skipped`.

---

## 8. Knowledge: facts, kg-mcp, Postgres and Graphiti

- **Record of truth:** the Postgres fact log ([fact_log.py](../packages/adce-kg/src/adce_kg/fact_log.py)). Each
  write commits the fact and an **outbox event** (`fact.proposed`, `fact.confirmed`, `fact.rejected`,
  `entity.registered`) in one transaction.
- **Graph:** [graphiti_projection.py](../packages/adce-kg/src/adce_kg/graphiti_projection.py) projects the outbox
  into Graphiti on Neo4j (entities = nodes, facts = edges, evidence = episodes, superseded edges expired). It can
  always be rebuilt from the log. It is used for search and context packs.
- **KnowledgeService** ([service.py](../packages/adce-kg/src/adce_kg/service.py)): the `KGPort` over both.
- **kg-mcp** ([server.py](../services/kg-mcp/src/kg_mcp/server.py)): the same operations as MCP tools; the only way
  agents reach knowledge in a deployment (ADR-2). `McpKG` ([client.py](../services/kg-mcp/src/kg_mcp/client.py))
  is the client: it buffers writes and flushes in batches, and `tools()` exposes **read** tools to a model. The
  bench runs an in-process kg-mcp over a `MemoryKG` (`McpKG.serving(...)`), which is why its results show
  `kg-mcp calls`.
- **Feedback history** ([feedback.py](../packages/adce-core/src/adce_core/feedback.py)): pairs each reviewer
  decision with what it decided on; tool `get_feedback_history`.

Fact kinds you will see, by writer:

| Writer | Kinds |
|--------|-------|
| 1 intake | `bi_input` |
| 27 extract | `bi_table`, `contains`, `reads_source`, `stores_qvd`, `reads_qvd`, `uses_mapping`, `variable_value`, `bi_issue`, row counts |
| 28 translate | `bi_transform`, `translation_memory`, `memory_check`, `gap_analysis` |
| 24 synthesize | `sql_candidate`, `chart_sql` |
| 25 check / repair | `sql_checked` |
| 26 reconcile / diagnose / record | `report_sql`, `figures_validated`, `translation_memory` and `memory_check` (diagnosis) |
| 20 publish | `pack_published` |
| reviewers | decisions on `report_sql`; `translation_override`, `bi_gap_accepted`, `variable_value`, `resolves_to`, `gap_answer`, `bi_issue_acknowledged` |

---

## 9. Review (human in the loop)

[hitl_service/service.py](../services/hitl-service/src/hitl_service/service.py). `ReviewService.refresh(app)` builds
review items **from facts** after a run; nothing in the pipeline waits for a person.

| Mode | Item | From | Decisions → fact |
|------|------|------|------------------|
| Ask | unresolved variable | `bi_issue` | answer → `variable_value` |
| Ask | gap | `gap_analysis` | answer → `gap_answer`; accept_gap → `bi_gap_accepted` |
| Queue | untranslated statement | `bi_transform.untranslated` | provide_sql → `translation_override`; accept_gap |
| Queue | other issues | `bi_issue` | acknowledge |
| Gate | approve SQL | `report_sql` (proposed) | approve / reject (with reason) → decision on `report_sql` |
| Seed | map source | `reads_source`, `sql_candidate.unmapped` | map → `resolves_to` |

Items are ranked by `impact × uncertainty`. Every decision goes through `KGPort.decide` or `propose`, so it lands
in the outbox and triggers re-runs (section 10).

---

## 10. Learning and event-driven re-runs

- [learning.py](../services/orchestrator/src/orchestrator/learning.py): `rerun_all` re-runs every application
  from its stored uploads (daily CronJob). Each run re-checks memory answers on that app's data, so answers get
  proven or disproved over time. `memory_report` shows what is learned and its evidence.
- [events.py](../services/orchestrator/src/orchestrator/events.py): `EventConsumer` reads the outbox after its
  saved position (`event_cursor` table), works out which apps an event affects (`affected()`; the table at the top
  of the file lists the triggers), and re-runs each affected app once per batch. Because fact ids are content
  hashes, a re-run that proposes the same facts emits nothing new, so events do not loop.
- **Re-runs enter at the node the change affects** (`events.start_for`): a variable's value → `extract`; a source
  mapping only → `resolve` with the translation reused; reviewer SQL, a gap answer, a rejection, a disproved
  answer → `translate`. `rerun_all(start_at=...)` loads the last run's summary (`learning.latest_summary`, from
  `apps/<app>/latest.json`); without one it runs from the start.

---

## 11. Front-end agents (charts, exports)

These are **not in the LangGraph pipeline yet**. They run in the bench chain mode only.

| # | Agent | File | Does |
|---|-------|------|------|
| 23 | `JoinPlanner` | [frontend_agents.py](../services/agents-reversesql/src/agents_reversesql/frontend_agents.py) | The data model's associations (shared field names), synthetic keys, cycles; join plans for charts |
| 29 | `ExpressionTranslator` | [expression_translator.py](../services/agents-bi-adapters/src/agents_bi_adapters/expression_translator.py) | Chart measures → SQL aggregations (route `translate_expression` for the rest) |
| 21 | `ReportParser` | frontend_agents.py | Reads exported reports (xlsx/csv) |
| 22 | `MetricMapper` | frontend_agents.py | Matches report columns to chart measures (route `map_metric` when labels differ) |

Chart SQL itself is built by [charts.py](../packages/adce-reversesql/src/adce_reversesql/charts.py)
(`ChartTranslator`), which handles Qlik's association semantics (no fan-out, outer association, set analysis,
TOTAL).

---

## 12. Services: API gateway, hitl-service, kg-mcp, workbench

[api_gateway/app.py](../services/api-gateway/src/api_gateway/app.py):

| Endpoint | Does |
|----------|------|
| `POST /apps/{app}/files` | upload files to `uploads/<app>/<sha>/<name>` |
| `POST /apps/{app}/runs` | run the LangGraph pipeline on the uploads (through kg-mcp when configured), then refresh review; with `ADCE_ORCHESTRATOR_MODE=worker` it queues the run instead (`status: queued`) |
| `GET /apps/{app}/runs/{run}` | the run summary; for a queued run its request status (queued, running, waiting_for_owner, error) |
| `POST /apps/{app}/runs/{run}/answers` | answers to a paused run's owner questions (`{answers: {review item id: value}}`): recorded as review decisions, then the run resumes (or a resume is queued) |
| `GET /apps`, `/apps/{app}/overview`, `/lineage`, `/tables`, `/packs/{run}/files/...`, `/facts` | reads |
| `/review/...` | hitl-service router ([api.py](../services/hitl-service/src/hitl_service/api.py)): items, decisions |

The **review workbench** ([apps/workbench](../apps/workbench/src)) is the product UI on top of these endpoints: an app
overview (inputs, coverage, level per table), the review queue (Ask, Queue, Gate, Seed items and decisions) and a lineage
graph. Its `/api/...` route proxies to the API gateway. The test bench (section 13) is an internal debugging tool, not
the product UI.

A run paused by `ask_owner` answers `status: waiting_for_owner` with `questions` (`run_view`). Settings:
`ADCE_INTERACTIVE_RUNS` (pause for owner questions), `ADCE_RUN_BUDGET_USD`, `ADCE_RUN_MAX_CALLS`, `ADCE_SUPERVISOR`
(`auto`/`llm`/`rules`), `ADCE_ORCHESTRATOR_MODE` (`inline`/`worker`).

**The orchestrator worker** ([worker.py](../services/orchestrator/src/orchestrator/worker.py)): `python -m
orchestrator.worker` takes queued requests from table `run_request` (migration 0004; `FOR UPDATE SKIP LOCKED`, so
several workers share the queue), runs or resumes the pipeline with the Postgres checkpointer, refreshes the review
queue, and stores the outcome on the request. A request left running by a crash is taken again after 30 minutes and
continues from its checkpoint. Helm: `worker` (Deployment `orchestrator-worker`); the API is set to queue.

Auth: Keycloak bearer tokens with roles viewer / reviewer / approver / admin ([auth.py](../services/api-gateway/src/api_gateway/auth.py)).

---

## 13. The test bench

[runner.py](../services/testbench/src/testbench/runner.py), [server.py](../services/testbench/src/testbench/server.py),
[static/index.html](../services/testbench/src/testbench/static/index.html).

A **case** (`eval/cases/<suite>/<name>/case.yaml` + files) is an app plus what to expect. A **variant** is
`ai` (off / replay / live), `data` (full, no_qvd, no_log, script_only, script_bare), `tools`, `model`.

`CaseRun.run()`:
1. uploads the case files into a temp artifact store;
2. starts an in-process kg-mcp over a fresh `MemoryKG`;
3. runs either **a chain of agents** (each agent separately: the Agents tab then shows each one's job, facts and
   artifacts) or **the orchestrator** (agent number 0: the whole LangGraph pipeline as one step);
4. `_check()` compares with the case's expectations; `_rows()` runs each table's SQL locally and compares rows
   with `expected_rows`;
5. status: `pass`, `fail`, `error`, `xfail`/`xpass` for known gaps, and **`unchecked`** when nothing could be
   compared. The result now also carries `notes` saying why.

Two things to know about checks:
- **Orchestrator mode runs fewer checks than chain mode.** Per-agent checks (fields, issue codes, blocking
  rules) need each agent's own facts and run only in chain mode. In orchestrator mode the checks are metrics,
  levels, pack files and rows.
- **A playground or newly saved case has no expectations** until you click "Approve result". Until then there is
  nothing to compare, so the run is `unchecked`.

**Orchestrator mode shows the run timeline**: on the Agents tab, the orchestrator step lists the supervisor's
decisions (action, table, who decided, why, the options it had) and every node the run visited with what it found
(the `trace`). Live runs use the LLM supervisor (`auto`); replay and AI off use the rules.

"Model calls" lists every gateway call with route, source (`live`, `memo` = answered from a recording, `replay`),
tokens, latency and error.

---

## 14. Debugging playbook

| You see | It means | Look at |
|---------|----------|---------|
| `0 checks`, status `unchecked` | The case has no expectations, and/or the SQL could not run locally | Result notes; Rows tab errors; "Approve result" to set expectations |
| `execution: no local data for: X` | Table X has no sample data in the case | Add `sources/<space>/<folder>/<table>.csv` to the case, or wait for Dremio (PD). Not a translation bug |
| Level stays V1 | SQL checked but did not run (see the error) | `report_sql.errors`, `validation.md` |
| Level V2, not V3 | Runs, but row counts differ from the reload log / QVD header | `report_sql.failed` comparisons, then which step (`rows_step <action> <span>`) |
| Level V3, not V4 | Counts match, values differ | `field_aggregate` comparisons: which field, `shapes`/`values` |
| `untranslated` > 0 | Rules could not translate some statements | `bi_transform.untranslated[].reason`; then AI outcomes |
| AI outcome `skipped` | Provider did not answer (quota, timeout, no recording in replay) | Model calls tab: the error |
| AI outcome `declined` | The model said it cannot be done in SQL | `notes`; it becomes a gap analysis |
| AI outcome `failed` | All attempts had problems | `attempts[].problems` in the translation artifact |
| `withheld` | The privacy guard saw something row-like in the inputs | the PrivacyViolation message |
| `"__file__"."..."`, `"__generated__"."..."`, bare table names in SQL | Source not mapped to a Dremio dataset | SQL header comment; Dremio sources panel; review "map source" items |
| `$(vSomething)` in a path | Variable unresolved (only known at reload time) | `unresolved_variable` issue; give a reload log, or answer the Ask item |
| Translate takes minutes | Live calls through a slow provider (the bridge) | Model calls latency; trace `translate.propose` rows; use replay or a faster config |
| A table was not fixed | The supervisor did not choose diagnose for it, or the fix was rejected | Supervisor decisions (options, by, reason); trace `diagnose` (status, notes); `ai_outcomes` in the translation |
| `fallback: ...` in a decision | The LLM supervisor's answer could not be used | the fallback text (no recording, provider error, or a choice outside the options) |
| Run status `waiting_for_owner` | An interactive run asked owner questions | `questions` in the run; answer with `POST .../answers` |
| `sql_repaired` in AI outcomes | Agent 25 replaced a statement that failed a blocking rule | trace `repair`; the outcome's notes |
| Level V1 with a `schema` comparison | The SQL runs but its columns differ from the Qlik table's fields | the comparison's expected vs got (missing / extra columns) |
| `fan_out` failing on a chart | an aggregate over a join that can repeat records | the rule message; the chart's SQL (`sql/charts/<visual>.sql`) |
| `join_fields` warning | a JOIN/KEEP with no shared field (cartesian product in Qlik too) | the script lines named in the warning |
| `loop_unrolled_from_log` issue | a file-list loop was unrolled with the values the reload log showed | the issue message lists them; check they are all the files |
| `section_access` issue | the script has Section Access; it is a requirement, not SQL | review.md "Row-level security"; `rls_requirement` facts |
| Level shown "(within tolerance, drift)" | counts or sums differ within the tolerance | the comparisons with basis `within tolerance (drift)` |
| Status `queued` for long | No orchestrator worker is running | `orchestrator-worker` pod / `python -m orchestrator.worker` |

### Worked example: the run on 2026-10-16 (playground case, orchestrator, claude-code-sonnet)

What the run file (`.adce/bench/last/playground_6db27d5cf8__...json`) shows:

- extract: 54 statements, coverage 1.0, 7 tables, 3 issues. Parsing was fine.
- translate: 14 steps, 0 untranslated, 1 override. The rules translated everything.
- check: 5 of 5 tables passed.
- rows: STORAGE, TRADE, PRICING and LastBalance failed with `no local data for: Atlas SE04.Business.ASSIGNMENTS,
  DEALS, OG_OPERATORS, STORAGE_BALANCES, ...`. The case has no sample data for those database tables, so their
  SQL cannot run locally. BUY_SELL_MAPPINGS ran (0 rows).
- model calls: 4 × `diagnose_mismatch`. The repair step treated "no local data" as a mismatch and asked the model
  to fix correct SQL. The model invented causes, rewrote SQL (one answer replaced a table with an empty `WHERE 1 = 0`
  query), and the fourth call timed out after minutes. That timeout is most of the 486 s in translate.
- checks: 0, because a playground case has no expectations and orchestrator mode skips per-agent checks.

Fixed: missing data is no longer sent to the model, and such a run is `unchecked` with notes that say why. To
check that script for real, add sample data for those Atlas tables to the case (or connect Dremio), then
"Approve result" once the rows look right.

---

## 15. What is declared but not wired yet

Be aware of these when you read specs:

- Routes `classify_file` and `parse_export` have prompts but no callers; intake and report parsing are
  deterministic.
- Agents 21, 22, 23 and 29 are not in the LangGraph graph (bench chain mode only).
- `DremioSource` is a stub; every validation is local until phase PD.
- Re-runs restart at a node for the whole application, not only the affected tables.
- No grain (key uniqueness) facts yet, so `fan_out` cannot accept pre-aggregated many-to-one joins (phase PD).
- Grouped comparisons per dimension member (to pinpoint mismatching rows) are not built; the diagnosis gets
  per-field aggregates.
- A crash in the middle of translation keeps the overrides and outcomes (state), but answers learned earlier in that
  run and not yet recorded are rebuilt from the facts, so their examples may differ in the resumed run.

See [design-review.md](design-review.md) for the full list of gaps against the design and the plan.

---

## 16. Glossary

- **BI IR**: the tool-neutral model of the BI app (`BIApp`): tables, steps, fields, sources, variables, visuals.
- **CTE**: one `WITH name AS (...)` block; the translation is one CTE per Qlik load step.
- **Override**: SQL for one statement number that replaces the rules' output (reviewer, memory or LLM).
- **Untranslated / root**: a statement the rules could not translate / one that does not fail only because an
  earlier one failed.
- **Tier T1/T2/T3**: how hard a construct is; T1 should be handled by rules.
- **Local venue**: DuckDB over the case's sample data and QVDs; stand-in for Dremio.
- **Placeholder source**: `"__file__"`, `"__qvd__"`, `"__generated__"` schemas in SQL; they need a Dremio dataset.
- **Recording / memo**: a saved model answer keyed by the exact request; used for replay and to avoid paying twice.
- **Outbox**: the ordered event list written with each fact; what re-runs and the graph projection follow.
- **Trust**: documented / validated / inferred / confirmed (only people confirm).
- **Supervisor**: the graph node that picks the next action after reconciliation (diagnose, ask the owner, publish).
- **Options**: the actions code allows at that point; the supervisor (model or rules) chooses among them.
- **Fan-out (`Send`)**: one graph branch per table, joined before the next node.
- **Interrupt / resume**: a run paused for the data owner (`ask_owner`) and continued with the answers.
- **Re-entry**: a re-run that starts at a later node with the previous run's results.
- **Trace**: the run timeline, one entry per node visited.
- **Fan-out (double counting)**: an aggregate over a join that repeats the aggregated records; Qlik never does it.
- **Drift**: evidence and data from different moments; small differences within the tolerance are not mismatches.
- **QUALIFY**: Qlik's renaming of fields to `Table.Field`.
