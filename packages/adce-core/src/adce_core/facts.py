"""Facts, evidence and trust: the unit of knowledge written through kg-mcp.

Facts are append-only. A correction or decision is a new fact that supersedes the old one.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Trust(StrEnum):
    CONFIRMED = "confirmed"  # a person decided
    DOCUMENTED = "documented"  # read from a source (script, view definition, log)
    VALIDATED = "validated"  # checked against data
    INFERRED = "inferred"  # proposed by reasoning


AGENT_MAX_TRUST = {Trust.DOCUMENTED, Trust.VALIDATED, Trust.INFERRED}  # agents never assign CONFIRMED


class FactStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class Venue(StrEnum):
    """Where a validation ran. Local results are never reported as Dremio results."""

    LOCAL = "local"
    DREMIO = "dremio"
    QLIK_ENGINE = "qlik_engine"


class Evidence(BaseModel):
    kind: str  # script_span | reload_log | qvd_header | qvd_snapshot | fixture | query_result | export | reviewer
    ref: str  # file path, query id, item id
    detail: dict[str, Any] = Field(default_factory=dict)


class Entity(BaseModel):
    """Something knowledge is about. Keys are stable and readable, e.g. "app:sales_overview/table:Orders"."""

    key: str
    type: str  # e.g. BIApp, QlikTable, LogicalSource, QvdFile, Variable
    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class Fact(BaseModel):
    kind: str  # e.g. bi_statement, scope_link, primary_key, report_sql
    subject: str  # entity key the fact is about
    object: str | None = None  # second entity key for relationship facts (e.g. table -> source)
    value: dict[str, Any]
    trust: Trust
    confidence: float = 1.0
    evidence: list[Evidence] = Field(default_factory=list)
    agent: str = ""  # set by BaseAgent.propose; the policy rejects facts without it
    agent_version: str = "0.1.0"
    status: FactStatus = FactStatus.PROPOSED
    supersedes: str | None = None
    valid_from: datetime | None = None  # when it became true in the data
    valid_to: datetime | None = None  # when it stopped being true in the data
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    invalidated_at: datetime | None = None  # set on the row that invalidates an earlier fact (KGPort.invalidate)
    # provenance and isolation (Technical Design §11.1)
    tenant: str = "default"  # the client the knowledge belongs to; a store refuses other tenants' facts
    scope: str | None = None  # the source or schema it is about, when it has one
    run_id: str | None = None  # the run that proposed it (BaseAgent.propose)
    model: str | None = None  # the model that produced it, for AI-assisted facts
    prompt_version: str | None = None  # the prompt version of that model call
    impact: int | None = None  # number of facts that depend on it (the trust gate's input)

    @property
    def id(self) -> str:
        """Stable content id: the same proposal always gets the same id, so re-runs are idempotent. The tenant is
        part of it (outside the default tenant), so two clients' identical facts stay two facts."""
        body: dict[str, Any] = {"k": self.kind, "s": self.subject, "o": self.object, "v": self.value,
                                "st": self.status.value, "sup": self.supersedes}
        if self.tenant != "default":
            body["t"] = self.tenant
        payload = json.dumps(body, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    @property
    def text(self) -> str:
        """One-line readable statement of the fact, used for search and display."""
        vals = ", ".join(f"{k}={v}" for k, v in self.value.items() if not k.startswith("_"))
        target = f" -> {self.object}" if self.object else ""
        return f"{self.subject} {self.kind}{target}: {vals}"
