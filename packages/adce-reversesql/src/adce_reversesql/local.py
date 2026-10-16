"""The local venue: run the Dremio SQL on DuckDB, with sources mapped to local data (until Dremio, phase PD).

- Logical SQL sources ("sales"."erp"."orders_v2") map to fixture files under <sources>/sales/erp/orders_v2.csv|parquet
- QVD inputs ("__qvd__"."lib://QVD/Orders.qvd") map to QVD files, read with PyQvd
- File inputs ("__file__"."lib://Data/x.csv") map to local files of the same name under <sources>/files/
- Generated inputs ("__generated__"."Qvc.Calendar(start, end)") are generated in DuckDB
- Dremio TO_DATE / TO_TIMESTAMP / TO_CHAR are rewritten to DuckDB's strptime / strftime
The SQL is rewritten on its syntax tree (sqlglot) and transpiled to DuckDB; results are labelled venue 'local'.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from typing import Any

import sqlglot
from adce_core.data import DuckDBSource
from adce_core.facts import Venue
from adce_core.ports import QueryResult
from sqlglot import exp
from sqlglot.expressions.core import Expr as SqlExpr

from .compose import DIALECT, EXTENSION_RX, FILE_DB, GEN_DB, QVD_DB
from .expressions import ExprContext, Translator, Untranslatable, _split_top
from .formats import to_duckdb

NO_DATA = "no local data for: "


class MissingData(LookupError):
    """The SQL reads sources with no local data: it cannot run here, which says nothing about the translation."""

    def __init__(self, sources: list[str]) -> None:
        self.sources = sorted(set(sources))
        super().__init__(f"{NO_DATA}{', '.join(self.sources)}")


CACHE_SIZE, CACHE_ROWS = 4000, 5000
_CACHE: OrderedDict[tuple[str, str, int], QueryResult] = OrderedDict()


def _local(name: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()


def logical_path(t: exp.Table) -> str:
    return ".".join(p.name for p in t.parts)


def read_qvd(path: str | Path) -> tuple[list[str], list[tuple[Any, ...]]]:
    from pyqvd import QvdTable

    table = QvdTable.from_qvd(str(path))
    assert isinstance(table, QvdTable)  # no chunk_size: one table, not an iterator
    d = table.to_dict()
    return list(d["columns"]), [tuple(r) for r in d["data"]]


class LocalVenue:
    venue = Venue.LOCAL

    def __init__(self) -> None:
        self.db = DuckDBSource()
        self.tables: dict[str, str] = {}  # logical key -> local table name
        self._fp = hashlib.sha256()  # fingerprint of the data loaded (file contents), for the result cache

    # ------------------------------------------------------------------ loading
    def add_source_dir(self, root: str | Path) -> None:
        root = Path(root)
        for f in sorted(root.rglob("*")):
            if f.suffix.lower() not in (".csv", ".parquet") or not f.is_file():
                continue
            rel = f.relative_to(root).with_suffix("")
            parts = list(rel.parts)
            if parts[0] == "files":
                key = f"{FILE_DB}:{f.name.lower()}"
            else:
                key = "sql:" + ".".join(parts).lower()
            name = "src__" + _local(".".join(parts))
            self._fp.update(key.encode() + hashlib.sha256(f.read_bytes()).digest())
            self.db.load_file(name, f)
            self.tables[key] = name

    def add_qvd(self, qvd_path: str, file: str | Path) -> None:
        """qvd_path: the path as the script names it (lib://QVD/Orders.qvd); file: the local QVD file."""
        self._fp.update(qvd_path.encode() + hashlib.sha256(Path(file).read_bytes()).digest())
        cols, rows = read_qvd(file)
        name = "qvd__" + _local(PurePosixPath(qvd_path.replace("\\", "/")).stem)
        self.db.load_rows(name, cols, rows)
        self.tables[f"{QVD_DB}:{PurePosixPath(qvd_path.replace(chr(92), '/')).name.lower()}"] = name

    # ------------------------------------------------------------------ SQL
    def to_local(self, dremio_sql: str) -> str:
        tree = sqlglot.parse_one(dremio_sql, read=DIALECT)
        missing: list[str] = []

        def rewrite(node: SqlExpr) -> SqlExpr:
            if not isinstance(node, exp.Table):
                return to_duckdb(node)
            if not node.db:
                return node
            if node.db == GEN_DB:
                gen = _generated(node.name)
                if gen is None:
                    missing.append(logical_path(node))
                    return node
                return exp.Subquery(this=gen, alias=node.args.get("alias") or exp.TableAlias(
                    this=exp.to_identifier("gen")))
            if node.db in (QVD_DB, FILE_DB):
                key = f"{node.db}:{PurePosixPath(node.name.replace(chr(92), '/')).name.lower()}"
            else:
                key = "sql:" + logical_path(node).lower()
            local = self.tables.get(key)
            if local is None:
                missing.append(logical_path(node))
                return node
            return exp.Table(this=exp.to_identifier(local), alias=node.args.get("alias"))

        out = tree.transform(rewrite)
        # a replaced node's children are not visited in the same pass (e.g. TIMESTAMPADD inside TO_CHAR): repeat
        # until nothing changes (every rewrite is idempotent)
        for _ in range(5):
            again = out.transform(rewrite)
            if again == out:
                break
            out = again
        if missing:
            raise MissingData(missing)
        return out.sql(dialect="duckdb")

    def fingerprint(self) -> str:
        """The data loaded, plus today's date (generated inputs such as a calendar run to today)."""
        return self._fp.hexdigest()[:24] + ":" + dt.date.today().isoformat()

    def run(self, dremio_sql: str, label: str, max_rows: int = 100_000) -> QueryResult:
        """Results are cached by (data fingerprint, SQL): the graph validates tables again after every fix, and
        the same query on the same data gives the same rows (small results only)."""
        key = (self.fingerprint(), dremio_sql, max_rows)
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
        r = self.db.run_query(self.to_local(dremio_sql), label=label, max_rows=max_rows)
        if len(r.rows) <= CACHE_ROWS:
            _CACHE[key] = r
            if len(_CACHE) > CACHE_SIZE:
                _CACHE.popitem(last=False)
        return r

    def scalar(self, dremio_sql: str, label: str) -> Any:
        return self.run(dremio_sql, label).rows[0][0]


def _generated(ref: str) -> SqlExpr | None:
    """A generated input in DuckDB: Qvc.Calendar(start, end) is one row per day with a Date column."""
    from adce_qlik.load_parser import LoadParseError, parse_load

    m = EXTENSION_RX.match(f"EXTENSION {ref}")
    if not m or m.group(1).lower() != "qvc.calendar":
        return None
    args = _split_top(m.group(2))
    if len(args) != 2:
        return None
    try:
        tr = Translator(ExprContext(alias="x", columns=[], mappings={}))
        bounds = [tr.value(parse_load(f"LOAD {a} AS v").fields[0].expr) for a in args]  # type: ignore[arg-type]
    except (LoadParseError, Untranslatable):
        return None
    lo, hi = (exp.Cast(this=b, to=exp.DataType.build("DATE")) for b in bounds)
    series = exp.Anonymous(this="generate_series", expressions=[lo, hi, exp.Interval(
        this=exp.Literal.string("1"), unit=exp.var("DAY"))])
    tbl = exp.Table(this=series, alias=exp.TableAlias(this=exp.to_identifier("g"), columns=[exp.to_identifier("d")]))
    return exp.select(exp.alias_(exp.Cast(this=exp.column("d"), to=exp.DataType.build("DATE")), "Date",
                                 quoted=True)).from_(tbl)
