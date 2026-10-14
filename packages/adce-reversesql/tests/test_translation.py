"""Qlik -> Dremio SQL translation, executed locally on DuckDB to check the semantics row by row."""
import pytest
import sqlglot
from adce_core.bi_ir import Expr
from adce_qlik import read_script
from adce_qlik.evaluate import parse_expr
from adce_qlik.model import build_app
from adce_reversesql.check import check_table
from adce_reversesql.compose import compose
from adce_reversesql.expressions import ExprContext, Translator, Untranslatable
from adce_reversesql.local import LocalVenue


def sql_of(text: str, value: bool = True) -> str:
    e: Expr = parse_expr(text)
    tr = Translator(ExprContext(alias="src", columns=None, mappings={}))
    return (tr.value(e) if value else tr.condition(e)).sql(dialect="dremio")


def rows(script: str, table: str, sources: dict | None = None) -> list[tuple]:
    t = compose(build_app(read_script("t", {"s.qvs": script})))
    assert t.tables[table].complete, t.tables[table].untranslated
    v = LocalVenue()
    for name, (cols, data) in (sources or {}).items():
        v.db.load_rows(name, cols, data)
        v.tables["sql:" + name.replace("__", ".")] = name
    sql = t.table_sql(table)
    assert sql is not None
    res = v.run(f"SELECT * FROM ({sql}) AS q ORDER BY 1, 2", "test")
    return res.rows


def test_expression_semantics():
    assert sql_of("a & b") == (
        "COALESCE(CAST(\"src\".\"a\" AS VARCHAR), '') || COALESCE(CAST(\"src\".\"b\" AS VARCHAR), '')")
    assert "NULLIF" in sql_of("x / y")
    assert sql_of("a > 1") == 'CASE WHEN "src"."a" > 1 THEN -1 ELSE 0 END'
    assert sql_of("a", value=False) == '"src"."a" <> 0'  # non-zero is true in Qlik
    assert sql_of("Date(d, 'YYYY-MM-DD')") == '"src"."d"'  # formatting only
    assert "ILIKE 'A%_'" in sql_of("n LIKE 'A*?'")
    with pytest.raises(Untranslatable):
        sql_of("Peek('x', -1)")


def test_null_concatenation_and_division_match_qlik():
    got = rows("T: LOAD k, a & '-' & b AS ab, x / y AS r INLINE [k, a, b, x, y\n1, p, q, 6, 3\n2, p, , 6, 0];",
               "T")
    assert got == [(1, "p-q", 2.0), (2, "p-", None)]


def test_concatenate_aligns_columns_and_join_merges_keys():
    script = ("A: LOAD k, v INLINE [k, v\n1, a\n2, b];\n"
              "CONCATENATE (A) LOAD k, w INLINE [k, w\n3, c];\n"
              "OUTER JOIN (A) LOAD k, z INLINE [k, z\n2, y\n4, x];\n")
    assert rows(script, "A") == [(1, "a", None, None), (2, "b", None, "y"), (3, None, "c", None),
                                 (4, None, None, "x")]


def test_keep_and_applymap_and_group_by():
    script = ("M: MAPPING LOAD * INLINE [code, name\nN, North\nS, South];\n"
              "O: LOAD id, cust, ApplyMap('M', reg, 'Other') AS region, amt INLINE [id, cust, reg, amt\n"
              "1, 10, N, 5\n2, 10, S, 7\n3, 20, X, 1\n4, 30, N, 2];\n"
              "C: LEFT KEEP (O) LOAD cust, cname INLINE [cust, cname\n10, ann\n20, ben\n99, zed];\n"
              "T: LOAD cust, Sum(amt) AS total, Count(id) AS n RESIDENT O GROUP BY cust;\n")
    assert rows(script, "O") == [(1, 10, "North", 5), (2, 10, "South", 7), (3, 20, "Other", 1), (4, 30, "North", 2)]
    assert rows(script, "C") == [(10, "ann"), (20, "ben")]  # 99 is not in O: removed by LEFT KEEP
    assert rows(script, "T") == [(10, 12, 2), (20, 1, 1), (30, 2, 1)]


def test_embedded_sql_keeps_dremio_paths_and_maps_locally():
    script = ('LIB CONNECT TO \'D\';\nT: LOAD id, Upper(n) AS name WHERE id > 1;\n'
              'SQL SELECT id, n FROM "space"."folder"."tbl";\n')
    t = compose(build_app(read_script("t", {"s.qvs": script})))
    sql = t.table_sql("T") or ""
    assert '"space"."folder"."tbl"' in sql
    sqlglot.parse_one(sql, read="dremio")
    assert check_table(t, "T").ok
    got = rows(script, "T", {"space__folder__tbl": (["id", "n"], [(1, "a"), (2, "b"), (3, "c")])})
    assert got == [(2, "B"), (3, "C")]


def test_untranslatable_statement_is_reported_not_guessed():
    t = compose(build_app(read_script("t", {"s.qvs": "T: LOAD Peek('x', -1) AS p RESIDENT X;\n"
                                                    "X: LOAD 1 AS x AUTOGENERATE 1;\n"})))
    r = t.tables["T"]
    assert not r.complete and r.untranslated and r.untranslated[0].statement == 0
    assert not check_table(t, "T").ok


def test_ai_override_is_spliced_and_marked(tmp_path):
    script = "S: LOAD a INLINE [a\n1\n2];\nT: LOAD RangeSum(a, 1) AS r RESIDENT S;\n"
    app = build_app(read_script("t", {"s.qvs": script}))
    assert not compose(app).tables["T"].complete
    t = compose(app, overrides={1: 'SELECT "a" + 1 AS "r" FROM src'})
    assert t.tables["T"].complete
    assert check_table(t, "T").failed()[0].name == "rules_only"  # AI-assisted: non-blocking, flagged for review
    v = LocalVenue()
    assert v.run(f"SELECT * FROM ({t.table_sql('T')}) q ORDER BY 1", "x").rows == [(2,), (3,)]
