"""Known Qlik script libraries: what their public SUBs do, so a CALL into a library that is not supplied can still
be modelled.

Scripts often include a shared library from a server path (for example QlikView Components, Qvc.qvs) that is not
part of the application's files. Instead of failing every table that depends on it, each known routine is
described by the Qlik statements that reproduce its effect on the data model. The interpreter runs those
statements in place of the CALL and records that a library model was used, so reviewers can see the assumption.

Effects modelled (QlikView Components, https://github.com/RobWunderlich/Qlikview-Components):
- Qvc.IncrementalSetup(table, ...) sets Qvc.Loader.v.Tablename; the incremental merge is modelled as a full load
  (in Dremio, refresh is the job of the dataset or reflection, not the query).
- Qvc.IncrementalStore stores that table to a QVD.
- Qvc.Calendar(start, end, table, prefix) creates a calendar table; modelled as a generated input whose Date field
  is <prefix>Date, mapped later to the estate's date dimension.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# Include targets that are known libraries: a missing include of these is expected, not an error.
LIBRARY_INCLUDES = {"qvc.qvs": "QlikView Components"}


@dataclass
class LibraryCall:
    statements: list[str]  # Qlik statements (without terminator) that reproduce the effect
    note: str


def _unquote(a: str) -> str:
    a = a.strip()
    return a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "'\"" else a


def _incremental_setup(args: list[str]) -> LibraryCall:
    table = _unquote(args[0]) if args else ""
    return LibraryCall([f"LET Qvc.Loader.v.Tablename = '{table}'"],
                       f"Qvc incremental load of {table!r} modelled as a full load")


def _incremental_store(args: list[str]) -> LibraryCall:
    return LibraryCall(["STORE [$(Qvc.Loader.v.Tablename)] INTO [qvc/$(Qvc.Loader.v.Tablename).qvd] (qvd)"],
                       "Qvc.IncrementalStore modelled as STORE of the loaded table")


def _calendar(args: list[str]) -> LibraryCall:
    start = args[0] if len(args) > 0 else "''"
    end = args[1] if len(args) > 1 else "Today()"
    table = _unquote(args[2]) if len(args) > 2 and _unquote(args[2]) else "Calendar"
    prefix = _unquote(args[3]) if len(args) > 3 else ""
    return LibraryCall([f"[{table}]: LOAD [Date] AS [{prefix}Date] EXTENSION Qvc.Calendar({start}, {end})"],
                       f"Qvc.Calendar modelled as a generated date input for {table!r} ({prefix}Date only; other "
                       "calendar fields are not modelled)")


ROUTINES: dict[str, Callable[[list[str]], LibraryCall]] = {
    "qvc.incrementalsetup": _incremental_setup,
    "qvc.incrementalstore": _incremental_store,
    "qvc.calendar": _calendar,
}


def library_call(name: str, args: list[str]) -> LibraryCall | None:
    fn = ROUTINES.get(name.lower())
    return fn(args) if fn else None


def library_of_include(target: str) -> str | None:
    base = re.split(r"[\\/]", target)[-1].lower()
    return LIBRARY_INCLUDES.get(base)
