"""Review items (Ask / Queue / Gate / Seed) built from recorded knowledge, and decisions turned into facts.

Review is data, never a blocking call: the pipeline keeps going and a decision re-runs only what it affects.
Decisions are written through KGPort, so they are append-only confirmed facts that supersede what they decide on
(adce_core.feedback pairs them up for learning; each one is an outbox event the orchestrator re-runs on):
- Gate  approve_sql          approve / reject       -> decide(report_sql) confirmed / rejected
- Queue untranslated         provide_sql / accept_gap -> translation_override (reviewer SQL) or bi_gap_accepted
- Ask   unresolved_variable  answer                 -> variable_value (seed for the next run)
- Seed  map_source           map                    -> resolves_to (logical source -> Dremio dataset, for phase PD)
- Ask   gap                  answer / accept_gap    -> gap_answer (the owner's answers to the model's questions about a
                                                        part of the script no rule or model could translate; the next
                                                        run's analysis uses them) or bi_gap_accepted
- Queue other issues         acknowledge            -> bi_issue_acknowledged
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from adce_core import queries
from adce_core.facts import Evidence, Fact, FactStatus, Trust
from adce_core.ports import KGPort
from pydantic import BaseModel, Field

from . import auto_review

Mode = Literal["ask", "queue", "gate", "seed", "correct"]

ASK_CODES = {"unresolved_variable": 5, "branch_not_evaluated": 4, "include_missing": 5, "call_unresolved": 4,
             "argument_not_evaluated": 3}
QUEUE_CODES = {"loop_not_unrolled": 3, "construct_tier": 3, "parse_error": 4, "unknown_statement": 3,
               "star_unresolved": 2, "duplicate_table_name": 2, "unknown_target": 3, "unbalanced_control": 4,
               "exit_in_branch": 2, "sql_parse_error": 4}
DECISIONS: dict[str, set[str]] = {
    "approve_sql": {"approve", "reject"},
    "untranslated": {"provide_sql", "accept_gap"},
    "unresolved_variable": {"answer"},
    "map_source": {"map"},
    "gap": {"answer", "accept_gap"},
    "conflict": {"keep_confirmed", "accept_new"},
    "scope": {"confirm", "reject"},
    "qvd_linkage": {"confirm", "reject"},
}
LEVEL_UNCERTAINTY = {"none": 1.0, "V0": 0.9, "V1": 0.7, "V2": 0.5, "V3": 0.25, "V4": 0.1, "V5": 0.05}


class ReviewItem(BaseModel):
    id: str
    app_id: str
    mode: Mode
    kind: str
    subject: str
    fact_id: str | None = None
    title: str
    detail: dict[str, Any] = Field(default_factory=dict)
    options: list[str] = Field(default_factory=list)
    impact: int = 1
    uncertainty: float = 0.5
    status: Literal["open", "decided", "superseded"] = "open"
    decision: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    payload: dict[str, Any] | None = None
    result_fact: str | None = None

    @property
    def score(self) -> float:
        return self.impact * self.uncertainty


class ReviewStore(Protocol):
    def upsert_open(self, item: ReviewItem) -> bool: ...

    def get(self, item_id: str) -> ReviewItem | None: ...

    def list(self, app_id: str | None, status: str | None) -> list[ReviewItem]: ...

    def save_decision(self, item: ReviewItem) -> None: ...


class MemoryReviewStore:
    def __init__(self) -> None:
        self.items: dict[str, ReviewItem] = {}

    def upsert_open(self, item: ReviewItem) -> bool:
        cur = self.items.get(item.id)
        if cur is not None and cur.status != "open":
            return False  # decided items are never reopened by a refresh
        self.items[item.id] = item
        return cur is None

    def get(self, item_id: str) -> ReviewItem | None:
        return self.items.get(item_id)

    def list(self, app_id: str | None, status: str | None) -> list[ReviewItem]:
        return [i for i in self.items.values()
                if (app_id is None or i.app_id == app_id) and (status is None or i.status == status)]

    def save_decision(self, item: ReviewItem) -> None:
        self.items[item.id] = item


class PostgresReviewStore:
    COLS = ("id, app_id, mode, kind, subject, fact_id, title, detail, options, impact, uncertainty, status, decision, "
            "decided_by, decided_at, payload, result_fact")

    def __init__(self, dsn: str) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.pool = ConnectionPool(dsn, min_size=1, max_size=4, open=True, kwargs={"row_factory": dict_row})

    def close(self) -> None:
        self.pool.close()

    def upsert_open(self, item: ReviewItem) -> bool:
        from psycopg.types.json import Jsonb

        with self.pool.connection() as con:
            cur = con.execute(
                f"INSERT INTO review_item ({self.COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, detail = EXCLUDED.detail, "
                "impact = EXCLUDED.impact, uncertainty = EXCLUDED.uncertainty, fact_id = EXCLUDED.fact_id "
                "WHERE review_item.status = 'open' RETURNING (xmax = 0) AS inserted",
                (item.id, item.app_id, item.mode, item.kind, item.subject, item.fact_id, item.title,
                 Jsonb(item.detail), Jsonb(item.options), item.impact, item.uncertainty, item.status, item.decision,
                 item.decided_by, item.decided_at, Jsonb(item.payload) if item.payload is not None else None,
                 item.result_fact))
            row = cur.fetchone()
            return bool(row and row["inserted"])

    def get(self, item_id: str) -> ReviewItem | None:
        with self.pool.connection() as con:
            r = con.execute(f"SELECT {self.COLS} FROM review_item WHERE id = %s", (item_id,)).fetchone()
        return ReviewItem(**r) if r else None

    def list(self, app_id: str | None, status: str | None) -> list[ReviewItem]:
        where, args = ["TRUE"], []
        if app_id:
            where.append("app_id = %s")
            args.append(app_id)
        if status:
            where.append("status = %s")
            args.append(status)
        with self.pool.connection() as con:
            sql = f"SELECT {self.COLS} FROM review_item WHERE {' AND '.join(where)}"
            rows = con.execute(sql, args).fetchall()  # type: ignore[arg-type]
        return [ReviewItem(**r) for r in rows]

    def save_decision(self, item: ReviewItem) -> None:
        from psycopg.types.json import Jsonb

        with self.pool.connection() as con:
            con.execute("UPDATE review_item SET status = %s, decision = %s, decided_by = %s, decided_at = %s, "
                        "payload = %s, result_fact = %s WHERE id = %s",
                        (item.status, item.decision, item.decided_by, item.decided_at,
                         Jsonb(item.payload) if item.payload is not None else None, item.result_fact, item.id))


def _id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class DecisionError(ValueError):
    pass


class ReviewService:
    def __init__(self, store: ReviewStore, kg: KGPort, gateway: Any | None = None) -> None:
        self.store = store
        self.kg = kg
        self.gateway = gateway  # with a model: suggestions refined by the route review_suggest

    # ------------------------------------------------------------------ build the queue from knowledge
    async def refresh(self, app_id: str) -> int:
        app_key = f"app:{app_id}"
        items: list[ReviewItem] = []
        for f in await self.kg.facts(kind="bi_issue", subject=app_key):
            code = f.value.get("code", "")
            mode: Mode = "ask" if code in ASK_CODES else "queue"
            kind = "unresolved_variable" if code == "unresolved_variable" else f"issue:{code}"
            items.append(ReviewItem(
                id=_id(app_id, "issue", f.id), app_id=app_id, mode=mode, kind=kind, subject=app_key, fact_id=f.id,
                title=f"{code.replace('_', ' ')}: {f.value.get('message', '')[:120]}", detail=f.value,
                options=sorted(DECISIONS.get(kind, {"acknowledge"})),
                impact=ASK_CODES.get(code, QUEUE_CODES.get(code, 1)), uncertainty=0.8 if mode == "ask" else 0.5))
        for f in await self._facts_for_app(app_id, "bi_transform"):
            for u in f.value.get("untranslated", []):
                items.append(ReviewItem(
                    id=_id(app_id, "untranslated", f.subject, str(u["statement"])), app_id=app_id, mode="queue",
                    kind="untranslated", subject=f.subject, fact_id=f.id,
                    title=f"{f.subject.split(':')[-1]}: statement {u['statement']} not translated ({u['tier']})",
                    detail=u, options=sorted(DECISIONS["untranslated"]), impact=5, uncertainty=1.0))
        for f in await self._facts_for_app(app_id, "report_sql"):
            if f.status != FactStatus.PROPOSED:
                continue
            if f.value.get("blocked_by"):  # an owner question it depends on is open: the Ask comes first
                continue
            level = f.value.get("level", "none")
            downstream = max(0, len(await queries.lineage(self.kg, f.subject, "downstream")) - 1)
            items.append(ReviewItem(
                id=_id(app_id, "gate", f.subject), app_id=app_id, mode="gate", kind="approve_sql", subject=f.subject,
                fact_id=f.id, title=f"Approve SQL for {f.subject.split(':')[-1]} ({level}, {f.value.get('venue')})",
                detail=f.value | {"tables_downstream": downstream}, options=sorted(DECISIONS["approve_sql"]),
                impact=4 + downstream, uncertainty=LEVEL_UNCERTAINTY.get(level, 0.5)))
        for f in await self._facts_for_app(app_id, "gap_analysis"):
            v = f.value
            items.append(ReviewItem(
                id=_id(app_id, "gap", f.subject, str(v.get("questions"))), app_id=app_id, mode="ask", kind="gap",
                subject=f.subject, fact_id=f.id,
                title=f"{v.get('pattern') or 'untranslated block'} ({v.get('span')}): {v.get('summary', '')[:120]}",
                detail=v, options=sorted(DECISIONS["gap"]), impact=5,
                uncertainty={"low": 1.0, "medium": 0.8, "high": 0.6}.get(v.get("confidence", ""), 0.8)))
        reads = {f.object for f in await self._facts_for_app(app_id, "reads_source") if f.object}
        conflicts = [f for f in await self.kg.facts(kind="conflict")
                     if f.subject.startswith(f"app:{app_id}/") or f.subject == app_key or f.subject in reads]
        for f in conflicts:  # the validator found a contradiction with a confirmed fact (kg-mcp write path)
            v = f.value
            items.append(ReviewItem(
                id=_id(app_id, "conflict", f.id), app_id=app_id, mode="queue", kind="conflict", subject=f.subject,
                fact_id=f.id,
                title=f"conflict on {v.get('kind')} {f.subject.split(':')[-1]}: {v.get('reason', '')[:120]}",
                detail=v, options=sorted(DECISIONS["conflict"]), impact=4, uncertainty=1.0))
        inputs = await self.kg.facts(kind="bi_input", subject=app_key)
        if inputs and not await self.kg.facts(kind="scope_confirmed", subject=app_key):
            files = sorted({(f.value["file"], f.value["class"]) for f in inputs})
            items.append(ReviewItem(
                id=_id(app_id, "scope"), app_id=app_id, mode="gate", kind="scope", subject=app_key,
                title=f"Confirm the {len(files)} files that make up {app_id}",
                detail={"files": [{"file": n, "class": c} for n, c in files]}, options=sorted(DECISIONS["scope"]),
                impact=2, uncertainty=0.3))
        read_qvds = {f.object for f in await self._facts_for_app(app_id, "reads_qvd") if f.object}
        for q in sorted(read_qvds):
            for h in await self.kg.facts(kind="qvd_header", subject=q):
                creator = h.value.get("creator_doc")
                if creator and not await self.kg.facts(kind="qvd_linkage", subject=f"{app_key}/{q}"):
                    items.append(ReviewItem(
                        id=_id(app_id, "qvd", q), app_id=app_id, mode="gate", kind="qvd_linkage",
                        subject=f"{app_key}/{q}", fact_id=h.id,
                        title=f"Confirm {q.removeprefix('qvd:')} comes from {creator}",
                        detail={"qvd": q, "creator_doc": creator, "records": h.value.get("records")},
                        options=sorted(DECISIONS["qvd_linkage"]), impact=3, uncertainty=0.4))
        for src in await self._pending_sources(app_id):
            items.append(ReviewItem(
                id=_id(app_id, "map", src), app_id=app_id, mode="seed", kind="map_source", subject=src,
                title=f"Which Dremio dataset is {src.split(':', 2)[-1] if src.startswith('source:') else src}?",
                options=["map"], impact=3,
                uncertainty=0.6, detail={"source": src, "note": "used when Dremio is connected (phase PD)"}))
        new = 0
        for it in items:  # each item carries a suggested decision (auto_review.suggest; refined by a model)
            old = await asyncio.to_thread(self.store.get, it.id)
            kept = (old.detail or {}).get("suggestion") if old and old.title == it.title else None
            if kept and str(kept.get("by", "")).startswith("llm"):
                it.detail = {**it.detail, "suggestion": kept}
            else:
                s = await auto_review.suggest(it, self.kg)
                if self.gateway is not None and it.mode != "gate":
                    s = await auto_review.suggest_with_model(it, s, self.gateway)
                it.detail = {**it.detail, "suggestion": s.model_dump()}
            new += self.store.upsert_open(it)
        for it in self.store.list(app_id, "open"):  # what only informs is acknowledged by the auto-reviewer
            if it.kind in auto_review.AUTO_DECIDE:
                await self.decide(it.id, "acknowledge", auto_review.AUTO_REVIEWER,
                                  {"auto": True, "reason": "handled by the system; reopen to review"})
        await auto_review.accept_facts(self.kg, app_id)
        return new

    async def reopen(self, item_id: str, reviewer: str) -> ReviewItem:
        """Undo a decision (a person's or the auto-reviewer's): the fact it wrote is invalidated, the item is open
        again."""
        item = await asyncio.to_thread(self.store.get, item_id)
        if item is None:
            raise DecisionError(f"unknown review item {item_id}")
        if item.status != "decided":
            raise DecisionError(f"item is {item.status}, not decided")
        if item.result_fact:
            await self.kg.invalidate(item.result_fact, f"decision reopened by {reviewer}", f"reviewer:{reviewer}")
        item = item.model_copy(update={"status": "open", "decision": None, "decided_by": None, "decided_at": None,
                                       "payload": None, "result_fact": None})
        await asyncio.to_thread(self.store.save_decision, item)
        return item

    async def map_source(self, subject: str, dataset: str, reviewer: str) -> str:
        """A source mapped to its Dremio dataset (Seed), outside a review item (the sources page)."""
        if not dataset.strip():
            raise DecisionError("map needs the Dremio dataset path")
        ev = [Evidence(kind="reviewer", ref=f"sources:{subject}", detail={"reviewer": reviewer})]
        return await self._confirmed(Fact(kind="resolves_to", subject=subject, object=f"dremio:{dataset.strip()}",
                                          trust=Trust.INFERRED, agent="hitl-service",
                                          value={"dataset": dataset.strip()}, evidence=ev), reviewer)

    async def correct(self, fact_id: str, correction: dict[str, Any], reviewer: str, reason: str = "") -> str:
        """Correct mode: a reviewer edits any proposed fact's value; the edit is a confirmed fact superseding it."""
        if not correction:
            raise DecisionError("a correction needs the changed fields")
        return await self.kg.decide(fact_id, FactStatus.CONFIRMED, reviewer,
                                    {**correction, **({"_reason": reason} if reason else {})})

    async def statement_sql(self, app_id: str, table: str, statement: int, span: str, sql: str, reviewer: str,
                            text: str = "", input_columns: list[str] | None = None) -> str:
        """Correct the SQL of any statement (translated or not): a confirmed translation_override for its span,
        used by the next run instead of the rules (and reused for the same statement in other applications)."""
        if not sql.strip().lower().startswith(("select", "with")):
            raise DecisionError("the SQL must be a SELECT reading from the relation named src")
        ev = [Evidence(kind="reviewer", ref=f"{app_id}/{span}", detail={"reviewer": reviewer})]
        return await self._confirmed(Fact(kind="translation_override", subject=f"app:{app_id}/table:{table}",
                                          trust=Trust.INFERRED, agent="hitl-service",
                                          value={"statement": statement, "span": span, "sql": sql.strip(),
                                                 "text": text, "input_columns": input_columns, "correction": True},
                                          evidence=ev), reviewer)

    async def _facts_for_app(self, app_id: str, kind: str) -> list[Fact]:
        prefix = f"app:{app_id}/"
        return [f for f in await self.kg.facts(kind=kind) if f.subject.startswith(prefix)]

    async def _pending_sources(self, app_id: str) -> list[str]:
        # database tables and files the script reads, plus what the generated SQL still names by a placeholder
        # (files, QVDs the script does not create, generated inputs such as a calendar routine)
        sources = {f.object for f in await self._facts_for_app(app_id, "reads_source")
                   if f.object and f.object.startswith("source:")}
        for f in await self._facts_for_app(app_id, "sql_candidate"):
            sources |= set(f.value.get("unmapped", []))
        sources = sorted(sources)
        out = []
        for s in sources:
            done = [f for f in await self.kg.facts(kind="resolves_to", subject=s) if f.trust == Trust.CONFIRMED]
            if not done:
                out.append(s)
        return out

    def queue(self, app_id: str | None, status: str | None = "open") -> list[ReviewItem]:
        return sorted(self.store.list(app_id, status), key=lambda i: (-i.score, i.title))

    # ------------------------------------------------------------------ decisions
    async def decide(self, item_id: str, decision: str, reviewer: str,
                     payload: dict[str, Any] | None = None) -> ReviewItem:
        item = await asyncio.to_thread(self.store.get, item_id)
        if item is None:
            raise DecisionError(f"unknown review item {item_id}")
        if item.status != "open":
            raise DecisionError(f"item already {item.status}")
        allowed = set(item.options)
        if decision not in allowed:
            raise DecisionError(f"decision {decision!r} not allowed; choose one of {sorted(allowed)}")
        payload = payload or {}
        result_fact = await self._apply(item, decision, reviewer, payload)
        item = item.model_copy(update={"status": "decided", "decision": decision, "decided_by": reviewer,
                                       "decided_at": datetime.now(UTC), "payload": payload,
                                       "result_fact": result_fact})
        await asyncio.to_thread(self.store.save_decision, item)
        return item

    async def _apply(self, item: ReviewItem, decision: str, reviewer: str, payload: dict[str, Any]) -> str | None:
        ev = [Evidence(kind="reviewer", ref=f"review_item:{item.id}", detail={"reviewer": reviewer})]
        if item.kind in ("scope", "qvd_linkage"):
            kind = "scope_confirmed" if item.kind == "scope" else "qvd_linkage"
            return await self._confirmed(Fact(kind=kind, subject=item.subject, trust=Trust.INFERRED,
                                              agent="hitl-service", value={**item.detail, "decision": decision,
                                                                           "reason": payload.get("reason", "")},
                                              evidence=ev), reviewer)
        if item.kind == "conflict":
            old_id, new_id = item.detail["facts"]
            if decision == "keep_confirmed":  # the proposal is rejected; the confirmed fact stays
                return await self.kg.decide(new_id, FactStatus.REJECTED, reviewer,
                                            {"reason": payload.get("reason") or "conflicts with a confirmed fact"})
            await self.kg.invalidate(old_id, payload.get("reason") or "replaced after a conflict review",
                                     f"reviewer:{reviewer}")
            return await self.kg.decide(new_id, FactStatus.CONFIRMED, reviewer, None)
        if item.kind == "approve_sql":
            assert item.fact_id
            status = FactStatus.CONFIRMED if decision == "approve" else FactStatus.REJECTED
            return await self.kg.decide(item.fact_id, status, reviewer,
                                        {"reason": payload["reason"]} if payload.get("reason") else None)
        if item.kind == "untranslated" and decision == "provide_sql":
            sql = (payload.get("sql") or "").strip()
            if not sql.lower().startswith("select"):
                raise DecisionError("provide_sql needs a SELECT reading from the relation named src")
            return await self._confirmed(Fact(
                kind="translation_override", subject=item.subject, trust=Trust.INFERRED, agent="hitl-service",
                value={"statement": item.detail.get("statement"), "span": item.detail.get("span"), "sql": sql,
                       # the statement itself, so the same statement in other applications can reuse this SQL
                       "text": item.detail.get("text"), "input_columns": item.detail.get("input_columns")},
                evidence=ev), reviewer)
        if item.kind == "untranslated":
            return await self._confirmed(Fact(kind="bi_gap_accepted", subject=item.subject, trust=Trust.INFERRED,
                                              agent="hitl-service", value=dict(item.detail) | payload, evidence=ev),
                                         reviewer)
        if item.kind == "unresolved_variable":
            name = payload.get("name") or _variable_name(item.detail.get("message", ""))
            if not name or "value" not in payload:
                raise DecisionError("answer needs the variable value (and its name if more than one is unresolved)")
            return await self._confirmed(Fact(kind="variable_value", subject=f"app:{item.app_id}/var:{name}",
                                              trust=Trust.INFERRED, agent="hitl-service",
                                              value={"value": str(payload["value"]), "origin": "reviewer"},
                                              evidence=ev), reviewer)
        if item.kind == "gap" and decision == "answer":
            answers = payload.get("answers")
            if not answers:
                raise DecisionError("answer needs `answers`: the answers to the questions (a list or a mapping)")
            return await self._confirmed(Fact(kind="gap_answer", subject=item.subject, trust=Trust.INFERRED,
                                              agent="hitl-service",
                                              value={"questions": item.detail.get("questions", []),
                                                     "answers": answers}, evidence=ev), reviewer)
        if item.kind == "gap":
            return await self._confirmed(Fact(kind="bi_gap_accepted", subject=item.subject, trust=Trust.INFERRED,
                                              agent="hitl-service", value=dict(item.detail) | payload, evidence=ev),
                                         reviewer)
        if item.kind == "map_source":
            dataset = (payload.get("dataset") or "").strip()
            if not dataset:
                raise DecisionError("map needs the Dremio dataset path")
            return await self._confirmed(Fact(kind="resolves_to", subject=item.subject, object=f"dremio:{dataset}",
                                              trust=Trust.INFERRED, agent="hitl-service",
                                              value={"dataset": dataset}, evidence=ev), reviewer)
        return await self._confirmed(Fact(kind="bi_issue_acknowledged", subject=item.subject, trust=Trust.INFERRED,
                                          agent="hitl-service", value={"item": item.id, **payload}, evidence=ev),
                                     reviewer)

    async def _confirmed(self, fact: Fact, reviewer: str) -> str:
        fid = await self.kg.propose_fact(fact)
        if reviewer == auto_review.AUTO_REVIEWER:  # only people confirm: the auto-reviewer's decision stays proposed
            return fid
        return await self.kg.decide(fid, FactStatus.CONFIRMED, reviewer)


def _variable_name(message: str) -> str | None:
    rest = message.split("unresolved:", 1)[-1].strip()
    names = [n.strip() for n in rest.split(",") if n.strip() and not n.strip().startswith("=")]
    return names[0] if len(names) == 1 else None
