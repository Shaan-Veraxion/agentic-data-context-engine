import pytest
from adce_qlik.load_parser import LoadParseError, parse_load


def test_fields_aliases_and_unaliased_expression_names():
    p = parse_load("LOAD a, b AS [B Field], Year(d), x * 2 AS y RESIDENT T")
    assert [f.name for f in p.fields] == ["a", "B Field", "Year(d)", "y"]
    assert p.source.kind == "resident" and p.source.ref == "T"


def test_keywords_usable_as_field_names():
    p = parse_load("LOAD Total, Order, Group, Sum(TOTAL Amount) AS t RESIDENT T GROUP BY Total, Order, Group")
    assert [f.name for f in p.fields] == ["Total", "Order", "Group", "t"]
    assert p.fields[3].expr is not None and p.fields[3].expr.modifier == "TOTAL"
    assert [g.text for g in p.group_by] == ["Total", "Order", "Group"]


def test_expression_ast_precedence():
    e = parse_load("LOAD a + b * c & 'x' = 'y' AND NOT d AS z AUTOGENERATE 1").fields[0].expr
    assert e is not None and e.kind == "binop" and e.name == "AND"
    cmp_ = e.args[0]
    assert cmp_.name == "=" and cmp_.args[0].name == "&"
    assert cmp_.args[0].args[0].name == "+" and cmp_.args[0].args[0].args[1].name == "*"
    assert e.args[1].kind == "unop" and e.args[1].name == "NOT"
    assert e.fields() == ["a", "b", "c", "d"]


def test_sources():
    assert parse_load("LOAD * FROM [lib://QVD/Orders.qvd] (qvd)").source.kind == "qvd"
    f = parse_load("LOAD a FROM lib://Data/x.csv (txt, utf8, embedded labels, delimiter is ',')").source
    assert (f.kind, f.ref, f.format) == ("file", "lib://Data/x.csv", "txt, utf8, embedded labels, delimiter is ','")
    inline = parse_load("LOAD * INLINE [\nK, V\n1, one\n2, two\n]").source
    assert inline.columns == ["K", "V"] and inline.inline_rows == [["1", "one"], ["2", "two"]]
    quoted = parse_load("LOAD * INLINE [\n\"QMTM Q1 (LCY1)\", 'b, c'\n0, 'it''s'\n1,\n]").source
    assert quoted.columns == ["QMTM Q1 (LCY1)", "b, c"] and quoted.inline_rows == [["0", "it's"], ["1", ""]]
    assert parse_load("LOAD a WHERE a > 1").source.kind == "preceding"


def test_count_star_distinct_and_strings():
    p = parse_load("LOAD Count(*) AS n, Count(DISTINCT 'it''s') AS m, \"Odd \"\"Q\"\"\" RESIDENT T")
    assert p.fields[0].expr is not None and p.fields[0].expr.args[0].kind == "star"
    assert p.fields[1].expr is not None and p.fields[1].expr.args[0].value == "it's"
    assert p.fields[2].name == 'Odd "Q"'


def test_where_and_while():
    assert parse_load("LOAD a RESIDENT T WHERE a LIKE 'x*' OR b <> 2").where is not None
    assert parse_load("LOAD IterNo() AS i AUTOGENERATE 1 WHILE IterNo() <= 3").while_ is not None


def test_parse_error_is_reported():
    with pytest.raises(LoadParseError):
        parse_load("LOAD a,, b RESIDENT T")
