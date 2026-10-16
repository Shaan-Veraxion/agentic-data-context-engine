"""Qlik constructs the starting stage commits to: IntervalMatch, Previous()/Peek(), QUALIFY, Section Access, loops over
file lists (from the reload log) and Above()/RangeSum(Above()) in charts. Expected rows are worked out by hand."""
from pathlib import Path

from adce_core.bi_ir import Visual, VisualMeasure
from adce_qlik import parse_reload_log
from adce_qlik.document import read_script
from adce_qlik.model import build_app
from adce_reversesql.charts import ChartTranslator
from adce_reversesql.cli import _load, _venue
from adce_reversesql.compose import compose
from adce_reversesql.local import LocalVenue

ORDERS = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "orders_local"


def _rows(script: str, table: str, log: str | None = None) -> tuple[list[str], list[tuple]]:
    app = build_app(read_script("t", {"s.qvs": script}, parse_reload_log(log) if log else None))
    t = compose(app)
    r = t.tables[table]
    assert not r.untranslated, r.untranslated
    res = LocalVenue().run(t.render(r.final_cte or "", comments=False), "t")
    return list(res.columns), sorted(res.rows, key=repr)


def test_intervalmatch_pairs_each_value_with_the_intervals_containing_it():
    script = ("Events:\nLOAD * INLINE [\nTime, Event\n1, a\n5, b\n12, c\n20, d\n];\n"
              "Intervals:\nLOAD * INLINE [\nStart, End, Shift\n0, 6, Morning\n5, 13, Day\n];\n"
              "Match:\nIntervalMatch(Time) LOAD Start, End RESIDENT Intervals;\n")
    cols, rows = _rows(script, "Match")
    assert cols == ["Time", "Start", "End"]
    assert rows == sorted([(1, 0, 6), (5, 0, 6), (5, 5, 13), (12, 5, 13)], key=repr)  # 20 is in no interval


def test_previous_and_peek_follow_the_loads_order_by():
    script = ("Raw:\nLOAD * INLINE [\nDay, Amount\n3, 7\n1, 10\n2, 15\n];\n"
              "Diffs:\nNoConcatenate LOAD Day, Amount, Amount - Previous(Amount) AS Change, Peek('Day') AS PrevDay,\n"
              "  Peek('Amount', 0) AS FirstAmount RESIDENT Raw ORDER BY Day;\n")
    _, rows = _rows(script, "Diffs")
    assert rows == sorted([(1, 10, None, None, 10), (2, 15, 5, 1, 10), (3, 7, -8, 2, 10)], key=repr)


def test_previous_without_an_order_or_a_running_total_stays_untranslated():
    script = ("Raw:\nLOAD * INLINE [\nDay, Amount\n1, 10\n];\n"
              "U:\nNoConcatenate LOAD Day, Previous(Amount) AS P RESIDENT Raw;\n"
              "R:\nNoConcatenate LOAD Day, Alt(Peek('Total'), 0) + Amount AS Total RESIDENT Raw ORDER BY Day;\n")
    t = compose(build_app(read_script("t", {"s.qvs": script})))
    assert "ORDER BY" in t.tables["U"].untranslated[0].reason and t.tables["U"].untranslated[0].tier == "T3"
    assert "recursive" in t.tables["R"].untranslated[0].reason


def test_qualify_renames_fields_so_tables_no_longer_associate_or_concatenate():
    script = ("QUALIFY *;\nUNQUALIFY Key;\nA:\nLOAD * INLINE [\nKey, Name\n1, x\n2, y\n];\n"
              "B:\nLOAD * INLINE [\nKey, Name\n1, p\n];\n")
    app = build_app(read_script("t", {"s.qvs": script}))
    assert app.tables["A"].fields == ["Key", "A.Name"] and app.tables["B"].fields == ["Key", "B.Name"]
    assert _rows(script, "A") == (["Key", "A.Name"], [(1, "x"), (2, "y")])


def test_section_access_is_a_security_requirement_not_a_table():
    script = ("Section Access;\nAccess:\nLOAD * INLINE [\nACCESS, USERID, REGION\nADMIN, ANNA, *\n"
              "USER, BOB, NORTH\n];\n"
              "Section Application;\nSales:\nLOAD * INLINE [\nREGION, Amount\nNORTH, 10\n];\n")
    app = build_app(read_script("t", {"s.qvs": script}))
    assert sorted(app.tables) == ["Sales"]
    assert [(a.name, a.reduction_fields) for a in app.section_access] == [("Access", ["REGION"])]
    assert any(i.code == "section_access" for i in app.issues)


def test_a_loop_over_a_file_list_is_unrolled_with_the_files_the_reload_log_shows():
    script = ("FOR EACH vFile in FileList('lib://Data/sales_*.csv')\n  Sales:\n"
              "  LOAD Region, Amount FROM [$(vFile)] (txt, delimiter is ',');\nNEXT vFile\n")
    load = "LOAD Region, Amount FROM [lib://Data/sales_{}.csv] (txt, delimiter is ',')"
    log = ("20261001T021400.000+0000 Execution started.\n"
           "20261001T021400.010+0000 0001  FOR EACH vFile in FileList('lib://Data/sales_*.csv')\n"
           "20261001T021400.011+0000 0002  Sales:\n"
           f"20261001T021400.011+0000 0003  {load.format(2025)}\n"
           "20261001T021400.100+0000       12 Lines fetched\n"
           "20261001T021400.110+0000 0004  NEXT vFile\n"
           "20261001T021400.111+0000 0002  Sales:\n"
           f"20261001T021400.111+0000 0003  {load.format(2026)}\n"
           "20261001T021400.200+0000       9 Lines fetched\n"
           "20261001T021400.210+0000 0004  NEXT vFile\n")
    doc = read_script("t", {"s.qvs": script}, parse_reload_log(log))
    assert [i.code for i in doc.issues] == ["loop_unrolled_from_log"]
    app = build_app(doc)
    assert [s.path for s in app.sources] == ["lib://Data/sales_2025.csv", "lib://Data/sales_2026.csv"]
    without_log = read_script("t", {"s.qvs": script})
    assert [i.code for i in without_log.issues] == ["loop_not_unrolled"]


def test_above_and_a_running_rangesum_over_the_charts_rows():
    _, app = _load(ORDERS)
    t = compose(app)  # type: ignore[arg-type]
    dim = app.visuals[0].dimensions[0]  # type: ignore[union-attr]
    v = Visual(id="above", type="table", dimensions=[dim], measures=[
        VisualMeasure(id="m1", label="S", expression="Sum(Amount)"),
        VisualMeasure(id="m2", label="Prev", expression="Above(Sum(Amount))"),
        VisualMeasure(id="m3", label="Run2", expression="RangeSum(Above(Sum(Amount), 0, 2))")])
    c = ChartTranslator(app, t).visual(v)  # type: ignore[arg-type]
    assert not c.untranslated and c.tier == "T2" and any("Above()" in n for n in c.notes)
    venue, _, _ = _venue(ORDERS, app)  # type: ignore[arg-type]
    rows = venue.run(c.sql or "", "t").rows
    by = {r[0]: r[1:] for r in rows}
    s = [by[k][0] for k in sorted(k for k in by if k is not None)]
    prev = [by[k][1] for k in sorted(k for k in by if k is not None)]
    run2 = [by[k][2] for k in sorted(k for k in by if k is not None)]
    assert prev == [None, *s[:-1]] and run2 == [s[0], *[a + b for a, b in zip(s, s[1:], strict=False)]]
