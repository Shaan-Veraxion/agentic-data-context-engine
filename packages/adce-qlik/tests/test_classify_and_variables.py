from adce_core.bi_ir import StatementKind as K
from adce_qlik.classify import classify
from adce_qlik.splitter import split_script
from adce_qlik.variables import expand


def one(text: str):
    return classify(split_script(text)[0], 0)


def test_label_and_prefixes():
    st = one("Orders:\nLEFT JOIN (Customers) NOCONCATENATE LOAD a RESIDENT T;")
    assert st.label == "Orders"
    assert st.prefixes == ["LEFT JOIN (Customers)", "NOCONCATENATE"]
    assert st.kind == K.LOAD
    assert st.text.startswith("LOAD a")


def test_bracketed_label_and_mapping_prefix():
    st = one("[Region Map]:\nMAPPING LOAD * INLINE [a, b\n1, 2];")
    assert (st.label, st.prefixes, st.kind) == ("Region Map", ["MAPPING"], K.LOAD)


def test_label_after_prefix():
    st = one("LEFT JOIN ([Fx_Murex])\n[Quarterly]:\nLOAD a RESIDENT T;")
    assert (st.label, st.prefixes, st.kind) == ("Quarterly", ["LEFT JOIN ([Fx_Murex])"], K.LOAD)


def test_set_with_colon_in_value_is_not_a_label():
    st = one("SET TimeFormat='hh:mm:ss';")
    assert st.label is None and st.kind == K.SET


def test_kinds():
    cases = {
        "SQL SELECT * FROM t;": K.SQL,
        "SELECT * FROM t;": K.SQL,
        "LIB CONNECT TO 'x';": K.CONNECT,
        "STORE T INTO [lib://a/T.qvd] (qvd);": K.STORE,
        "DROP TABLES A, B;": K.DROP,
        "RENAME FIELD a TO b;": K.RENAME,
        "QUALIFY *;": K.QUALIFY,
        "UNQUALIFY Key;": K.UNQUALIFY,
        "vX = 1;": K.LET,
        "$(Must_Include=lib://Scripts/common.qvs);": K.INCLUDE,
        "CROSSTABLE(Month, Sales) LOAD * RESIDENT T;": K.LOAD,
        "Map Region using RegionMap;": K.MAP_USING,
        "Exit Script;": K.EXIT,
        "something unknown;": K.OTHER,
    }
    for text, kind in cases.items():
        assert one(text).kind == kind, text


def test_prefix_without_load_is_other():
    assert one("NOCONCATENATE DROP TABLE T;").kind == K.OTHER


def test_expand_nested_and_unresolved():
    env = {"a": "$(b)", "b": "2025", "c": None}
    text, unresolved = expand("x >= '$(a)' and y = $(c) and z = $(=Max(D))", env)
    assert text == "x >= '2025' and y = $(c) and z = $(=Max(D))"
    assert unresolved == ["c", "=Max(D)"]
