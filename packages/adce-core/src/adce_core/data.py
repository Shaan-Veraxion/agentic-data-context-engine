"""DataPort adapters: DuckDB (local venue, now) and Dremio (phase PD)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb

from .facts import Venue
from .ports import QueryResult

_READ_ONLY = re.compile(r"^\s*(with|select|explain|describe|show|values)\b", re.IGNORECASE)


class ReadOnlyViolation(ValueError):
    pass


class DuckDBSource:
    """Local stand-in for Dremio: tables loaded from QVD extracts or fixtures, queried read-only."""

    venue = Venue.LOCAL
    dialect = "duckdb"

    def __init__(self, database: str = ":memory:") -> None:
        self._con = duckdb.connect(database)

    # loading is the only write path, and it is not exposed through DataPort
    def load_rows(self, name: str, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        cols = ", ".join(f'"{c}"' for c in columns)
        if not rows:
            defs = ", ".join(f'"{c}" VARCHAR' for c in columns)
            self._con.execute(f'CREATE OR REPLACE TABLE "{name}" ({defs})')
            return
        row_ph = "(" + ", ".join("?" for _ in columns) + ")"
        values = ", ".join(row_ph for _ in rows)
        params = [v for row in rows for v in row]  # column types are inferred from the values
        self._con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM (VALUES {values}) t({cols})', params)

    def load_file(self, name: str, path: str | Path) -> None:
        p = str(path).replace("'", "''")
        reader = {".csv": "read_csv_auto", ".parquet": "read_parquet"}.get(Path(path).suffix.lower())
        if not reader:
            raise ValueError(f"unsupported file type: {path}")
        self._con.execute(f"CREATE OR REPLACE TABLE \"{name}\" AS SELECT * FROM {reader}('{p}')")

    def has_table(self, name: str) -> bool:
        return bool(self._con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]).fetchone())

    def run_query(self, sql: str, *, label: str, max_rows: int = 10_000) -> QueryResult:
        if not _READ_ONLY.match(sql) or ";" in sql.strip().rstrip(";"):
            raise ReadOnlyViolation("only single read-only statements are allowed")
        cur = self._con.execute(f"/* adce:{label} */ {sql}")
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_rows)
        return QueryResult(columns=cols, rows=rows, venue=self.venue, sql=sql,
                           meta={"label": label, "truncated": len(rows) == max_rows})


class DremioSource:
    """Arrow Flight SQL (ADBC) + REST catalog API v3. Implemented in phase PD, when Dremio access is available."""

    venue = Venue.DREMIO
    dialect = "dremio"

    def __init__(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError("Dremio is connected in phase PD (see docs/implementation-plan.md)")
