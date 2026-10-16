"""What kind of mismatch a validation found, and where it should go next (agent 26, before any model call).

The validation ladder says *that* a table's SQL disagrees with the evidence; this module says *how*, from the
failed comparisons alone, so the orchestrator can route each table to the step most likely to fix it
(Architecture and Workflow §9, "Symptom → likely cause → routed to"):

category: symptom -> likely cause -> route
- no_data: the SQL reads sources with no local data -> evidence missing, not the SQL -> none
- execution_error: the SQL fails to run -> a function or type the venue lacks, a wrong expression -> diagnose
- rows_more: a step or stored table has more rows than Qlik loaded -> join fan-out, duplicate keys -> diagnose
- rows_fewer: fewer rows than Qlik loaded -> a filter, or a variable resolved differently -> diagnose (or ask)
- fields: the stored table's columns differ from the QVD's -> a field renamed or missing -> diagnose
- values: row counts match, field aggregates differ -> expression semantics (types, rounding, formats) -> diagnose
- mapping_keys: a mapping table has duplicate keys -> ApplyMap's first match is not exact in SQL -> review
- none: nothing failed

The route is a recommendation: the supervisor (an LLM node when it is on) picks the next action from these.
"""
from __future__ import annotations

from typing import Any

from .local import NO_DATA

ROUTES = {"no_data": "none", "execution_error": "diagnose", "rows_more": "diagnose", "rows_fewer": "diagnose",
          "fields": "diagnose", "values": "diagnose", "mapping_keys": "review", "none": "none"}


def _rows(c: dict[str, Any]) -> str | None:
    """rows_more / rows_fewer for a failed row-count comparison."""
    exp, act = c.get("expected"), c.get("actual")
    if isinstance(act, dict):  # a step: Qlik's count can be the rows read or the rows produced; use the produced
        act = act.get("produced") if act.get("produced") is not None else act.get("read")
    if not isinstance(exp, int | float) or not isinstance(act, int | float):
        return None
    return "rows_more" if act > exp else "rows_fewer" if act < exp else None


def classify(failed: list[dict[str, Any]], errors: list[str]) -> dict[str, Any]:
    """failed: the failed comparisons (Comparison as dicts); errors: the validation's errors.
    Returns {category, route, detail}: the first category that applies, in the order of the table above."""
    real_errors = [e for e in errors if NO_DATA not in e]
    if errors and not real_errors and not failed:
        return {"category": "no_data", "route": "none",
                "detail": "; ".join(e.removeprefix("execution: ") for e in errors)[:300]}
    if real_errors:
        return {"category": "execution_error", "route": "diagnose", "detail": real_errors[0][:300]}
    for want in ("rows_more", "rows_fewer"):
        hits = [c for c in failed if c.get("check") in ("rows_step", "rows_store") and _rows(c) == want]
        if hits:
            c = hits[0]
            return {"category": want, "route": ROUTES[want],
                    "detail": f"{c.get('subject')}: expected {c.get('expected')}, got {c.get('actual')}"}
    for check, category in (("fields", "fields"), ("field_aggregate", "values"), ("mapping_keys", "mapping_keys")):
        hits = [c for c in failed if c.get("check") == check]
        if hits:
            return {"category": category, "route": ROUTES[category],
                    "detail": ", ".join(str(c.get("subject")) for c in hits[:5])}
    if failed:  # a comparison kind not listed above
        return {"category": "values", "route": "diagnose", "detail": str(failed[0].get("subject"))}
    return {"category": "none", "route": "none", "detail": ""}
