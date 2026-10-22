"""OpenTelemetry: a run makes one span per agent, a model call a gateway span, a request a server span."""
from pathlib import Path

import httpx
import pytest
from adce_core.artifacts import LocalArtifactStore
from adce_core.ports import MemoryKG
from orchestrator.pipeline import Deps, run_pipeline

sdk = pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "orders_local"
EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def provider():
    p = sdk.TracerProvider()
    p.add_span_processor(SimpleSpanProcessor(EXPORTER))
    trace.set_tracer_provider(p)
    yield p


@pytest.fixture(autouse=True)
def clear():
    EXPORTER.clear()


async def test_agents_in_a_run_are_spans(tmp_path):
    store = LocalArtifactStore(tmp_path)
    files = [{"name": p.name, "key": store.put(f"uploads/o/{p.name}", p.read_bytes())}
             for p in [APP / "script.qvs", APP / "reload.log"]]
    await run_pipeline(app_id="o", run_id="r1", files=files, deps=Deps(kg=MemoryKG(), artifacts=store))
    spans = {s.name: s for s in EXPORTER.get_finished_spans()}
    assert {"agent.intake_router", "agent.publisher_lite"} <= set(spans)
    attrs = spans["agent.publisher_lite"].attributes or {}
    assert attrs["adce.run_id"] == "r1" and attrs["adce.app_id"] == "o" and attrs["adce.agent"] == 20


async def test_a_model_call_is_a_span_even_when_it_fails(tmp_path):
    from adce_gateway.gateway import ModelGateway, Recordings
    from pydantic import BaseModel

    class Out(BaseModel):
        x: int

    gw = ModelGateway("replay", Recordings(tmp_path))
    with pytest.raises(Exception):  # noqa: B017 - no recording: the call fails, the span stays
        await gw.call("plan_next", {"q": 1}, Out, task="t1")
    s = next(s for s in EXPORTER.get_finished_spans() if s.name == "gateway.plan_next")
    assert (s.attributes or {})["adce.task"] == "t1" and s.status.status_code.name == "ERROR"


async def test_requests_are_server_spans_that_continue_the_callers_trace(tmp_path):
    from api_gateway.app import Services, create_app
    from hitl_service.service import MemoryReviewStore, ReviewService

    kg = MemoryKG()
    app = create_app(Services(kg=kg, artifacts=LocalArtifactStore(tmp_path), review=ReviewService(MemoryReviewStore(),
                                                                                                 kg)))
    parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/health", headers={"traceparent": parent})).status_code == 200
    s = next(s for s in EXPORTER.get_finished_spans() if s.name == "GET /health")
    assert s.context is not None and format(s.context.trace_id, "032x") == "0af7651916cd43dd8448eb211c80319c"
    assert (s.attributes or {})["http.response.status_code"] == 200
