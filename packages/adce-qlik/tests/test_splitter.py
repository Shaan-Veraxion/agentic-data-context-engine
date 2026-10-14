from adce_qlik.splitter import blank_comments, split_script


def texts(script: str) -> list[str]:
    return [s.text for s in split_script(script)]


def test_semicolons_inside_strings_brackets_and_comments_do_not_split():
    script = (
        "LOAD 'a;b' AS x, [we;ird] AS y FROM [lib://Data/f;1.qvd] (qvd);\n"
        "// a; comment\n"
        "/* block; comment */ SET z = \"q;\";\n"
    )
    assert texts(script) == [
        "LOAD 'a;b' AS x, [we;ird] AS y FROM [lib://Data/f;1.qvd] (qvd)",
        'SET z = "q;"',
    ]


def test_lib_double_slash_is_not_a_comment():
    assert texts("STORE T INTO lib://QVD/T.qvd (qvd);") == ["STORE T INTO lib://QVD/T.qvd (qvd)"]


def test_doubled_quotes_are_escapes():
    assert texts("SET a = 'it''s; fine';LOAD 1 AUTOGENERATE 1;") == ["SET a = 'it''s; fine'", "LOAD 1 AUTOGENERATE 1"]


def test_control_statements_end_at_end_of_line():
    script = "IF 1 = 1 THEN\n  TRACE hi;\nELSE\n  TRACE no;\nEND IF\nFOR i = 1 TO 3\nNEXT i\n"
    assert texts(script) == ["IF 1 = 1 THEN", "TRACE hi", "ELSE", "TRACE no", "END IF", "FOR i = 1 TO 3", "NEXT i"]


def test_label_word_starting_with_control_keyword_is_not_control():
    # "Forecast:" must not be treated as a FOR statement ending at the line break
    assert texts("Forecast:\nLOAD a\nRESIDENT T;") == ["Forecast:\nLOAD a\nRESIDENT T"]


def test_rem_comment_statement_is_skipped():
    assert texts("REM this is ignored;\nLOAD 1 AUTOGENERATE 1;") == ["LOAD 1 AUTOGENERATE 1"]


def test_dollar_expansion_with_semicolon_inside():
    assert texts("LET x = $(=Concat(F, ';'));DROP TABLE T;") == ["LET x = $(=Concat(F, ';'))", "DROP TABLE T"]


def test_unterminated_last_statement_is_kept_and_flagged():
    stmts = split_script("DROP TABLE A;\nDROP TABLE B")
    assert [s.terminated for s in stmts] == [True, False]


def test_spans_are_exact():
    script = "SET a=1;\n\nOrders:\nLOAD x\nRESIDENT T;\n"
    stmts = split_script(script, file="main.qvs")
    second = stmts[1]
    assert (second.span.start_line, second.span.end_line) == (3, 5)
    assert script[second.span.start_offset:second.span.end_offset] == "Orders:\nLOAD x\nRESIDENT T"
    assert second.span.file == "main.qvs"


def test_blank_comments_preserves_length_and_newlines():
    s = "a // c\n/* x\ny */ b"
    out = blank_comments(s)
    assert len(out) == len(s) and out.count("\n") == s.count("\n")
    assert out.split() == ["a", "b"]
