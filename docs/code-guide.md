# Code guide: how ADCE works, file by file

This guide explains the code as it is today: what happens when you run the pipeline, which file does each step,
what every agent reads and writes, where LangGraph and the models come in, and where to look when something fails.
It is written to be read next to the code; every section links the files it describes.

For why the system is built this way, see [architecture.md](architecture.md). For how to run things, see
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
12. [Services: API gateway, hitl-service, kg-mcp](#12-services-api-gateway-hitl-service-kg-mcp)
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

**How:** a fixed chain of agents, run by a LangGraph graph:

```
files ─▶ intake (1) ─▶ extract (27) ─▶ resolve ─▶ translate (28) ─▶ synthesize (24) ─▶ check (25)
                                                                                           │
          summary ◀── publish (20) ◀── reconcile (26) ◀──────────────────────────────────┘
```

| # | Node | Agent class | In one sentence |
|---|------|-------------|-----------------|
| 1 | intake | `IntakeRouter` | Decides what each file is (script, log, QVD, sample data, ...). |
| 27 | extract | `BIExtractor` | Parses the script into the BI IR: tables, load steps, fields, sources, variables. |
| – | resolve | (function) | Lists the database sources that still need a Dremio dataset. |
| 28 | translate | `ScriptTranslator` | Turns every load statement into SQL: rules first, then memory, then the LLM. |
| 24 | synthesize | `Synthesizer` | Writes one `.sql` file per table, with sources mapped to Dremio names. |
| 25 | check | `QueryChecker` | Static rule checks on each table's SQL (parses, known fields, ...). |
| 26 | reconcile | `ReconcilerAgent` | Runs the SQL on local data and compares with the reload log and QVDs (V0 to V5). |
| 20 | publish | `Publisher` | Writes the migration pack (SQL, validation.md, review.md, lineage, manifest). |

**Deterministic first, LLM second.** Most of the work is plain code (a Qlik parser and a translator built on
sqlglot). The LLM is used in three places only, all inside agent 28:

1. `translate_statement`: a statement the rules could not translate;
2. `diagnose_mismatch`: a table whose SQL runs but whose data disagrees with the evidence;
3. `explain_gap`: what is still untranslated, explained for a person, with questions for the data owner.

Every LLM answer is checked by code before it is used, and every accepted answer is remembered (translation
memory), so the next run of any application reuses it without calling the model.

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
| [services/orchestrator](../services/orchestrator/src/orchestrator) | The LangGraph pipeline, scheduled learning re-runs, the event consumer | [pipeline.py](../services/orchestrator/src/orchestrator/pipeline.py), [events.py](../services/orchestrator/src/orchestrator/events.py), [learning.py](../services/orchestrator/src/orchestrator/learning.py) |
| [services/kg-mcp](../services/kg-mcp/src/kg_mcp) | The knowledge service as MCP tools; the client agents use | [server.py](../services/kg-mcp/src/kg_mcp/server.py), [client.py](../services/kg-mcp/src/kg_mcp/client.py) |
| [services/hitl-service](../services/hitl-service/src/hitl_service) | Review items and decisions | [service.py](../services/hitl-service/src/hitl_service/service.py) |
| [services/api-gateway](../services/api-gateway/src/api_gateway) | HTTP entry point: uploads, runs, reads; Keycloak auth | [app.py](../services/api-gateway/src/api_gateway/app.py) |
| [services/testbench](../services/testbench/src/testbench) | The browser bench and `adce-bench` CLI | [runner.py](../services/testbench/src/testbench/runner.py), [server.py](../services/testbench/src/testbench/server.py), [static/index.html](../services/testbench/src/testbench/static/index.html) |
| [prompts/](../prompts) | One system prompt per model route and version (`prompts/<route>/<version>.md`) | [translate_statement/v3.md](../prompts/translate_statement/v3.md) |
| [eval/](../eval) | Synthetic gold apps, bench cases, recordings of model answers, eval results | [eval/cases](../eval/cases) |
| [deploy/](../deploy) | docker compose, Helm chart, gateway configs (`deploy/gateway/*.toml`) | [deploy/gateway](../deploy/gateway) |
| [migrations/](../migrations) | Alembic migrations for Postgres (fact log, outbox, review items, event cursor) | |

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

LangGraph is used in one file, [pipeline.py](../services/orchestrator/src/orchestrator/pipeline.py), and only as a
**state machine with checkpoints**. No LLM agent framework is involved: there are no LangChain chains, LLM
tool-routing or LangGraph prebuilt agents. Each node is an ordinary async function that calls one agent.

- **State** (`PipelineState`, a `TypedDict`): plain data only (app id, run id, file list, artifact keys, table
  names, levels, pending sources). Two fields are *reducers*: `events` is appended (`operator.add`) and `metrics`
  is merged (`_merge`), so each node adds its part without overwriting the others.
- **Dependencies** (`Deps`: `kg`, `artifacts`, `gateway`, `llm_tools`) cannot be checkpointed, so they travel in
  LangGraph's **runtime context** (`StateGraph(PipelineState, context_schema=Deps)`; each node gets
  `runtime: Runtime[Deps]`). `_ctx()` turns state + runtime into the agent's `AgentContext`.
- **Edges** are fixed and linear. The only branch is `_after_extract`: if extraction found no script
  (`status = needs_inputs`) the run ends.
- **Checkpointer**: the API gateway passes a Postgres checkpointer, so a crashed run resumes from the last
  finished node (thread id `"<app>:<run>"`). The bench runs without one.
- `run_pipeline(...)` compiles the graph and calls `graph.ainvoke(state, config, context=deps)`.

To add a node: write `async def mynode(state, runtime) -> dict` that returns only the keys it changes, then
`g.add_node` and `g.add_edge` in `build_graph()`.

### 4.2 Node by node

For each node: what it reads, what it does, what it writes, how it typically fails.

#### intake → agent 1 `IntakeRouter`
[intake_router.py](../services/agents-ingestion/src/agents_ingestion/intake_router.py)

- Reads: the uploaded files (`{name, key}`).
- Does: `classify_file(name, first bytes)`, using the name, extension and a regex on the first bytes, never data
  rows. Classes: `script, reload_log, qvd, report_export, app_objects, connection_map, source_extract,
  app_binary, qlikview_project, other`. A file under `sources/<space>/.../<table>.csv` is a **source extract**:
  sample data for that database table, used by the local venue.
- Writes: `bi_input` facts (what the app was given, used by re-runs); output `files` with `class` added.
- Fails: a script named oddly is classed `other` and never parsed. Check the `classified` list.

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
   evaluates constant `IF`s and keeps both branches otherwise (`branch_not_evaluated`). `DO ... LOOP` runs
   once and is flagged `loop_not_unrolled`. Calls into known libraries (QlikView Components `Qvc.*`) are replaced
   by modelled statements from [libraries.py](../packages/adce-qlik/src/adce_qlik/libraries.py); that is where
   `"__generated__"."Qvc.Calendar(...)"` comes from.
4. [variables.py](../packages/adce-qlik/src/adce_qlik/variables.py) resolves `SET`/`LET` and `$(var)`.
   Values only known at reload time are recovered from the reload log echo lines. If neither works, the
   statement keeps `unresolved_variables` and an `unresolved_variable` issue is raised. This is how
   `"__file__"."$(vLoadfile)"` ends up in SQL.
5. [load_parser.py](../packages/adce-qlik/src/adce_qlik/load_parser.py) parses each LOAD into fields
   (expressions as a small AST, `Expr`), source, WHERE, GROUP BY, INLINE rows.
6. [model.py](../packages/adce-qlik/src/adce_qlik/model.py) `build_app` builds the **BI IR** (`BIApp`): Qlik
   tables as a sequence of steps (create, concatenate, auto-concatenate, join, keep), preceding loads, mapping
   tables, drops, renames, stores, logical sources. Embedded `SQL SELECT` is parsed in the source database's
   dialect ([sql_dialect.py](../packages/adce-qlik/src/adce_qlik/sql_dialect.py)).
7. [reload_log.py](../packages/adce-qlik/src/adce_qlik/reload_log.py) and
   [qvd.py](../packages/adce-qlik/src/adce_qlik/qvd.py) read row counts and QVD headers.

- Writes: the BI IR artifact (`bi_ir_key`); facts `bi_table`, `contains`, `reads_source`, `stores_qvd`,
  `reads_qvd`, `uses_mapping`, `variable_value`, `bi_issue`, row counts. Metrics: `statements`, `coverage`
  (share of statements understood), `tables`, `issues`.
- Fails: coverage < 1.0 means some statements are `OTHER` (not understood). Issues are listed as `bi_issue` facts
  and show in the review queue.

#### resolve (function in pipeline.py)

For every logical database source, look for a confirmed `resolves_to` fact. Without one, the source is
**pending** with `waits_for: dremio_catalog (phase PD)`. This only records the fact; nothing blocks. A run with
pending sources ends with status `waiting_for_dremio` instead of `complete`.

#### translate → agent 28 `ScriptTranslator`
The core of the system. Section 5 covers it in full.

- Writes: the translation artifact (`translation_key`: overrides, AI notes, AI outcomes, gap analyses); facts
  `bi_transform` (per table: final CTE, untranslated statements with reason, tier and text), `translation_memory`,
  `memory_check`, `gap_analysis`. Metrics: `steps`, `untranslated`, `ai_overrides`, `gap_analyses`,
  `translated_ratio`.

#### synthesize → agent 24 `Synthesizer`
[agents.py](../services/agents-reversesql/src/agents_reversesql/agents.py)

- For each output table (still in the data model, or stored to a QVD) it renders the SQL, then
  `apply_mappings` ([resolve.py](../packages/adce-reversesql/src/adce_reversesql/resolve.py)) swaps
  placeholders (`__file__`, `__qvd__`, `__generated__`, bare database tables) for Dremio datasets from confirmed
  `resolves_to` facts or the case's `dremio_sources`. The SQL starts with a comment listing what is still
  unmapped.
- Writes: `apps/<app>/runs/<run>/sql/<table>.sql`, `sql_candidate` facts (`unmapped`, `unqualified`, `mapped`),
  chart SQL (`chart_sql`).

#### check → agent 25 `QueryChecker`
[check.py](../packages/adce-reversesql/src/adce_reversesql/check.py) `check_table`: rules per table:
`translated`, `composed`, `dremio_syntax`, `transpiles_for_local_run`, `known_fields` (blocking), and
`deterministic`, `inputs_in_dremio`, `rules_only` (warnings). Writes `sql_checked`.

#### reconcile → agent 26 `ReconcilerAgent`
Runs the validation ladder (section 6) for every output table in the **local venue**, then V5 figure checks
against exported reports. Writes `report_sql` (level, venue, comparisons, errors), `figures_validated`,
`validation.json`, `figures.json`. Results are always labelled `venue: local`.

#### publish → agent 20 `Publisher`
Writes the pack under `apps/<app>/packs/<run>/`: `sql/<table>.sql`, `sql/charts/*.sql`, `lineage.json`
(OpenLineage-shaped), `validation.md`, `review.md`, `manifest.json`. Fact `pack_published`.

#### summarize
Writes `runs/<run>/summary.json` and the final `status`.

---

## 5. Inside translation (agent 28)

Entry point: `ScriptTranslator.run` in
[script_translator.py](../services/agents-bi-adapters/src/agents_bi_adapters/script_translator.py). The order:

```
1 rules           compose(app, {})                         → SQL for everything the rules can translate
2 reviewer SQL    translation_override facts for this app  → overrides
3 memory          _memory(): translation_memory facts + reviewer SQL from any app
4 translate_with_ai   untranslated root statements → memory or LLM (translate_statement) → verified → overrides
5 repair_with_ai      tables that run but contradict the data → LLM (diagnose_mismatch) → verified → overrides
6 record          memory_check facts, new translation_memory facts
7 explain_gaps    what is still untranslated → LLM (explain_gap) → gap_analysis facts (review "Ask" items)
8 artifact + bi_transform facts
```

### 5.1 The rules: compose.py and expressions.py

[compose.py](../packages/adce-reversesql/src/adce_reversesql/compose.py): `Composer.compose()` walks the BI IR in
execution order and builds **one CTE per load step**. A Qlik table is a series of versions; each step makes a new
CTE: create = the chain's output; concatenate = `UNION ALL` with columns aligned; join = join on all shared
field names (Qlik semantics); keep = semi-join; `RESIDENT X` reads X's version current at that point.
`Translation.table_sql(T)` returns `WITH ... SELECT * FROM <final cte>`.

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
- In agents, the memory is rebuilt from facts each run (`ScriptTranslator._memory`). An entry is **proven** when
  re-checks passed on 2+ apps and never failed, and **disproved** (no longer used) as soon as one app's data
  contradicts it.

### 5.3 translate_with_ai (route translate_statement)

1. Apply learned rule corrections (`rule_fix`), each re-checked on this app's data first.
2. Up to `max_rounds` rounds: compose, take the root untranslated statements, and for each one call `_one()`.
3. `_one()`: memory hit → verify → use. Otherwise build the prompt inputs: `statement`, `why_rules_failed`,
   `input_relation: "src"`, `input_columns`, `settings` (date formats, MonthNames ...), `examples` (similar
   memory entries), `previous_attempts` (earlier SQL with its problems), and `other_relations` when the statement
   names other tables. Call the gateway. Outcome:
   - output invalid → try again;
   - `PrivacyViolation` → `withheld`;
   - provider/quota/replay miss → `skipped` (stops the round; the next run retries);
   - model says not translatable → `declined`;
   - otherwise `_verify()`: `static_problems` (parses as Dremio, reads only `src`, known columns), compose
     again, `check_table` blocking rules, then the **validator** (local data, section 6). Any problem goes
     back as `previous_attempts` for the next try (up to `max_attempts`).
   - pass → `ai_validated` (V3+) or `ai_checked`; stored in memory; becomes an override.

### 5.4 repair_with_ai (route diagnose_mismatch)

For tables that are fully translated (often by the rules) but whose validator reports problems: wrong row counts,
different value sets, or SQL that fails to execute. The model sees each statement of the table with its SQL and
the failed comparisons, names the statement to replace and gives new SQL. The same verify-and-retry loop runs (max
3). An accepted fix is stored with `replaces_rule=True`; that is a learned rule correction.

**Since 2026-10-16:** sources with no local data (`execution: no local data for: ...`) are **not** problems. They
mean the check could not run; they say nothing about the SQL. Before this, those errors went to the model, which
then "fixed" correct SQL. See [local.py](../packages/adce-reversesql/src/adce_reversesql/local.py) `MissingData` and
`make_validator` in [runtime.py](../packages/adce-reversesql/src/adce_reversesql/runtime.py).

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

---

## 6. Validation: the V0 to V5 ladder

[reconcile.py](../packages/adce-reversesql/src/adce_reversesql/reconcile.py) `Reconciler.validate(table)`:

| Level | Means | How |
|-------|-------|-----|
| V0 | parses | no "does not parse" untranslated |
| V1 | checked | `check_table` passes |
| V2 | runs | the full SQL executes in the venue |
| V3 | row counts match | each step vs the reload log's "lines fetched"; stored tables vs the QVD header |
| V4 | data matches | per-field aggregates (count, distinct, min/max, value shapes, value fingerprints) vs the stored QVD's rows |
| V5 | figures match | chart SQL result vs an exported report (in `_figures`, [exports.py](../packages/adce-reversesql/src/adce_reversesql/exports.py)) |

The level reached is the last one passed. A failure at V2 stops the ladder there.

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
  (`strong/standard/light`), effort, max tokens and prompt version. Agents name a route, never a model.
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
| 25 check | `sql_checked` |
| 26 reconcile | `report_sql`, `figures_validated` |
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

## 12. Services: API gateway, hitl-service, kg-mcp

[api_gateway/app.py](../services/api-gateway/src/api_gateway/app.py):

| Endpoint | Does |
|----------|------|
| `POST /apps/{app}/files` | upload files to `uploads/<app>/<sha>/<name>` |
| `POST /apps/{app}/runs` | run the LangGraph pipeline on the uploads (through kg-mcp when configured), then refresh review |
| `GET /apps/{app}/runs/{run}` | the run summary |
| `GET /apps`, `/apps/{app}/overview`, `/lineage`, `/tables`, `/packs/{run}/files/...`, `/facts` | reads |
| `/review/...` | hitl-service router ([api.py](../services/hitl-service/src/hitl_service/api.py)): items, decisions |

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
| Translate takes minutes | Live calls through a slow provider (the bridge) | Model calls latency; use replay or a faster config |

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

- Route `repair_sql` is declared on agent 25, but `QueryChecker` makes no model calls.
- Agent 26 declares `diagnose_mismatch`, but the diagnosis runs inside agent 28 (`repair_with_ai`), so corrected SQL
  flows to synthesis, checks and the pack in the same run.
- Routes `classify_file` and `parse_export` have prompts but no callers; intake and report parsing are
  deterministic.
- Agents 21, 22, 23 and 29 are not in the LangGraph graph (bench chain mode only).
- `DremioSource` is a stub; every validation is local until phase PD.

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
