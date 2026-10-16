"""Event contracts (ADR-1 #3): every event an agent emits or the knowledge service writes to its outbox.

Agents emit the events they declare in AgentSpec.emits, and BaseAgent checks them against this list. Outbox events
(written by the knowledge service with each fact) carry a payload; OUTBOX lists what each payload holds.
"""
from __future__ import annotations

# agent events: name -> what it means (Architecture §5.8, CP-001)
AGENT_EVENTS: dict[str, str] = {
    "upload.received": "files were uploaded for an application",
    "upload.classified[bi_app]": "the files of a BI application are classified (intake, agent 1)",
    "bi_inputs.received": "new files for an application already registered",
    "bi_ir.ready": "the BI IR of an application is written (agent 27)",
    "bi_app.reloaded": "the BI application was reloaded (later: Qlik server polling)",
    "bi_transform.ready": "the translation of the load script is written (agent 28)",
    "bi_metric_def.ready": "chart measures are translated (agent 29)",
    "measures.translated": "chart measures are translated (agent 29)",
    "joins.planned": "the data model's associations and chart join plans are written (agent 23)",
    "exports.parsed": "exported reports are read (agent 21)",
    "metrics.mapped": "export columns are matched to chart measures (agent 22)",
    "sql.candidates": "SQL per table and chart is written (agent 24)",
    "sql.checked": "the SQL passed through the query checker (agent 25)",
    "report_sql.validated": "the SQL was validated and the results recorded (agent 26)",
    "pack.published": "the migration pack is written (agent 20)",
    "dataset.registered": "a Dremio dataset was registered (phase PD)",
    "dataset.changed": "a Dremio dataset or view definition changed (phase PD)",
    "conflict.flagged": "two facts disagree; a review item is raised",
}

# outbox topics (knowledge service) -> the keys of their payload
OUTBOX: dict[str, set[str]] = {
    "fact.proposed": {"fact", "fact_id"},
    "fact.confirmed": {"fact", "fact_id"},
    "fact.rejected": {"fact", "fact_id"},
    "fact.superseded": {"fact", "fact_id"},
    "entity.registered": {"entity"},
    "conflict.flagged": {"fact", "fact_id"},
}


def declared(name: str) -> bool:
    return name in AGENT_EVENTS or name in OUTBOX


def outbox_problems(topic: str, payload: dict[str, object]) -> list[str]:
    if topic not in OUTBOX:
        return [f"outbox topic {topic!r} is not declared (adce_core.events)"]
    missing = sorted(OUTBOX[topic] - set(payload))
    return [f"{topic} payload needs {', '.join(missing)}"] if missing else []
