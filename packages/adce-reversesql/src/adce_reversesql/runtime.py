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
