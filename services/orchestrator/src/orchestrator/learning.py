"""Learning over time: re-run every application from its stored uploads, and report what the memory has learned.

Each pipeline run reuses learned answers (translation memory) and re-checks each one on that application's data;
agent 28 records every check as a memory_check fact and retires an answer the data disproves. Running all
applications again on a schedule (and whenever an application gets new uploads, e.g. a newer reload log or QVDs)
therefore keeps re-testing what was learned against more and newer data:

- an answer that passed on two or more applications and never failed is proven: a candidate for a permanent rule;
- an answer that failed anywhere is disproved and no longer used;
- statements that were skipped (provider quota) are tried again.

    uv run python -m orchestrator.learning report            # what the memory holds, with its evidence
    uv run python -m orchestrator.learning rerun             # re-run every application with uploads
Scheduled in Kubernetes by the learning CronJob (deploy/helm), daily by default.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from adce_core.ports import KGPort
from adce_reversesql.ai_loop import memory_stats

from .pipeline import Deps, run_pipeline


async def uploads_by_app(kg: KGPort) -> dict[str, list[dict[str, Any]]]:
    """The files each application was given (intake's bi_input facts): the latest upload of each file name."""
    apps: dict[str, dict[str, dict[str, Any]]] = {}
    for f in await kg.facts(kind="bi_input"):
        app = f.subject.removeprefix("app:")
        key = next((e.ref for e in f.evidence if e.kind == "upload"), None)
        if key:
            apps.setdefault(app, {})[f.value["file"]] = {"name": f.value["file"], "key": key}
    return {a: list(files.values()) for a, files in apps.items()}


async def rerun_all(deps: Deps, *, apps: list[str] | None = None,
                    run_prefix: str = "scheduled") -> list[dict[str, Any]]:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    out = []
    for app_id, files in sorted((await uploads_by_app(deps.kg)).items()):
        if apps and app_id not in apps:
            continue
        try:
            st = dict(await run_pipeline(app_id=app_id, run_id=f"{run_prefix}-{stamp}", files=files, deps=deps))
            out.append({"app_id": app_id, "status": st.get("status"), "levels": st.get("levels", {})})
        except Exception as e:  # noqa: BLE001 - one application failing does not stop the others
            out.append({"app_id": app_id, "status": "error", "error": f"{type(e).__name__}: {e}"})
    return out


@asynccontextmanager
async def agents_kg(settings: Any, kg: KGPort) -> AsyncIterator[Any]:
    """The knowledge port agents get in re-runs: kg-mcp when the deployment uses it (ADR-2), else `kg`."""
    if settings.kg_transport != "mcp":
        yield kg
        return
    from kg_mcp.client import McpKG

    async with McpKG.connect(settings.kg_mcp_url) as mcp:
        yield mcp


async def memory_report(kg: KGPort) -> list[dict[str, Any]]:
    """Every learned answer with its evidence: where it was accepted, where re-checks passed or failed, its state."""
    latest: dict[str, Any] = {}
    for f in await kg.facts(kind="translation_memory"):
        latest[f.subject] = f
    stats = memory_stats(await kg.facts(kind="memory_check"))
    rows = []
    for subject, f in sorted(latest.items()):
        v = f.value
        st = stats.get(subject.removeprefix("memory:"), {"passed_apps": set(), "failed_apps": set(), "checks": 0})
        # the app it was learned on counts once when the answer was validated there
        confirmed = set(st["passed_apps"]) | ({v.get("app_id")} if v.get("status") == "ai_validated" else set())
        state = "disproved" if v.get("status") == "disproved" or st["failed_apps"] else \
            "proven" if len(confirmed) >= 2 else "learned"
        rows.append({"memory": subject, "state": state, "status": v.get("status"),
                     "replaces_rule": v.get("replaces_rule", False), "learned_on": v.get("app_id"),
                     "checks": st["checks"], "passed_on": sorted(st["passed_apps"]),
                     "failed_on": sorted(st["failed_apps"]), "statement": v.get("statement", "")[:160],
                     "sql": v.get("sql", "")})
    return rows


async def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator.learning", description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("command", choices=["report", "rerun"])
    p.add_argument("--app", action="append", help="only these applications (rerun)")
    args = p.parse_args(argv)

    from adce_core.artifacts import from_settings as artifacts_from_settings
    from adce_core.settings import get_settings
    from adce_gateway.gateway import from_settings as gateway_from_settings
    from adce_kg import KnowledgeService

    s = get_settings()
    kg = await KnowledgeService.from_settings(s)
    try:
        if args.command == "report":
            print(json.dumps(await memory_report(kg), indent=1))
            return 0
        async with agents_kg(s, kg) as akg:
            deps = Deps(kg=akg, artifacts=artifacts_from_settings(s), gateway=gateway_from_settings(s),
                        llm_tools=s.llm_tools)
            print(json.dumps(await rerun_all(deps, apps=args.app), indent=1))
        return 0
    finally:
        close = getattr(kg, "close", None)
        if close is not None:
            await close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
