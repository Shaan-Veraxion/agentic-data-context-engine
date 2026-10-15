"""Event-driven re-runs and learning from reviewers: a reviewer writes the SQL for a statement in one application;
the decision's outbox event re-runs that application and every other one with the same statement, which reuses
the reviewer's SQL (checked on its own data). Nothing loops: a second pass finds no new work."""
from pathlib import Path

from adce_core.artifacts import LocalArtifactStore
from adce_core.feedback import feedback_history
from adce_core.ports import MemoryKG
from hitl_service.service import MemoryReviewStore, ReviewService
from orchestrator.events import EventConsumer
from orchestrator.pipeline import Deps, run_pipeline

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "eval" / "cases" / "llm" / "textbetween_purgechar"
SQL = ("SELECT Id, SUBSTR(Label, STRPOS(Label, '[') + 1, STRPOS(Label, ']') - STRPOS(Label, '[') - 1) AS Tag, "
       "REGEXP_REPLACE(Code, '[-/]', '') AS CleanCode FROM src")


def upload(store: LocalArtifactStore, app_id: str) -> list[dict]:
    files = []
    for p in [CASE / "script.qvs", CASE / "reload.log", *sorted((CASE / "qvd").glob("*.qvd")),
              *sorted((CASE / "sources").rglob("*.csv"))]:
        name = p.relative_to(CASE).as_posix() if "sources" in p.parts else p.name
        files.append({"name": name, "key": store.put(f"uploads/{app_id}/{name}", p.read_bytes())})
    return files


async def test_a_reviewer_decision_re_runs_the_apps_it_affects(tmp_path):
    kg, store = MemoryKG(), LocalArtifactStore(tmp_path)
    deps = Deps(kg=kg, artifacts=store)  # AI off: only rules, memory and reviewers
    for app in ("shop_eu", "shop_us", "shop_apac"):
        await run_pipeline(app_id=app, run_id="r1", files=upload(store, app), deps=deps)
    consumer = EventConsumer(deps)
    assert await consumer.drain() == []  # the runs' own facts affect no other application

    review = ReviewService(MemoryReviewStore(), kg)
    await review.refresh("shop_eu")
    [item] = [i for i in review.queue("shop_eu") if i.kind == "untranslated"]
    assert "TextBetween" in item.detail["text"]
    await review.decide(item.id, "provide_sql", "ana", {"sql": SQL})

    results = {r["app_id"]: r for r in await consumer.drain()}
    assert results["shop_eu"]["levels"] == {"L": "V4"} and results["shop_eu"]["because"] == ["reviewer SQL"]
    # the same statement elsewhere: the reviewer's SQL comes from the memory and is validated on that app's data
    assert set(results) == {"shop_eu", "shop_us", "shop_apac"}
    assert results["shop_us"]["levels"] == {"L": "V4"}
    assert results["shop_us"]["because"] == ["reviewer SQL for the same statement"]
    assert await consumer.drain() == []  # the re-runs wrote nothing that needs another run

    [rec] = await feedback_history(kg, text="TextBetween(Label")
    assert rec["kind"] == "translation_override" and rec["reviewer"] == "ana" and rec["edit"]["sql"] == SQL

    # a rejection is an event too: the application runs again
    await review.refresh("shop_us")
    gate = next(i for i in review.queue("shop_us") if i.kind == "approve_sql")
    await review.decide(gate.id, "reject", "ana", {"reason": "Tag must be upper case"})
    [again] = await consumer.drain()
    assert again["app_id"] == "shop_us" and again["because"] == ["reviewer rejected SQL"]
    [rej] = await feedback_history(kg, kinds=["report_sql"], app_id="shop_us")
    assert rej["decision"] == "rejected" and rej["reason"] == "Tag must be upper case"
