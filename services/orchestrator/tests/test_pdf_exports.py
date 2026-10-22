"""Front-end agents in the graph, and a PDF export read through the route parse_export reaching V5."""
from pathlib import Path
from typing import Any

from adce_core.artifacts import LocalArtifactStore, get_json
from adce_core.ports import MemoryKG
from adce_reversesql.exports import ParsedExport, ParsedTable, pdf_pages, read_export
from orchestrator.pipeline import Deps, run_pipeline

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "orders_local"
XLSX = APP / "exports" / "Sales by region.xlsx"


def _cell(v: Any) -> str:
    return "-" if v is None else f"{v:,.2f}" if isinstance(v, float) else str(v)


def _pdf(lines: list[str]) -> bytes:
    """A one-page PDF printing the lines (Helvetica), as a Qlik PDF export of a table would."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    text = "BT /F1 10 Tf 50 780 Td 14 TL " + " ".join(f"({esc(x)}) Tj T*" for x in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> "
            "/Contents 5 0 R >>", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Length {len(text)} >>\nstream\n{text}\nendstream"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


class _Result:
    def __init__(self, output: Any) -> None:
        self.output, self.model, self.cost_usd, self.prompt_version = output, "stub", 0.0, "v1"


class StubGateway:
    """Answers parse_export as a correct model would (copies the printed table); any other route is not recorded."""

    mode = "replay"

    def __init__(self, table: ParsedTable) -> None:
        self.table = table
        self.seen: list[dict[str, Any]] = []

    async def call(self, route: str, inputs: dict[str, Any], output_model: Any, **kw: Any) -> Any:
        from adce_gateway.gateway import ReplayMiss

        if route != "parse_export":
            raise ReplayMiss(f"no recording for {route}")
        self.seen.append(inputs)
        return _Result(ParsedExport(tables=[self.table]))


def _upload(store: LocalArtifactStore, pdf: bytes) -> list[dict[str, Any]]:
    files = []
    frontend = sorted(p for p in (APP / "frontend").rglob("*") if p.is_file())
    paths = [APP / "script.qvs", APP / "reload.log", *sorted((APP / "qvd").glob("*.qvd")), *frontend,
             *sorted((APP / "sources").rglob("*.csv"))]
    for p in paths:  # named as the bench names them: the path inside the app folder
        name = p.relative_to(APP).as_posix()
        files.append({"name": name, "key": store.put(f"uploads/o/{name}", p.read_bytes())})
    files.append({"name": "Sales by region.pdf", "key": store.put("uploads/o/Sales by region.pdf", pdf)})
    return files


async def test_a_pdf_export_is_read_by_the_model_and_reaches_v5(tmp_path):
    t = read_export(XLSX.name, XLSX.read_bytes())[0]
    printed = [t.headers, *([_cell(c) for c in r] for r in t.rows)]
    pdf = _pdf(["Sales by region", "", *("    ".join(r) for r in printed)])
    assert "Sales by region" in pdf_pages(pdf)[0]

    table = ParsedTable(title=t.sheet, headers=t.headers, rows=[[_cell(c) for c in r] for r in t.rows],
                        totals=[_cell(c) for c in t.totals] if t.totals else None)
    gw = StubGateway(table)
    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    out: dict = dict(await run_pipeline(app_id="o", run_id="r1", files=_upload(store, pdf),
                                         deps=Deps(kg=kg, artifacts=store, gateway=gw)))
    nodes = [e["node"] for e in out["trace"]]
    assert {"plan_joins", "measures", "read_reports", "map_metrics"} <= set(nodes)
    assert nodes.index("map_metrics") < nodes.index("synthesize")
    assert gw.seen and any("Sales by region" in p for p in gw.seen[0]["pages"])  # the PDF's text reached the route

    exports = {(e["file"], e["via"]) for e in out["exports"]}
    assert ("Sales by region.pdf", "model") in exports
    figures = get_json(store, out["figures_key"])
    pdf_fig = next(f for f in figures if f["export"] == "Sales by region.pdf")
    assert pdf_fig["level"] == "V5" and pdf_fig["read_by"] == "model", pdf_fig
    mapped = await kg.facts(kind="export_maps_to")
    assert any("Sales by region.pdf" in f.subject for f in mapped)  # agent 22 matched it to its chart


async def test_without_a_model_a_pdf_is_left_for_a_person(tmp_path):
    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    out: dict = dict(await run_pipeline(app_id="o", run_id="r1", files=_upload(store, _pdf(["Sales"])),
                                         deps=Deps(kg=kg, artifacts=store)))
    assert not [e for e in out["exports"] if e["file"].endswith(".pdf")]
    unreadable = await kg.facts(kind="export_unreadable")
    assert unreadable and "no model" in unreadable[0].value["reason"]
