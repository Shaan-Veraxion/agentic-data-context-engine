"""Every test case in eval/cases, in each variant it declares, run through its agent chain (no network: AI replays
recorded answers). Known gaps must still fail; when one starts passing, mark it active."""
import pytest
from testbench.cases import all_cases, cap_level
from testbench.generate import reload_log
from testbench.runner import Variant, default_variants, resolve_chain, run_case

RUNS = [(c, v) for c in all_cases() if not c.private for v in default_variants(c) if v.ai != "live"]


@pytest.mark.parametrize("case,variant", RUNS, ids=[f"{c.id}[{v.ai}-{v.data}]" for c, v in RUNS])
async def test_case(case, variant):
    r = await run_case(case, variant)
    bad = [f"[{c.section}] {c.name}: expected {c.expected!r}, got {c.actual!r}" for c in r.checks if not c.ok]
    bad += [f"agent {s['number']}: {s['error']}" for s in r.steps if s["error"]]
    if case.status == "known_gap":
        assert r.status == "xfail", f"{case.id} now passes: mark it active"
    else:
        assert r.status == "pass", "\n".join(bad)


async def test_the_orchestrator_runs_a_case_end_to_end():
    case = next(c for c in all_cases() if c.id == "apps/orders_local")
    r = await run_case(case, Variant("off", "full"), [0])
    assert r.status == "pass", [c for c in r.checks if not c.ok]


def test_prerequisites_are_added_in_pipeline_order():
    assert resolve_chain([26]) == [(1, True), (27, True), (28, True), (26, False)]
    assert resolve_chain([22]) == [(1, True), (27, True), (28, True), (21, True), (22, False)]
    assert resolve_chain([0, 26]) == [(0, False)]


def test_levels_are_capped_by_the_data_uploaded():
    assert cap_level("V4", "no_qvd", True) == "V3"
    assert cap_level("V4", "no_log", True) == "V4"
    assert cap_level("V3", "no_log", False) == "V2"
    assert cap_level("V0", "script_only", True) == "V0"


def test_generated_reload_log_counts_row_reading_statements_only():
    script = "SET a = 1;\nT:\nLOAD x;\nSQL SELECT x FROM y;\nU:\nLOAD * INLINE [\nx\n1\n];\nSTORE T INTO [t.qvd];\n"
    log = reload_log(script, [7, 1])
    assert "7 Lines fetched" in log and "1 Lines fetched" in log
    with pytest.raises(ValueError):
        reload_log(script, [7])
