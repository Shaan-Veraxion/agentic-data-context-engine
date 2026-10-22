"""Read a Qlik application's script(s) into a ScriptDocument: split, classify, splice includes, run control flow,
resolve variables, measure coverage."""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from adce_core.bi_ir import Coverage, ScriptDocument, Statement, StatementKind

from .classify import classify
from .interpreter import execute
from .reload_log import ReloadLog
from .splitter import split_script

INCLUDE_REF = re.compile(r"(?i)\$\(\s*(?:must_)?include\s*=\s*([^)]+?)\s*\)")


def include_targets(files: dict[str, str]) -> set[str]:
    """Base names of files that other files pull in with $(Include=…); they are not main scripts."""
    out = set()
    for text in files.values():
        for m in INCLUDE_REF.finditer(text):
            out.add(PurePosixPath(m.group(1).strip().strip("'\"[]").replace("\\", "/")).name.lower())
    return out


def read_script(app_id: str, files: dict[str, str], reload_log: ReloadLog | None = None,
                seed_variables: dict[str, str] | None = None) -> ScriptDocument:
    """files: ordered mapping of file name -> script text (Qlik Sense sections or .qvs files, includes too)."""
    included = include_targets(files)
    mains = {n: t for n, t in files.items() if PurePosixPath(n.replace("\\", "/")).name.lower() not in included}
    statements: list[Statement] = []
    for name, text in mains.items():
        for raw in split_script(text, file=name):
            statements.append(classify(raw, len(statements)))
    written, executed, variables, issues = execute(statements, files, reload_log, seed_variables)
    other = sum(1 for s in written if s.kind == StatementKind.OTHER)
    unresolved = sum(1 for s in executed if s.unresolved_variables)
    cov = Coverage(total=len(written), understood=len(written) - other, other=other,
                   unresolved_variables=unresolved)
    return ScriptDocument(app_id=app_id, files=list(files), statements=written, executed=executed,
                          variables=variables, coverage=cov, issues=issues,
                          reload_time=reload_log.reload_time() if reload_log else None)
