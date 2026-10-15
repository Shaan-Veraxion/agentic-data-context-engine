from pathlib import Path

from adce_core.bi_ir import TableAction as A
from adce_qlik import parse_reload_log, read_script
from adce_qlik.model import build_app

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "sales_overview"


def build(script: str):
    return build_app(read_script("t", {"s.qvs": script}))


def codes(app) -> list[str]:
    return [i.code for i in app.issues]


def test_fixture_app_model():
    log = parse_reload_log((APP / "reload.log").read_text(encoding="utf-8"))
    app = build_app(read_script("sales_overview", {"script.qvs": (APP / "script.qvs").read_text(encoding="utf-8")},
                                log))
    assert set(app.tables) == {"Customers", "RegionMap", "Orders"}
    orders = app.tables["Orders"]
    assert orders.fields == ["OrderID", "CustomerID", "OrderDate", "Amount", "OrderSize", "RegionName", "LoadedOn",
                             "CustomerRegion"]  # equals the fields in the Orders.qvd header
    assert [(s.action, s.join_kind) for s in orders.steps] == [(A.CREATE, None), (A.JOIN, "left")]
    assert [o.source.kind for o in orders.steps[0].ops] == ["preceding", "sql"]
    assert orders.stored_to == ["lib://QVD/Orders.qvd"]
    assert app.tables["RegionMap"].mapping and app.tables["Customers"].dropped_at is not None
    assert [s.key for s in app.sources] == ["Dremio_Sales:sales.crm.customers", "Dremio_Sales:sales.erp.orders_v2"]
    assert app.issues == [] and app.coverage.ratio == 1.0
    assert [t.name for t in app.data_tables()] == ["Orders"]


def test_auto_and_explicit_concatenate_and_noconcatenate():
    app = build("A: LOAD x, y INLINE [x, y\n1, 2];\n"
                "B: LOAD y, x INLINE [x, y\n3, 4];\n"
                "C: NOCONCATENATE LOAD x, y RESIDENT A;\n"
                "CONCATENATE (C) LOAD x, z RESIDENT A;\n")
    assert set(app.tables) == {"A", "C"}
    assert [s.action for s in app.tables["A"].steps] == [A.CREATE, A.AUTO_CONCATENATE]
    assert [s.action for s in app.tables["C"].steps] == [A.CREATE, A.CONCATENATE]
    assert app.tables["C"].fields == ["x", "y", "z"]


def test_join_without_target_uses_last_table_and_keep_records_other():
    app = build("T: LOAD k, a INLINE [k, a\n1, 2];\n"
                "JOIN LOAD k, b INLINE [k, b\n1, 3];\n"
                "K: LEFT KEEP (T) LOAD k, c INLINE [k, c\n1, 4];\n")
    t = app.tables["T"]
    assert [(s.action, s.join_kind) for s in t.steps] == [(A.CREATE, None), (A.JOIN, "outer")]
    assert t.fields == ["k", "a", "b"]
    keep = app.tables["K"].steps[0]
    assert (keep.action, keep.join_kind, keep.other_table) == (A.KEEP, "left", "T")


def test_default_names_and_qvd_chain_sources():
    app = build("LOAD * FROM [lib://QVD/Orders.qvd] (qvd);\nSTORE Orders INTO [lib://QVD/Out.qvd] (qvd);\n")
    t = app.tables["Orders"]
    assert t.name_inferred and t.stored_to == ["lib://QVD/Out.qvd"]
    assert [s.key for s in app.sources] == [":lib://QVD/Orders.qvd"]
    assert "star_unresolved" in codes(app)


def test_drop_and_rename():
    app = build("T: LOAD a, b INLINE [a, b\n1, 2];\nDROP FIELD b FROM T;\nRENAME FIELD a TO aa;\n"
                "RENAME TABLE T TO U;\n")
    assert list(app.tables) == ["U"] and app.tables["U"].fields == ["aa"] and app.tables["U"].renamed_from == ["T"]


def test_subs_called_loops_unrolled_and_tiers_flagged():
    app = build("SUB Load1\n  X: LOAD 1 AS a AUTOGENERATE 1;\nEND SUB\n"
                "FOR i = 1 TO 2\n  Y: LOAD $(i) AS b AUTOGENERATE 1;\nNEXT i\n"
                "Z: CROSSTABLE(Month, Sales) LOAD * RESIDENT Y;\nCALL Load1;\n")
    assert set(app.tables) == {"X", "Y", "Z"}
    y = app.tables["Y"]
    assert [(s.action, s.context) for s in y.steps] == [(A.CREATE, ["FOR i = 1 TO 2 (i=1)"]),
                                                         (A.AUTO_CONCATENATE, ["FOR i = 1 TO 2 (i=2)"])]
    assert app.tables["X"].steps[0].context == ["CALL Load1()"]
    # LOAD * RESIDENT Y takes Y's fields; CROSSTABLE keeps the first (b) and adds the attribute and data fields
    assert app.tables["Z"].fields == ["b", "Month", "Sales"]
    assert "construct_tier" in codes(app) and "star_unresolved" not in codes(app)


def test_parse_error_keeps_table_and_lowers_coverage():
    app = build("T: LOAD a,, b RESIDENT X;\n")
    assert "T" in app.tables and app.tables["T"].steps[0].ops[0].parse_error
    assert app.coverage.other == 1 and "parse_error" in codes(app)


def test_unquoted_sql_names_follow_the_database_case_rules():
    # Oracle reports unquoted names in upper case: the script then reads ENTITY, not Entity
    app = build_app(read_script("t", {"s.qvs": "LIB CONNECT TO 'ORA';\nT:\nLOAD *;\nSQL SELECT GC.CODE, "
                                                "GC.DESCRIPTION as Entity, \"Mixed\" AS \"Keep\" FROM GC;\n"}),
                    connection_dialects={"ORA": "oracle"})
    [step] = app.tables["T"].steps
    assert step.ops[-1].source.columns == ["CODE", "ENTITY", "Keep"]


def test_inline_cells_may_be_quoted():
    app = build_app(read_script("t", {"s.qvs": 'T:\nLOAD * INLINE [\n"Q 1 (L1)", Name\n0, "a, b"\n\'x\', y\n];\n'}))
    src = app.tables["T"].steps[0].ops[-1].source
    assert src.columns == ["Q 1 (L1)", "Name"] and src.inline_rows == [["0", "a, b"], ["x", "y"]]
