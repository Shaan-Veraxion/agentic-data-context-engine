"""Dremio names for the sources a translation reads.

The translator writes each source as the script names it: database tables as written in the script's SQL
(e.g. tblinvoicehead), files as "__file__"."<path>", QVDs the script does not create as "__qvd__"."<file>", and
inputs a library routine generates (e.g. a calendar) as "__generated__"."<call>". None of these can run in
Dremio until someone says which Dremio dataset each one is: a reviewer's map decision (resolves_to fact),
seeded mappings, or, from phase PD, the Dremio catalog.

apply_mappings rewrites a SQL text with the known mappings and lists what is still unmapped; the SQL then
starts with a comment naming each source that still needs a Dremio dataset.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from .compose import DIALECT, FILE_DB, GEN_DB, QVD_DB


def _norm(path: str) -> str:
    """A table path compared without quotes, brackets or case: "dbo"."TblX" ~ dbo.tblx ~ [dbo].[tblx]."""
    return re.sub(r'["`\[\]]', "", path).strip().lower()


def subject_of(t: exp.Table, ctes: set[str]) -> str | None:
    """The knowledge-service subject of a source table in the SQL, or None for a CTE of the query itself."""
    if not t.db and t.name in ctes:
        return None
    if t.db == FILE_DB:
        return f"source::{t.name}"
    if t.db == QVD_DB:
        return f"qvd:{t.name}"
    if t.db == GEN_DB:
        return f"generated:{t.name}"
    return "sql:" + _norm(".".join(p.name for p in t.parts))


def mapping_key(subject: str) -> str:
    """The key a mapping is stored under: as subject_of gives it. Database sources ("source:<connection>:<path>")
    match by path, whatever the connection."""
    if subject.startswith(("source::", "qvd:", "generated:")):
        return subject
    if subject.startswith("source:"):
        return "sql:" + _norm(subject.split(":", 2)[2])
    return subject


@dataclass
class Resolved:
    sql: str
    mapped: dict[str, str] = field(default_factory=dict)  # subject -> Dremio dataset used
    unmapped: list[str] = field(default_factory=list)  # placeholders (files, QVDs, generated) with no dataset
    unqualified: list[str] = field(default_factory=list)  # database tables kept as written in the script


def _dataset(path: str, alias: Any) -> exp.Table:
    parts = [p.strip().strip('"') for p in re.split(r'\.(?=(?:[^"]*"[^"]*")*[^"]*$)', path) if p.strip()]
    t = exp.Table(this=exp.to_identifier(parts[-1], quoted=True))
    if len(parts) > 1:
        t.set("db", exp.to_identifier(parts[-2], quoted=True))
    if len(parts) > 2:
        t.set("catalog", exp.Dot.build([exp.to_identifier(p, quoted=True) for p in parts[:-2]])
              if len(parts) > 3 else exp.to_identifier(parts[0], quoted=True))
    if alias is not None:
        t.set("alias", alias)
    return t


def apply_mappings(sql: str, mappings: dict[str, str]) -> Resolved:
    """mappings: mapping_key(subject) -> Dremio dataset path ("space.folder.table" or quoted parts)."""
    tree = sqlglot.parse_one(sql, read=DIALECT)
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    out = Resolved(sql)

    def swap(node: exp.Expr) -> exp.Expr:
        if not isinstance(node, exp.Table):
            return node
        subject = subject_of(node, ctes)
        if subject is None:
            return node
        target = mappings.get(subject)
        if target:
            out.mapped[subject] = target
            return _dataset(target, node.args.get("alias"))
        if subject.startswith("sql:"):
            if subject[4:] not in out.unqualified:
                out.unqualified.append(subject[4:])
        elif subject not in out.unmapped:
            out.unmapped.append(subject)
        return node

    body = tree.transform(swap).sql(dialect=DIALECT, pretty=True)
    notes = []
    if out.unmapped:
        notes.append("-- NOT RUNNABLE IN DREMIO YET: these sources need a Dremio dataset (review: map source):")
        notes += [f"--   {s}" for s in out.unmapped]
    if out.unqualified:
        notes.append("-- Database tables as the script names them; map each to its Dremio dataset path:")
        notes += [f"--   {s}" for s in out.unqualified]
    if out.mapped:
        notes.append("-- Sources mapped to Dremio datasets:")
        notes += [f"--   {s} -> {d}" for s, d in out.mapped.items()]
    out.sql = ("\n".join(notes) + "\n" if notes else "") + body
    return out


async def known_mappings(kg: Any, seeded: dict[str, str] | None = None) -> dict[str, str]:
    """Confirmed resolves_to facts (reviewer map decisions) plus seeded mappings (subject -> dataset)."""
    out = {mapping_key(k): v for k, v in (seeded or {}).items()}
    for f in await kg.facts(kind="resolves_to"):
        if f.trust.value == "confirmed" and f.value.get("dataset"):
            out[mapping_key(f.subject)] = f.value["dataset"]
    return out
