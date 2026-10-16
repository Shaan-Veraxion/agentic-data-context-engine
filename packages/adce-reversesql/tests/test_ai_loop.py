"""The LLM pathway on synthetic apps, replayed from recorded model answers (no network, no key).

The recordings were made live (Groq, gpt-oss-120b). longtail_local: statements the rules cannot translate;
rulefix_local: a statement the rules translate into SQL that runs but contradicts the QVD (number-to-text
formatting). Each app's oracle is its make_fixture.py, computed by hand, never from SQL.
"""
import asyncio
from pathlib import Path

from adce_gateway.config import load_config
from adce_gateway.gateway import ModelGateway, Recordings
from adce_reversesql.ai_loop import TranslationMemory, repair_with_ai, static_problems, translate_with_ai
from adce_reversesql.cli import _load, _venue
from adce_reversesql.compose import compose
from adce_reversesql.reconcile import Reconciler, _shape, literal_words
from adce_reversesql.runtime import make_validator

ROOT = Path(__file__).resolve().parents[3]
APPS = ROOT / "eval" / "gold" / "bi" / "apps"
CONFIG = ROOT / "deploy" / "gateway" / "groq-free.toml"


def _setup(name: str, recordings: Path):  # type: ignore[no-untyped-def]
    app_dir = APPS / name
    log, app = _load(app_dir)
    venue, heads, conts = _venue(app_dir, app)
    gw = ModelGateway("replay", Recordings(recordings), config=load_config(CONFIG))
    return app, make_validator(venue, log, heads, conts), gw, (venue, log, heads, conts)


def _levels(app, overrides, data, tables):  # type: ignore[no-untyped-def]
    t = compose(app, overrides)
    return {x: Reconciler(t, *data).validate(x) for x in tables}


def test_sources_with_no_local_data_are_not_sent_to_the_model_as_a_mismatch(tmp_path):
    from adce_reversesql.local import LocalVenue

    app, _, _, (_, log, heads, conts) = _setup("rulefix_local", ROOT / "eval" / "recordings")
    validator = make_validator(LocalVenue(), log, heads, conts)  # a venue with none of the app's data
    t = compose(app, {})
    level, problems = validator(t, "Products")
    assert level == "V1" and problems == []
    # a gateway with no recordings: any model call would fail the test with ReplayMiss, recorded as "skipped"
    empty = ModelGateway("replay", Recordings(tmp_path / "none"), config=load_config(CONFIG))
    _, fixes = asyncio.run(repair_with_ai(app, empty, TranslationMemory(tmp_path / "m.json"), validator))
    assert fixes == []


def test_untranslatable_statements_are_translated_validated_and_remembered(tmp_path):
    app, validator, gw, data = _setup("longtail_local", ROOT / "eval" / "recordings")
    memory = TranslationMemory(tmp_path / "memory.json")
    overrides, outcomes = asyncio.run(translate_with_ai(app, gw, memory, validator=validator))
    assert {o.table: o.status for o in outcomes} == {"Sales": "ai_validated", "Running": "ai_validated"}
    for tv in _levels(app, overrides, data, ["Sales", "Running"]).values():
        assert tv.level.name == "V4" and all(c.ok for c in tv.comparisons)
    memory.save()

    # second run: answers come from memory; a gateway with no recordings proves no model call is made
    empty = ModelGateway("replay", Recordings(tmp_path / "none"), config=load_config(CONFIG))
    _, again = asyncio.run(translate_with_ai(app, empty, TranslationMemory(tmp_path / "memory.json"),
                                             validator=validator))
    assert {o.status for o in again} == {"memory"}


def test_a_wrong_rule_translation_is_diagnosed_corrected_and_reused(tmp_path):
    app, validator, gw, data = _setup("rulefix_local", ROOT / "eval" / "recordings")
    assert _levels(app, {}, data, ["Products"])["Products"].level.name == "V3"  # rules alone: values differ
    memory = TranslationMemory(tmp_path / "memory.json")
    overrides, fixes = asyncio.run(repair_with_ai(app, gw, memory, validator))
    assert [f.status for f in fixes] == ["ai_repaired"]
    assert _levels(app, overrides, data, ["Products"])["Products"].level.name == "V4"
    assert memory.entries[0].replaces_rule
    memory.save()

    # next run: the learned correction is re-checked on the data and applied before the rules, without a model
    empty = ModelGateway("replay", Recordings(tmp_path / "none"), config=load_config(CONFIG))
    ov2, again = asyncio.run(translate_with_ai(app, empty, TranslationMemory(tmp_path / "memory.json"),
                                               validator=validator))
    assert [o.status for o in again] == ["memory_fix"]
    assert _levels(app, ov2, data, ["Products"])["Products"].level.name == "V4"


def test_static_checks():
    assert static_problems("SELECT \"src\".\"a\" AS \"a\" FROM src", ["a"]) == []
    assert "columns not in src" in static_problems("SELECT src.b FROM src", ["a"])[0]
    assert "reads tables other than src" in static_problems("SELECT a FROM other", ["a"])[0]
    assert "not valid Dremio SQL" in static_problems("SELECT FROM WHERE", None)[0]


def test_formats_show_no_data():
    bands = ["0 <= x < 500", "500 <= x < 1000", "1000 <= x < 1500"]
    assert _shape("1000.0 <= x < 1500.0", literal_words(bands)) == "9.9 <= x < 9.9"
    names = ["acme corp", "bolt inc", "zed llc"]
    assert _shape("acme corp", literal_words(names)) == "a a"  # varying words never shown
    assert literal_words(["P20", "P20"]) == frozenset()  # too few distinct values: nothing literal


class Asked:
    """A gateway that records what it was asked and declines every statement."""

    def __init__(self) -> None:
        self.inputs: list[dict] = []

    async def call(self, route, inputs, output, **kw):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        self.inputs.append(inputs)
        return SimpleNamespace(output=output(sql="", translatable=False, notes="declined"), tool_calls=[])


def test_reviewer_decisions_steer_the_loop(tmp_path):
    app, validator, gw, _ = _setup("longtail_local", ROOT / "eval" / "recordings")
    memory = TranslationMemory()
    _, learned = asyncio.run(translate_with_ai(app, gw, memory, validator=validator))
    spans = {o.table: o.span for o in learned}

    # a reviewer rejected Sales's SQL after the answer was learned: not reused, the model is asked again with the
    # rejected SQL and the reason as a failed attempt (Running reads Sales, so it waits for it)
    asked = Asked()
    _, outcomes = asyncio.run(translate_with_ai(app, asked, memory, validator=validator,
                                                rejected={"Sales": {"reason": "net of returns", "at": "9999"}}))
    assert [(o.table, o.status) for o in outcomes] == [("Sales", "declined")]
    [prev] = asked.inputs[0]["previous_attempts"]
    assert "a reviewer rejected the SQL of table Sales: net of returns" in prev["problems"]

    # statements a reviewer accepted as gaps are not sent to the model
    asked = Asked()
    _, outcomes = asyncio.run(translate_with_ai(app, asked, TranslationMemory(), validator=validator,
                                                accepted_gaps=set(spans.values())))
    assert {o.status for o in outcomes} == {"gap_accepted"} and asked.inputs == []


def test_an_input_the_privacy_guard_refuses_is_withheld_not_a_crash(tmp_path):
    from adce_gateway.privacy import PrivacyViolation

    class Guarded:
        async def call(self, route, inputs, output, **kw):  # type: ignore[no-untyped-def]
            raise PrivacyViolation("$.statements: 4 uniform records look like rows")

    app, validator, _, _ = _setup("rulefix_local", ROOT / "eval" / "recordings")
    _, fixes = asyncio.run(repair_with_ai(app, Guarded(), TranslationMemory(), validator))
    assert [f.status for f in fixes] == ["withheld"]
    app, validator, _, _ = _setup("longtail_local", ROOT / "eval" / "recordings")
    _, outcomes = asyncio.run(translate_with_ai(app, Guarded(), TranslationMemory(), validator=validator))
    assert {o.status for o in outcomes} == {"withheld"}
