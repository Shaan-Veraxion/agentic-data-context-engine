"""V5, figures match: run each chart's SQL and compare it with the exported report Qlik produced."""
from __future__ import annotations

from dataclasses import dataclass, field

from adce_core.bi_ir import BIApp

from .charts import ChartSQL, ChartTranslator
from .compose import Translation
from .exports import ExportMatch, FigureCheck, compare, map_export, read_export
from .local import LocalVenue


@dataclass
class ExportResult:
    file: str
    sheet: str
    match: ExportMatch | None
    chart: ChartSQL | None = None
    check: FigureCheck | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def level_v5(self) -> bool:
        return self.check is not None and self.check.ok


def check_exports(app: BIApp, t: Translation, venue: LocalVenue, exports: dict[str, bytes]) -> list[ExportResult]:
    """exports: file name -> content (.xlsx / .csv)."""
    tr = ChartTranslator(app, t)
    seps = {k: (app.variables[k].value or "") for k in ("ThousandSep", "DecimalSep", "DateFormat")
            if k in app.variables}
    out: list[ExportResult] = []
    for name, data in sorted(exports.items()):
        try:
            tables = read_export(name, data)
        except ValueError as e:
            out.append(ExportResult(name, "", None, problems=[str(e)]))
            continue
        for et in tables:
            m = map_export(et, app.visuals)
            res = ExportResult(name, et.sheet, m)
            out.append(res)
            if m.visual_id is None:
                res.problems.append(f"not matched to a visual: {m.reason}")
                continue
            v = next(x for x in app.visuals if x.id == m.visual_id)
            chart = tr.visual(v)
            res.chart = chart
            if not chart.complete or chart.sql is None:
                res.problems += chart.untranslated or ["chart SQL not produced"]
                continue
            try:
                result = venue.run(chart.sql, f"chart:{v.id}")
                totals = None
                if et.totals is not None:
                    kpi = tr.visual(v, dims_override=[])
                    if kpi.sql is not None and kpi.complete:
                        rows = venue.run(kpi.sql, f"chart-totals:{v.id}").rows
                        totals = rows[0] if rows else None
            except Exception as e:  # noqa: BLE001 - reported as the export's problem, the run goes on
                res.problems.append(f"chart SQL did not run locally: {type(e).__name__}: {str(e).splitlines()[0]}")
                continue
            res.check = compare(m, result.columns, [tuple(r) for r in result.rows], totals,
                                thousand_sep=seps.get("ThousandSep") or ",", decimal_sep=seps.get("DecimalSep") or ".",
                                date_format=seps.get("DateFormat"))
    return out
