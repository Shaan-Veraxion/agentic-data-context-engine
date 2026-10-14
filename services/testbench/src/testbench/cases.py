"""Test cases on disk: eval/cases/<suite>/<name>/case.yaml plus the files an upload would carry.

A case folder holds script.qvs, reload.log, qvd/, sources/, frontend/, exports/, connections.json (any subset) and
expected/<Table>.csv (the rows Qlik holds after the reload, computed by hand). A case may instead point at an app
folder elsewhere (`app:`), so the gold apps are runnable without copies.

Data variants say which of those files are uploaded, so one case tests the combinations a client can bring:
  full         everything
  no_qvd       no QVD files (validation can reach V3 from the reload log at most)
  no_log       no reload log (row counts come from QVD headers only)
  script_only  script, app objects and source extracts (SQL runs: V2; no exports to compare charts with)
  script_bare  script and app objects alone (nothing executes: V1)
Real client cases may live in eval/private/cases (git-ignored); they are listed in the suite "private".
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[4]
CASES = ROOT / "eval" / "cases"
PRIVATE = ROOT / "eval" / "private" / "cases"
LEVELS = ["none", "V0", "V1", "V2", "V3", "V4", "V5"]
DATA_VARIANTS = ["full", "no_qvd", "no_log", "script_only", "script_bare"]
AI_MODES = ["off", "replay", "live"]


@dataclass
class Case:
    id: str
    dir: Path
    title: str
    covers: list[str] = field(default_factory=list)
    status: str = "active"
    gap: str = ""
    oracle: str = "hand"
    oracle_note: str = ""
    chain: list[int] = field(default_factory=list)
    variants: dict[str, list[str]] = field(default_factory=dict)
    levels: dict[str, str] = field(default_factory=dict)
    expect: dict[str, Any] = field(default_factory=dict)
    when: list[dict[str, Any]] = field(default_factory=list)  # [{if: {ai: off}, expect: {...}, levels: {...}}]
    app: str | None = None
    private: bool = False

    @property
    def suite(self) -> str:
        return self.id.split("/", 1)[0]

    @property
    def data_dir(self) -> Path:
        return (self.dir / self.app).resolve() if self.app else self.dir

    def files(self, data: str = "full") -> list[tuple[str, Path]]:
        """(upload name, local path) for the files the data variant uploads."""
        d = self.data_dir
        out: list[tuple[str, Path]] = []
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(d).as_posix()
            top = rel.split("/", 1)[0]
            if top in ("expected", "private") or p.name in ("case.yaml", "make_fixture.py", "README.md") \
                    or p.suffix in (".py", ".xml"):
                continue
            if data in ("no_qvd", "script_only", "script_bare") and p.suffix.lower() == ".qvd":
                continue
            if data in ("no_log", "script_only", "script_bare") and p.suffix.lower() == ".log":
                continue
            if data == "script_bare" and top == "sources":
                continue
            if data in ("script_only", "script_bare") and top == "exports":
                continue  # exported reports are evidence, like QVDs; app objects (frontend/) stay
            out.append((rel, p))
        return out

    def expected_rows(self) -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
        out = {}
        for p in sorted((self.dir / "expected").glob("*.csv")):
            out[p.stem] = read_csv(p)
        return out

    def expectations(self, ai: str, data: str) -> tuple[dict[str, Any], dict[str, str]]:
        """The expectations and table levels for one variant: the case's own, then matching `when` overrides,
        then levels capped by what the data variant can prove."""
        expect = _deep_copy(self.expect)
        levels = dict(self.levels)
        for w in self.when:
            cond = w.get("if", {})
            if all(str({"ai": ai, "data": data}.get(k)) == str(v) for k, v in cond.items()):
                _merge(expect, w.get("expect", {}))
                levels.update(w.get("levels", {}))
        stored = set(self.stored_tables())
        capped = {t: cap_level(lv, data, t in stored) for t, lv in levels.items()}
        return expect, capped

    def stored_tables(self) -> list[str]:
        script = next((p for _, p in self.files("full") if p.suffix.lower() == ".qvs"), None)
        if script is None:
            return []
        import re
        return re.findall(r"(?im)^\s*STORE\s+(\w+)\s+INTO", script.read_text(encoding="utf-8-sig"))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "suite": self.suite, "title": self.title, "covers": self.covers,
                "status": self.status, "gap": self.gap, "oracle": self.oracle, "oracle_note": self.oracle_note,
                "chain": self.chain, "variants": self.variants, "levels": self.levels, "expect": self.expect,
                "when": self.when, "app": self.app, "private": self.private}


def cap_level(level: str, data: str, stored: bool) -> str:
    """The highest level a data variant can prove: without sources nothing runs, without a log or QVD there is
    nothing to count against, and data (V4) needs a stored table's QVD contents."""
    cap = {"full": "V5", "no_qvd": "V3", "no_log": "V4" if stored else "V2", "script_only": "V2",
           "script_bare": "V1"}[data]
    return min(level, cap, key=LEVELS.index)


def read_csv(p: Path) -> tuple[list[str], list[tuple[Any, ...]]]:
    with p.open(encoding="utf-8", newline="") as fh:
        r = csv.reader(fh)
        cols = next(r)
        rows = [tuple(_typed(v) for v in row) for row in r]
    return cols, rows


def write_csv(p: Path, cols: list[str], rows: list[tuple[Any, ...]]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(cols)
        w.writerows([["" if v is None else v for v in r] for r in rows])


def _typed(v: str) -> Any:
    if v == "":
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _deep_copy(d: dict[str, Any]) -> dict[str, Any]:
    return yaml.safe_load(yaml.safe_dump(d)) or {}


def _merge(a: dict[str, Any], b: dict[str, Any]) -> None:
    """Override per section: {translate: {ai_status: {}}} replaces translate.ai_status, keeps translate's others."""
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            a[k].update(v)
        else:
            a[k] = v


def load_case(path: Path, root: Path = CASES, private: bool = False) -> Case:
    meta = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    variants = meta.get("variants") or {"ai": ["off"], "data": ["full"]}
    # YAML 1.1 reads a bare off/on as a boolean
    variants["ai"] = ["off" if v is False else "live" if v is True else str(v) for v in variants.get("ai", ["off"])]
    for w in meta.get("when", []):
        if w.get("if", {}).get("ai") is False:
            w["if"]["ai"] = "off"
    cid = meta.get("id") or path.parent.relative_to(root).as_posix()
    if private:
        cid = "private/" + path.parent.relative_to(root).as_posix().replace("/", "_")
    return Case(id=cid, dir=path.parent, title=meta.get("title", cid), covers=meta.get("covers", []),
                status=meta.get("status", "active"), gap=meta.get("gap", ""), oracle=meta.get("oracle", "hand"),
                oracle_note=meta.get("oracle_note", ""), chain=meta.get("chain", [1, 27, 28, 24, 25, 26, 20]),
                variants=variants, levels=meta.get("levels", {}),
                expect=meta.get("expect", {}), when=meta.get("when", []), app=meta.get("app"), private=private)


def all_cases() -> list[Case]:
    out = [load_case(p) for p in sorted(CASES.rglob("case.yaml"))]
    if PRIVATE.is_dir():
        out += [load_case(p, PRIVATE, private=True) for p in sorted(PRIVATE.rglob("case.yaml"))]
    return out


def get_case(case_id: str) -> Case:
    for c in all_cases():
        if c.id == case_id:
            return c
    raise KeyError(case_id)


def save_case(case: Case) -> Path:
    meta = {k: v for k, v in case.to_dict().items() if k not in ("suite", "private") and v not in (None, "", [], {})}
    meta.setdefault("status", "active")
    p = case.dir / "case.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    return p
