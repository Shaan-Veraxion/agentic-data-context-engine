import pytest
from adce_gateway import privacy
from adce_gateway.gateway import (
    Budget,
    BudgetExceeded,
    ModelGateway,
    OutputInvalid,
    Recordings,
    ReplayMiss,
    Usage,
)
from adce_gateway.routes import ROUTES
from pydantic import BaseModel


class Translation(BaseModel):
    sql: str
    translatable: bool
    notes: str = ""


class FakeClient:
    def __init__(self, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first

    async def complete(self, route, system, user, output_model):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise OutputInvalid("truncated")
        return output_model(sql="SELECT 1", translatable=True), Usage(input_tokens=1000, output_tokens=200), route.model


INPUTS = {"statement": "LOAD a RESIDENT T", "inputs": [{"name": "T", "columns": ["a"]}]}


async def test_record_then_replay_is_identical_and_free(tmp_path):
    rec = Recordings(tmp_path)
    client = FakeClient()
    live = ModelGateway("record", rec, client)
    r1 = await live.call("translate_statement", INPUTS, Translation)
    assert r1.source == "live" and client.calls == 1
    assert r1.cost_usd == pytest.approx((1000 * 2.0 + 200 * 10.0) / 1e6)

    replay = ModelGateway("replay", rec)
    r2 = await replay.call("translate_statement", INPUTS, Translation)
    assert r2.source == "replay" and r2.output == r1.output and r2.key == r1.key

    again = await live.call("translate_statement", INPUTS, Translation)  # memoized: no second model call
    assert again.source == "memo" and client.calls == 1


async def test_replay_miss_never_calls_a_model(tmp_path):
    with pytest.raises(ReplayMiss):
        await ModelGateway("replay", Recordings(tmp_path)).call("translate_statement", INPUTS, Translation)


async def test_one_retry_on_invalid_output(tmp_path):
    client = FakeClient(fail_first=True)
    r = await ModelGateway("record", Recordings(tmp_path), client).call("translate_statement", INPUTS, Translation)
    assert isinstance(r.output, Translation) and r.output.translatable and client.calls == 2


async def test_budget_is_enforced(tmp_path):
    gw = ModelGateway("record", Recordings(tmp_path), FakeClient(), budget=Budget(run_usd=0.0001))
    await gw.call("translate_statement", INPUTS, Translation)
    with pytest.raises(BudgetExceeded):
        await gw.call("translate_statement", {**INPUTS, "statement": "LOAD b RESIDENT T"}, Translation)


async def test_rows_are_rejected_before_any_call(tmp_path):
    client = FakeClient()
    gw = ModelGateway("record", Recordings(tmp_path), client)
    with pytest.raises(privacy.PrivacyViolation):
        await gw.call("translate_statement", {**INPUTS, "sample_values": ["a", "b"]}, Translation)
    with pytest.raises(privacy.PrivacyViolation):
        await gw.call("translate_statement", {"x": [{"id": i, "amount": i * 2} for i in range(10)]}, Translation)
    assert client.calls == 0


def test_privacy_allows_schemas_and_small_aggregates():
    privacy.check({"columns": [{"name": c, "type": "int"} for c in "abcdefgh"],
                   "diffs": [{"measure": "SUM(Amount)", "expected": 10, "actual": 9}]})
    with pytest.raises(privacy.PrivacyViolation):
        privacy.check({"text": "\n".join(f"{i},{i * 2},x" for i in range(30))})


def test_every_route_has_a_prompt():
    for r in ROUTES.values():
        assert r.system_prompt().strip()
