"""Translation of patterns found in real applications (names invented; structure kept), checked by running the
generated SQL in the local venue."""
import pytest
from adce_qlik.document import read_script
from adce_qlik.model import build_app
from adce_reversesql.compose import compose
from adce_reversesql.local import LocalVenue


def translate(script: str, **kw):  # type: ignore[no-untyped-def]
    return compose(build_app(read_script("t", {"s.qvs": script}), **kw))


def rows(t, table: str, venue: LocalVenue | None = None) -> list[tuple]:  # type: ignore[no-untyped-def]
    sql = t.table_sql(table)
    assert sql, t.tables[table].untranslated
    return (venue or LocalVenue()).run(sql, "t").rows


def test_date_parse_month_name_fiscal_year_and_pick():
    # fiscal year from October: 15 Sep 2024 is in 2023-2024, 3 Jan 2025 in 2024-2025
    t = translate(
        "SET MonthNames='Jan;Feb;Mar;Apr;May;Jun;Jul;Aug;Sept;Oct;Nov;Dec';\n"
        "R: LOAD *, Month(D) & '-' & YearName(D, 0, 10) AS MonthFY,\n"
        "  Pick(WildMatch(Month(D) & '', 'Jan', 'Sept'), 'Q2', 'Q4') AS Q,\n"
        "  Date(D, 'DD/MM/YYYY') & '|' AS Shown;\n"
        "LOAD Date#(S, 'DD-MMM-YY') AS D INLINE [S\n15-Sep-24\n03-Jan-25];\n")
    got = sorted(rows(t, "R"), key=lambda r: str(r[0]))
    assert [r[1:] for r in got] == [("Sept-2023-2024", "Q4", "15/09/2024|"), ("Jan-2024-2025", "Q2", "03/01/2025|")]


def test_quarter_bounds_and_match():
    t = translate("R: LOAD Month(QuarterStart(D)) AS QS, Day(QuarterEnd(D)) AS QED, Match(C, 'a', 'b') AS M;\n"
                  "LOAD Date#(S, 'YYYY-MM-DD') AS D, C INLINE [S, C\n2024-05-20, b];\n")
    assert rows(t, "R") == [(4, 30, 2)]


def test_autogenerate_and_recno():
    t = translate("G: LOAD RecNo() * 10 AS n AUTOGENERATE 3;\n")
    assert sorted(r[0] for r in rows(t, "G")) == [10, 20, 30]


def test_qvc_calendar_is_a_generated_input():
    t = translate("CALL Qvc.Calendar (date('2024-10-01','YYYY-MM-dd'), date('2024-10-05','YYYY-MM-dd'), 'CAL', "
                  "'c.');\nINNER JOIN (CAL) LOAD c.Date, 1 AS f RESIDENT CAL;\n")
    r = t.tables["CAL"]
    assert r.inputs and r.inputs[0].startswith("Qvc.Calendar(")
    assert '"__generated__"' in (t.table_sql("CAL") or "")
    assert len(rows(t, "CAL")) == 5


def test_drop_then_reload_and_rename_onto_dropped_name():
    t = translate("T: LOAD * INLINE [k\n1\n2];\nT2: NOCONCATENATE LOAD k, k * 2 AS y RESIDENT T;\nDROP TABLE T;\n"
                  "RENAME TABLE T2 TO T;\nU: NOCONCATENATE LOAD k, y + 1 AS z RESIDENT T;\n")
    assert sorted(rows(t, "U")) == [(1, 3), (2, 5)]
    assert t.tables["T"].complete


def test_oracle_and_sql_server_embedded_sql_become_dremio_sql():
    t = translate("LIB CONNECT TO 'Ora';\nA: SQL SELECT rawtohex(ID) AS ID, NVL(AMT, 0) AS AMT FROM SEC_VW "
                  "WHERE D >= TO_DATE('01-04-2024','DD-MM-YYYY') AND E > SYSDATE;\n"
                  "LIB CONNECT TO 'Ms';\nB: SQL SELECT S.SecID AS 'Security ID' FROM db.dbo.tblSec S WITH (NOLOCK);\n")
    a, b = t.table_sql("A") or "", t.table_sql("B") or ""
    assert "UPPER(TO_HEX(" in a and "TO_DATE('01-04-2024', 'DD-MM-YYYY')" in a and "SYSDATE" not in a
    assert '"Security ID"' in b and "NOLOCK" not in b.upper()


def test_rownum_is_flagged_not_guessed():
    t = translate("LIB CONNECT TO 'Ora';\nA: SQL SELECT ID FROM T WHERE ROWNUM < 10;\n")
    assert t.tables["A"].untranslated and "ROWNUM" in t.tables["A"].untranslated[0].reason


def test_file_metadata_functions_go_to_review():
    t = translate("F: LOAD FileTime('x.csv') AS ft AUTOGENERATE 1;\n")
    u = t.tables["F"].untranslated
    assert u and u[0].tier == "T3"


@pytest.mark.parametrize("fmt,expected", [("DD-MMM-YY", "DD-MON-YY"), ("YYYY-MM-dd", "YYYY-MM-DD"),
                                          ("DD/MM/YYYY hh:mm:ss", "DD/MM/YYYY HH24:MI:SS")])
def test_format_conversion(fmt: str, expected: str):
    from adce_reversesql.formats import qlik_to_dremio
    assert qlik_to_dremio(fmt) == expected


def test_date_parse_with_non_english_month_names_is_not_translated_as_english():
    t = translate("SET MonthNames='janv;févr;mars;avr;mai;juin;juil;août;sept;oct;nov;déc';\n"
                  "R: LOAD Date#(S, 'DD MMM YYYY') AS D INLINE [S\n03 févr 2025];\n")
    [u] = t.tables["R"].untranslated
    assert u.tier == "T2" and "MonthNames" in u.reason
