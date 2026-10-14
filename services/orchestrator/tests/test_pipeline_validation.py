"""The whole Qlik path on orders_local: intake -> ... -> reconcile -> migration pack, levels V4 in the local venue."""
import json
from pathlib import Path

from adce_core.artifacts import LocalArtifactStore, get_json
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
    manifest = get_json(store, f"{prefix}/manifest.json")
    assert set(manifest["files"]) == {"sql/Orders.sql", "sql/CustomerTotals.sql", "lineage.json", "validation.md",
                                      "review.md"}
    sql = store.get(f"{prefix}/sql/Orders.sql").decode()
    assert '"sales"."erp"."orders_v2"' in sql and "LEFT JOIN" in sql
    lineage = json.loads(store.get(f"{prefix}/lineage.json"))
    orders_job = next(j for j in lineage["jobs"] if j["job"]["name"] == "load:Orders")
    assert {"namespace": "dremio://Dremio_Sales", "name": "sales.erp.orders_v2"} in orders_job["inputs"]
    assert {"namespace": "qvd://", "name": "lib://QVD/Orders.qvd"} in orders_job["outputs"]
    report = store.get(f"{prefix}/validation.md").decode()
    assert "| Orders | V4 | local |" in report


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
