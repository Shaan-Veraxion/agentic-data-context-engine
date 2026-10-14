# Open Questions

The questions that still need an answer before or during the build of the Agentic Dataset Context Engine. `architecture.md` section 10 points here; this file is the single list.

How to use it:
- When a question is answered, move it to **Resolved** at the bottom with the date and the answer, and update `architecture.md` where the answer lands.
- **Default** is what the team does if nobody decides by the deadline. Accepting a default is a valid answer.
- **Blocks** says what cannot start until the question is answered.

Last updated: 2026-10-14 (no Dremio in the first phase; see implementation-plan.md).

---

## A. Blocking: answer before week 1

### Q-01 Pilot domain and data
- **Question:** Which business domain is the pilot? Which Dremio spaces or folders are in scope? Do we have at least one real data dictionary, one DDL or view-definition set, and 3–5 reports whose SQL or logic is known?
- **Blocks:** the gold set every squad is measured against (weeks 1–2).
- **Default:** the domain with the most Qlik scripts, since those give us reports with known logic.

### Q-02 Brahmastra code
- **Question:** When will the latest `brahmastra-v3` code be shared?
- **Blocks:** importing it into `packages/kg-engine` and running the R1–R15 acceptance suite (M0 week 1).
- **Default:** none. This is a hand-over.

### Q-03 What "Google API" means for the models
- **Question:** Is it (a) **Claude models through Google Cloud Vertex AI**, (b) **Gemini models**, or (c) both?
- **Blocks:** the model-gateway adapter, prompt design and the cost estimate.
- **Default:** (a) Claude on Vertex AI, which keeps the routing, prompts and evals in this doc. Gemini stays reachable through the same gateway for routes where it measures better (ADR-14).

### Q-04 Google Cloud setup
- **Question:** Which GCP organization and project(s)? Which region(s), for data residency? Is Vertex AI quota for Claude Opus 5.5, Sonnet 5.5 and Haiku 4.5 (or the chosen Gemini models) already approved?
- **Blocks:** local and staging environments calling the model (M0).
- **Default:** one dev project in a single region, plus one staging project.

### Q-05 Dremio access for development
- **Question:** Will developers get a **non-production Dremio** with representative data, or a **sample dump** that we load into a local Dremio OSS container?
- **Blocks:** the local environment and every data-touching agent (M0–M1).
- **Default:** sample dump into local Dremio OSS (`dremio-local` compose profile), plus read-only access to a non-prod Dremio for staging.

### Q-06 Dremio edition, version and permissions
- **Question:**
  - Dremio Software/Enterprise or Dremio Cloud, and which version? Flight SQL needs 21.0 or later.
  - Can the service account read the **catalog API** (view SQL definitions, wikis, tags) and **`sys.jobs_recent`** (query history)?
- **Blocks:** the Dremio adapter design (ADR-13), and whether query history can be used as join evidence.
- **Default:** assume Software ≥ 25 with catalog read and `sys.jobs_recent` access. Verify in week 1.

### Q-07 Qlik inputs we will actually receive
- **Question:**
  - In which format do the scripts arrive: Qlik Sense load scripts as text/`.qvs`, `.qvf` files, QlikView `.qvw` with `-prj` folders?
  - Are **script reload logs** included? They are needed to resolve `$(variables)`.
  - Do we get **chart/measure definitions** (master items, sheet objects), or **only load scripts**?
- **Blocks:** Mode A scope.
  - With only load scripts, Mode A covers data-model and transform lineage and the SQL behind each Qlik table, but **not the figures on report visuals**.
  - With measure definitions, it can reconstruct visual-level SQL.
- **Default:** load scripts + `-prj` where available + reload logs. Visual-level translation is limited to apps that come with measure definitions.

### Q-32 Which build path?
- **Answered 2026-10-14:** the Qlik-first path, starting without Dremio. See Resolved and `implementation-plan.md`. How the work is staffed is left to the team.
- **Question:** Do we follow:
  - (a) the parallel plan in `architecture.md`,
  - (b) the **Qlik & Reverse-SQL first** path in `architecture-alt-qlik-first.md`, or
  - (c) (b) with the other squads starting at Q2?
- **Blocks:** squad assignments and the week-1 task list.
- **Default:** (c). The headline is proven first, and nobody waits past week 5.

### Q-33 QVD files
- **Question:** Will we receive the **QVD files** the Qlik apps read and write, not just the scripts?
- **Why it matters:** QVD headers give lineage (creating app, source SQL) and row counts (validation level V3). QVD contents give column-level parity (V4).
- **Default:** ask for the QVDs of the 2–3 gold apps at least.

---

## B. Decide in weeks 1–2 (a default exists)

### Q-34 Reload log source
- **Question:** Do the reload logs come from client-managed Qlik (detailed per-table "Lines fetched") or from Qlik Cloud? Qlik Cloud's API log has been a summary since Feb 2026.
- **Default:** use detailed script logs where they exist; otherwise rely on QVD headers for row counts.

### Q-35 Qlik data connections
- **Question:** What Qlik data-connection names (`LIB CONNECT TO …`, ODBC/OLEDB) do the scripts use, and what does each point to (Dremio, or an original source system)?
- **Default:** build the mapping table from the scripts in Q0 and have a data owner confirm it (Seed mode).

### Q-36 Iceberg in Dremio
- **Question:** Are the Dremio datasets behind the in-scope reports **Iceberg** tables (time travel with `AT TIMESTAMP`), and how far back do snapshots go?
- **Why it matters:** exact parity at reload time vs comparison with a drift tolerance.
- **Default:** check per dataset during the catalog crawl; use a drift tolerance otherwise.

### Q-08 Production and client deployment shape
- **Question:** For the first client, do **we host** it (a dedicated GCP project per client), or do we **install it in the client's environment** (their cloud or on-prem, their Dremio, their LLM endpoint)? Is an **air-gapped** install ever required?
- **Default:** design for both from day 1 (ADR-15). The first production deployment is a per-client GCP project hosted by us.

### Q-09 Production cloud services
- **Question:** Is Google Cloud also the production platform (GKE, Cloud SQL for Postgres, Memorystore, GCS, Secret Manager), or only the LLM endpoint?
- **Default:** GCP for our hosted environments, with every dependency behind an interface so a client install can swap it (ADR-15).

### Q-10 Load we may put on the client's Dremio
- **Question:**
  - How many concurrent queries, and in which time windows?
  - What is the maximum query runtime?
  - May we create **Reflections** to speed up profiling?
  - Is there a separate Dremio engine or queue for our workload?
- **Default:** ≤ 4 concurrent queries, 5-minute timeout, profiling scheduled off-peak, no Reflections without approval.

### Q-37 Where the queries between Dremio and Qlik live
- **Question:**
  - Is the SQL that shapes the data mostly **embedded in the Qlik scripts** (`SQL SELECT … FROM` Dremio), in **Dremio views** built for Qlik, or both?
  - Do the scripts also read anything that is not in Dremio (Excel files, other databases)?
- **Why it matters:** decides how much lineage comes from Dremio view definitions vs from the scripts, and whether any sources need mapping outside Dremio.
- **Default:** handle both; the catalog crawl and the script parser cover each case.

### Q-12 Report outputs for validating Mode A
- **Question:** Until Qlik server access exists, can report owners provide **exported report outputs** (PDF/Excel) or reference figures for the visuals in scope?
- **Why it matters:** without them, translated SQL can be checked structurally and run in Dremio but not compared with the real numbers. Those reports are scored as `translated, not validated`.
- **Default:** ask for exports of at least 10 visuals for the BI gold set.

### Q-13 When Qlik server access arrives
- **Question:** Rough date, and which product (client-managed Qlik Sense, Qlik Cloud, QlikView Server)?
- **Why it matters:** this is when engine-backed reconciliation (Mode A2) can start.
- **Default:** treat A2 as post-POC unless access arrives by M3.

### Q-14 Target SQL dialects beyond Dremio
- **Question:** Generated SQL targets **Dremio SQL** and executes in Dremio. Do any clients also need it in another dialect (e.g. their warehouse)?
- **Default:** Dremio only for the POC. sqlglot can transpile later.

### Q-15 Squad sizes and leads
- **Question:** Who leads each of the 9 squads in `architecture.md` section 7.1, and how many engineers per squad?
- **Default:** sizes as in section 7.1 (about 23–30 engineers plus domain experts).

### Q-16 Embeddings
- **Question:** Voyage, self-hosted bge-m3, **Vertex AI embeddings**, or the engine's current local model?
- **Default:** self-hosted bge-m3 for client installs (keeps data in place); Vertex embeddings acceptable for our hosted GCP environments if measured equal or better.

### Q-17 Fact log placement
- **Question:** Should the `fact_log` sit in its own schema in the engine's Postgres, or in a separate database?
- **Default:** its own schema in the same Postgres.

### Q-18 Future of Brahmastra's memory features
- **Question:** Should notes, meetings, sessions, code index and Notion live on as `kg-engine/apps/memory` on the shared core, or stay frozen in the old `vaultgraph-qy` repo?
- **Default:** keep them as an optional app on the shared core (one engine, two products).

### Q-19 Neo4j edition
- **Question:** Community or Enterprise? Enterprise adds per-tenant databases, RBAC and clustering.
- **Default:** Community for local, staging and single-tenant client installs. Revisit for multi-tenant hosting.

### Q-20 Default selection state per visual (Mode A2 only)
- **Question:** For engine-backed reconciliation, who defines what "the report" shows with no selections (bookmarks, default filters)?
- **Default:** the report owner states it per visual; otherwise no selections. Not needed until Q-13 is resolved.

### Q-21 Client identity provider
- **Question:** Which IdP does the first client use (Entra ID, Okta, Google Workspace, other)? Keycloak federates to it.
- **Default:** Keycloak with local users for dev and staging.

### Q-22 Developer machines
- **Question:** How much RAM do developer machines have? The full local stack needs roughly 16–24 GB. Compose profiles let people run subsets.
- **Default:** 32 GB recommended, 16 GB minimum using profiles.

### Q-27 Size and layout of the Dremio estate
- **Question:**
  - Roughly how many spaces, sources, physical datasets and views?
  - Which spaces are **curated** (intended for consumption) and which are personal home spaces, scratch or dump areas?
- **Why it matters:** sizes the crawl, the equivalence work (ADR-16) and the R12 load test. Location is a strong golden-vs-dump signal.
- **Default:** crawl everything for metadata, and treat home spaces and names like `_bkp`/`_old`/`_tmp` as low-authority until shown otherwise.

### Q-28 Known golden records and their owners
- **Question:** For the pilot domain, are any datasets already known to be the golden record? Who owns each business entity (customer, order, product …) and can **confirm** golden records in the workbench?
- **Blocks:** the golden gate (ADR-16) needs named owners. Known golden datasets can be seeded on day 1.
- **Default:** none seeded; golden recommendations wait in the gate until an owner is named.

### Q-29 Dremio query history
- **Question:**
  - How many days of history does Dremio keep (`jobs.max.age_in_days`)?
  - Can our service account read it?
  - Can Qlik's queries be told apart in it (a dedicated user or ODBC client)?
- **Why it matters:** usage-first scoping and the golden score depend on usage, and Qlik-identified queries link reports to datasets directly.
- **Default:** use whatever retention exists, and snapshot `sys.jobs_recent` daily from the start so ADCE builds its own longer usage history.

### Q-30 Estate rationalization report
- **Question:** Is the **estate rationalization report** (duplicate clusters, recommended golden records, stale datasets, reports reading non-golden copies) wanted as a client deliverable? Who receives it?
- **Reminder:** ADCE only recommends; it never changes Dremio.
- **Default:** produce it for the pilot domain at M3, for internal review first.

### Q-31 Estate gold set
- **Question:** Can the domain expert label, for the pilot domain, about 50 dataset pairs (identical / same logic / subset / snapshot / unrelated) and confirm the golden record for 5–10 business entities?
- **Blocks:** measuring equivalence precision and golden acceptance (section 7.3).
- **Default:** none; this needs domain-expert time (see Q-26).

### Q-38 Data model output format (later stage)
- **Question:** When the data model designer (ADR-17) produces physical artifacts, should they be **Dremio view SQL**, a **dbt project**, or both? Who applies them on the client side?
- **Blocks:** the data-model stage only (after the core platform). Nothing earlier.
- **Default:** decide when that stage is planned (Shaan, 2026-10-09: "we will sort it later").

### Q-39 First data model form and subject area
- **Question:** For the first data model, which form is wanted first (star schema for reporting, normalized business model, or semantic layer of curated Dremio views), and for which subject area?
- **Default:** decide when the stage is planned; the designer builds one logical model and can render any of the three.

---

## C. Numbers and budget to confirm

### Q-23 Accuracy targets
- **Question:** Are these acceptable?
  - PK F1 ≥ 90%; FK F1 ≥ 80% within a source.
  - Mode A: ≥ 90% of script statements translated, and ≥ 70% of in-scope report figures exact or within tolerance where ground truth exists.
  - Less than 5% of auto-accepted facts reversed.
  - Estate: equivalence precision ≥ 95% (`identical`/`same_logic`) and ≥ 85% (other relations); golden recommendations accepted by owners ≥ 80%.
- **Default:** as stated.

### Q-24 Scale targets for the KG
- **Question:** Are these the right size for real clients?
  - ≥ 1M fact versions per tenant.
  - p95 context pack < 500 ms.
  - Join paths < 1 s at 10k columns.
  - How many Dremio datasets and columns does the first client have?
- **Default:** as stated; the load test uses 5k datasets, 100k columns, 1M fact versions.

### Q-25 Budget
- **Question:** The proposal asked for $800/month for LLM + infrastructure, sized for 2 engineers. With 9 squads, gold-set evaluation runs and Vertex pricing (no Anthropic Batch API discount on Vertex), what is the new monthly limit for dev/staging?
- **Default:** needs an owner. There is no safe default.

### Q-26 Domain-expert time
- **Question:** The proposal assumed about 3 hours a week. Building the gold set in weeks 1–2 needs much more. Who provides it, and how many hours?
- **Default:** needs an owner.

---

## Resolved

| Date | Question | Answer | Landed in |
|---|---|---|---|
| 2026-10-08 | Scope: reduce to fewer agents? | No. Full scope, big team. | v0.2 |
| 2026-10-08 | Which BI tools? | QlikView and Qlik Sense now; more tools later via the BI IR. | v0.4, CP-001 |
| 2026-10-08 | Build the KG from scratch? | No. Evolve Brahmastra in place; it can be changed freely. | ADR-12 (v0.5) |
| 2026-10-08 | MCP framework | One MCP stack on FastMCP 3.x. | ADR-12 |
| 2026-10-08 | Qlik access model | **Scripts first**; server access possibly later. | ADR-11 (v0.6) |
| 2026-10-08 | Where the tables live | **Dremio** holds the tables and views; it is the primary data source. | ADR-13 |
| 2026-10-08 | Target SQL dialect | **Dremio SQL**, executed in Dremio (Q-14 covers other dialects). | ADR-13 |
| 2026-10-08 | LLM provider | **Google** (see Q-03 for which models). | ADR-14 |
| 2026-10-08 | Is Dremio well structured? | **No.** Many near-duplicate views, redundant copies and dumps, with some golden records. | ADR-16 (v0.7) |
| 2026-10-09 | Q-11: do Qlik scripts read from Dremio? | **Yes.** The data lives in Dremio as loosely structured tables and dumps; queries (Dremio views and/or SQL inside the scripts) and Qlik load scripts on top of them produce the reports. | ADR-13, ADR-16, ADR-Q4 |
| 2026-10-09 | ADCE should also help create a data model? | **Yes, as later work** within the overall architecture: star schema, normalized business model, or a semantic layer of curated Dremio views. | ADR-17 (v0.8) |
| 2026-10-09 | Should Qlik read from the new data model? | **No.** The model is a separate deliverable. | ADR-17 |
| 2026-10-08 | Environments | **Local first, then production; must be shippable to clients.** | ADR-15 |
| 2026-10-14 | Q-05 (partly), Q-32: Dremio in the first phase? Which build path? | **No Dremio in the first phase; the Qlik-first path is taken.** The catalog crawl and all Dremio-dependent work move to phase PD. Until then the output is still Dremio SQL; local checks run on DuckDB over QVD and fixture data, labelled venue `local`. Q-05/Q-06 stay open for phase PD. | implementation-plan.md |
| 2026-10-14 | Which knowledge service until Brahmastra's latest code is available (Q-02)? | **Graphiti** (graphiti-core, Apache-2.0) on Neo4j 5.26, behind `KGPort`, with the Postgres fact log as the record. Brahmastra plugs in later as another `KGPort` implementation. | implementation-plan.md (decisions) |
