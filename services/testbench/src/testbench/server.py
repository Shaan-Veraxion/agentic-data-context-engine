"""The test bench web app: browse cases and their files, run any agent chain on any variant, compare with the
expected results, try a script in the playground and save it as a case. Local only (binds 127.0.0.1).

    uv run adce-bench serve      -> http://127.0.0.1:8765
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from adce_core.ports import MemoryKG
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .cases import (
    AI_MODES,
    CASES,
    DATA_VARIANTS,
    PRIVATE,
    ROOT,
    Case,
    all_cases,
    get_case,
    load_case,
    save_case,
    write_csv,
)
from .runner import Variant, agent_catalog, resolve_chain, run_case

STATIC = Path(__file__).parent / "static"
STATE = ROOT / ".adce" / "bench"
PLAYGROUND = STATE / "playground"
LAST = STATE / "last"

app = FastAPI(title="ADCE test bench")
_session_kg = MemoryKG()  # shared knowledge across runs when memory = "session" (tests learning and reuse)


def _last_path(case_id: str, variant: Variant, chain: list[int] | None = None) -> Path:
    """The last result per case and variant; a run with a custom agent chain is kept apart from the default."""
    tag = "" if not chain else "__chain-" + "-".join(map(str, chain))
    return LAST / f"{re.sub(r'[^0-9A-Za-z_.-]+', '_', case_id)}__{variant.ai}__{variant.data}{tag}.json"


def _last(case_id: str) -> list[dict[str, Any]]:
    if not LAST.is_dir():
        return []
    prefix = re.sub(r"[^0-9A-Za-z_.-]+", "_", case_id) + "__"
    out = []
    for p in sorted(LAST.glob(prefix + "*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"variant": d["variant"], "status": d["status"], "passed": d["passed"], "failed": d["failed"],
                    "seconds": d["seconds"], "chain": d.get("selected")})
    return out


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    return {"agents": agent_catalog(), "ai_modes": AI_MODES, "data_variants": DATA_VARIANTS,
            "private_cases": PRIVATE.is_dir()}


@app.get("/api/cases")
def cases() -> list[dict[str, Any]]:
    return [c.to_dict() | {"last": _last(c.id)} for c in all_cases()]


def _file_view(rel: str, p: Path) -> dict[str, Any]:
    kind = "binary"
    view: dict[str, Any] = {"name": rel, "bytes": p.stat().st_size}
    suffix = p.suffix.lower()
    if suffix == ".qvd":
        from adce_reversesql.local import read_qvd

        try:
            cols, rows = read_qvd(p)
            view.update({"columns": cols, "rows": [[_plain(v) for v in r] for r in rows[:200]], "total": len(rows)})
            kind = "table"
        except Exception as e:  # noqa: BLE001
            view["error"] = str(e)
    elif suffix == ".csv":
        from .cases import read_csv

        cols, rows = read_csv(p)
        view.update({"columns": cols, "rows": [list(r) for r in rows[:200]], "total": len(rows)})
        kind = "table"
    elif suffix in (".qvs", ".log", ".txt", ".json", ".yaml", ".yml", ".md", ".xml"):
        view["text"] = p.read_text(encoding="utf-8-sig", errors="replace")[:200_000]
        kind = "text"
    view["kind"] = kind
    return view


def _plain(v: Any) -> Any:
    return v if isinstance(v, (int, float, str)) or v is None else str(v)


@app.get("/api/case/{case_id:path}")
def case_detail(case_id: str) -> dict[str, Any]:
    try:
        c = get_case(case_id)
    except KeyError as e:
        raise HTTPException(404, f"no case {case_id}") from e
    files = [_file_view(rel, p) for rel, p in c.files("full")]
    expected = [_file_view(f"expected/{p.name}", p) for p in sorted((c.dir / "expected").glob("*.csv"))]
    yaml_text = (c.dir / "case.yaml").read_text(encoding="utf-8") if (c.dir / "case.yaml").exists() else ""
    return c.to_dict() | {"files": files, "expected": expected, "case_yaml": yaml_text, "last": _last(c.id)}


class RunRequest(BaseModel):
    case: str
    agents: list[int] | None = None
    ai: str = "off"
    data: str = "full"
    memory: str = "fresh"  # fresh | session


async def _run(c: Case, req: RunRequest) -> dict[str, Any]:
    if req.ai not in AI_MODES or req.data not in DATA_VARIANTS:
        raise HTTPException(400, "unknown variant")
    variant = Variant(req.ai, req.data)
    kg = _session_kg if req.memory == "session" else None
    r = await run_case(c, variant, req.agents or None, kg)
    custom = sorted(req.agents) if req.agents and sorted(req.agents) != sorted(c.chain) else None
    out = r.to_dict() | {"selected": custom}
    LAST.mkdir(parents=True, exist_ok=True)
    _last_path(c.id, variant, custom).write_text(json.dumps(out, default=str), encoding="utf-8")
    return out


@app.post("/api/run")
async def run(req: RunRequest) -> dict[str, Any]:
    try:
        c = get_case(req.case)
    except KeyError as e:
        raise HTTPException(404, f"no case {req.case}") from e
    return await _run(c, req)


@app.get("/api/chain")
def chain(agents: str) -> list[dict[str, Any]]:
    sel = [int(a) for a in agents.split(",") if a.strip()]
    return [{"number": n, "auto": auto} for n, auto in resolve_chain(sel)]


@app.post("/api/memory/reset")
def reset_memory() -> dict[str, Any]:
    global _session_kg
    _session_kg = MemoryKG()
    return {"ok": True}


@app.get("/api/memory")
async def memory() -> list[dict[str, Any]]:
    facts = await _session_kg.facts(kind="translation_memory")
    return [{"subject": f.subject, "trust": f.trust.value, "value": f.value} for f in facts]


# ---------------------------------------------------------------------- playground
class SourceIn(BaseModel):
    path: str  # shop/raw/orders  (SQL source "shop"."raw"."orders") or files/prices (lib://.../prices.csv)
    csv: str


class PlaygroundRequest(BaseModel):
    script: str
    reload_log: str = ""
    sources: list[SourceIn] = []
    expected: list[SourceIn] = []  # path = table name
    agents: list[int] | None = None
    ai: str = "off"
    memory: str = "fresh"
    title: str = "playground"


def _safe_rel(path: str) -> str:
    parts = [p for p in re.split(r"[\\/]+", path.strip()) if p and p not in (".", "..")]
    if not parts:
        raise HTTPException(400, f"bad path {path!r}")
    return "/".join(re.sub(r"[^0-9A-Za-z_.-]+", "_", p) for p in parts)


def _write_playground(req: PlaygroundRequest, d: Path) -> None:
    import csv
    import io

    d.mkdir(parents=True, exist_ok=True)
    (d / "script.qvs").write_text(req.script, encoding="utf-8")
    if req.reload_log.strip():
        (d / "reload.log").write_text(req.reload_log, encoding="utf-8")
    for s in req.sources:
        rows = list(csv.reader(io.StringIO(s.csv.strip())))
        if rows:
            write_csv(d / "sources" / f"{_safe_rel(s.path)}.csv", rows[0], [tuple(r) for r in rows[1:]])
    for e in req.expected:
        rows = list(csv.reader(io.StringIO(e.csv.strip())))
        if rows:
            write_csv(d / "expected" / f"{_safe_rel(e.path)}.csv", rows[0], [tuple(r) for r in rows[1:]])
    meta = {"id": f"playground/{d.name}", "title": req.title, "oracle": "hand" if req.expected else "none",
            "chain": req.agents or [1, 27, 28, 24, 25, 26, 20], "variants": {"ai": [req.ai], "data": ["full"]}}
    import yaml

    (d / "case.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")


@app.post("/api/playground")
async def playground(req: PlaygroundRequest) -> dict[str, Any]:
    pid = uuid.uuid4().hex[:10]
    d = PLAYGROUND / pid
    _write_playground(req, d)
    c = load_case(d / "case.yaml", PLAYGROUND)
    out = await _run(c, RunRequest(case=c.id, agents=req.agents, ai=req.ai, data="full", memory=req.memory))
    return out | {"playground_id": pid}


class SaveRequest(BaseModel):
    playground_id: str
    suite: str
    name: str
    title: str
    private: bool = False  # private cases go to eval/private/cases (git-ignored): real client scripts
    covers: list[str] = []


@app.post("/api/cases/save")
def save_from_playground(req: SaveRequest) -> dict[str, Any]:
    src = PLAYGROUND / _safe_rel(req.playground_id)
    if not src.is_dir():
        raise HTTPException(404, "playground run not found")
    root = PRIVATE if req.private else CASES
    dest = root / _safe_rel(req.suite) / _safe_rel(req.name)
    if dest.exists():
        raise HTTPException(409, f"{dest.relative_to(ROOT).as_posix()} exists")
    shutil.copytree(src, dest)
    c = load_case(dest / "case.yaml", root, private=req.private)
    c.title, c.covers = req.title, req.covers
    if not req.private:  # private ids come from the folder
        c.id = f"{_safe_rel(req.suite)}/{_safe_rel(req.name)}"
    save_case(c)
    return {"saved": dest.relative_to(ROOT).as_posix(), "id": load_case(dest / "case.yaml", root, req.private).id}


class ApproveRequest(BaseModel):
    case: str
    ai: str = "off"
    data: str = "full"
    rows: bool = True  # also write expected/<table>.csv from the SQL results when the case has none


@app.post("/api/cases/approve")
def approve(req: ApproveRequest) -> dict[str, Any]:
    """Accept the last run's results as the case's expectations (oracle: approved). For cases without a hand
    oracle, e.g. a real script checked by a reviewer; a hand oracle is never overwritten."""
    try:
        c = get_case(req.case)
    except KeyError as e:
        raise HTTPException(404, f"no case {req.case}") from e
    p = _last_path(c.id, Variant(req.ai, req.data))
    if not p.exists():
        raise HTTPException(400, "run the case with this variant first")
    r = json.loads(p.read_text(encoding="utf-8"))
    steps = {s["step"]: s for s in r["steps"] if not s.get("error")}
    if "reconcile" in steps:
        c.levels = dict(steps["reconcile"]["outputs"].get("levels", {}))
    if "extract" in steps:
        c.expect.setdefault("extract", {})["tables"] = steps["extract"]["outputs"].get("tables", [])
    if "translate" in steps:
        c.expect.setdefault("translate", {})["untranslated"] = steps["translate"]["metrics"].get("untranslated")
    written = []
    if req.rows and c.oracle != "hand" and not (c.dir / "expected").exists():
        for t, v in r.get("rows", {}).items():
            if v.get("actual") is not None:
                write_csv(c.dir / "expected" / f"{t}.csv", v.get("actual_columns") or v["columns"],
                          [tuple(x) for x in v["actual"]])
                written.append(t)
    if c.oracle != "hand":
        c.oracle = "approved"
    save_case(c)
    return {"levels": c.levels, "expected_rows_written": written, "oracle": c.oracle}
