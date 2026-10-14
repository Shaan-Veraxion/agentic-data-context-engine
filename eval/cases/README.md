# Test cases and the test bench

One folder per case: `eval/cases/<suite>/<name>/`. A case is a small Qlik application plus what Qlik would
produce from it, so any agent, or any chain of agents, can be run on it and checked.

| File | What it is |
|---|---|
| `case.yaml` | title, constructs covered, default agent chain, variants, expected levels and checks |
| `script.qvs` | the load script |
| `sources/<space>/<folder>/<table>.csv` | stand-in data for `"space"."folder"."table"` (`sources/files/x.csv` for file loads) |
| `reload.log` | the reload log, with "Lines fetched" per row-reading statement |
| `qvd/*.qvd` | the QVDs the script stores |
| `expected/<Table>.csv` | the rows each table holds after the reload: the oracle |

**Oracles are computed by hand, never from SQL.** Generated cases come from
`services/testbench/src/testbench/catalogue.py`, where each expected table is computed in plain Python from Qlik's
documented behaviour. The reload log and QVDs are written from the same oracle. `oracle: approved` marks a case
whose expectations a person accepted from a run on the bench, typically a real script with no reload data.

`apps/*` cases point to the gold apps in `eval/gold/bi/apps` (`app:`), so they run without copies.

Real client scripts go in `eval/private/cases/`. That folder is git-ignored and appears on the bench as the suite
`private`.

## Commands

```
uv run adce-bench generate                    # (re)write the generated cases from the catalogue
uv run adce-bench run                         # every case in each variant it declares
uv run adce-bench run joins/ --agents 26 -v   # one suite, agent 26 (prerequisites added), every check shown
uv run adce-bench run --all-variants          # every data variant, AI off and replay
uv run adce-bench serve                       # the test bench page: http://127.0.0.1:8765
uv run pytest services/testbench              # the same runs, as CI does
```

## Variants

| AI | Meaning |
|---|---|
| `off` | rules only |
| `replay` | recorded model answers from `eval/recordings` (no key, no network) |
| `live` | the provider configured in `deploy/compose/.env`; answers are recorded to `.adce/recordings` and reused |

| Data | Uploaded | Highest level it can prove |
|---|---|---|
| `full` | everything | V4 (V5 for charts with exports) |
| `no_qvd` | no QVDs | V3 from the reload log |
| `no_log` | no reload log | V4 for stored tables (QVD header counts and contents) |
| `script_only` | script, app objects, sources | V2: the SQL runs |
| `script_bare` | script and app objects | V1 |

Expected levels in `case.yaml` are for `full`; other variants are capped automatically. Use `when:` for
variant-specific expectations:

```yaml
when:
  - if: {ai: "off"}
    levels: {Sales: V0}
    expect: {translate: {untranslated: 2}}
```

## Checks a case can declare

```yaml
levels: {Orders: V4}                                    # agent 26, per table
expect:
  intake:     {classes: {script.qvs: script}}           # agent 1
  extract:    {tables: [..], fields: {T: [..]}, sources: [..], issues: [codes], coverage: 1.0}
  translate:  {untranslated: 0, ai_status: {T: ai_validated}}
  synthesize: {sql_contains: {T: ['"shop"."raw"."orders"']}}
  check:      {failing: {T: [rule names]}}              # blocking rule failures expected (default none)
  publish:    {files: [..], pack_contains: {validation.md: ["..."]}}
  <step>:     {metrics: {name: value}}                  # any agent's metrics: joins, measures, reports, metrics, ...
```

Every table with `expected/<T>.csv` is also run in the local venue and its rows compared with the oracle. Numbers
are compared as Qlik shows them (`20`, not `20.0`).

`status: known_gap` (with `gap:`) keeps a construct the rules do not handle yet. Its expectations stay correct;
it is reported as an expected failure until it passes.
