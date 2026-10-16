"""Learning over time: a correction learned on one app is re-checked on every later app; it becomes proven when two
apps' data confirm it and is disproved, and no longer used, when another app's data contradicts it.

The correction the model gave for rulefix_local ('P' & Price written without trailing zeros) strips zeros with a
regex: right for 20.0, wrong for a whole number stored as an integer (100 -> 'P1'). Only data shows it."""
import csv
from pathlib import Path

from adce_core.artifacts import LocalArtifactStore
from adce_core.ports import MemoryKG
from adce_gateway.config import load_config
from adce_gateway.gateway import ModelGateway, Recordings
from orchestrator.events import affected
from orchestrator.learning import memory_report, rerun_all, uploads_by_app
from orchestrator.pipeline import Deps, run_pipeline
from pyqvd import QvdTable

ROOT = Path(__file__).resolve().parents[3]
RULEFIX = ROOT / "eval" / "gold" / "bi" / "apps" / "rulefix_local"


def integer_prices_app(d: Path) -> Path:
    """rulefix_local's script over whole-number prices delivered as integers; Qlik writes 'P100'."""
    rows = [(1, "crate", 100), (2, "pallet", 250), (3, "box", 7)]
    (d / "sources" / "shop" / "raw").mkdir(parents=True)
    with (d / "sources" / "shop" / "raw" / "products.csv").open("w", newline="") as fh:
        csv.writer(fh).writerows([("ID", "Name", "Price"), *rows])
    script = (RULEFIX / "script.qvs").read_text(encoding="utf-8")
    (d / "script.qvs").write_text(script, encoding="utf-8")
    log = (RULEFIX / "reload.log").read_text(encoding="utf-8").replace("5 Lines fetched", "3 Lines fetched")
    (d / "reload.log").write_text(log, encoding="utf-8")
    (d / "qvd").mkdir()
    QvdTable.from_dict({"columns": ["ID", "Name", "Price", "PriceTag"],
                        "data": [[i, n, p, f"P{p}"] for i, n, p in rows]}).to_qvd(str(d / "qvd" / "Products.qvd"))
    return d


def upload(store: LocalArtifactStore, app_id: str, d: Path) -> list[dict]:
    files = []
    for p in [d / "script.qvs", d / "reload.log", *sorted((d / "qvd").glob("*.qvd")),
              *sorted((d / "sources").rglob("*.csv"))]:
        name = p.relative_to(d).as_posix() if "sources" in p.parts else p.name
        files.append({"name": name, "key": store.put(f"uploads/{app_id}/{name}", p.read_bytes())})
    return files


async def test_a_learned_correction_is_proven_then_disproved_by_new_data(tmp_path):
    store, kg = LocalArtifactStore(tmp_path / "a"), MemoryKG()
    gw = ModelGateway("replay", Recordings(ROOT / "eval" / "recordings"),
                      config=load_config(ROOT / "deploy" / "gateway" / "groq-free.toml"))
    deps = Deps(kg=kg, artifacts=store, gateway=gw)

    first = await run_pipeline(app_id="rulefix_local", run_id="r1", files=upload(store, "rulefix_local", RULEFIX),
                               deps=deps)
    assert first.get("levels") == {"Products": "V4"}  # learned: the diagnosis corrected the rules
    again = await run_pipeline(app_id="rulefix_copy", run_id="r1", files=upload(store, "rulefix_copy", RULEFIX),
                               deps=deps)
    assert again.get("levels") == {"Products": "V4"}  # reused without a model call and re-checked: passes
    [row] = await memory_report(kg)
    assert row["state"] == "proven" and row["passed_on"] == ["rulefix_copy"] and row["learned_on"] == "rulefix_local"

    ints = integer_prices_app(tmp_path / "ints")
    third = await run_pipeline(app_id="integer_prices", run_id="r1", files=upload(store, "integer_prices", ints),
                               deps=deps)
    assert third.get("levels") == {"Products": "V4"}  # the rules are right here; the learned correction is not used
    [row] = await memory_report(kg)
    assert row["state"] == "disproved" and row["failed_on"] == ["integer_prices"]
    # as an event: the applications that relied on the answer run again, and stop using it
    assert await affected(kg, await kg.events()) == {
        "rulefix_local": ["a learned answer it used was disproved"],
        "rulefix_copy": ["a learned answer it used was disproved"]}

    # the scheduled re-run finds every app from its uploads; the disproved correction is no longer applied
    assert set(await uploads_by_app(kg)) == {"rulefix_local", "rulefix_copy", "integer_prices"}
    results = await rerun_all(deps, apps=["integer_prices"])
    assert results == [{"app_id": "integer_prices", "status": "waiting_for_dremio", "levels": {"Products": "V4"},
                        "started_at": "intake"}]
