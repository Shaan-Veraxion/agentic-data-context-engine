# Agentic Dataset Context Engine (ADCE)

The starting stage reconstructs how Qlik reports are produced: it turns Qlik load scripts into validated Dremio SQL, with lineage, evidence and human review. There is no Dremio access in the first phase, so its work is phase PD.

- Architecture: [docs/architecture.md](docs/architecture.md) and [docs/architecture-alt-qlik-first.md](docs/architecture-alt-qlik-first.md)
- What is being built now and its status: [docs/implementation-plan.md](docs/implementation-plan.md)
- Open questions: [docs/open-questions.md](docs/open-questions.md)

## Layout

| Path | What |
|---|---|
| `packages/adce-core` | Contracts (BI IR, ExprAST, facts, entities), ports (`KGPort`, `DataPort`), agent runtime, artifact store, settings |
| `packages/adce-qlik` | Qlik script splitter, classifier, variables, reload logs, QVD headers, LOAD grammar (Lark), BI IR builder |
| `packages/adce-kg` | Knowledge service adapter: Postgres fact log, projected into Graphiti on Neo4j |
| `packages/adce-gateway` | Model gateway: routes, privacy guard, memoization, record/replay, budgets, Claude on Vertex AI |
| `packages/adce-reversesql` | Qlik → Dremio SQL: expression translation, CTE composition, checks, local venue (DuckDB), validation ladder, `adce` CLI |
| `services/*` | Agent workers (intake, BI extractor, script translator, synthesizer, query checker, reconciler, publisher), `kg-mcp` (FastMCP), `orchestrator` (LangGraph), `hitl-service` (review), `api-gateway` (FastAPI) |
| `apps/workbench` | Review workbench: Next.js + React Flow (overview, review queue, lineage) |
| `deploy/compose` | Local stack: Postgres, Neo4j and S3 (SeaweedFS); profiles `obs` (Langfuse) and `auth` (Keycloak) |
| `deploy/docker`, `deploy/helm/adce`, `deploy/terraform/gcp` | Images, one Helm chart for staging/production/client installs, Google Cloud infrastructure |
| `migrations` | Alembic schema: fact log, entities, outbox, review items |
| `eval/gold/bi/apps` | Fixture Qlik apps; `orders_local` has data and an independently computed expected result |

## Getting started (local)

Requirements: [uv](https://docs.astral.sh/uv/) and Docker. uv installs Python 3.12 itself.

```bash
uv sync --all-packages

# local stack
cp deploy/compose/.env.example deploy/compose/.env
docker compose -f deploy/compose/compose.yaml --env-file deploy/compose/.env up -d
# optional: --profile obs (Langfuse at :3000), --profile auth (Keycloak at :8080)

# schema
cd migrations && uv run alembic upgrade head && cd ..

# checks
uv run ruff check . && uv run pyright && uv run pytest
```

Tests marked `integration` need the stack, and they are skipped when it is not running.

## Without services: the `adce` command line

```bash
uv run adce parse eval/gold/bi/apps/orders_local       # statements, coverage, tables, issues
uv run adce translate eval/gold/bi/apps/orders_local   # Dremio SQL per table
uv run adce validate eval/gold/bi/apps/orders_local    # V0-V4 in the local venue (DuckDB)
```

An app folder holds `*.qvs`, a reload log, `qvd/*.qvd` and, until Dremio is connected, local source extracts in
`sources/<space>/<folder>/<table>.csv`.

## Running the services

```bash
uv run python -m api_gateway --port 8700            # API (runs the LangGraph pipeline in process)
uv run python -m kg_mcp.server                      # knowledge tools over MCP at http://localhost:8701/mcp
cd apps/workbench && npx pnpm@9.15.9 install && NEXT_PUBLIC_ADCE_API=http://localhost:8700 npx pnpm@9.15.9 dev
                                                    # workbench at http://localhost:3001
```

A sample run looks like this:

```bash
curl -F files=@eval/gold/bi/apps/sales_overview/script.qvs -F files=@eval/gold/bi/apps/sales_overview/reload.log \
     http://localhost:8700/apps/sales_overview/files
curl -X POST -H 'content-type: application/json' -d '{"app_name": "Sales Overview"}' \
     http://localhost:8700/apps/sales_overview/runs
curl http://localhost:8700/apps/sales_overview/tables
```

Settings are `ADCE_*` environment variables (see `deploy/compose/.env.example`):

- **Authentication** is off locally. To check Keycloak tokens, set `ADCE_AUTH_MODE=keycloak`.
- **The model gateway** defaults to `replay`, which makes no model calls. Set `ADCE_GATEWAY_MODE=record` and `ADCE_VERTEX_PROJECT` to call Claude on Vertex AI.

## Deploying

- Images: `docker build -f deploy/docker/python.Dockerfile -t adce-python .` and `deploy/docker/workbench.Dockerfile` (needs several GB of free disk).
- Infrastructure: `terraform -chdir=deploy/terraform/gcp apply -var-file=staging.tfvars` (copy `staging.tfvars.example`).
- Release: `helm upgrade --install adce deploy/helm/adce -n adce -f deploy/helm/adce/values-staging.yaml`; migrations run as a pre-install/pre-upgrade hook.

## Rules

- Read-only towards Dremio and Qlik. No row-level data goes to AI models; the model gateway enforces this.
- Knowledge is written only through `KGPort` / kg-mcp, with evidence. The fact log is append-only.
- Every Qlik construct needs a fixture and a test before it counts as supported.
- Real client material goes in `eval/gold/bi/apps/<app>/private/`, which git ignores.
