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
