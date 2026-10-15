"""Gaps for a person: a part of the script no rule or model can translate (files in a folder, read in a loop, with
their modification time) is explained by the explain_gap route, becomes an Ask item with the model's questions,
and the owner's answers go back into the next analysis, through the outbox event they raise."""
from types import SimpleNamespace

from adce_core.artifacts import LocalArtifactStore
from adce_core.ports import MemoryKG
from adce_reversesql.runtime import GapAnalysis
from hitl_service.service import MemoryReviewStore, ReviewService
from orchestrator.events import EventConsumer
from orchestrator.pipeline import Deps, run_pipeline

SCRIPT = """LET vPath = 'lib://Data/';
FOR EACH File IN FileList('$(vPath)fx_month_*.csv')
Files:
LOAD '$(File)' AS FilePath, FileTime('$(File)') AS Modified AUTOGENERATE 1;
NEXT File
"""


class Model:
    """Declines every statement; explains every gap, more concretely once the owner answered."""

    def __init__(self) -> None:
        self.gap_inputs: list[dict] = []

    async def call(self, route, inputs, output, **kw):  # type: ignore[no-untyped-def]
        if route == "explain_gap":
            self.gap_inputs.append(inputs)
            answered = "owner_answers" in inputs
            out = GapAnalysis(summary="Lists the monthly FX files in a folder with their modification time.",
                              pattern="files in a folder, one per month",
                              dremio_approach="Promote the folder as one dataset; the file date comes from a column.",
                              questions=[] if answered else ["Which Dremio dataset holds the FX files?"],
                              proposed_sql='SELECT * FROM "fx"."monthly"' if answered else "SELECT * FROM <dataset>",
                              confidence="high" if answered else "medium")
        else:
            out = output.model_validate({"sql": "", "translatable": False, "notes": "file metadata",
                                         "statement": None, "cause": "n/a"})
        return SimpleNamespace(output=out, tool_calls=[], model="scripted")


async def test_a_gap_is_explained_asked_answered_and_explained_again(tmp_path):
    kg, store, model = MemoryKG(), LocalArtifactStore(tmp_path), Model()
    deps = Deps(kg=kg, artifacts=store, gateway=model)
    files = [{"name": "script.qvs", "key": store.put("uploads/fx/script.qvs", SCRIPT.encode())}]
    await run_pipeline(app_id="fx", run_id="r1", files=files, deps=deps)

    [first] = model.gap_inputs
    assert "FileList" in first["script_block"] and first["variables"]["vPath"] == "lib://Data/"
    [gap] = await kg.facts(kind="gap_analysis")
    assert gap.value["pattern"] == "files in a folder, one per month" and gap.value["tables"] == ["Files"]

    review = ReviewService(MemoryReviewStore(), kg)
    await review.refresh("fx")
    [ask] = [i for i in review.queue("fx") if i.kind == "gap"]
    assert ask.mode == "ask" and ask.detail["questions"] == ["Which Dremio dataset holds the FX files?"]
    await review.decide(ask.id, "answer", "ana", {"answers": {"Which Dremio dataset holds the FX files?":
                                                             '"fx"."monthly"'}})

    [rerun] = await EventConsumer(deps).drain()
    assert rerun["app_id"] == "fx" and rerun["because"] == ["owner answered questions about a gap"]
    assert model.gap_inputs[-1]["owner_answers"]["answers"] == {"Which Dremio dataset holds the FX files?":
                                                                '"fx"."monthly"'}
    latest = [f for f in await kg.facts(kind="gap_analysis") if f.value["confidence"] == "high"]
    assert latest and latest[0].value["proposed_sql"] == 'SELECT * FROM "fx"."monthly"'
