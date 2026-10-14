"""The local venue: run the Dremio SQL on DuckDB, with sources mapped to local data (until Dremio, phase PD).

- Logical SQL sources ("sales"."erp"."orders_v2") map to fixture files under <sources>/sales/erp/orders_v2.csv|parquet
- QVD inputs ("__qvd__"."lib://QVD/Orders.qvd") map to QVD files, read with PyQvd
- File inputs ("__file__"."lib://Data/x.csv") map to local files of the same name under <sources>/files/
The SQL is rewritten on its syntax tree (sqlglot) and transpiled to DuckDB; results are labelled venue 'local'.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

import sqlglot
from adce_core.data import DuckDBSource
from adce_core.facts import Venue
from adce_core.ports import QueryResult
from sqlglot import exp
from sqlglot.expressions.core import Expr as SqlExpr

from .compose import DIALECT, FILE_DB, QVD_DB


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
            self.db.load_file(name, f)
            self.tables[key] = name

    def add_qvd(self, qvd_path: str, file: str | Path) -> None:
        """qvd_path: the path as the script names it (lib://QVD/Orders.qvd); file: the local QVD file."""
        cols, rows = read_qvd(file)
        name = "qvd__" + _local(PurePosixPath(qvd_path.replace("\\", "/")).stem)
        self.db.load_rows(name, cols, rows)
        self.tables[f"{QVD_DB}:{PurePosixPath(qvd_path.replace(chr(92), '/')).name.lower()}"] = name

    # ------------------------------------------------------------------ SQL
    def to_local(self, dremio_sql: str) -> str:
        tree = sqlglot.parse_one(dremio_sql, read=DIALECT)
        missing: list[str] = []

        def rewrite(node: SqlExpr) -> SqlExpr:
            if not isinstance(node, exp.Table) or not node.db:
                return node
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
        if missing:
            raise LookupError(f"no local data for: {', '.join(sorted(set(missing)))}")
        return out.sql(dialect="duckdb")

    def run(self, dremio_sql: str, label: str, max_rows: int = 100_000) -> QueryResult:
        return self.db.run_query(self.to_local(dremio_sql), label=label, max_rows=max_rows)

    def scalar(self, dremio_sql: str, label: str) -> Any:
        return self.run(dremio_sql, label).rows[0][0]
