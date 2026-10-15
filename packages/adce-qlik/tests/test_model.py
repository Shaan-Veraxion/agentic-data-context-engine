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
    assert app.tables["Z"].fields == ["b"]  # LOAD * RESIDENT Y takes Y's fields
    assert "construct_tier" in codes(app) and "star_unresolved" not in codes(app)


def test_parse_error_keeps_table_and_lowers_coverage():
    app = build("T: LOAD a,, b RESIDENT X;\n")
    assert "T" in app.tables and app.tables["T"].steps[0].ops[0].parse_error
    assert app.coverage.other == 1 and "parse_error" in codes(app)


def test_label_inside_preceding_chain_names_the_table():
    app = build("LIB CONNECT TO 'Db';\n"
                "LOAD id AS cid, name;\n"
                "[Company_vw]:\n"
                "SQL SELECT id, name FROM COMPANY_VW;\n"
                "LEFT JOIN ([Company_vw]) LOAD cid, region INLINE [cid, region\n1, N];\n"
                "DROP TABLE [Company_vw];\n")
    assert list(app.tables) == ["Company_vw"]
    t = app.tables["Company_vw"]
    assert not t.name_inferred and [s.action for s in t.steps] == [A.CREATE, A.JOIN]
    assert "unknown_target" not in codes(app)


def test_dropped_table_name_is_reused():
    app = build("Files: LOAD a INLINE [a\n1];\n"
                "Out: LOAD a AS b RESIDENT Files;\n"
                "DROP TABLE Files;\n"
                "Files: LOAD a, c INLINE [a, c\n2, 3];\n"
                "Out2: NOCONCATENATE LOAD a AS d RESIDENT Files;\n"
                "DROP TABLE Files;\n")
    assert "duplicate_table_name" not in codes(app)
    assert set(app.tables) == {"Files", "Out", "Files#2", "Out2"}
    assert app.tables["Files"].dropped_at is not None and app.tables["Files#2"].dropped_at is not None
    assert app.tables["Out2"].steps[0].ops[-1].source.ref == "Files#2"


def test_qualify_names_fields_and_prevents_auto_concatenate():
    app = build("A: LOAD x, y INLINE [x, y\n1, 2];\n"
                "QUALIFY *;\n"
                "UNQUALIFY y;\n"
                "B: LOAD x, y RESIDENT A;\n"
                "Unqualify*;\n"
                "C: LOAD x, y RESIDENT A;\n")
    assert app.tables["B"].fields == ["B.x", "y"]
    assert [s.action for s in app.tables["A"].steps] == [A.CREATE, A.AUTO_CONCATENATE]  # C, unqualified


def test_oracle_source_columns_are_upper_case_and_file_sheets_are_kept():
    doc = read_script("t", {"s.qvs": "LIB CONNECT TO 'Ora';\n"
                                     "T: SQL SELECT t.effective_from, x AS id, y \"Mixed\" FROM v t;\n"
                                     "LEFT JOIN (T) LOAD a AS ID, b FROM ['lib://D/'x.xlsx]\n"
                                     "(ooxml, table is [My Sheet]);\n"})
    app = build_app(doc, connection_dialects={"Ora": "oracle"})
    assert app.tables["T"].fields == ["EFFECTIVE_FROM", "ID", "Mixed", "b"]
    assert ":'lib://D/'x.xlsx#My Sheet" in [s.key for s in app.sources]
    assert "file_path_quoted" in codes(app)
