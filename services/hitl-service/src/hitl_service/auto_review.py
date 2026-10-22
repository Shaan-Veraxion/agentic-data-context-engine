"""The auto-reviewer: what the system decides itself, and what it recommends to a person (Overall Proposal §12).

Three things, all recorded as the auto-reviewer's own decisions (never as a person's), all reversible:

1. Automatic acceptance (`accept_facts`): facts the trust gate routes `auto` (validated by data, confidence ≥ 0.9,
   low impact; adce_core.queries.gate) of the kinds in AUTO_KINDS get an `auto_accepted` fact. A reviewer who
   rejects the fact later reverses it; `stats` reports how often that happens (target: below 5%).
2. Suggestions (`suggest`): every open review item gets a recommended decision with its reason and confidence,
   from rules over the knowledge (and, with a model gateway, refined by the route review_suggest). The reviewer
   applies it with one click or decides otherwise.
3. Automatic decisions (`AUTO_DECIDE`): items that only inform (an issue the system already handled, e.g. a loop
   unrolled from the reload log) are acknowledged by the auto-reviewer; a reviewer can reopen them.

Gated kinds (reconstructed SQL, source mappings, keys, security requirements) are never decided automatically.
"""
from __future__ import annotations

import re
from typing import Any

from adce_core.facts import Evidence, Fact, FactStatus, Trust
from adce_core.queries import gate
from pydantic import BaseModel

AUTO_REVIEWER = "auto_reviewer"
AUTO_KINDS = {"translation_memory", "figures_validated"}  # accepted automatically when the gate says auto
# issue codes that only inform: the system already handled them; acknowledged automatically
AUTO_DECIDE = {"issue:loop_unrolled_from_log", "issue:library_call", "issue:section_access"}


class Suggestion(BaseModel):
    """A recommended decision for a review item (structured output of the route review_suggest)."""

    decision: str | None  # one of the item's options, or None when a person must look
    payload: dict[str, Any] = {}
    reason: str
    confidence: str = "medium"  # low | medium | high
    by: str = "rules"


async def accept_facts(kg: Any, app_id: str) -> int:
    """Auto-accept this application's facts the trust gate routes auto. Returns how many were accepted now."""
    prefix = f"app:{app_id}/"
    done = {f.value.get("fact_id") for f in await kg.facts(kind="auto_accepted")}
    new = 0
    for kind in sorted(AUTO_KINDS):
        for f in await kg.facts(kind=kind):
            ours = f.subject.startswith(prefix) or f.value.get("app_id") == app_id
            if not ours or f.id in done or f.status != FactStatus.PROPOSED:
                continue
            if gate(f, f.impact or 0) != "auto":
                continue
            await kg.propose_fact(Fact(
                kind="auto_accepted", subject=f.subject, trust=Trust.VALIDATED, agent=AUTO_REVIEWER,
                value={"fact_id": f.id, "kind": f.kind, "app_id": app_id,
                       "rule": "validated by data, confidence >= 0.9, low impact (trust gate: auto)"},
                evidence=[Evidence(kind="fact", ref=f.id)]))
            new += 1
    return new


async def stats(kg: Any) -> dict[str, Any]:
    """Automatic acceptances and how many a reviewer reversed (rejected the fact afterwards)."""
    accepted = await kg.facts(kind="auto_accepted")
    history = await kg.facts(current=False)
    rejected = {f.supersedes for f in history if f.status == FactStatus.REJECTED and f.supersedes}
    reversed_ = [a for a in accepted if a.value.get("fact_id") in rejected]
    n = len(accepted)
    return {"auto_accepted": n, "reversed": len(reversed_),
            "reversal_rate": round(len(reversed_) / n, 4) if n else 0.0, "target": 0.05}


def _level_rank(level: str | None) -> int:
    m = re.match(r"V(\d)", level or "")
    return int(m.group(1)) if m else -1


async def suggest(item: Any, kg: Any) -> Suggestion:
    """A rules-based recommendation for one review item."""
    d: dict[str, Any] = item.detail or {}
    k = item.kind
    if k == "approve_sql":
        level, failed, errors = d.get("level"), d.get("failed") or [], d.get("errors") or []
        if any("no local data" in str(e) for e in errors):
            return Suggestion(decision=None, reason="the SQL could not run locally (no sample data for its sources):"
                              " add sample data or validate in Dremio before approving", confidence="high")
        if _level_rank(level) >= 4 and not failed:
            return Suggestion(decision="approve", reason=f"{level}: row counts and data match the QVD and the "
                              "reload log", confidence="high")
        if _level_rank(level) == 3 and not failed:
            return Suggestion(decision="approve", reason="V3: row counts match; no QVD contents to compare data",
                              confidence="medium")
        if failed:
            return Suggestion(decision="reject", payload={"reason": f"{len(failed)} comparisons fail"},
                              reason=f"{len(failed)} comparisons with the evidence fail", confidence="medium")
        return Suggestion(decision=None, reason=f"{level}: not enough evidence to approve", confidence="low")
    if k == "untranslated":
        span = d.get("span")
        for g in await kg.facts(kind="gap_analysis"):
            sql = str(g.value.get("proposed_sql") or "")
            if span and str(g.value.get("span", "")).split(":")[0] == str(span).split(":")[0] and sql \
                    and "<" not in sql:
                return Suggestion(decision="provide_sql", payload={"sql": sql}, confidence="low",
                                  reason="the gap analysis proposes this SQL; check it against the script")
        return Suggestion(decision=None, reason="no rule or model translated it; see the gap analysis",
                          confidence="low")
    if k == "unresolved_variable":
        name = re.sub(r".*unresolved:\s*", "", str(d.get("message", ""))).split(",")[0].strip()
        for f in await kg.facts(kind="variable_value", subject=f"app:{item.app_id}/var:{name}"):
            if f.value.get("value") not in (None, ""):
                return Suggestion(decision="answer", payload={"value": f.value["value"], "name": name},
                                  reason=f"the {f.value.get('origin', 'script')} gives {name} = {f.value['value']}",
                                  confidence="medium")
        return Suggestion(decision=None, reason="only the owner knows this value", confidence="low")
    if k == "map_source":
        leaf = str(item.subject).replace('"', "").split(".")[-1].split(":")[-1].lower()
        for f in await kg.facts(kind="resolves_to"):
            ds = str(f.value.get("dataset", ""))
            if f.trust == Trust.CONFIRMED and ds.replace('"', "").split(".")[-1].lower() == leaf:
                return Suggestion(decision="map", payload={"dataset": ds}, confidence="low",
                                  reason=f"another source with the same table name was mapped to {ds}")
        return Suggestion(decision=None, reason="needs the Dremio catalog (phase PD) or the owner", confidence="low")
    if k == "conflict":
        return Suggestion(decision="keep_confirmed", reason="a person confirmed the other value; keep it unless "
                          "the new evidence is better", confidence="medium")
    if k in AUTO_DECIDE:
        return Suggestion(decision="acknowledge", reason="already handled by the system; for information",
                          confidence="high")
    if k == "scope":
        return Suggestion(decision="confirm", reason="the files were classified for this application",
                          confidence="medium")
    if k == "qvd_linkage":
        return Suggestion(decision="confirm", reason="the QVD header names the document that created it",
                          confidence="medium")
    return Suggestion(decision=None, reason="a person decides", confidence="low")


async def suggest_with_model(item: Any, rules: Suggestion, gateway: Any) -> Suggestion:
    """The route review_suggest refines the rules' suggestion (one of the item's options, or none); on any failure
    the rules' suggestion stands."""
    inputs = {"kind": item.kind, "title": item.title, "options": item.options, "subject": item.subject,
              "detail": {k: v for k, v in (item.detail or {}).items() if k != "suggestion"},
              "rules_suggestion": rules.model_dump()}
    try:
        r = await gateway.call("review_suggest", inputs, Suggestion, task=f"review:{item.app_id}")
    except Exception:  # noqa: BLE001 - no model, no recording, provider down: the rules decide
        return rules
    s = r.output
    assert isinstance(s, Suggestion)
    if s.decision is not None and s.decision not in item.options:
        return rules
    return s.model_copy(update={"by": f"llm:{getattr(r, 'model', '')}"})
