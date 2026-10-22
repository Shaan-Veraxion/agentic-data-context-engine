"""Fact payload contracts (ADR-1 #2): every fact kind is declared, with the value keys it must carry.

The write policy (ports.check_policy) rejects a fact whose kind is not declared here or whose value lacks a
required key, so a typo or a changed agent cannot write knowledge other agents cannot read. Extra keys are
allowed (kinds grow by adding optional keys; a removed key is a contract change).

Names: the code uses the left column; where the architecture names the same knowledge differently, the design name
is given (renaming would change every fact id and break the supersession links of the history, so the mapping is
documented instead):

| Code kind | Design name (ADR-3, CP-001) |
|---|---|
| reads_source, stores_qvd, reads_qvd | scope_link / READS, WRITES |
| associates | bi_model_link |
| measure_translation | bi_metric_def |
| export_maps_to | metric_mapping |
| report_export | report_structure |
| lineage | lineage (DERIVED_FROM) |
"""
from __future__ import annotations

# kind -> value keys every fact of that kind carries
REQUIRED: dict[str, set[str]] = {
    # 1 intake
    "bi_input": {"file", "class"},
    # 27 extract
    "bi_table": {"fields", "steps"},
    "contains": {"table"},
    "reads_source": {"statement"},
    "stores_qvd": {"path"},
    "reads_qvd": set(),
    "uses_mapping": {"statement"},
    "variable_value": {"value"},
    "loaded_rows": {"lines_fetched"},
    "qvd_header": {"table"},
    "bi_coverage": {"total", "understood"},
    "bi_issue": {"code", "message"},
    "bi_measure": {"expression"},
    "bi_visual": {"dimensions", "measures"},
    "rls_requirement": {"table", "reduction_fields"},
    "lineage": {"inputs", "outputs"},
    # 28 translate
    "bi_transform": {"final_cte", "untranslated"},
    "translation_memory": {"statement", "sql", "status"},
    "memory_check": {"app_id", "passed"},
    "gap_analysis": {"span", "summary"},
    # 29 measures, 23 joins, 21/22 reports
    "measure_translation": set(),
    "associates": {"keys"},
    "model_issue": {"message"},
    "join_plan": set(),
    "report_export": set(),
    "export_unreadable": {"file"},
    "export_unmapped": {"reason"},
    "export_maps_to": set(),
    # 24 synthesize, 25 check, 26 reconcile, 20 publish
    "sql_candidate": {"sql_sha256"},
    "chart_sql": {"complete"},
    "sql_checked": {"ok", "rules"},
    "report_sql": {"level", "venue"},
    "figures_validated": {"level"},
    "pack_published": {"run_id", "files"},
    # reviewers (hitl-service) and later agents
    "translation_override": {"sql"},
    "bi_gap_accepted": set(),
    "gap_answer": set(),
    "resolves_to": {"dataset"},
    "bi_issue_acknowledged": set(),
    "primary_key": set(),
    "scope_link": set(),
    "scope_confirmed": {"files", "decision"},
    "qvd_linkage": {"qvd", "decision"},
    "auto_accepted": {"fact_id"},
    "qvd_produced": {"app_id", "table", "path", "sql"},
    # knowledge service (kg-mcp)
    "conflict": {"facts", "reason"},
    "context": {"text"},
    "entity_link": {"relation"},
}


def problems(kind: str, value: dict[str, object]) -> list[str]:
    """What is wrong with a fact's kind and value; empty when it follows the contract."""
    if kind not in REQUIRED:
        return [f"fact kind {kind!r} is not declared (adce_core.fact_schemas)"]
    missing = sorted(REQUIRED[kind] - set(value))
    return [f"{kind} needs {', '.join(missing)} in its value"] if missing else []
