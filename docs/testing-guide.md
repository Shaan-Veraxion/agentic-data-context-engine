# Testing guide: run, test and evaluate every agent yourself

Everything here runs on one laptop. Sections 1–4 need no Docker, no keys and no network; sections 5–6 use a model;
section 7 uses the full local stack.

| I want to… | Go to |
|---|---|
| Click through agents and cases in a browser | 2. The test bench |
| Run one agent, or a chain, from the terminal | 3. Agents from the command line |
| Try my own Qlik script | 4. Your own scripts |
| See what the LLM does, and compare models | 5. The LLM pathway, 6. Evaluating models |
| Run the real services: upload, review, learning, events | 7. The full stack |
| Know everything still works after a change | 8. Before you commit |

## 1. Setup (once)

```
uv sync --all-packages
uv run adce-bench list            # every test case, with its suite, variants and status
```

Keys (only for sections 5–6) go in `deploy/compose/.env`, which git ignores:
`GROQ_API_KEY=...`, optional `GROQ_API_KEY_2=...`, and `ADCE_GATEWAY_CONFIG=deploy/gateway/groq-free.toml`.

## 2. The test bench (browser)

```
uv run adce-bench serve           # http://127.0.0.1:8765
```

| Tab | What you do there |
|---|---|
| Cases | Browse every case: the script, the uploaded files, the expected rows (the oracle) |
| Run | Pick a case, the agents, the AI mode and the data variant, and run. Every check is listed as ok / failed, with row differences, the SQL, each agent's outputs and metrics, the kg-mcp calls and every model call (turns, tools used, tokens) |
| Matrix | One case in every variant at once: where a level drops when data is missing |
| Agents | The agents and what each needs before it (prerequisites are added for you) |
| Memory | What the translation memory has learned: each answer, the apps it passed or failed on, and its state (learned / proven / disproved). Reset clears it |
| Playground | Paste any script (and optionally data), run it, and Save it as a new case. A case whose expectations you accept from a run is saved as approved |

Options on the Run tab:
- **AI**: `off` (rules only), `replay` (recorded model answers, no key, no network), `live` (calls the provider in
  `deploy/compose/.env`; answers are recorded in `.adce/recordings` and reused next time).
- **Data**: what was uploaded. `full` (everything), `no_qvd`, `no_log`, `script_only` (script and source data),
  `script_bare` (script only). The highest level a case can prove drops with less data; the bench adjusts the
  expected levels for you.
- **LLM tools**: lets the model call tools (check its SQL, read other statements, kg-mcp lookups) before it answers.

## 3. Agents from the command line

The agents of the Qlik path, by number (as in docs/architecture.md):

| # | Name on the bench | What it does | Needs |
|---|---|---|---|
| 1 | intake | classifies the uploaded files | — |
| 27 | extract | reads the script into the BI IR: tables, fields, sources, variables, issues | 1 |
| 28 | translate | load statements → Dremio SQL; rules first, then memory / reviewer SQL, then the model | 27 |
| 24 | synthesize | one SQL per output table | 28 |
| 25 | check | static checks on the SQL (parses, reads known sources, no blocking problems) | 28 |
| 26 | reconcile | runs the SQL locally and compares with the evidence: V0–V5 | 28 |
| 20 | publish | the context pack (SQL, lineage, validation report) | 26 |
| 23 | joins | join plan between tables | 28 |
| 29 | measures | chart measures and dimensions → SQL expressions | 28 |
| 21 | reports | reads app objects and report exports | 1 |
| 22 | metrics | maps report fields to SQL columns | 21, 28 |
| 0 | — | the whole LangGraph pipeline, as the API runs it | — |

```
uv run adce-bench run                                   # every case, in the variants it declares
uv run adce-bench run joins/                            # one suite
uv run adce-bench run loads/crosstable --agents 26 -v   # agent 26 (and what it needs), every check shown
uv run adce-bench run --agents 1,27                     # only intake and extract, on every case
uv run adce-bench run --agents 0                        # the full pipeline on every case
uv run adce-bench run --all-variants                    # every data variant, AI off and replay
uv run adce-bench run --data no_qvd -v                  # one data variant
uv run adce-bench run --json out.json                   # results as a file
```

Each line says PASS, FAIL or XFAIL (a known gap: a construct the rules do not handle yet; it is expected to fail).

Without the bench, on any app folder:

```
uv run adce parse eval/gold/bi/apps/orders_local        # statements, coverage, tables, issues
uv run adce translate eval/gold/bi/apps/orders_local    # Dremio SQL per table
uv run adce validate eval/gold/bi/apps/orders_local     # V0–V4 in the local venue
uv run adce report eval/gold/bi/apps/*                  # coverage and translation measures
```

What the levels mean: V0 parsed, V1 checks pass, V2 the SQL runs, V3 row counts match the reload log or QVD
headers, V4 the values match the QVDs, V5 report figures match the exports.

## 4. Your own scripts

Real client scripts stay out of git: put them in `eval/private/cases/<name>/` (ignored by git). They appear on
the bench as the suite `private`.

A case folder (see `eval/cases/README.md` for every option):

```
eval/private/cases/my_app/
  case.yaml                 # title, chain, variants, expected levels and checks (can start almost empty)
  script.qvs                # the load script
  reload.log                # optional: the reload log (row counts, gives V3)
  qvd/*.qvd                 # optional: the QVDs the script stores (gives V4)
  sources/<space>/<folder>/<table>.csv   # optional: extracts of the source tables, so the SQL can run locally
  expected/<Table>.csv      # optional: the rows Qlik produced, compared row by row
```

The quickest way in: Playground → paste the script → Run → look at the result → Save. When the result is right,
approve it on the bench; it becomes the case's expected result.

## 5. The LLM pathway

Statements the rules cannot translate go to the model (agent 28), and tables whose SQL runs but contradicts the
data get a diagnosis (agent 26's step). Every answer is checked and validated on the data before it is accepted.

```
uv run adce-bench run llm/ --ai replay -v               # recorded answers: no key needed
uv run adce-bench run llm/ --ai live -v                 # the configured provider
uv run adce-bench run llm/ --ai live --tools -v         # the model may call tools first
```

On the Run tab, open a model call to see the prompt inputs (statement, input columns: never rows), each turn, the
tools it called, its answer, the checks it faced and the tokens used.

## 6. Evaluating models

`adce-bench eval` runs the LLM cases from scratch in each trial (no recorded answers, empty memory) in two arms,
plain and tools, and writes `eval/results/<name>-<time>.json`:

```
uv run adce-bench eval --trials 2                       # both arms, the provider in deploy/compose/.env
uv run adce-bench eval --arms plain --trials 3 --cases llm/mapsubstring
```

Read the summary at the end: correct per arm, tokens and seconds per trial, and each case's score. Trials the
provider could not answer (quota) are marked invalid and not counted as wrong.

**Claude through your Claude Code login (local branch `local/claude-code-bridge` only, not on GitHub):**

```
git switch local/claude-code-bridge
uv run python tools/claude-code-bridge/bridge.py --port 8790             # keep this running
ADCE_GATEWAY_CONFIG=deploy/gateway/claude-code.toml uv run adce-bench eval --trials 2 --name claude-haiku
ADCE_GATEWAY_CONFIG=deploy/gateway/claude-code-sonnet.toml uv run adce-bench eval --trials 2 --name claude-sonnet
```

The same `ADCE_GATEWAY_CONFIG=...` works with `adce-bench run ... --ai live` and `adce-bench serve` (live mode on
the bench). In PowerShell set it first: `$env:ADCE_GATEWAY_CONFIG = "deploy/gateway/claude-code.toml"`.
For anything beyond measuring, use Claude through the Anthropic API or Vertex AI (the gateway's `anthropic` /
`anthropic_vertex` providers), not a CLI login.

## 7. The full stack: upload, review, learning, events

```
cp deploy/compose/.env.example deploy/compose/.env      # first time only; then add your keys
docker compose -f deploy/compose/compose.yaml --env-file deploy/compose/.env up -d
cd migrations && uv run alembic upgrade head && cd ..
uv run python -m kg_mcp.server                          # terminal 1: knowledge tools, http://localhost:8701/mcp
uv run python -m api_gateway --port 8700                # terminal 2: the API (set ADCE_KG_TRANSPORT=mcp to go via kg-mcp)
uv run python -m orchestrator.events run                # terminal 3: re-runs apps when reviewers decide
```

Upload and run an app, then review it:

```
A=eval/gold/bi/apps/orders_local
curl -F files=@$A/script.qvs -F files=@$A/reload.log -F files=@$A/qvd/Orders.qvd \
     -F files=@$A/qvd/CustomerTotals.qvd \
     -F "files=@$A/sources/sales/crm/customers.csv;filename=sources/sales/crm/customers.csv" \
     -F "files=@$A/sources/sales/erp/orders_v2.csv;filename=sources/sales/erp/orders_v2.csv" \
     http://localhost:8700/apps/orders_local/files       # source extracts keep their sources/... path
curl -X POST -H 'content-type: application/json' -d '{}' http://localhost:8700/apps/orders_local/runs
curl http://localhost:8700/apps/orders_local/overview                    # levels per table
curl "http://localhost:8700/review/items?app_id=orders_local"            # what needs a person
curl -X POST -H 'content-type: application/json' -d '{"decision": "approve"}' \
     http://localhost:8700/review/items/<item id>/decision
```

Review decisions: `approve` / `reject` (with `{"payload": {"reason": "..."}}`) for SQL; `provide_sql` (with
`{"payload": {"sql": "SELECT ... FROM src"}}`) or `accept_gap` for untranslated statements; `answer` for
variables; `map` (with `{"payload": {"dataset": "space.folder.table"}}`) for sources.

What happens after a decision (orchestrator.events):
- reviewer SQL → that app re-runs, and so does every app with the same statement untranslated (they reuse it);
- a rejection → the app re-runs, and the model is asked again with your reason;
- a variable answer or a source mapping → the apps it affects re-run.

Learning over time:

```
uv run python -m orchestrator.learning report           # every learned answer: learned / proven / disproved
uv run python -m orchestrator.learning rerun            # re-run every app from its uploads (daily in Kubernetes)
uv run python -m orchestrator.events once               # handle pending events once, then stop
curl "http://localhost:8700/facts?kind=translation_memory"
```

## 8. Before you commit

```
uv run ruff check . && uv run pyright && uv run pytest   # what CI runs (the bench cases are part of pytest)
uv run adce-bench run                                    # the same cases, readable
```

Tests marked `integration` need the Docker stack and are skipped without it.
