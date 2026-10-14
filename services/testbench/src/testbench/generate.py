"""Write the catalogue's cases to eval/cases: script, sources, reload log, QVDs, expected rows and case.yaml.

Everything an agent compares against (reload-log counts, QVD contents, expected rows) comes from the hand oracle in
catalogue.py, never from running SQL. Re-running replaces generated cases only; cases saved from the bench or
written by hand (no `generated: catalogue` in case.yaml) are left alone.

    uv run adce-bench generate
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml
from pyqvd import QvdTable

from .cases import CASES, write_csv
from .catalogue import Spec, specs

_FETCHING = re.compile(r"(?is)\bSQL\s+SELECT\b|\bLOAD\b.*\b(FROM|RESIDENT|INLINE|AUTOGENERATE)\b")


def reload_log(script: str, fetched: list[int]) -> str:
    """A reload log as Qlik writes it: each script line echoed with its number, and "N Lines fetched" after every
    statement that reads rows (a preceding LOAD without a source shares the count of the statement below it)."""
    ts = "20261001T040000.{:03d}+0000"
    out = [f"{ts.format(0)} Execution started."]
    counts = iter(fetched)
    stmt: list[str] = []
    used = 0
    for i, line in enumerate(script.splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("//"):
            continue
        out.append(f"{ts.format(i % 1000)} {i:04d}  {line.rstrip().rstrip(';')}")
        stmt.append(text)
        if text.endswith(";"):
            if _FETCHING.search(" ".join(stmt)):
                n = next(counts, None)
                if n is None:
                    raise ValueError(f"no 'lines fetched' given for the statement ending at line {i}")
                used += 1
                out.append(f"{ts.format(i % 1000)}       {n} Lines fetched")
            stmt = []
    if used != len(fetched):
        raise ValueError(f"{len(fetched)} counts given for {used} row-reading statements")
    out.append(f"{ts.format(999)}       Execution finished.")
    return "\n".join(out) + "\n"


def write_case(spec: Spec, root: Path = CASES) -> Path:
    d = root / spec.id
    if d.exists():
        meta = yaml.safe_load((d / "case.yaml").read_text(encoding="utf-8")) if (d / "case.yaml").exists() else {}
        if (meta or {}).get("generated") != "catalogue":
            raise FileExistsError(f"{d} was not generated from the catalogue; not overwriting it")
        shutil.rmtree(d)
    d.mkdir(parents=True)
    (d / "script.qvs").write_text(spec.script, encoding="utf-8")
    for path, t in spec.sources.items():
        write_csv(d / "sources" / f"{path}.csv", t.columns, t.rows)
    if spec.fetched:
        (d / "reload.log").write_text(reload_log(spec.script, spec.fetched), encoding="utf-8")
    stored = re.findall(r"(?im)^\s*STORE\s+(\w+)\s+INTO\s+\[lib://QVD/([^\]]+)\]", spec.script)
    for table, target in stored:
        t = spec.outputs[table]
        (d / "qvd").mkdir(exist_ok=True)
        QvdTable.from_dict({"columns": t.columns, "data": [list(r) for r in t.rows]}).to_qvd(str(d / "qvd" / target))
    for name, t in spec.outputs.items():
        write_csv(d / "expected" / f"{name}.csv", t.columns, t.rows)
    levels = spec.levels or {n: "V4" if n in {s for s, _ in stored} else "V3" for n in spec.outputs}
    meta = {"id": spec.id, "title": spec.title, "covers": spec.covers, "status": spec.status}
    if spec.gap:
        meta["gap"] = spec.gap
    meta |= {"oracle": "hand", "generated": "catalogue", "chain": spec.chain, "variants": spec.variants,
             "levels": levels, "expect": spec.expect}
    if spec.oracle_note:
        meta["oracle_note"] = spec.oracle_note
    if spec.when:
        meta["when"] = spec.when
    (d / "case.yaml").write_text(yaml.safe_dump(meta, sort_keys=False, width=120), encoding="utf-8")
    return d


def generate(root: Path = CASES) -> list[Path]:
    return [write_case(s, root) for s in specs()]
