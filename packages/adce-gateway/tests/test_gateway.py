import json

import httpx2 as httpx  # the HTTP client package the openai SDK uses
import pytest
from adce_gateway import privacy
from adce_gateway.config import GatewayConfig, default_config, load_config
from adce_gateway.gateway import (
    Budget,
    BudgetExceeded,
    ModelGateway,
    OutputInvalid,
    ProviderError,
    Recordings,
    ReplayMiss,
    Usage,
)
from adce_gateway.providers import OpenAICompatibleClient, ProviderConfig
from adce_gateway.routes import ROUTES
from pydantic import BaseModel


class Translation(BaseModel):
    sql: str
    translatable: bool
    notes: str = ""


class FakeClient:
    def __init__(self, fail_first: bool = False, down: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_first = fail_first
        self.down = down

    async def complete(self, model, system, user, output_model, *, max_tokens, effort):
        self.calls.append(model)
        if self.down:
            raise ProviderError("unavailable")
        if self.fail_first and len(self.calls) == 1:
            raise OutputInvalid("truncated")
        return output_model(sql="SELECT 1", translatable=True), Usage(input_tokens=1000, output_tokens=200), model


INPUTS = {"statement": "LOAD a RESIDENT T", "inputs": [{"name": "T", "columns": ["a"]}]}

TWO_PROVIDERS = """
[providers.main]
kind = "anthropic"
api_key_env = "TEST_MAIN_KEY"

[providers.backup]
kind = "openai_compatible"
base_url = "http://backup.local/v1"
api_key_env = "TEST_BACKUP_KEY"
structured_output = "json_object"

[models]
strong = "main:big-model"
standard = "main:mid-model"
light = "backup:small-model"

[routes.translate_statement]
fallbacks = ["backup:other-model"]

[prices."backup:other-model"]
input = 1.0
output = 2.0
"""


async def test_record_then_replay_is_identical_and_free(tmp_path):
    rec = Recordings(tmp_path)
    client = FakeClient()
    live = ModelGateway("record", rec, client)
    r1 = await live.call("translate_statement", INPUTS, Translation)
    assert r1.source == "live" and client.calls == ["claude-sonnet-5-5"] and r1.provider == "vertex"
    assert r1.cost_usd == pytest.approx((1000 * 2.0 + 200 * 10.0) / 1e6)

    replay = ModelGateway("replay", rec)
    r2 = await replay.call("translate_statement", INPUTS, Translation)
    assert r2.source == "replay" and r2.output == r1.output and r2.key == r1.key

    again = await live.call("translate_statement", INPUTS, Translation)  # memoized: no second model call
    assert again.source == "memo" and len(client.calls) == 1


async def test_replay_miss_never_calls_a_model(tmp_path):
    with pytest.raises(ReplayMiss):
        await ModelGateway("replay", Recordings(tmp_path)).call("translate_statement", INPUTS, Translation)


async def test_one_retry_on_invalid_output(tmp_path):
    client = FakeClient(fail_first=True)
    r = await ModelGateway("record", Recordings(tmp_path), client).call("translate_statement", INPUTS, Translation)
    assert isinstance(r.output, Translation) and r.output.translatable and len(client.calls) == 2


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
    assert client.calls == []


def test_privacy_allows_schemas_and_small_aggregates():
    privacy.check({"columns": [{"name": c, "type": "int"} for c in "abcdefgh"],
                   "diffs": [{"measure": "SUM(Amount)", "expected": 10, "actual": 9}]})
    with pytest.raises(privacy.PrivacyViolation):
        privacy.check({"text": "\n".join(f"{i},{i * 2},x" for i in range(30))})


def test_every_route_has_a_prompt():
    for r in ROUTES.values():
        assert r.system_prompt().strip()


def test_config_maps_tiers_and_fallbacks(tmp_path):
    p = tmp_path / "gateway.toml"
    p.write_text(TWO_PROVIDERS, encoding="utf-8")
    cfg = load_config(p)
    assert [b.key for b in cfg.bindings("translate_statement", "standard")] == ["main:mid-model",
                                                                                 "backup:other-model"]
    assert [b.key for b in cfg.bindings("classify_file", "light")] == ["backup:small-model"]
    price = cfg.price(cfg.resolve("backup:other-model"))
    assert price is not None and price.output == 2.0
    assert cfg.price(cfg.resolve("main:mid-model")) is None  # unpriced: reported, not guessed
    bad = tmp_path / "bad.toml"
    bad.write_text(TWO_PROVIDERS.replace('light = "backup:small-model"', 'light = "nowhere:x"'), encoding="utf-8")
    with pytest.raises(ValueError, match="provider"):
        load_config(bad)


async def test_fallback_on_provider_error_and_memo_survives_it(tmp_path):
    p = tmp_path / "gateway.toml"
    p.write_text(TWO_PROVIDERS, encoding="utf-8")
    main, backup = FakeClient(down=True), FakeClient()
    gw = ModelGateway("record", Recordings(tmp_path / "rec"), config=load_config(p),
                      clients={"main": main, "backup": backup})
    r = await gw.call("translate_statement", INPUTS, Translation)
    assert r.provider == "backup" and r.model == "other-model" and r.attempts == ["main:mid-model",
                                                                                   "backup:other-model"]
    assert r.cost_usd == pytest.approx((1000 * 1.0 + 200 * 2.0) / 1e6) and r.priced
    again = await gw.call("translate_statement", INPUTS, Translation)
    assert again.source == "memo" and len(backup.calls) == 1


async def test_all_providers_down_is_reported(tmp_path):
    p = tmp_path / "gateway.toml"
    p.write_text(TWO_PROVIDERS, encoding="utf-8")
    gw = ModelGateway("record", Recordings(tmp_path / "rec"), config=load_config(p),
                      clients={"main": FakeClient(down=True), "backup": FakeClient(down=True)})
    with pytest.raises(ProviderError, match="every provider failed"):
        await gw.call("translate_statement", INPUTS, Translation)


def test_default_is_claude_on_vertex():
    cfg = default_config()
    assert cfg.providers["vertex"].kind == "anthropic_vertex"
    assert cfg.bindings("repair_sql", "strong")[0].key == "vertex:claude-opus-5-5"


def _fake_openai(content: str, finish: str = "stop") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert "JSON Schema" in body["messages"][0]["content"] and body["max_completion_tokens"] == 500
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}})
    return httpx.MockTransport(handler)


async def test_openai_compatible_client_validates_output(monkeypatch):
    import openai

    monkeypatch.setenv("TEST_BACKUP_KEY", "k")
    cfg = ProviderConfig(name="groq", kind="openai_compatible", base_url="http://x/v1", api_key_env="TEST_BACKUP_KEY",
                         structured_output="json_object")
    c = OpenAICompatibleClient(cfg)
    c.client = openai.AsyncOpenAI(api_key="k", base_url="http://x/v1", http_client=httpx.AsyncClient(
        transport=_fake_openai('```json\n{"sql": "SELECT 2", "translatable": true}\n```')))
    out, usage, model = await c.complete("m1", "sys", "user", Translation, max_tokens=500, effort="low")
    assert isinstance(out, Translation) and out.sql == "SELECT 2" and usage.input_tokens == 50 and model == "m1"

    c.client = openai.AsyncOpenAI(api_key="k", base_url="http://x/v1", http_client=httpx.AsyncClient(
        transport=_fake_openai('{"sql": 1}')))
    with pytest.raises(OutputInvalid):
        await c.complete("m1", "sys", "user", Translation, max_tokens=500, effort=None)


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("TEST_BACKUP_KEY", raising=False)
    with pytest.raises(ValueError, match="TEST_BACKUP_KEY"):
        OpenAICompatibleClient(ProviderConfig(name="g", kind="openai_compatible", base_url="http://x/v1",
                                              api_key_env="TEST_BACKUP_KEY"))


def test_gateway_config_type():
    assert isinstance(default_config(), GatewayConfig)


def test_example_configs_load():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "deploy" / "gateway"
    files = sorted(root.glob("*.toml"))
    assert len(files) >= 4
    for f in files:
        cfg = load_config(f)
        for name, route in ROUTES.items():
            assert cfg.bindings(name, route.tier)


def test_rate_limiter_waits_for_room():
    from adce_gateway.limits import RateLimiter

    now = [10.0]
    lim = RateLimiter(rpm=2, tpm=1000, clock=lambda: now[0])
    lim._events.extend([[0.0, 100.0], [10.0, 100.0]])
    assert lim.wait_time(100) == pytest.approx(50.0)  # third request waits for the first to leave the window
    now[0] = 61.0
    assert lim.wait_time(100) == 0.0
    lim2 = RateLimiter(rpm=None, tpm=1000, clock=lambda: 30.0)
    lim2._events.extend([[0.0, 600.0], [20.0, 300.0]])
    assert lim2.wait_time(200) == pytest.approx(30.0)  # 600 + 300 + 200 > 1000: wait until the 600 expires
    assert lim2.wait_time(100) == 0.0
