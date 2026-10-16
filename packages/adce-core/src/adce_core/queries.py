"""Knowledge queries and the write path's checks (ADR-2), over any KGPort.

The knowledge service's tools beyond plain reads and writes are built here once, from `facts()`, so the in-memory
store, the Postgres knowledge service and kg-mcp answer them the same way:

- get_neighbors, get_source_context, get_lineage, find_join_paths: reads over the facts' subjects and objects;
- context_pack: an entity's knowledge ranked by trust and trimmed to a token budget (what an agent reads first);
- validate: the validator of the write path: a proposal that contradicts a confirmed fact is a conflict
  (kept, never overwriting, and flagged for review);
- gate: the trust gate: how much depends on a fact (impact) and where its review goes (auto / queue / gate).
"""
from __future__ import annotations

import json
from collections import deque
from typing import Any

from .facts import Evidence, Fact, Trust

TRUST_RANK = {Trust.CONFIRMED: 0, Trust.VALIDATED: 1, Trust.DOCUMENTED: 2, Trust.INFERRED: 3}

# kinds with one value per subject: a second, different value contradicts the first (the key that must agree)
FUNCTIONAL = {"resolves_to": "dataset", "variable_value": "value", "primary_key": "columns",
              "translation_override": "sql"}
# kinds whose facts a person must approve before they are trusted (the gate), whatever their evidence
GATED = {"report_sql", "resolves_to", "primary_key", "rls_requirement"}


def _tokens(x: Any) -> int:
    return max(1, len(json.dumps(x, default=str)) // 4)


async def neighbors(kg: Any, key: str) -> list[Fact]:
    """Current facts about the entity, or pointing at it."""
    return [f for f in await kg.facts() if f.subject == key or f.object == key]


async def source_context(kg: Any, key: str) -> dict[str, Any]:
    """An entity with its own facts and those of everything under it (keys starting with key + '/')."""
    ent = await kg.entity(key)
    under = [f for f in await kg.facts() if f.subject == key or f.subject.startswith(key + "/")]
    return {"entity": ent.model_dump() if ent else None, "facts": [f.model_dump(mode="json") for f in under]}


async def lineage(kg: Any, key: str, direction: str = "upstream", depth: int = 5) -> list[dict[str, Any]]:
    """Lineage facts walked from a table: upstream (what it is derived from) or downstream (what derives from it).
    Each step: the table, its inputs and outputs (OpenLineage names)."""
    facts = {f.subject: f for f in await kg.facts(kind="lineage")}
    by_output: dict[str, list[str]] = {}
    for subject, f in facts.items():
        for i in f.value.get("inputs", []):
            by_output.setdefault(i.get("name", ""), []).append(subject)
    out, seen, todo = [], {key}, deque([(key, 0)])
    while todo:
        k, d = todo.popleft()
        f = facts.get(k)
        if f is None or d > depth:
            continue
        out.append({"table": k, "depth": d, "inputs": f.value.get("inputs", []), "outputs": f.value.get("outputs", [])})
        app = k.split("/table:")[0]
        if direction == "upstream":
            nxt = [f"{app}/table:{i['name']}" for i in f.value.get("inputs", []) if i.get("namespace", "").startswith(
                "qlik://")]
        else:
            nxt = by_output.get(k.split("/table:")[-1], [])
        for n in nxt:
            if n not in seen:
                seen.add(n)
                todo.append((n, d + 1))
    return out


async def join_paths(kg: Any, a: str, b: str, max_hops: int = 4) -> list[list[dict[str, Any]]]:
    """Shortest association paths between two tables (associates facts: Qlik's same-named fields), each hop
    with its keys; trusted links first when there are several shortest paths."""
    edges: dict[str, list[tuple[str, Fact]]] = {}
    for f in await kg.facts(kind="associates"):
        if f.object:
            edges.setdefault(f.subject, []).append((f.object, f))
            edges.setdefault(f.object, []).append((f.subject, f))
    paths: list[list[dict[str, Any]]] = []
    todo: deque[tuple[str, list[dict[str, Any]]]] = deque([(a, [])])
    best = None
    while todo:
        node, path = todo.popleft()
        if best is not None and len(path) > best:
            break
        if node == b and path:
            best = len(path)
            paths.append(path)
            continue
        if len(path) >= max_hops:
            continue
        for nxt, f in sorted(edges.get(node, []), key=lambda e: TRUST_RANK[e[1].trust]):
            if any(h["to"] == nxt for h in path) or nxt == a:
                continue
            todo.append((nxt, [*path, {"from": node, "to": nxt, "keys": f.value.get("keys", []),
                                       "trust": f.trust.value}]))
    return paths


async def context_pack(kg: Any, key: str, token_budget: int = 4000) -> dict[str, Any]:
    """What an agent reads first about an entity (ADR-2 context assembler): the entity, then its facts and its
    neighbours' facts, highest trust first and newest first within a trust level, trimmed to the token budget.
    Deterministic for the same knowledge, so prompts built from it cache."""
    ent = await kg.entity(key)
    pack: dict[str, Any] = {"entity": ent.model_dump() if ent else None, "facts": [], "truncated": False}
    used = _tokens(pack)
    candidates = [f for f in await kg.facts() if f.subject == key or f.object == key
                  or f.subject.startswith(key + "/")]
    candidates.sort(key=lambda f: (TRUST_RANK[f.trust], -f.recorded_at.timestamp(), f.id))
    for f in candidates:
        item = {"id": f.id, "kind": f.kind, "subject": f.subject, "object": f.object, "trust": f.trust.value,
                "value": f.value}
        t = _tokens(item)
        if used + t > token_budget:
            pack["truncated"] = True
            break
        pack["facts"].append(item)
        used += t
    pack["tokens"] = used
    return pack


def conflict_with(fact: Fact, current: list[Fact]) -> Fact | None:
    """The confirmed fact this proposal contradicts (same functional kind and subject, a different value)."""
    field = FUNCTIONAL.get(fact.kind)
    if field is None:
        return None
    for f in current:
        if (f.kind == fact.kind and f.subject == fact.subject and f.trust == Trust.CONFIRMED and f.id != fact.id
                and f.value.get(field) != fact.value.get(field)):
            return f
    return None


def conflict_fact(new: Fact, old: Fact, by: str = "kg-mcp:validator") -> Fact:
    field = FUNCTIONAL.get(new.kind, "value")
    return Fact(kind="conflict", subject=new.subject, value={
        "facts": [old.id, new.id], "kind": new.kind,
        "reason": f"{new.agent or 'a proposal'} says {field}={new.value.get(field)!r}, a reviewer confirmed "
                  f"{old.value.get(field)!r}"}, trust=Trust.INFERRED, agent=by, tenant=new.tenant,
        evidence=[Evidence(kind="fact", ref=old.id), Evidence(kind="fact", ref=new.id)])


def impact_of(fact: Fact, current: list[Fact]) -> int:
    """How much depends on a fact: the current facts about the same entity or pointing at it (other than itself)."""
    return sum(1 for f in current if f.id != fact.id and (f.subject == fact.subject or f.object == fact.subject))


def gate(fact: Fact, impact: int) -> str:
    """Where a proposal's review goes: gate (a person approves it before it is trusted), auto (validated,
    confident and low-impact: accepted automatically, reversible), queue (batch review)."""
    if fact.kind in GATED:
        return "gate"
    if fact.trust == Trust.VALIDATED and fact.confidence >= 0.9 and impact <= 3:
        return "auto"
    return "queue"
