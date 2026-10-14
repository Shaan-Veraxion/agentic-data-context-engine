"""The gateway's tool loop with a scripted model: tools run, results go back, a bad answer is returned to the model
as an error, the final answer is validated, and the whole exchange is recorded and replayed without the model."""
from typing import Any

import pytest
from adce_gateway.config import load_config
from adce_gateway.gateway import ModelGateway, Recordings
from adce_gateway.providers import OutputInvalid, Usage
from adce_gateway.tools import SUBMIT, Tool, ToolCall, Turn
from pydantic import BaseModel


class Answer(BaseModel):
    sql: str
    translatable: bool
    notes: str = ""


class Scripted:
    """A model that plays back turns: each is a list of (tool name, arguments)."""

    def __init__(self, turns: list[list[tuple[str, Any]]]) -> None:
        self.turns, self.seen, self.forced = list(turns), [], []

    def start(self, system: str, user: str) -> Any:
        return [user]

    async def turn(self, model: str, conv: Any, tools: list[dict[str, Any]], force: str | None, *,
                   max_tokens: int, effort: str | None) -> Turn:
        self.forced.append(force)
        calls = [ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(self.turns.pop(0))]
        return Turn(calls, "", Usage(input_tokens=100, output_tokens=10), model)

    def add_results(self, conv: Any, results: list[tuple[ToolCall, str]]) -> None:
        self.seen += [(c.name, text) for c, text in results]

    def nudge(self, conv: Any, text: str) -> None:
        self.seen.append(("nudge", text))


def gateway(tmp_path, client, mode="record") -> ModelGateway:
    toml = tmp_path / "g.toml"
    toml.write_text('[providers.p]\nkind = "openai_compatible"\nbase_url = "http://x"\n[models]\nstrong = "p:m"\n'
                    'standard = "p:m"\nlight = "p:m"\n', encoding="utf-8")
    return ModelGateway(mode, Recordings(tmp_path / "rec"), clients={"p": client}, config=load_config(toml))


def tools(log: list) -> list[Tool]:
    async def check(args):
        log.append(args)
        return {"ok": args["sql"].startswith("SELECT"), "problems": [] if args["sql"].startswith("SELECT") else ["x"]}
    return [Tool("check_sql", "check", {"type": "object", "properties": {"sql": {"type": "string"}}}, check)]


async def test_tools_run_bad_answers_bounce_and_the_exchange_replays(tmp_path):
    model = Scripted([[("check_sql", {"sql": "SELEC 1"})],
                      [("check_sql", {"sql": "SELECT 1"})],
                      [(SUBMIT, {"sql": "SELECT 1"})],  # missing 'translatable': schema error, goes back
                      [(SUBMIT, {"sql": "SELECT 1", "translatable": True})]])
    calls: list = []
    gw = gateway(tmp_path, model)
    r = await gw.call("translate_statement", {"statement": "LOAD 1"}, Answer, tools=tools(calls))
    assert r.output == Answer(sql="SELECT 1", translatable=True) and r.turns == 4
    assert [c["sql"] for c in calls] == ["SELEC 1", "SELECT 1"]
    assert [t["tool"] for t in r.tool_calls] == ["check_sql", "check_sql", SUBMIT]
    assert any("does not match the schema" in text for _, text in model.seen)
    assert r.usage.input_tokens == 400

    again = await gateway(tmp_path, Scripted([]), mode="replay").call(
        "translate_statement", {"statement": "LOAD 1"}, Answer, tools=tools([]))
    assert again.source == "replay" and again.output == r.output and len(again.tool_calls) == 3


async def test_the_last_turn_forces_an_answer(tmp_path):
    model = Scripted([[("check_sql", {"sql": "SELECT 1"})]] * 4)
    with pytest.raises(OutputInvalid):
        await gateway(tmp_path, model).call("translate_statement", {"statement": "LOAD 2"}, Answer,
                                            tools=tools([]), max_turns=2)
    assert model.forced == [None, SUBMIT, SUBMIT, SUBMIT]  # the last turn, then two grace turns
    assert any(name == "nudge" and "Last turn" in text for name, text in model.seen)


async def test_an_answer_alias_counts_as_the_answer(tmp_path):
    toml = tmp_path / "g.toml"
    toml.write_text('[providers.p]\nkind = "openai_compatible"\nbase_url = "http://x"\nanswer_aliases = ["json"]\n'
                    '[models]\nstrong = "p:m"\nstandard = "p:m"\nlight = "p:m"\n', encoding="utf-8")
    model = Scripted([[("json", {"sql": "SELECT 2", "translatable": True})]])
    gw = ModelGateway("record", Recordings(tmp_path / "rec"), clients={"p": model}, config=load_config(toml))
    r = await gw.call("translate_statement", {"statement": "LOAD 4"}, Answer, tools=tools([]))
    assert r.output == Answer(sql="SELECT 2", translatable=True)


async def test_a_tool_result_that_looks_like_rows_is_withheld(tmp_path):
    async def leak(args):
        return {"rows": [[1, 2]]}
    model = Scripted([[("leak", {})], [(SUBMIT, {"sql": "SELECT 1", "translatable": True})]])
    await gateway(tmp_path, model).call("translate_statement", {"statement": "LOAD 3"}, Answer,
                                        tools=[Tool("leak", "x", {"type": "object", "properties": {}}, leak)])
    assert model.seen[0][1].startswith("withheld")
