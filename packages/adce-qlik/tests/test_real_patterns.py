"""Script patterns found in real applications (names invented; structure kept)."""
from adce_core.bi_ir import StatementKind as K, TableAction
from adce_qlik.document import read_script
from adce_qlik.model import build_app
from adce_qlik.sql_dialect import detect


def app_of(script: str, **kw):  # type: ignore[no-untyped-def]
    return build_app(read_script("t", {"s.qvs": script}), **kw)


def test_label_after_prefix_is_accepted():
    app = app_of("A: LOAD * INLINE [k, v\n1, x];\n"
                 "NOCONCATENATE\nB:\nLOAD k, v RESIDENT A;\n"
                 "LEFT JOIN (A)\n[Ignored]:\nLOAD k, 1 AS f RESIDENT B;\n")
    assert "B" in app.tables and app.tables["B"].steps[0].action == TableAction.CREATE
    assert [s.action for s in app.tables["A"].steps] == [TableAction.CREATE, TableAction.JOIN]
    assert "Ignored" not in app.tables
    assert not [i for i in app.issues if i.code == "unknown_statement"]


def test_variable_in_join_target():
    app = app_of("LET vT = 'Facts';\n[$(vT)]: LOAD * INLINE [k\n1];\n"
                 "LEFT JOIN ([$(vT)]) LOAD * INLINE [k, x\n1, a];\n")
    assert [s.action for s in app.tables["Facts"].steps] == [TableAction.CREATE, TableAction.JOIN]


def test_label_between_preceding_load_and_sql():
    app = app_of("LIB CONNECT TO 'Ref';\nLOAD NAME AS Customer, ID AS CustomerId;\n[Customers]:\n"
                 "SQL SELECT NAME, ID FROM CUSTOMER_VW WHERE EFFECTIVE_TO IS NULL;\n")
    assert "Customers" in app.tables and app.tables["Customers"].fields == ["Customer", "CustomerId"]


def test_qvc_library_routines_are_modelled():
    doc = read_script("t", {"s.qvs": (
        "$(must_include=lib://Shared\\Script\\Qvc.qvs);\n"
        "CALL Qvc.Calendar (date('2024-10-01','YYYY-MM-dd'), today(), 'TRADE', 'tr.');\n"
        "INNER JOIN (TRADE) LOAD tr.Date, 1 AS Flag RESIDENT TRADE;\n"
        "CALL Qvc.IncrementalSetup ('ACCOUNTS', '', '', -1);\n"
        "[$(Qvc.Loader.v.Tablename)]: SQL SELECT ID, NAME FROM ACCOUNTS_VW;\n"
        "CALL Qvc.IncrementalStore;\n")})
    app = build_app(doc)
    codes = [i.code for i in app.issues]
    assert "include_missing" not in codes and "call_unresolved" not in codes
    assert codes.count("library_call") == 3 and "library_include" in codes
    assert app.tables["TRADE"].steps[0].ops[0].source.kind == "extension"
    assert app.tables["TRADE"].fields[0] == "tr.Date"
    assert app.tables["ACCOUNTS"].stored_to == ["qvc/ACCOUNTS.qvd"]


def test_dropped_name_can_be_loaded_again():
    app = app_of("S: LOAD * INLINE [k\n1];\nTMP: NOCONCATENATE LOAD k, 2 AS x RESIDENT S;\nDROP TABLE S;\n"
                 "S: NOCONCATENATE LOAD k, x RESIDENT TMP;\nDROP TABLE TMP;\n")
    s = app.tables["S"]
    assert s.dropped_at is None and len(s.earlier_drops) == 1 and len(s.steps) == 2
    assert not [i for i in app.issues if i.code == "duplicate_table_name"]


def test_rename_onto_a_dropped_name_keeps_both_histories():
    app = app_of("T: LOAD * INLINE [k\n1];\nT2: NOCONCATENATE LOAD k, 1 AS y RESIDENT T;\nDROP TABLE T;\n"
                 "RENAME TABLE T2 TO T;\n")
    assert app.tables["T"].renamed_from == ["T2"] and app.tables["T"].fields == ["k", "y"]
    old = app.tables["T~1"]
    assert old.qlik_name == "T" and old.dropped_at is not None
    assert app.tables["T"].name_at(1) == "T2" and old.name_at(0) == "T"


def test_sql_dialect_detection():
    assert detect("SELECT rawtohex(ID) FROM X WHERE D > SYSDATE", "TITAN")[0] == "oracle"
    assert detect("SELECT a FROM db.dbo.t", "Src")[0] == "tsql"
    assert detect("SELECT a FROM t", "Dremio_Dev")[0] == "dremio"
    assert detect("SELECT a FROM t", "Src", configured={"src": "postgres"})[0] == "postgres"
    app = app_of("LIB CONNECT TO 'Ora';\nA: SQL SELECT NVL(a, 0) AS a FROM T1;\nB: SQL SELECT b FROM T2;\n")
    assert app.connection_dialects == {"Ora": "oracle"}
    assert app.tables["B"].steps[0].ops[0].source.dialect == "oracle"  # learned from the connection


def test_statements_classified():
    doc = read_script("t", {"s.qvs": "NOCONCATENATE X: LOAD 1 AS a AUTOGENERATE 1;\n"})
    assert doc.statements[0].kind == K.LOAD and doc.statements[0].label == "X"
