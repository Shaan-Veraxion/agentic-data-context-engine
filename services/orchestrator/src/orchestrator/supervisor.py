"""The supervisor: after reconciliation it decides what the run does next, until nothing more can be improved.

Code decides what is *allowed* (the options); the supervisor decides what to *do* among them:

- diagnose <table>: the evidence contradicts the table's SQL in a way a corrected statement can fix (agent 26,
  route diagnose_mismatch); each table is diagnosed at most once per run;
- ask_owner: variables the script sets at reload time are unknown, and the run is interactive (it pauses with
  LangGraph `interrupt()` until an owner answers, then extracts again with the answers);
- publish: record the results and write the migration pack (always allowed, and chosen when the step limit is
  reached).

Policies:
- llm: the model (route plan_next) sees each table's level, mismatch category and what was tried, the options, and
  the budget used, and picks one option with its reason. An answer outside the options, or no answer (no
  recording, provider down, budget used), falls back to the rules, and the fallback is recorded.
- rules: diagnose the tables in table order, then ask, then publish.
`auto` uses llm when the gateway calls a model (modes live and record) and rules otherwise (replay, AI off), so
replayed runs and tests stay deterministic.

Every decision is kept in the run state (decisions) with who made it and why: the run timeline shows them.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class NextAction(BaseModel):
    """Structured output of the model-gateway route plan_next."""

    action: Literal["diagnose", "ask_owner", "publish"]
    table: str | None = None
    reason: str = ""


def options(validations: dict[str, dict[str, Any]], diagnosed: dict[str, int], *, has_model: bool,
            can_ask: bool) -> list[dict[str, Any]]:
    """What the run may do next. validations: table -> the reconciler's entry (level, mismatch)."""
    out: list[dict[str, Any]] = []
    if has_model:
        for t, e in validations.items():
            if e["mismatch"]["route"] == "diagnose" and diagnosed.get(t, 0) < 1:
                out.append({"action": "diagnose", "table": t})
    if can_ask:
        out.append({"action": "ask_owner", "table": None})
    out.append({"action": "publish", "table": None})
    return out


def by_rules(opts: list[dict[str, Any]]) -> dict[str, Any]:
    return {**opts[0], "reason": "rules: first option (diagnose in table order, then ask, then publish)",
            "by": "rules"}


def plan_inputs(validations: dict[str, dict[str, Any]], diagnosed: dict[str, int], opts: list[dict[str, Any]],
                steps_left: int, budget: dict[str, Any], questions: list[str]) -> dict[str, Any]:
    """What plan_next sees: per table (keyed by name: these are tables, not data rows) the level reached, the
    mismatch category with its detail (aggregate comparisons only), whether it was already diagnosed."""
    tables = {t: {"level": e["level"], "mismatch": e["mismatch"]["category"],
                  "detail": e["mismatch"]["detail"][:300], "comparisons_failed": len(e["failed"]),
                  "diagnosed_already": diagnosed.get(t, 0)} for t, e in validations.items()}
    return {"tables": tables, "options": [f"{o['action']}:{o['table']}" if o["table"] else o["action"] for o in opts],
            "steps_left": steps_left, "budget": budget, "owner_questions": questions[:20]}


async def by_llm(gateway: Any, inputs: dict[str, Any], opts: list[dict[str, Any]], task: str) -> dict[str, Any]:
    """The model's choice, or the rules' with the reason the model's could not be used."""
    try:
        r = await gateway.call("plan_next", inputs, NextAction, task=task)
    except Exception as e:  # noqa: BLE001 - any failure: the rules decide, and the failure is recorded
        return {**by_rules(opts), "fallback": f"{type(e).__name__}: {str(e)[:200]}"}
    a = r.output
    assert isinstance(a, NextAction)
    for o in opts:
        if o["action"] == a.action and (o["table"] or None) == (a.table or None):
            return {**o, "reason": a.reason[:500], "by": f"llm:{getattr(r, 'model', '')}"}
    return {**by_rules(opts), "fallback": f"the model chose {a.action}:{a.table}, which is not an option"}
