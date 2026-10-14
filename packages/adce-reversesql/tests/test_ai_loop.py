"""The LLM pathway on longtail_local, replayed from recorded model answers (no network, no key).

The recordings were made live (Groq, gpt-oss-120b): the first answer for Sales failed the V4 comparison of
Class(), the problems went back to the model, and the second answer passed. The oracle is make_fixture.py.
"""
import asyncio
from pathlib import Path

from adce_gateway.config import load_config
from adce_gateway.gateway import ModelGateway, Recordings
from adce_reversesql.ai_loop import TranslationMemory, static_problems, translate_with_ai
from adce_reversesql.cli import _load, _venue
from adce_reversesql.compose import compose
from adce_reversesql.reconcile import Reconciler
from adce_reversesql.runtime import make_validator

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "eval" / "gold" / "bi" / "apps" / "longtail_local"


def _setup(tmp_path):  # type: ignore[no-untyped-def]
    log, app = _load(APP)
    venue, heads, conts = _venue(APP, app)

    validator = make_validator(venue, log, heads, conts)

    gw = ModelGateway("replay", Recordings(ROOT / "eval" / "recordings"),
                      config=load_config(ROOT / "deploy" / "gateway" / "groq-free.toml"))
    return app, validator, gw, log, venue, heads, conts


def test_replayed_llm_answers_reach_v4_and_are_remembered(tmp_path):
    app, validator, gw, log, venue, heads, conts = _setup(tmp_path)
    memory = TranslationMemory(tmp_path / "memory.json")
    overrides, outcomes = asyncio.run(translate_with_ai(app, gw, memory, validator=validator))
    status = {o.table: (o.status, len(o.attempts)) for o in outcomes}
    assert status == {"Sales": ("ai_validated", 2), "Running": ("ai_validated", 1)}
    t = compose(app, overrides)
    for table in ("Sales", "Running"):
        tv = Reconciler(t, venue, log, heads, conts).validate(table)
        assert tv.level.name == "V4" and all(c.ok for c in tv.comparisons)
    memory.save()

    # second run: answers come from memory; a gateway with no recordings proves no model call is made
    empty = ModelGateway("replay", Recordings(tmp_path / "none"),
                         config=load_config(ROOT / "deploy" / "gateway" / "groq-free.toml"))
    _, again = asyncio.run(translate_with_ai(app, empty, TranslationMemory(tmp_path / "memory.json"),
                                             validator=validator))
    assert {o.status for o in again} == {"memory"}


def test_static_checks():
    assert static_problems("SELECT \"src\".\"a\" AS \"a\" FROM src", ["a"]) == []
    assert "columns not in src" in static_problems("SELECT src.b FROM src", ["a"])[0]
    assert "reads tables other than src" in static_problems("SELECT a FROM other", ["a"])[0]
    assert "not valid Dremio SQL" in static_problems("SELECT FROM WHERE", None)[0]
