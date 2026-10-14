"""Validation ladder (agent 26, rule part): V0 parsed, V1 checked, V2 executes, V3 row counts, V4 data.

Row counts (V3): every load step with a reload-log count is compared, and stored tables are compared with the QVD
header. Qlik's "lines fetched" is the number of rows read for the statement; when a preceding LOAD filters, the
rows kept can differ, so both the rows read (source) and the rows produced (chain) are compared, and the basis that
matched is recorded.
Data (V4): when the stored QVD's contents are available, per-field aggregates of the SQL result (computed by the
venue's engine) are compared with the same aggregates of the QVD rows.
Every result names its venue; local results are never presented as Dremio results.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum
from pathlib import PurePosixPath
from typing import Any

from adce_qlik.qvd import QvdHeader
from adce_qlik.reload_log import ReloadLog

from .check import CheckResult, check_table
from .compose import Translation
from .local import LocalVenue


class Level(IntEnum):
    NONE = -1
    V0 = 0
    V1 = 1
    V2 = 2
    V3 = 3
    V4 = 4
    V5 = 5


@dataclass
class Comparison:
    check: str  # rows_step | rows_store | field_aggregate | mapping_keys
    subject: str
    expected: Any
    actual: Any
    ok: bool
    basis: str = ""
    evidence: str = ""


@dataclass
class TableValidation:
    table: str
    level: Level
    venue: str
    sql: str | None
    check: CheckResult
    comparisons: list[Comparison] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"table": self.table, "level": self.level.name if self.level >= 0 else "none", "venue": self.venue,
                "rules": [{"name": r.name, "ok": r.ok, "message": r.message} for r in self.check.rules],
                "comparisons": [c.__dict__ for c in self.comparisons], "errors": self.errors}


def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float, Decimal)):
        f = float(v)
        return int(f) if f.is_integer() else round(f, 9)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return str(v)


def _same(a: Any, b: Any) -> bool:
    a, b = _norm(a), _norm(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def _aggregates(cols: list[str], rows: list[tuple[Any, ...]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i, c in enumerate(cols):
        vals = [_norm(r[i]) for r in rows if r[i] is not None and r[i] != ""]
        numeric = bool(vals) and all(isinstance(v, (int, float)) for v in vals)
        out[c] = {"count": len(vals), "distinct": len(set(vals)),
                  "min": min(vals) if vals else None, "max": max(vals) if vals else None,
                  "sum": round(sum(vals), 6) if numeric else None}
    return out


class Reconciler:
    def __init__(self, t: Translation, venue: LocalVenue, log: ReloadLog | None = None,
                 qvd_headers: dict[str, QvdHeader] | None = None,
                 qvd_contents: dict[str, tuple[list[str], list[tuple[Any, ...]]]] | None = None) -> None:
        self.t = t
        self.venue = venue
        self.log = log
        self.headers = {self._k(k): v for k, v in (qvd_headers or {}).items()}
        self.contents = {self._k(k): v for k, v in (qvd_contents or {}).items()}

    @staticmethod
    def _k(path: str) -> str:
        return PurePosixPath(path.replace("\\", "/")).name.lower()

    def _count(self, cte: str, label: str) -> int:
        return int(self.venue.scalar(f"SELECT COUNT(*) FROM ({self.t.render(cte, comments=False)}) AS q", label))

    def validate(self, table: str) -> TableValidation:
        r = self.t.tables[table]
        chk = check_table(self.t, table)
        tv = TableValidation(table=table, level=Level.NONE, venue=self.venue.venue.value, sql=chk.sql, check=chk)
        parse_ok = not any("does not parse" in u.reason for u in r.untranslated)
        if not parse_ok:
            return tv
        tv.level = Level.V0
        if not chk.ok:
            return tv
        tv.level = Level.V1
        try:
            self._count(r.final_cte or "", f"v2:{table}")
        except Exception as e:  # noqa: BLE001 - an execution failure is the V2 result
            tv.errors.append(f"execution: {str(e).splitlines()[0]}")
            return tv
        tv.level = Level.V2

        self._mapping_keys(tv)
        rows_checks = self._row_counts(tv, r)
        if rows_checks and all(c.ok for c in rows_checks):
            tv.level = Level.V3
            data_checks = self._data(tv, r)
            if data_checks and all(c.ok for c in data_checks):
                tv.level = Level.V4
        return tv

    # ------------------------------------------------------------------ V3
    def _row_counts(self, tv: TableValidation, r: Any) -> list[Comparison]:
        out: list[Comparison] = []
        if self.log is not None:
            for step in r.steps:
                fetches = [f for f in self.log.fetches
                           if f.line_no is not None and step.start_line <= f.line_no <= step.end_line]
                for f in fetches:
                    produced = self._count(step.chain_cte, f"v3:{tv.table}:chain") if step.chain_cte else None
                    read = self._count(step.source_cte, f"v3:{tv.table}:source") if step.source_cte else None
                    basis = "rows_read" if read == f.lines else "rows_produced" if produced == f.lines else ""
                    out.append(Comparison("rows_step", f"{step.action} {step.spans[0]}", f.lines,
                                          {"read": read, "produced": produced}, bool(basis), basis,
                                          f"reload log line {f.line_no}"))
        for target, cte in r.stores:
            h = self.headers.get(self._k(target))
            if h is not None and h.records is not None:
                n = self._count(cte, f"v3:{tv.table}:store")
                out.append(Comparison("rows_store", target, h.records, n, n == h.records, "qvd_header",
                                      f"QVD header of {target}"))
        tv.comparisons += out
        return out

    # ------------------------------------------------------------------ V4
    def _data(self, tv: TableValidation, r: Any) -> list[Comparison]:
        out: list[Comparison] = []
        for target, cte in r.stores:
            content = self.contents.get(self._k(target))
            if content is None:
                continue
            q_cols, q_rows = content
            res = self.venue.run(self.t.render(cte, comments=False), f"v4:{tv.table}")
            if set(res.columns) != set(q_cols):
                out.append(Comparison("fields", target, sorted(q_cols), sorted(res.columns), False, "qvd_contents"))
                continue
            mine = _aggregates(res.columns, res.rows)
            theirs = _aggregates(q_cols, q_rows)
            for c in q_cols:
                for agg in ("count", "distinct", "min", "max", "sum"):
                    e, a = theirs[c][agg], mine[c][agg]
                    if e is None and a is None:
                        continue
                    out.append(Comparison("field_aggregate", f"{target}.{c}.{agg}", e, a, _same(e, a),
                                          "qvd_contents", f"QVD contents of {target}"))
        tv.comparisons += out
        return out

    def _mapping_keys(self, tv: TableValidation) -> None:
        sql = tv.sql or ""
        for name, m in self.t.tables.items():
            if not m.mapping or not m.final_cte or m.final_cte not in sql:
                continue
            cols = self.t.ctes[m.final_cte].columns or []
            if not cols:
                continue
            body = self.t.render(m.final_cte, comments=False)
            total = int(self.venue.scalar(f"SELECT COUNT(*) FROM ({body}) AS q", f"map:{name}"))
            distinct = int(self.venue.scalar(f'SELECT COUNT(DISTINCT "{cols[0]}") FROM ({body}) AS q',
                                             f"map:{name}"))
            tv.comparisons.append(Comparison("mapping_keys", name, total, distinct, total == distinct,
                                             "unique keys keep ApplyMap's first match exact"))
