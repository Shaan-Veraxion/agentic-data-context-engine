"""API gateway: the single entry point for the workbench and for uploads.

Run:  uv run uvicorn api_gateway.app:app --port 8700
"""
from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from adce_core import telemetry
from adce_core.artifacts import ArtifactStore, get_json
from adce_core.ports import KGPort
from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from hitl_service.api import create_router as review_router
from hitl_service.service import ReviewService
from orchestrator.pipeline import Deps, resume_pipeline, run_pipeline
from pydantic import BaseModel

from .auth import Principal, require

APP_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_UPLOAD = 200 * 1024 * 1024
LEVEL_ORDER = ["none", "V0", "V1", "V2", "V3", "V4", "V5"]


@dataclass
class Services:
    kg: KGPort
    artifacts: ArtifactStore
    review: ReviewService
    checkpointer: Any | None = None
    gateway: Any | None = None
    kg_mcp_url: str | None = None  # agents reach knowledge through kg-mcp at this URL (settings.kg_transport=mcp)
    llm_tools: bool = False
    interactive: bool = False  # owner questions pause runs (needs the checkpointer)
    budget_usd: float | None = None
    max_calls: int | None = None
    supervisor: str = "auto"
    tolerance: float = 0.0
    queue: Any | None = None  # orchestrator.worker.RunQueue: runs are queued for the worker instead of run here

    def deps(self, kg: Any) -> Deps:
        return Deps(kg=kg, artifacts=self.artifacts, gateway=self.gateway, llm_tools=self.llm_tools,
                    supervisor=self.supervisor, interactive=self.interactive and self.checkpointer is not None,
                    budget_usd=self.budget_usd, max_calls=self.max_calls, tolerance=self.tolerance)


def services(request: Request) -> Services:
    return request.app.state.services


def review_service(request: Request) -> ReviewService:
    return request.app.state.services.review


def reviewer_name(p: Principal = Depends(require("reviewer"))) -> str:
    return p.subject


def safe_name(filename: str) -> str:
    """Keep a 'sources/<path>' prefix (local source extracts) and a QlikView '<App>-prj/' folder; otherwise only the
    file's own name."""
    parts = [p for p in filename.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    prj = next((i for i, p in enumerate(parts[:-1]) if p.lower().endswith("-prj")), None)
    if prj is not None:
        return "/".join(re.sub(r"[^\w.\- ]", "_", p) for p in parts[prj:])
    if "sources" in parts:
        parts = parts[parts.index("sources"):]
        return "/".join(re.sub(r"[^\w.\- ]", "_", p) for p in parts)
    return re.sub(r"[^\w.\- ]", "_", parts[-1]) if parts else "file"


def check_app_id(app_id: str) -> str:
    if not APP_ID.match(app_id):
        raise HTTPException(422, "app_id must be 1-64 letters, digits, '-' or '_'")
    return app_id


def upload_name(key: str) -> str:
    """uploads/<app>/<sha>/<name...> -> <name...> (keeps sources/<path>)."""
    return key.split("/", 3)[3] if key.count("/") >= 3 else key.split("/")[-1]


class AnswersIn(BaseModel):
    answers: dict[str, str]  # review item id (Ask: unresolved_variable) -> the value


def run_view(run_id: str, out: Any, new_items: int) -> dict[str, Any]:
    """A run's response: its status and results, or the owner questions it is waiting for."""
    view: dict[str, Any] = {"run_id": run_id, "status": out.get("status"), "tables": out.get("tables", []),
                            "levels": out.get("levels", {}), "pending": out.get("pending", []),
                            "pack_prefix": out.get("pack_prefix"), "new_review_items": new_items,
                            "metrics": out.get("metrics", {}), "decisions": out.get("decisions", [])}
    paused = out.get("__interrupt__")
    if paused:
        view["status"] = "waiting_for_owner"
        view["questions"] = [q for i in paused for q in (getattr(i, "value", {}) or {}).get("questions", [])]
    return view


class ScopeRunRequest(BaseModel):
    apps: list[str]  # applications run together, producers of QVDs first


class RunRequest(BaseModel):
    app_name: str | None = None
    files: list[str] | None = None  # artifact keys; default: every file uploaded for the app


async def with_agents_kg(s: Services, fn: Any) -> Any:
    """Run fn(kg) with the knowledge port agents use: kg-mcp when configured (ADR-2), else the service itself."""
    if s.kg_mcp_url:
        from kg_mcp.client import McpKG

        async with McpKG.connect(s.kg_mcp_url) as kg:
            return await fn(kg)
    return await fn(s.kg)


def create_app(svc: Services | None = None, cors_origins: list[str] | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if svc is not None:
            app.state.services = svc
            yield
            return
        from adce_core.artifacts import from_settings as artifacts_from_settings
        from adce_core.settings import get_settings
        from adce_gateway.gateway import from_settings as gateway_from_settings
        from adce_kg import KnowledgeService
        from hitl_service.service import PostgresReviewStore
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        s = get_settings()
        kg = await KnowledgeService.from_settings(s)
        store = PostgresReviewStore(s.pg_dsn)
        gateway = gateway_from_settings(s)
        async with AsyncPostgresSaver.from_conn_string(s.pg_dsn) as saver:
            await saver.setup()
            queue = None
            if s.orchestrator_mode == "worker":
                from orchestrator.worker import PostgresRunQueue

                queue = PostgresRunQueue(s.pg_dsn)
            app.state.services = Services(kg=kg, artifacts=artifacts_from_settings(s), queue=queue,
                                          review=ReviewService(store, kg, gateway), checkpointer=saver,
                                          gateway=gateway,
                                          kg_mcp_url=s.kg_mcp_url if s.kg_transport == "mcp" else None,
                                          llm_tools=s.llm_tools, interactive=s.interactive_runs,
                                          budget_usd=s.run_budget_usd, max_calls=s.run_max_calls,
                                          supervisor=s.supervisor, tolerance=s.validation_tolerance)
            try:
                yield
            finally:
                if queue is not None:
                    queue.close()
                store.close()
                await kg.close()

    app = FastAPI(title="ADCE API gateway", version="0.1.0", lifespan=lifespan)
    telemetry.instrument(app, "api-gateway")
    app.add_middleware(CORSMiddleware, allow_origins=cors_origins or ["http://localhost:3001"],
                       allow_methods=["*"], allow_headers=["*"])
    app.include_router(review_router(review_service, reviewer_name), prefix="/review",
                       dependencies=[Depends(require("viewer"))])

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------ uploads and runs
    @app.post("/apps/{app_id}/files")
    async def upload(app_id: str, files: list[UploadFile], s: Services = Depends(services),
                     _p: Principal = Depends(require("reviewer"))) -> dict[str, Any]:
        check_app_id(app_id)
        stored = []
        for f in files:
            data = await f.read()
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, f"{f.filename} is larger than {MAX_UPLOAD} bytes")
            name = safe_name(f.filename or "file")
            sha = hashlib.sha256(data).hexdigest()
            key = s.artifacts.put(f"uploads/{app_id}/{sha[:20]}/{name}", data, f.content_type or
                                  "application/octet-stream")
            stored.append({"name": name, "key": key, "sha256": sha, "bytes": len(data)})
        return {"app_id": app_id, "files": stored}

    @app.get("/apps/{app_id}/files")
    async def list_files(app_id: str, s: Services = Depends(services),
                         _p: Principal = Depends(require("viewer"))) -> list[dict[str, str]]:
        return [{"name": upload_name(k), "key": k} for k in s.artifacts.list(f"uploads/{check_app_id(app_id)}/")]

    @app.post("/runs")
    async def start_scope_run(req: ScopeRunRequest, s: Services = Depends(services),
                              _p: Principal = Depends(require("reviewer"))) -> dict[str, Any]:
        """Run several applications as one scope (ADR-Q4 QVD chains): producers of QVDs before their consumers, so
        each consumer's SQL inlines its producers' latest SQL. Links are known after a first run; a consumer that
        ran before its producer runs again from `resolve` (its translation reused)."""
        from orchestrator.learning import latest_summary, qvd_order, qvd_producers

        apps = [check_app_id(a) for a in dict.fromkeys(req.apps)]
        uploads = {a: s.artifacts.list(f"uploads/{a}/") for a in apps}
        if missing := [a for a, k in uploads.items() if not k]:
            raise HTTPException(409, f"no files uploaded for {', '.join(missing)}")
        order = await qvd_order(s.kg, apps)
        if s.queue is not None:  # one request per app, in order (one worker runs them in that order)
            runs = []
            for a in order:
                run_id = uuid.uuid4().hex[:12]
                files = [{"name": upload_name(k), "key": k} for k in uploads[a]]
                runs.append({"app_id": a, "run_id": run_id, "status": "queued",
                             "request_id": s.queue.put("run", a, run_id, {"files": files})})
            return {"order": order, "runs": runs}

        async def run_one(a: str, start_at: str | None = None) -> dict[str, Any]:
            run_id = uuid.uuid4().hex[:12]
            files = [{"name": upload_name(k), "key": k} for k in uploads[a]]

            async def run(kg: Any) -> Any:
                prev = latest_summary(s.artifacts, a) if start_at else None
                return await run_pipeline(app_id=a, run_id=run_id, files=files, deps=s.deps(kg),
                                          checkpointer=s.checkpointer, start_at=start_at if prev else None,
                                          previous=prev, reuse_translation=bool(prev))

            out = await with_agents_kg(s, run)
            return {"app_id": a, **run_view(run_id, out, await s.review.refresh(a))}

        runs = [await run_one(a) for a in order]
        producers = await qvd_producers(s.kg, apps)  # known now that every app has run
        late = [a for a in order if any(order.index(p) > order.index(a) for p in producers[a])]
        for a in late:  # ran before one of its producers: inline the producer's SQL now
            runs.append(await run_one(a, "resolve") | {"again": "a producer ran after it"})
        return {"order": await qvd_order(s.kg, apps), "runs": runs}

    @app.post("/apps/{app_id}/runs")
    async def start_run(app_id: str, req: RunRequest, s: Services = Depends(services),
                        _p: Principal = Depends(require("reviewer"))) -> dict[str, Any]:
        check_app_id(app_id)
        keys = req.files or s.artifacts.list(f"uploads/{app_id}/")
        if not keys:
            raise HTTPException(409, "no files uploaded for this app")
        files = [{"name": upload_name(k), "key": k} for k in keys]
        run_id = uuid.uuid4().hex[:12]
        if s.queue is not None:  # the orchestrator worker runs it; poll GET /apps/{app}/runs/{run}
            rid = s.queue.put("run", app_id, run_id, {"files": files, "app_name": req.app_name})
            return {"run_id": run_id, "status": "queued", "request_id": rid}

        async def run(kg: Any) -> Any:
            return await run_pipeline(app_id=app_id, run_id=run_id, files=files, app_name=req.app_name,
                                      deps=s.deps(kg), checkpointer=s.checkpointer)

        out = await with_agents_kg(s, run)
        return run_view(run_id, out, await s.review.refresh(app_id))

    @app.post("/apps/{app_id}/runs/{run_id}/answers")
    async def answer_run(app_id: str, run_id: str, body: AnswersIn, s: Services = Depends(services),
                         reviewer: str = Depends(reviewer_name)) -> dict[str, Any]:
        """Answer the owner questions a paused run asked: each answer is recorded as a review decision (a confirmed
        variable_value fact), then the run continues and reads the script again with the answers."""
        check_app_id(app_id)
        if s.checkpointer is None:
            raise HTTPException(409, "runs cannot be resumed without the checkpointer")
        await s.review.refresh(app_id)
        answered = []
        for item_id, value in body.answers.items():
            try:
                await s.review.decide(item_id, "answer", reviewer, {"value": value})
            except Exception as e:  # noqa: BLE001 - unknown or already decided item: tell the caller
                raise HTTPException(409, f"{item_id}: {e}") from e
            answered.append(item_id)
        if s.queue is not None:
            rid = s.queue.put("resume", app_id, run_id, {"answer": {"answered": answered}})
            return {"run_id": run_id, "status": "queued", "request_id": rid, "answered": answered}

        async def run(kg: Any) -> Any:
            return await resume_pipeline(app_id=app_id, run_id=run_id, deps=s.deps(kg), checkpointer=s.checkpointer,
                                         answer={"answered": answered})

        out = await with_agents_kg(s, run)
        return run_view(run_id, out, await s.review.refresh(app_id))

    @app.get("/apps/{app_id}/runs/{run_id}")
    async def get_run(app_id: str, run_id: str, s: Services = Depends(services),
                      _p: Principal = Depends(require("viewer"))) -> Any:
        key = f"apps/{check_app_id(app_id)}/runs/{run_id}/summary.json"
        req = s.queue.latest(app_id, run_id) if s.queue is not None else None
        if req is not None and req.status != "done":  # queued, running, waiting for the owner, or failed
            return {"run_id": run_id, "status": req.status, "request_id": req.id, **req.result}
        if not s.artifacts.exists(key):
            raise HTTPException(404, "run not found")
        return get_json(s.artifacts, key)

    # ------------------------------------------------------------------ workbench reads
    @app.get("/apps")
    async def apps(s: Services = Depends(services), _p: Principal = Depends(require("viewer"))) -> list[dict]:
        out = []
        for f in await s.kg.facts(kind="bi_coverage"):
            ent = await s.kg.entity(f.subject)
            out.append({"app_id": f.subject.split(":", 1)[1], "name": ent.name if ent else f.subject,
                        "coverage": f.value})
        return sorted(out, key=lambda a: a["app_id"])

    @app.get("/apps/{app_id}/overview")
    async def overview(app_id: str, s: Services = Depends(services),
                       _p: Principal = Depends(require("viewer"))) -> dict[str, Any]:
        app_key = f"app:{check_app_id(app_id)}"
        cov = await s.kg.facts(kind="bi_coverage", subject=app_key)
        if not cov:
            raise HTTPException(404, "app not found")
        tables = []
        for link in await s.kg.facts(kind="contains", subject=app_key):
            tk = link.object or ""
            t = (await s.kg.facts(kind="bi_table", subject=tk) or [None])[0]
            if t is None or t.value.get("mapping"):
                continue
            rep = (await s.kg.facts(kind="report_sql", subject=tk) or [None])[-1]
            chk = (await s.kg.facts(kind="sql_checked", subject=tk) or [None])[-1]
            tables.append({"key": tk, "name": tk.split(":")[-1], "fields": t.value.get("fields", []),
                           "dropped": t.value.get("dropped", False),
                           "level": rep.value.get("level") if rep else None,
                           "venue": rep.value.get("venue") if rep else None,
                           "approved": bool(rep and rep.trust.value == "confirmed"),
                           "comparisons": f"{rep.value['comparisons_ok']}/{rep.value['comparisons']}" if rep else None,
                           "checks_ok": chk.value.get("ok") if chk else None})
        issues = await s.kg.facts(kind="bi_issue", subject=app_key)
        packs = await s.kg.facts(kind="pack_published", subject=app_key)
        open_items = s.review.queue(app_id, "open")
        return {"app_id": app_id, "coverage": cov[-1].value, "tables": tables,
                "issues": [i.value for i in issues], "open_review_items": len(open_items),
                "latest_pack": packs[-1].value if packs else None}

    @app.get("/apps/{app_id}/runs")
    async def runs(app_id: str, s: Services = Depends(services),
                   _p: Principal = Depends(require("viewer"))) -> list[dict[str, Any]]:
        """The application's runs, newest first: status, levels, how many supervisor decisions and nodes."""
        out = []
        for key in s.artifacts.list(f"apps/{check_app_id(app_id)}/runs/"):
            if not key.endswith("/summary.json"):
                continue
            sm = get_json(s.artifacts, key)
            trace = sm.get("trace") or []
            out.append({"run_id": sm.get("run_id"), "status": sm.get("status"), "levels": sm.get("levels") or {},
                        "decisions": len(sm.get("decisions") or []), "steps": len(trace),
                        "started": trace[0]["at"] if trace else None, "scope_tables": sm.get("scope_tables")})
        return sorted(out, key=lambda r: r["started"] or 0, reverse=True)

    @app.get("/apps/{app_id}/tables/{table}")
    async def table_detail(app_id: str, table: str, s: Services = Depends(services),
                           _p: Principal = Depends(require("viewer"))) -> dict[str, Any]:
        """One table: its validation, what was not translated, and its SQL step by step next to the script lines each
        step came from (from the latest run)."""
        from adce_reversesql.compose import DIALECT
        from adce_reversesql.runtime import load_translation

        check_app_id(app_id)
        tk = f"app:{app_id}/table:{table}"
        latest_key = f"apps/{app_id}/latest.json"
        if not s.artifacts.exists(latest_key):
            raise HTTPException(404, "no run for this app")
        sm = get_json(s.artifacts, get_json(s.artifacts, latest_key)["summary_key"])
        loaded = load_translation(s.artifacts, sm["bi_ir_key"], sm.get("translation_key"))
        r = loaded.translation.tables.get(table)
        if r is None:
            raise HTTPException(404, "table not found")
        scripts: dict[str, list[str]] = {}
        for f in sm.get("classified") or []:
            if f.get("class") == "script":
                scripts[f["name"]] = s.artifacts.get(f["key"]).decode("utf-8-sig", errors="replace").splitlines()

        def lines(span: str) -> str:
            file, _, rng = span.rpartition(":")
            a, _, b = rng.partition("-")
            src = scripts.get(file) or next((v for k, v in scripts.items() if k.endswith(file)), [])
            if not a.isdigit():
                return ""
            lo, hi = int(a), int(b or a)
            return "\n".join(f"{n}: {src[n - 1]}" for n in range(lo, min(hi, len(src)) + 1))

        statements = {op.statement: op for qt in loaded.app.tables.values() for st in qt.steps for op in st.ops}
        own = {n for st in r.steps for n in st.statements}  # this table's statements
        ctes = loaded.translation.ctes
        order: list[str] = []

        def visit(n: str) -> None:  # the table's CTEs in dependency order (as in its SQL)
            if n in order or n not in ctes:
                return
            for d in ctes[n].deps:
                visit(d)
            order.append(n)

        if r.final_cte:
            visit(r.final_cte)
        steps = []
        for name in order:
            c = ctes[name]
            if not set(c.statements) & own:
                continue
            op = statements.get(c.statements[0]) if c.statements else None
            first_dep = ctes.get(c.deps[0]) if c.deps else None
            steps.append({"cte": name, "kind": c.kind, "statements": c.statements, "spans": c.spans, "ai": c.ai,
                          "sql": c.body.sql(dialect=DIALECT, pretty=True),
                          "script": "\n".join(lines(sp) for sp in c.spans),
                          "input_columns": first_dep.columns if first_dep else None, "text": op.text if op else ""})
        rep = (await s.kg.facts(kind="report_sql", subject=tk) or [None])[-1]
        return {"table": table, "key": tk, "sql": loaded.translation.table_sql(table),
                "steps": steps, "untranslated": [u.__dict__ for u in r.untranslated],
                "validation": rep.value if rep else None, "notes": r.notes}

    @app.get("/apps/{app_id}/sources")
    async def sources(app_id: str, s: Services = Depends(services),
                      _p: Principal = Depends(require("viewer"))) -> list[dict[str, Any]]:
        """Every source the application's SQL reads, with its Dremio dataset when it is mapped."""
        prefix = f"app:{check_app_id(app_id)}/"
        subjects: set[str] = {f.object for f in await s.kg.facts(kind="reads_source")
                              if f.subject.startswith(prefix) and f.object and f.object.startswith("source:")}
        for f in await s.kg.facts(kind="sql_candidate"):
            if f.subject.startswith(prefix):
                subjects |= set(f.value.get("unmapped", [])) | {f"sql:{u}" for u in f.value.get("unqualified", [])}
        mapped = {f.subject: f.value.get("dataset") for f in await s.kg.facts(kind="resolves_to")
                  if f.trust.value == "confirmed"}
        return [{"source": x, "dataset": mapped.get(x)} for x in sorted(subjects)]

    @app.get("/apps/{app_id}/lineage")
    async def lineage_graph(app_id: str, s: Services = Depends(services),
                            _p: Principal = Depends(require("viewer"))) -> dict[str, Any]:
        """Nodes and edges for the lineage view: sources and QVDs -> Qlik tables -> stored QVDs."""
        app_key = f"app:{check_app_id(app_id)}"
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        for link in await s.kg.facts(kind="contains", subject=app_key):
            tk = link.object or ""
            t = (await s.kg.facts(kind="bi_table", subject=tk) or [None])[0]
            if t is None:
                continue
            rep = (await s.kg.facts(kind="report_sql", subject=tk) or [None])[-1]
            nodes[tk] = {"id": tk, "type": "mapping" if t.value.get("mapping") else "table",
                         "label": tk.split(":")[-1], "level": rep.value.get("level") if rep else None,
                         "dropped": t.value.get("dropped", False)}
            for kind in ("reads_source", "reads_qvd", "stores_qvd", "uses_mapping"):
                for f in await s.kg.facts(kind=kind, subject=tk):
                    other = f.object or ""
                    if other not in nodes:
                        ntype = "qvd" if other.startswith("qvd:") else "table" if other.startswith("app:") \
                            else "source"
                        label = other.rsplit("/", 1)[-1] if ntype == "qvd" else other.split(":")[-1]
                        nodes[other] = {"id": other, "type": ntype, "label": label}
                    src, dst = (tk, other) if kind == "stores_qvd" else (other, tk)
                    edges.append({"id": f.id, "source": src, "target": dst, "kind": kind,
                                  "via": f.value.get("via")})
        return {"nodes": list(nodes.values()), "edges": edges}

    @app.get("/apps/{app_id}/packs/{pack}/files/{path:path}")
    async def pack_file(app_id: str, pack: str, path: str, s: Services = Depends(services),
                        _p: Principal = Depends(require("viewer"))) -> Response:
        """A file of a migration pack (`pack` is its folder name, `<app>__<reload_time>__pack-v<N>`)."""
        if ".." in path or ".." in pack or "/" in pack:
            raise HTTPException(400, "bad path")
        key = f"apps/{check_app_id(app_id)}/packs/{pack}/{path}"
        if not s.artifacts.exists(key):
            raise HTTPException(404, "file not found")
        media = {"json": "application/json", "md": "text/markdown", "csv": "text/csv", "sql": "text/plain",
                 "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}.get(
            path.rsplit(".", 1)[-1], "text/plain")
        name = path.rsplit("/", 1)[-1]
        headers = {"content-disposition": f'attachment; filename="{name}"'} if path.endswith(".xlsx") else None
        return Response(s.artifacts.get(key), media_type=media, headers=headers)

    @app.get("/apps/{app_id}/tables")
    async def tables(app_id: str, s: Services = Depends(services),
                     _p: Principal = Depends(require("viewer"))) -> list[dict[str, Any]]:
        app_key = f"app:{check_app_id(app_id)}"
        out = []
        for link in await s.kg.facts(kind="contains", subject=app_key):
            assert link.object is not None
            for t in await s.kg.facts(kind="bi_table", subject=link.object):
                rows = await s.kg.facts(kind="loaded_rows", subject=link.object)
                out.append({"key": link.object, **t.value, "trust": t.trust.value,
                            "loaded_rows": [r.value for r in rows]})
        return out

    @app.get("/facts")
    async def facts(kind: str | None = None, subject: str | None = None, history: bool = False,
                    s: Services = Depends(services),
                    _p: Principal = Depends(require("viewer"))) -> list[dict[str, Any]]:
        return [{"id": f.id, **f.model_dump(mode="json")}
                for f in await s.kg.facts(kind=kind, subject=subject, current=not history)]

    return app


app = create_app()
