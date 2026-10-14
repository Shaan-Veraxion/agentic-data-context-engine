"""Shared helpers for the reverse-SQL agents: rebuild the Translation from artifacts and set up the local venue.

The Translation is a pure function of (BI IR, AI overrides), so agents exchange only artifact keys and every agent
recomputes the same SQL deterministically.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from adce_core.artifacts import ArtifactStore, get_json
from adce_core.bi_ir import BIApp
from pydantic import BaseModel

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
        problems = [describe(c) for c in tv.comparisons if not c.ok] + tv.errors
        return (tv.level.name if tv.level >= 0 else "none"), problems

    return validate
