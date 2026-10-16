"""The fact log: append-only record of all knowledge, in Postgres (schema: migrations/alembic).

Every write commits the fact (or entity) and an outbox event in one transaction. The outbox is what the Graphiti
projection and other consumers read, so the graph can always be rebuilt from this log.

A log is bound to one tenant (migration 0005): every read and write is filtered by it, and a fact of another tenant
is refused. There is no way to read across tenants through this class.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from adce_core.facts import Entity, Evidence, Fact, FactStatus, Trust
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

_FACT_COLS = ("fact_id, kind, subject, object, value, trust, confidence, evidence, agent, agent_version, status, "
              "supersedes, valid_from, recorded_at, tenant_id, scope_id, run_id, model, prompt_version, impact, "
              "valid_to, invalidated_at")
_PLACEHOLDERS = "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
assert _PLACEHOLDERS.count("%s") == len(_FACT_COLS.split(","))


def _row_to_fact(r: dict[str, Any]) -> Fact:
    return Fact(kind=r["kind"], subject=r["subject"], object=r["object"], value=r["value"], trust=Trust(r["trust"]),
                confidence=r["confidence"], evidence=[Evidence(**e) for e in r["evidence"]], agent=r["agent"],
                agent_version=r["agent_version"], status=FactStatus(r["status"]), supersedes=r["supersedes"],
                valid_from=r["valid_from"], recorded_at=r["recorded_at"], tenant=r["tenant_id"],
                scope=r["scope_id"], run_id=r["run_id"], model=r["model"], prompt_version=r["prompt_version"],
                impact=r["impact"], valid_to=r["valid_to"], invalidated_at=r["invalidated_at"])


class PostgresFactLog:
    def __init__(self, dsn: str, tenant: str, *, min_size: int = 1, max_size: int = 5) -> None:
        if not tenant:
            raise ValueError("the fact log needs a tenant binding (ADCE_TENANT); it does not start without one")
        self.tenant = tenant
        self.pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=True,
                                   kwargs={"row_factory": dict_row})

    def close(self) -> None:
        self.pool.close()

    # ------------------------------------------------------------------ writes (each with its outbox event)
    def upsert_entity(self, e: Entity) -> bool:
        with self.pool.connection() as con, con.transaction():
            cur = con.execute(
                "INSERT INTO entity (tenant_id, key, type, name, attributes) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, key) DO NOTHING RETURNING key",
                (self.tenant, e.key, e.type, e.name, Jsonb(e.attributes)))
            created = cur.fetchone() is not None
            if created:
                self._outbox(con, "entity.registered", {"entity": e.model_dump(mode="json")})
            return created

    def append(self, fact: Fact, topic: str, extra: dict[str, Any] | None = None) -> bool:
        """Append a fact. Returns False if the same fact (same id) is already in the log."""
        if fact.tenant == "default" and self.tenant != "default":
            fact = fact.model_copy(update={"tenant": self.tenant})
        if fact.tenant != self.tenant:
            raise ValueError(f"a fact of tenant {fact.tenant!r} in the log of tenant {self.tenant!r}")
        with self.pool.connection() as con, con.transaction():
            cur = con.execute(
                f"INSERT INTO fact_log ({_FACT_COLS}) VALUES ({_PLACEHOLDERS}) "
                "ON CONFLICT (fact_id) DO NOTHING RETURNING seq",
                (fact.id, fact.kind, fact.subject, fact.object, Jsonb(fact.value), fact.trust.value, fact.confidence,
                 Jsonb([ev.model_dump(mode="json") for ev in fact.evidence]), fact.agent, fact.agent_version,
                 fact.status.value, fact.supersedes, fact.valid_from, fact.recorded_at, fact.tenant, fact.scope,
                 fact.run_id, fact.model, fact.prompt_version, fact.impact, fact.valid_to, fact.invalidated_at))
            if cur.fetchone() is None:
                return False
            self._outbox(con, topic, {"fact": json.loads(fact.model_dump_json()), "fact_id": fact.id, **(extra or {})})
            return True

    def _outbox(self, con: Any, topic: str, payload: dict[str, Any]) -> None:
        from adce_core.events import outbox_problems

        bad = outbox_problems(topic, payload)  # the event contract (adce_core.events)
        if bad:
            raise ValueError("; ".join(bad))
        con.execute("INSERT INTO outbox (tenant_id, topic, payload) VALUES (%s, %s, %s)",
                    (self.tenant, topic, Jsonb(payload)))
        con.execute("SELECT pg_notify('adce_outbox', %s)", (topic,))

    # ------------------------------------------------------------------ reads
    def get(self, fact_id: str) -> Fact | None:
        with self.pool.connection() as con:
            r = con.execute(f"SELECT {_FACT_COLS} FROM fact_log WHERE fact_id = %s AND tenant_id = %s",
                            (fact_id, self.tenant)).fetchone()
        return _row_to_fact(r) if r else None

    def get_many(self, fact_ids: list[str]) -> list[Fact]:
        if not fact_ids:
            return []
        with self.pool.connection() as con:
            rows = con.execute(f"SELECT {_FACT_COLS} FROM fact_log WHERE fact_id = ANY(%s) AND tenant_id = %s",
                               (fact_ids, self.tenant)).fetchall()
        order = {f: i for i, f in enumerate(fact_ids)}
        return sorted((_row_to_fact(r) for r in rows), key=lambda f: order[f.id])

    def entity(self, key: str) -> Entity | None:
        with self.pool.connection() as con:
            r = con.execute("SELECT key, type, name, attributes FROM entity WHERE key = %s AND tenant_id = %s",
                            (key, self.tenant)).fetchone()
        return Entity(**r) if r else None

    def query(self, *, kind: str | None, subject: str | None, current: bool,
              as_of: datetime | None = None) -> list[Fact]:
        """Facts in log order. current=True hides superseded facts. as_of reads the log as it was at that time."""
        where: list[str] = ["f.tenant_id = %s"]
        args: list[Any] = [self.tenant]
        if kind:
            where.append("f.kind = %s")
            args.append(kind)
        if subject:
            where.append("f.subject = %s")
            args.append(subject)
        if as_of:
            where.append("f.recorded_at <= %s")
            args.append(as_of)
        if current:
            sup = "s.supersedes = f.fact_id AND s.tenant_id = f.tenant_id" + (" AND s.recorded_at <= %s" if as_of
                                                                                else "")
            where.append(f"NOT EXISTS (SELECT 1 FROM fact_log s WHERE {sup})")
            where.append("f.status <> 'superseded'")  # invalidation rows (KGPort.invalidate) are not knowledge
            if as_of:
                args.append(as_of)
        sql = f"SELECT {', '.join('f.' + c.strip() for c in _FACT_COLS.split(','))} FROM fact_log f " \
              f"WHERE {' AND '.join(where)} ORDER BY f.seq"
        with self.pool.connection() as con:
            return [_row_to_fact(r) for r in con.execute(sql, args).fetchall()]  # type: ignore[arg-type]

    # ------------------------------------------------------------------ outbox
    def pending(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.pool.connection() as con:
            return con.execute("SELECT seq, topic, payload FROM outbox WHERE projected_at IS NULL AND tenant_id = %s "
                               "ORDER BY seq LIMIT %s", (self.tenant, limit)).fetchall()

    def mark_projected(self, seqs: list[int]) -> None:
        if seqs:
            with self.pool.connection() as con:
                con.execute("UPDATE outbox SET projected_at = now() WHERE seq = ANY(%s)", (seqs,))

    def events_after(self, after: int, limit: int = 500) -> list[dict[str, Any]]:
        """Events after a consumer's position, in order (consumers other than the graph projection)."""
        with self.pool.connection() as con:
            return con.execute("SELECT seq, topic, payload FROM outbox WHERE seq > %s AND tenant_id = %s "
                               "ORDER BY seq LIMIT %s", (after, self.tenant, limit)).fetchall()

    def all_events(self) -> list[dict[str, Any]]:
        """Every event in order, for rebuilding the projection from scratch."""
        with self.pool.connection() as con:
            return con.execute("SELECT seq, topic, payload FROM outbox WHERE tenant_id = %s ORDER BY seq",
                               (self.tenant,)).fetchall()
