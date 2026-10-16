"""Shared helpers for the reverse-SQL agents: rebuild the Translation from artifacts and set up the local venue.

The Translation is a pure function of (BI IR, AI overrides), so agents exchange only artifact keys and every agent
recomputes the same SQL deterministically.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from adce_core.artifacts import ArtifactStore, content_key, get_json, put_json
from adce_core.bi_ir import BIApp
from pydantic import BaseModel, field_validator

from .compose import Translation, compose
from .local import LocalVenue


class StatementTranslation(BaseModel):
    """Structured output of the model-gateway route translate_statement."""

    sql: str  # a SELECT reading from the relation named src
    translatable: bool
    notes: str = ""


class MismatchFix(BaseModel):
    """Structured output of the model-gateway route diagnose_mismatch."""

    statement: int | None  # statement whose SQL to replace, or None when no single statement is at fault
    sql: str
    cause: str
    explanation: str = ""


class SqlRepair(BaseModel):
    """Structured output of the model-gateway route repair_sql (agent 25): one statement's SQL replaced so the
    table passes the checks it failed."""

    statement: int | None  # statement whose SQL to replace, or None when no single statement can fix it
    sql: str
    notes: str = ""


_FIELD_TAGS = r"</?(summary|pattern|dremio_approach|questions|proposed_sql|confidence)>"


class GapAnalysis(BaseModel):
    """Structured output of the model-gateway route explain_gap: what a part of the script that stays
    untranslated does, how to reproduce it in Dremio, and what a person must answer to finish it."""

    summary: str
    pattern: str = ""
    dremio_approach: str
    questions: list[str] = []
    proposed_sql: str = ""
    confidence: str = "medium"  # low | medium | high

    @field_validator("summary", "pattern", "dremio_approach", "proposed_sql", mode="before")
    @classmethod
    def _clean(cls, v: Any) -> Any:  # some models wrap fields in stray whitespace or tags
        return re.sub(_FIELD_TAGS, "", v).strip() if isinstance(v, str) else v

    @field_validator("questions", mode="before")
    @classmethod
    def _clean_questions(cls, v: Any) -> Any:
        return [re.sub(_FIELD_TAGS, "", str(q)).strip() for q in v if str(q).strip()] if isinstance(v, list) else v

    @field_validator("confidence", mode="before")
    @classmethod
    def _level(cls, v: Any) -> str:
        m = re.search(r"low|medium|high", str(v).lower())
        return m.group(0) if m else "medium"


@dataclass
class Loaded:
    app: BIApp
    overrides: dict[int, str]
    translation: Translation


def load_translation(store: ArtifactStore, bi_ir_key: str, translation_key: str | None = None) -> Loaded:
    ir = get_json(store, bi_ir_key)
    app = BIApp.model_validate(ir["app"])
    overrides: dict[int, str] = {}
    if translation_key:
        overrides = {int(k): v for k, v in get_json(store, translation_key).get("overrides", {}).items()}
    return Loaded(app=app, overrides=overrides, translation=compose(app, overrides))


def local_venue_from_files(store: ArtifactStore, files: list[dict[str, Any]], workdir: str | None = None
                           ) -> tuple[LocalVenue, dict[str, Path]]:
    """Load uploaded source extracts (class source_extract) and QVD files into a DuckDB local venue.

    Returns the venue and the local paths of the QVD files by file name (for headers and contents)."""
    venue = LocalVenue()
    root = Path(workdir or tempfile.mkdtemp(prefix="adce-local-"))
    src_root = root / "sources"
    qvds: dict[str, Path] = {}
    for f in files:
        name = f["name"].replace("\\", "/")
        if f["class"] == "source_extract":
            rel = name[name.lower().index("sources/") + len("sources/"):]
            p = src_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(store.get(f["key"]))
        elif f["class"] == "qvd":
            p = root / "qvd" / PurePosixPath(name).name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(store.get(f["key"]))
            qvds[PurePosixPath(name).name.lower()] = p
    if src_root.exists():
        venue.add_source_dir(src_root)
    return venue, qvds


@dataclass
class LocalEvidence:
    """What the uploads offer for local validation: the venue (source extracts, QVDs), the reload log, and the
    headers and contents of the QVDs this app stores."""

    venue: LocalVenue
    log: Any
    log_key: str | None
    headers: dict[str, Any]
    contents: dict[str, Any]
    unreadable: list[str]


def local_evidence(store: ArtifactStore, files: list[dict[str, Any]], app: BIApp) -> LocalEvidence:
    """Input QVDs are registered under their file name; QVDs the app stores also under the store target, so a
    table that reads a stored QVD back runs locally. Header-only QVDs still give row counts (V3)."""
    from adce_qlik import parse_reload_log, read_qvd_header

    from .local import read_qvd

    venue, qvd_paths = local_venue_from_files(store, files)
    logs = [f for f in files if f["class"] == "reload_log"]
    log = parse_reload_log(store.get(logs[-1]["key"]).decode("utf-8", errors="replace")) if logs else None
    targets = {PurePosixPath(s.target.replace("\\", "/")).name.lower(): s.target for s in app.stores}
    headers: dict[str, Any] = {}
    contents: dict[str, Any] = {}
    unreadable: list[str] = []
    for fname, path in qvd_paths.items():
        target = targets.get(fname)
        try:
            data = read_qvd(path)
        except Exception:  # noqa: BLE001 - header-only or damaged QVD: row counts still checkable
            data = None
            unreadable.append(fname)
        if target is None:
            if data is not None:
                venue.add_qvd(f"lib://local/{fname}", path)  # an input QVD read by this app
            continue
        headers[target] = read_qvd_header(path)
        if data is not None:
            contents[target] = data
            venue.add_qvd(target, path)
    return LocalEvidence(venue, log, logs[-1]["key"] if logs else None, headers, contents, unreadable)


def validator_from_files(store: ArtifactStore, files: list[dict[str, Any]], app: BIApp) -> Any:
    """A validator for the LLM pathway (ai_loop.Validator) built from uploaded files. With no data it still runs
    the SQL (V2) and reports no comparisons."""
    ev = local_evidence(store, files, app)
    return make_validator(ev.venue, ev.log, ev.headers, ev.contents)


def make_validator(venue: LocalVenue, log: Any, headers: dict[str, Any], contents: dict[str, Any]) -> Any:
    """ai_loop.Validator over a local venue: the level reached and every failed comparison, worded the same way
    everywhere (the wording is part of the retry prompt, so recordings replay only if it is stable)."""
    from .local import NO_DATA
    from .reconcile import Reconciler

    def describe(c: Any) -> str:
        agg = c.subject.rsplit(".", 1)[-1]
        if agg == "shapes":
            return (f"{c.subject}: value formats differ (a = a varying word, 9 = a number, other characters and "
                    f"words common to all values shown as they are): "
                    f"expected {c.expected}, got {c.actual}")
        if agg == "values":
            return f"{c.subject}: the set of values differs (fingerprints {c.expected} vs {c.actual})"
        if agg in ("min", "max") and (isinstance(c.expected, str) or isinstance(c.actual, str)):
            return f"{c.subject}: differs (text values are never shown; see the formats)"
        return f"{c.check} {c.subject}: expected {c.expected}, got {c.actual}"

    def validate(t: Translation, table: str) -> tuple[str, list[str]]:
        tv = Reconciler(t, venue, log, headers, contents).validate(table)
        # sources with no local data stop the check, they do not contradict it: the model is not asked to fix them
        problems = [describe(c) for c in tv.comparisons if not c.ok] + [e for e in tv.errors if NO_DATA not in e]
        return (tv.level.name if tv.level >= 0 else "none"), problems

    return validate


def translation_doc(store: ArtifactStore, translation_key: str | None) -> dict[str, Any]:
    """The translation artifact: overrides (statement -> SQL), ai_notes, ai_outcomes, gaps."""
    return get_json(store, translation_key) if translation_key else {}


def save_translation(store: ArtifactStore, app_id: str, bi_ir_key: str, overrides: dict[int, str],
                     ai_notes: dict[int, str], ai_outcomes: list[dict[str, Any]],
                     gaps: list[dict[str, Any]] | None = None) -> str:
    """Write a translation artifact; its key depends on the BI IR and the overrides, so the same SQL has one key.
    Agents 28 (translation), 25 (repair) and 26 (diagnosis) each write the next version."""
    return put_json(store, content_key(f"apps/{app_id}/translation",
                                       repr(sorted(overrides.items())).encode() + bi_ir_key.encode(), ".json"),
                    {"bi_ir_key": bi_ir_key, "overrides": {str(k): v for k, v in overrides.items()},
                     "ai_notes": {str(k): v for k, v in ai_notes.items()}, "ai_outcomes": ai_outcomes,
                     "gaps": gaps or []})


def outcome_record(o: Any) -> dict[str, Any]:
    """An ai_loop.Outcome as the translation artifact lists it."""
    return {"statement": o.statement, "table": o.table, "status": o.status, "attempts": len(o.attempts),
            "model_calls": o.model_calls, "tool_calls": o.tool_calls, "notes": o.notes}
