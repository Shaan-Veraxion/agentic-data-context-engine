"""Reviewer feedback history (ADR-5 learning, kg-mcp tool get_feedback_history).

Every reviewer decision is a fact that supersedes what it decided on, so the fact log already holds each
(proposal, decision, edit, reason). This module pairs them up and ranks them for a task, so agents and models can
learn from what reviewers did before:

- report_sql approved / rejected (with the reason): the SQL of a table was accepted or not;
- translation_override: a reviewer wrote the SQL for a statement the rules and the model could not translate;
- bi_gap_accepted: a reviewer accepted that a statement stays untranslated;
- variable_value, resolves_to, bi_issue_acknowledged: answers and mappings a reviewer gave.

Ranking is by word overlap with the task text for now; ADR-5's embedding retrieval replaces it with the knowledge
engine's vector search.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .facts import Fact, FactStatus

DECISION_KINDS = ("report_sql", "translation_override", "bi_gap_accepted", "variable_value", "resolves_to",
                  "bi_issue_acknowledged")
REVIEWER_INPUT = "hitl-service"  # facts a reviewer wrote (the proposal is the reviewer's own input)


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z_#$][A-Za-z0-9_#$]*\(?|\d+", text or "")}


def _public(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items() if not k.startswith("_")}


def records(history: list[Fact]) -> list[dict[str, Any]]:
    """Reviewer decisions in the full fact history, each with what it decided on."""
    by_id = {f.id: f for f in history}
    out = []
    for f in history:
        if f.status not in (FactStatus.CONFIRMED, FactStatus.REJECTED) or not f.agent.startswith("reviewer:"):
            continue
        old = by_id.get(f.supersedes or "")
        proposal = _public(old.value) if old is not None else {}
        decided = _public(f.value)
        by_reviewer = old is None or old.agent == REVIEWER_INPUT
        edit = decided if by_reviewer else {k: v for k, v in decided.items()
                                            if k != "reason" and proposal.get(k) != v}
        out.append({
            "kind": f.kind, "subject": f.subject, "object": f.object,
            "decision": f.status.value, "reviewer": f.agent.removeprefix("reviewer:"),
            "reason": f.value.get("reason", ""),
            "proposed_by": None if by_reviewer else old.agent if old else None,
            "proposal": None if by_reviewer else proposal, "edit": edit or None,
            "at": f.recorded_at.isoformat(), "fact_id": f.id,
        })
    return out


def rank(recs: list[dict[str, Any]], *, kinds: list[str] | None = None, app_id: str | None = None,
         text: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Filter by kind and application, then most similar to `text` first (newest first without text)."""
    rows = [r for r in recs if (not kinds or r["kind"] in kinds)
            and (not app_id or r["subject"].startswith(f"app:{app_id}/") or r["subject"] == f"app:{app_id}")]
    if not text:
        return sorted(rows, key=lambda r: r["at"], reverse=True)[:limit]
    want = _words(text)
    scored = []
    for r in rows:
        have = _words(json.dumps([r["proposal"], r["edit"], r["reason"]], default=str))
        if want and have and want & have:
            scored.append((len(want & have) / len(want | have), r))
    scored.sort(key=lambda x: (-x[0], x[1]["at"]))
    return [{"similarity": round(s, 3), **r} for s, r in scored[:limit]]


async def feedback_history(kg: Any, *, kinds: list[str] | None = None, app_id: str | None = None,
                           text: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """From any KGPort; McpKG answers through kg-mcp's get_feedback_history tool."""
    remote = getattr(kg, "feedback_history", None)
    if remote is not None:
        return await remote(kinds=kinds, app_id=app_id, text=text, limit=limit)
    history: list[Fact] = []
    for k in kinds or DECISION_KINDS:
        history += await kg.facts(kind=k, current=False)
    return rank(records(history), kinds=kinds, app_id=app_id, text=text, limit=limit)
