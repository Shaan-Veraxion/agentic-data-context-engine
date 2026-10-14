# Agentic Dataset Context Engine: Alternate Path, Qlik & Reverse-SQL First

Status: Draft v0.1 (2026-10-08).
Audience: the engineering squads and the sponsor.

Relationship to [`architecture.md`](architecture.md) (v0.7):
- The **target architecture is the same**: the same 31 agents, ADRs, contracts and stores.
- What changes is the **order of work**. The first deliverable is a working **Qlik → Dremio SQL** pipeline, and only the platform pieces it needs are built first, in a form that is not thrown away later.
- Which path to follow is open question Q-32 in [`open-questions.md`](open-questions.md).

---

## 1. Why start here

- **It is the headline.** "We reconstruct how your numbers are produced" is the go-to-market claim. This path proves it first instead of last.
- **The inputs exist now.** Qlik scripts are available today (Q-07). Server access comes later.
- **Translation is more reliable than inference.** Mode A (translate real logic) is far more deterministic than Mode B (infer SQL from report outputs). It is the lowest-risk way to a real end-to-end result.
- **It produces knowledge every other group reuses**: lineage from load scripts and QVD chains, join evidence from Qlik data models, definitions from master items. The KG starts with high-value, documented facts.
- **It is sellable on its own.** A validated *Qlik → Dremio migration pack* (lineage, SQL per table and per measure, validation evidence) is valuable to a client even before the rest of the platform exists.

---

## 2. What is true at week 12

1. Real client Qlik apps (QlikView and Qlik Sense, scripts first) are parsed into BI IR, including multi-app **QVD chains**.
2. Every Qlik table is reproduced as **Dremio SQL**, executed in Dremio, and validated as far up the ladder in section 3 as the available ground truth allows.
3. Where measure definitions are available, report figures are reproduced and checked against exported report outputs.
4. All results are **facts with provenance** in the fact log (the ADR-12 target design) and visible as a lineage graph.
5. Reviewers have resolved ambiguities (unresolved variables, source mappings, untranslated statements) in a review UI, and those decisions are reused.
6. A **migration pack** per app can be exported: SQL files, lineage, validation report, untranslated-statement list.
7. Everything runs locally from the same images that later deploy to staging and client environments (ADR-15).

---

## 3. Ground truth without Qlik server access: the validation ladder

Without the Qlik engine, we still have several independent sources of truth. Every translated table or figure gets the **highest level it reaches**. Reports always state the level; a result is never called "correct" without one.

| Level | Check | Ground truth used | Available when |
|---|---|---|---|
| **V0 Parsed** | Every statement parsed into BI IR; nothing silently skipped | Script text | always |
| **V1 Checked** | Generated SQL parses in the Dremio dialect (sqlglot) and passes Query-checker rules (grain, fan-out, set-analysis semantics) | KG + rules | always |
| **V2 Executes** | SQL runs in Dremio within budget and returns the expected columns and types | Dremio | Dremio access |
| **V3 Row-count parity** | Row count per Qlik table matches the **reload log** ("`<table> << <source>` N Lines fetched") and/or the **QVD header** `NoOfRecords` | Reload logs, QVD headers | logs or QVDs provided |
| **V4 Data parity** | Column-level comparison against the **QVD file contents**: distinct counts, sums, min/max, sampled rows (read with PyQvd/qvdrs) | QVD files | QVDs provided |
| **V5 Figure parity** | Report figures match **exported report outputs** (Excel/PDF) or owner-provided figures | Exports | exports provided |
| **V6 Engine parity** (Mode A2, later) | `GetHyperCubeData` across ≥3 selection slices | Qlik engine | server access (Q-13) |

**Time alignment.** Reload logs, QVDs and exports describe the data *at reload time*, while Dremio returns data *now*.
- If the Dremio sources are **Iceberg** tables, SQL runs `AT TIMESTAMP <reload_time>` (Dremio time travel), giving exact comparisons (Q-36).
- Otherwise differences are reported with a **drift tolerance** and the snapshot gap is recorded as the reason. Drift never silently becomes "match".

**QVD headers are lineage gold.** Each QVD's XML header records:
- `CreatorDoc`: the app that created it.
- `CreateUtcTime` and `NoOfRecords`.
- `Lineage/LineageInfo`: `Discriminator` (the source) and `Statement` (for database sources, the SQL that was run).

That links QVD chains across apps even when we only have some of the scripts.

---

## 4. Critical-path architecture

```
 Qlik inputs                      Dremio
 (scripts, -prj, reload logs,     (catalog, views, data)
  QVDs, report exports)                  │
        │                                │ data_port (Flight SQL / catalog API)
        ▼                                ▼
 [1] Intake router ──► [27] BI extractor ──► BI IR ──► [Source resolver: 5, 2, 4-lite]
                                │                         │ Qlik source / QVD → Dremio dataset
                                ▼                         ▼
                      [28] Script translator    [29] Expression translator
                                └──────────┬──────────────┘
                                           ▼  (ExprAST → Dremio SQL)
                     [23] Join planner ─► [24] SQL synthesizer ─► [25] Query checker
                                           │                       (needs grain: [9-lite])
                                           ▼
                                  [26] Reconciler ── V2–V5 checks in Dremio / vs logs, QVDs, exports
                                           │
                     ┌─────────────────────┴───────────────────────┐
                     ▼                                             ▼
              kg_port → fact_log (Postgres)                  Review UI (HITL)
              (+ Neo4j projection from Q2)                   Ask / Queue / Gate
                     │
                     ▼
              [19-lite] Lineage + [20-lite] Migration pack export
```

Everything goes through the contracts in `architecture.md` ADR-1 (`AgentSpec`, fact and event schemas, BI IR, `kg_port`, `data_port`). Lean early versions implement the **same interfaces** as the full ones, so nothing is rewritten when the full version arrives.

### 4.1 Agents on this path, by phase

| Phase | Agents (numbers as in `architecture.md` §5) | Notes |
|---|---|---|
| Q0–Q1 | **27** BI extractor (A1), **28** Script translator, **25** Query checker, **26** Reconciler (V0–V4) | The core of the path |
| Q0–Q1 (supporting, lean) | **1** Intake router (file classes: script, `-prj`, reload log, QVD, export), **5** Scope registrar + **2** Schema parser (Dremio catalog crawl + view definitions), **4-lite** Entity linker (Qlik source → Dremio dataset, deterministic first), **9-lite** Key discoverer (deterministic uniqueness via pushdown, for grain rules) | Deterministic first; LLM only where the full agent needs it |
| Q2 | **29** Expression translator, **21** Report parser (exports → figures), **22** Metric mapper (Mode A), **23** Join planner (from `bi_model_link`), **24** SQL synthesizer (compose), **19-lite** Lineage (extraction from `bi_transform`, QVD headers, view definitions) | Visual-level reproduction (V5) |
| Q3 | **20-lite** Dictionary publisher (migration pack), **30** Equivalence detector L1/L3 (only if Qlik reads non-Dremio sources or duplicate copies, Q-11) | Packaging and widening |
| After Q3 | All other agents, per `architecture.md` §7 | Mode B, profiling, comparison, definitions, estate |

### 4.2 The pipeline, step by step

1. **Intake.** Files land in MinIO under an app scope. The Intake router classifies them: load script (`.qvs`/text), QlikView `-prj` folder, Qlik Sense app file, reload log, QVD, report export.
2. **Extract to BI IR (27).**
   - **Statement splitting:** handles comments, `REM`, strings containing `;`, `SUB`/`END SUB`, control blocks, `$(Include=…)`.
   - **Statement parsing:** a Lark grammar; anything unparseable becomes `kind: other`, flagged, never dropped.
   - **Variable resolution:** from `LET`/`SET` and reload logs; anything unresolved goes to **Ask**.
   - **From `-prj` XML:** expressions and master items.
   - **From QVD headers:** lineage, `CreatorDoc`, `NoOfRecords`.
3. **Resolve sources (5, 2, 4-lite).**
   - Each `SQL SELECT … FROM`, `LOAD … FROM` and `LIB CONNECT TO` target is mapped to a Dremio dataset or to another app's QVD.
   - Qlik **data-connection names** map to Dremio or source systems through a reviewed mapping table (Seed mode, Q-35).
   - QVD consumers are linked to their producing app through the QVD header's `CreatorDoc` and the `STORE` statements.
4. **Translate (28, 29).**
   - Embedded SQL is transpiled from its source dialect to Dremio with sqlglot.
   - Qlik transforms become CTE chains (rule table in `architecture.md` §5.7).
   - Measures go to `ExprAST`, then to SQL, with a T1/T2/T3 tier on each.
   - LLM help (Sonnet; Opus for T3) only for statements the rules don't cover, and its output is always re-parsed.
5. **Plan and compose (23, 24).** The Qlik data model's association keys become `bi_model_link` facts and are checked against Dremio view joins. Table and visual SQL is composed from CTEs, measures and the join plan.
6. **Check (25).**
   - **Grain and fan-out:** an aggregated field is aggregated at its native grain before joining. The table grain comes from **9-lite** (pushdown uniqueness in Dremio).
   - **Qlik semantics:** set-analysis override, `TOTAL`/`Aggr`, dual values.
7. **Reconcile (26).** Execute in Dremio (time travel when Iceberg), then climb the validation ladder V2 → V5. Mismatches are diagnosed by Opus with aggregate diffs only (no row-level data leaves the environment).
8. **Record.** Every step writes facts with evidence to the **fact log** through `kg_port`: `bi_transform`, `bi_model_link`, `bi_metric_def`, `lineage`, `report_sql`, validation level.
9. **Review.** The review UI shows unresolved variables (Ask), source mappings and untranslated statements (Queue), and final SQL approval (Gate). Decisions become confirmed facts and few-shot examples.
10. **Export.** Migration pack per app: Dremio SQL per table and measure, lineage graph, validation report per object (level reached, diffs, tolerances), untranslated list, open questions.

---

## 5. Supporting pieces: what is lean now and what comes later

Rule: **lean means fewer features, never a different interface.** Each item below implements the `architecture.md` contract it belongs to.

| Piece | Lean now (Q0–Q1) | Full later | Why it isn't throwaway |
|---|---|---|---|
| Repo, CI, config schema, Compose profiles | Monorepo, `uv`, CI (lint/type/test), config validated at startup; profiles `core`, `dremio-local`, `bi-fixtures` | All profiles, Helm, signed images (ADR-15) | Same layout and images all the way to production |
| `data_port` | `DremioSource` (Flight SQL queries, catalog API, view definitions) + `FileSource`; load limits | `sys.jobs_recent` usage, equivalence fingerprints (ADR-13, ADR-16) | Same port |
| KG | `kg_port` implemented over the **`fact_log` in Postgres** (the ADR-12 system of record) + outbox | Brahmastra `kg-engine` core: Neo4j projection (from Q2), hybrid search, resolution, isolation guard | The fact log *is* the target source of truth; the projection is added on top |
| `model-gateway` | Vertex AI adapter (Claude), structured outputs, prompt caching, memoization, record/replay, budgets | Gemini adapter, other providers, full routing table (ADR-7, ADR-14) | Same service |
| Privacy | **Policy: no row-level data goes to any LLM on this path.** LLMs see scripts, schemas and aggregate diffs only; enforced in the gateway | Presidio masking when sample-based agents arrive (ADR-8) | The gateway is the single enforcement point either way |
| HITL | `hitl-service` with Ask, Queue, Gate stored as data | Seed and Correct UX polish, impact ranking (ADR-5) | Same service and tables |
| Review UI | Next.js workbench with three screens: app overview (ladder per table), review queue, lineage view (from Q2) | Full workbench | Same app, more screens |
| Auth | Keycloak dev realm with one user and per-service accounts | Roles enforced, IdP federation (ADR-9) | Same realm |
| Orchestration | LangGraph subgraph for the Qlik pipeline; events through the Postgres outbox | Redis Streams, all subgraphs (ADR-6) | Same graph, more subgraphs |
| Observability | Langfuse in the `obs` profile, OTel | Dashboards, cost views | — |
| Eval | **BI gold set** + harness from week 1 | All gold sets (ADR-10) | Same harness |

---

## 6. Phases

| Phase | Weeks | Build | Done when |
|---|---|---|---|
| **Q0 Foundations** | 1–2 | Repo + CI + config; Compose with Postgres, MinIO, Keycloak dev, local Dremio OSS + sample dump; contracts frozen (AgentSpec, fact/event schemas, BI IR, ExprAST, `kg_port`, `data_port`); fact log + outbox; gateway (Vertex + replay); **Qlik grammar spike** on all sample apps; QVD header reader; reload-log parser; **BI gold set v0** (2–3 apps with scripts, logs, QVDs, exports) | Sample apps parse to BI IR with a measured statement coverage; one simple Qlik table translated and **executing in local Dremio (V2)** |
| **Q1 Tables** | 3–5 | 27 complete for A1 inputs; 28 for T1 then T2 transforms; source resolver (5, 2, 4-lite) incl. QVD chains and connection mapping; 9-lite grain; 25 grain rules; 26 for V2–V4; review UI (queue + Ask); facts in the fact log | ≥ 90% of statements translated on gold apps; ≥ 80% of gold tables at **V3 or higher**; every unresolved variable and source reaches the review queue |
| **Q2 Figures & lineage** | 6–8 | 29 (T1–T2), 21 report parser for exports, 22, 23, 24 composition; 25 set-analysis/`TOTAL`/`Aggr` rules; V5 reconciliation; **Brahmastra `kg-engine` projection to Neo4j** + lineage view; Iceberg time travel if available | **≥ 3 real report figures at V5** (T1–T2); lineage graph across QVD chains browsable in the UI |
| **Q3 Harden & package** | 9–12 | More apps; T3 attempts; migration-pack export (20-lite); 30 L1/L3 if needed (Q-11); performance and Dremio load tuning; staging on GCP (ADR-15); **Mode A2 adapter** if server access exists | Targets in §8 met on the gold set; a migration pack accepted by a report owner; same build runs in staging |

From Q2 on, the remaining squads (Profiling, Comparison & Estate, Definition) start building against the same contracts, following `architecture.md` §7. **Mode B** and the estate work begin there.

---

## 7. Squads for this path

| Squad | Focus on this path | Size |
|---|---|---|
| **Qlik extraction & translation** | 27, 28, 29; Qlik grammar; reload-log and QVD readers; rule tables | 4–5 |
| **SQL correctness** | 23, 24, 25, 26; validation ladder; Dremio execution and time travel | 3–4 |
| **Data access & resolution** | `data_port`, Dremio crawl (5, 2), source resolver (4-lite), 9-lite grain, connection mapping | 2–3 |
| **Platform** | Repo, CI, config, Compose, fact log + `kg_port`, outbox, gateway, Keycloak dev, then the Brahmastra projection in Q2 | 3–4 |
| **Review UI** | Workbench screens for this path | 2 |
| **Evaluation & domain** | BI gold set, harness, report-owner liaison | 1–2 + domain expert |
| Profiling / Comparison & Estate / Definition | Contracts review in Q0–Q1, then start building from Q2 | per `architecture.md` |

---

## 8. Success metrics (this path)

| Metric | Target by week 12 |
|---|---|
| Script statements translated without the `untranslated` flag (per app) | ≥ 90% |
| Gold tables at V3 (row-count parity) or higher | ≥ 80% |
| Gold tables at V4 (data parity vs QVD), where QVDs exist | ≥ 70% |
| Report figures at V5 (exact or within tolerance), T1–T2, where exports exist | ≥ 70% |
| Results reported with their ladder level and drift tolerance | 100% (no unlabeled "match") |
| Unresolved variables or sources silently assumed | 0 (all go to Ask/Queue) |
| Cost per app translated (gateway + Langfuse) | Measured and reported |

---

## 9. Risks specific to this path

| Risk | Mitigation |
|---|---|
| **Qlik syntax long tail** breaks the parser | Grammar grown from real scripts, statement-level fallback to `kind: other` + LLM, coverage tracked per app from Q0 |
| **No measure definitions provided** (scripts only) | Table-level ladder (V0–V4) still delivers lineage and SQL. Visual-level work waits for `-prj` or app files (Q-07). |
| **Data drift since reload** makes parity look wrong | Iceberg time travel where possible (Q-36); otherwise drift tolerance with the snapshot gap recorded |
| **Qlik Cloud reload logs are summarized** (format change from Feb 2026, so per-table "lines fetched" may be missing) | Use client-managed script logs where available; fall back to QVD headers (V3) and QVD data (V4) (Q-34) |
| **Qlik reads sources that are not in Dremio**, or reads duplicate copies | Connection mapping table (Seed), 4-lite linker with schema/data fingerprints, 30 L1/L3 in Q3 (Q-11) |
| **Lean KG now causes rework later** | The fact log is the target system of record; `kg_port` keeps callers stable; the Neo4j projection is additive |
| **Load on the client's Dremio** from reconciliation queries | Load limits (ADR-13, Q-10), result caching per dataset version, off-peak batch runs |
| **Other groups idle during Q0–Q1** | They review contracts, build fixtures and deterministic cores against `kg_port`/`data_port` mocks, and start building in Q2 |

---

## 10. Tools specific to this path

| Need | Tool |
|---|---|
| Qlik script grammar | **Lark** (Python), grown from real scripts; Qlik function list from the official script and chart function docs |
| QlikView `-prj` XML | lxml |
| QVD headers and data | **PyQvd** (maintenance mode); **qvdrs** (Rust, Python bindings, recommended for new work) evaluated in Q0 |
| Reload logs | Custom parser (statement, expanded variables, "`<table> << <source>` N Lines fetched", timestamps) |
| SQL | sqlglot (source dialects → `dremio`), Query-checker rules on sqlglot AST |
| Dremio | ADBC Flight SQL driver, REST catalog API v3, Iceberg `AT TIMESTAMP` / `AT SNAPSHOT` |
| Report exports | openpyxl (Excel); Claude PDF/vision via the gateway (PDF) |
| Everything else | As in `architecture.md` §6 |

---

## 11. Questions this path adds

Tracked in [`open-questions.md`](open-questions.md):
- **Q-32** Which path do we follow: this one, the parallel plan in `architecture.md`, or this one with the parallel squads starting at Q2 (recommended)?
- **Q-33** Will we receive **QVD files** (for V3/V4 parity and lineage headers)?
- **Q-34** Are the reload logs from client-managed Qlik (detailed per-table logs) or Qlik Cloud (summarized since Feb 2026)?
- **Q-35** What are the Qlik **data-connection names**, and what does each point to (Dremio, or an original source)?
- **Q-36** Are the Dremio sources behind these reports **Iceberg** tables, so time travel can align with reload times?

Also directly relevant: Q-05, Q-06, Q-07, Q-11, Q-12 and Q-13.

---

## Sources
- QVD XML header (`CreatorDoc`, `NoOfRecords`, `LineageInfo` with `Discriminator`/`Statement`): https://community.qlik.com/t5/QlikView/qvd-files-and-their-origin/td-p/782093
- PyQvd (and the qvdrs recommendation): https://pypi.org/project/PyQvd · https://github.com/MuellerConstantin/PyQvd
- Qlik reload log "Lines fetched" format and the Feb 2026 Qlik Cloud change: https://qlik.dev/changelog/167-changes-to-reload-log/
- Dremio Iceberg time travel (`AT SNAPSHOT`, `AT TIMESTAMP`): https://docs.dremio.com/current/reference/sql/commands/SELECT-statements
- Everything else: see `architecture.md` Sources
