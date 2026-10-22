"""The whole Qlik path on orders_local: intake -> ... -> reconcile -> migration pack, levels V4 in the local venue."""
import json
from pathlib import Path

from adce_core.artifacts import LocalArtifactStore, get_json
from adce_core.facts import FactStatus
from adce_core.ports import MemoryKG
from orchestrator.pipeline import Deps, run_pipeline

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "orders_local"


def upload(store: LocalArtifactStore) -> list[dict]:
    files = []
    for p in [APP / "script.qvs", APP / "reload.log", *sorted((APP / "qvd").glob("*.qvd")),
              *sorted((APP / "sources").rglob("*.csv"))]:
        name = p.relative_to(APP).as_posix() if "sources" in p.parts else p.name
        files.append({"name": name, "key": store.put(f"uploads/orders_local/{name}", p.read_bytes())})
    return files


async def test_full_qlik_path_reaches_v4_and_publishes_a_pack(tmp_path):
    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    out: dict = dict(await run_pipeline(app_id="orders_local", run_id="r1", files=upload(store),
                                         deps=Deps(kg=kg, artifacts=store)))
    assert out["levels"] == {"Orders": "V4", "CustomerTotals": "V4"}
    assert out["status"] == "waiting_for_dremio"  # local validation only; Dremio re-validation is phase PD

    facts = {f.subject.split(":")[-1]: f for f in await kg.facts(kind="report_sql")}
    assert facts["Orders"].trust.value == "validated" and facts["Orders"].value["venue"] == "local"

    prefix = out["pack_prefix"]
    assert prefix.endswith("/orders_local__20261001T020000__pack-v1")  # <app>__<reload_time>__pack-v<N> (ADR-Q7)
    manifest = get_json(store, f"{prefix}/manifest.json")
    assert set(manifest["files"]) == {"sql/tables/Orders.sql", "sql/tables/CustomerTotals.sql",
                                      "lineage/lineage.json", "lineage/lineage.md", "validation/report.md",
                                      "validation/report.xlsx", "review/untranslated.csv", "review/decisions.csv",
                                      "review/open-items.md"}
    assert manifest["run_id"] == "r1" and manifest["routes"]["translate_statement"] and manifest["software"]
    sql = store.get(f"{prefix}/sql/tables/Orders.sql").decode()
    assert '"sales"."erp"."orders_v2"' in sql and "LEFT JOIN" in sql
    lineage = json.loads(store.get(f"{prefix}/lineage/lineage.json"))
    orders_job = next(j for j in lineage["jobs"] if j["job"]["name"] == "load:Orders")
    assert {"namespace": "dremio://Dremio_Sales", "name": "sales.erp.orders_v2"} in orders_job["inputs"]
    assert {"namespace": "qvd://", "name": "lib://QVD/Orders.qvd"} in orders_job["outputs"]
    report = store.get(f"{prefix}/validation/report.md").decode()
    assert "| Orders | V4 | local |" in report
    assert "## QVD chain" in store.get(f"{prefix}/lineage/lineage.md").decode()
    assert "## Sources without a Dremio dataset" in store.get(f"{prefix}/review/open-items.md").decode()

    # reproducible: the same inputs give the same version, byte for byte; a decision that changes the pack gives v2
    files = {p: store.get(f"{prefix}/{p}") for p in manifest["files"]}
    again: dict = dict(await run_pipeline(app_id="orders_local", run_id="r2", files=upload(store),
                                           deps=Deps(kg=kg, artifacts=store)))
    assert again["pack_prefix"] == prefix
    assert {p: store.get(f"{prefix}/{p}") for p in manifest["files"]} == files
    assert get_json(store, f"{prefix}/manifest.json")["run_id"] == "r1"  # the version's first run
    fresh_store, fresh_kg = LocalArtifactStore(tmp_path / "fresh"), MemoryKG()
    fresh: dict = dict(await run_pipeline(app_id="orders_local", run_id="r9", files=upload(fresh_store),
                                           deps=Deps(kg=fresh_kg, artifacts=fresh_store)))
    assert {p: fresh_store.get(f"{fresh['pack_prefix']}/{p}") for p in manifest["files"]} == files  # elsewhere too
    rep = (await kg.facts(kind="report_sql", subject="app:orders_local/table:Orders"))[-1]
    await kg.decide(rep.id, FactStatus.CONFIRMED, "owner")
    v2: dict = dict(await run_pipeline(app_id="orders_local", run_id="r3", files=upload(store),
                                        deps=Deps(kg=kg, artifacts=store)))
    assert v2["pack_prefix"].endswith("__pack-v2")
    assert "report_sql" in store.get(f"{v2['pack_prefix']}/review/decisions.csv").decode()


async def test_llm_pathway_in_the_pipeline_reaches_v4_and_remembers(tmp_path):
    """Agent 28 with the model gateway (replaying recorded Groq answers): statements the rules cannot translate are
    proposed by the model, validated against the reload log and QVDs, and stored as translation memory."""
    from adce_gateway.config import load_config
    from adce_gateway.gateway import ModelGateway, Recordings

    root = Path(__file__).resolve().parents[3]
    app = root / "eval" / "gold" / "bi" / "apps" / "longtail_local"
    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    files = []
    for p in [app / "script.qvs", app / "reload.log", *sorted((app / "qvd").glob("*.qvd")),
              *sorted((app / "sources").rglob("*.csv"))]:
        name = p.relative_to(app).as_posix() if "sources" in p.parts else p.name
        files.append({"name": name, "key": store.put(f"uploads/longtail_local/{name}", p.read_bytes())})
    gw = ModelGateway("replay", Recordings(root / "eval" / "recordings"),
                      config=load_config(root / "deploy" / "gateway" / "groq-free.toml"))
    out: dict = dict(await run_pipeline(app_id="longtail_local", run_id="r1", files=files,
                                         deps=Deps(kg=kg, artifacts=store, gateway=gw)))
    assert out["levels"] == {"Sales": "V4", "Running": "V4"}
    memory = await kg.facts(kind="translation_memory")
    assert len(memory) == 2 and {f.value["status"] for f in memory} == {"ai_validated"}
    # provenance: which run, model and prompt version produced each learned answer
    assert all(f.run_id == "r1" and f.model and f.prompt_version == "v3" for f in memory), [
        (f.run_id, f.model, f.prompt_version) for f in memory]


async def test_pipeline_corrects_a_wrong_rule_translation(tmp_path):
    """rulefix_local: the rules' SQL runs but contradicts the QVD; the diagnosis step (replayed Groq answers) corrects
    the statement, the pack's table reaches V4, and the correction is remembered as a rule fix."""
    from adce_gateway.config import load_config
    from adce_gateway.gateway import ModelGateway, Recordings

    root = Path(__file__).resolve().parents[3]
    app = root / "eval" / "gold" / "bi" / "apps" / "rulefix_local"
    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    files = []
    for p in [app / "script.qvs", app / "reload.log", *sorted((app / "qvd").glob("*.qvd")),
              *sorted((app / "sources").rglob("*.csv"))]:
        name = p.relative_to(app).as_posix() if "sources" in p.parts else p.name
        files.append({"name": name, "key": store.put(f"uploads/rulefix_local/{name}", p.read_bytes())})
    gw = ModelGateway("replay", Recordings(root / "eval" / "recordings"),
                      config=load_config(root / "deploy" / "gateway" / "groq-free.toml"))
    out: dict = dict(await run_pipeline(app_id="rulefix_local", run_id="r1", files=files,
                                         deps=Deps(kg=kg, artifacts=store, gateway=gw)))
    assert out["levels"] == {"Products": "V4"}
    memory = await kg.facts(kind="translation_memory")
    assert [f.value["replaces_rule"] for f in memory] == [True]
