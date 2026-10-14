from adce_core.bi_ir import StatementKind as K
from adce_qlik import read_script
from adce_qlik.evaluate import NotEvaluable, evaluate_text
from adce_qlik.model import build_app


def run(files: dict[str, str]):
    doc = read_script("t", files)
    return doc, build_app(doc)


def labels(doc) -> list[str]:
    return [s.label or "" for s in doc.executed if s.kind in (K.LOAD, K.SQL)]


def codes(doc) -> list[str]:
    return [i.code for i in doc.issues]


def test_evaluator():
    assert evaluate_text("'2026-10-01' >= '2026-01-01'") == -1
    assert evaluate_text("1 + 2 * 3") == 7
    assert evaluate_text("'a' & 1") == "a1"
    assert evaluate_text("NOT (1 = 2) AND Len('abc') = 3") == -1
    try:
        evaluate_text("Year(Today()) > 2020")
        raise AssertionError("should not evaluate")
    except NotEvaluable:
        pass


def test_include_is_spliced_with_its_own_spans():
    doc, app = run({"main.qvs": "SET vX = 1;\n$(Include=lib://Scripts/common.qvs);\nB: LOAD 2 AS b AUTOGENERATE 1;\n",
                    "common.qvs": "A: LOAD $(vX) AS a AUTOGENERATE 1;\n"})
    assert labels(doc) == ["A", "B"]
    a = next(s for s in doc.executed if s.label == "A")
    assert a.span.file == "common.qvs" and a.expanded_text == "LOAD 1 AS a AUTOGENERATE 1"
    assert doc.files == ["main.qvs", "common.qvs"] and set(app.tables) == {"A", "B"}


def test_missing_include_is_flagged():
    doc, _ = run({"main.qvs": "$(Must_Include=lib://x/missing.qvs);\n"})
    assert "include_missing" in codes(doc)


def test_if_branches():
    doc, _ = run({"s.qvs": "SET v = 2;\nIF $(v) = 1 THEN\n  A: LOAD 1 AS a AUTOGENERATE 1;\n"
                           "ELSEIF $(v) = 2 THEN\n  B: LOAD 1 AS b AUTOGENERATE 1;\nELSE\n"
                           "  C: LOAD 1 AS c AUTOGENERATE 1;\nEND IF\n"})
    assert labels(doc) == ["B"]
    b = next(s for s in doc.executed if s.label == "B")
    assert b.context == ["ELSEIF 2 = 2 THEN (true)"]


def test_unevaluable_condition_keeps_every_branch_and_flags():
    doc, _ = run({"s.qvs": "IF Year(Today()) > 2020 THEN\n  A: LOAD 1 AS a AUTOGENERATE 1;\nELSE\n"
                           "  B: LOAD 1 AS b AUTOGENERATE 1;\nEND IF\n"})
    assert labels(doc) == ["A", "B"] and "branch_not_evaluated" in codes(doc)


def test_for_each_and_step_loops():
    doc, _ = run({"s.qvs": "FOR EACH vT IN 'North', 'South'\n  [$(vT)]: LOAD '$(vT)' AS r AUTOGENERATE 1;\nNEXT vT\n"
                           "FOR i = 10 TO 0 STEP -5\n  T: LOAD $(i) AS n AUTOGENERATE 1;\nNEXT\n"})
    assert labels(doc) == ["North", "South", "T", "T", "T"]
    assert [s.expanded_text for s in doc.executed][-1] == "LOAD 0 AS n AUTOGENERATE 1"


def test_loop_over_data_is_modelled_once_and_flagged():
    doc, _ = run({"s.qvs": "FOR EACH f IN FileList('lib://Data/*.csv')\n  T: LOAD * FROM [$(f)] (txt);\nNEXT f\n"})
    assert labels(doc) == ["T"] and "loop_not_unrolled" in codes(doc)


def test_sub_parameters_and_switch():
    doc, _ = run({"s.qvs": "SUB Make(name, val)\n  [$(name)]: LOAD $(val) AS v AUTOGENERATE 1;\nEND SUB\n"
                           "CALL Make('One', 1);\nCALL Make('Two', 2);\n"
                           "SET m = 'b';\nSWITCH '$(m)'\nCASE 'a'\n  A: LOAD 1 AS x AUTOGENERATE 1;\n"
                           "CASE 'b', 'c'\n  B: LOAD 1 AS x AUTOGENERATE 1;\nDEFAULT\n"
                           "  D: LOAD 1 AS x AUTOGENERATE 1;\nEND SWITCH\n"})
    assert labels(doc) == ["One", "Two", "B"]
    assert [s.expanded_text for s in doc.executed[:2]] == ["LOAD 1 AS v AUTOGENERATE 1", "LOAD 2 AS v AUTOGENERATE 1"]


def test_exit_script_stops_execution():
    doc, _ = run({"s.qvs": "A: LOAD 1 AS a AUTOGENERATE 1;\nEXIT SCRIPT;\nB: LOAD 1 AS b AUTOGENERATE 1;\n"})
    assert labels(doc) == ["A"] and doc.coverage.total == 3


def test_unbalanced_control_is_reported():
    doc, _ = run({"s.qvs": "FOR i = 1 TO 2\n  T: LOAD 1 AS a AUTOGENERATE 1;\n"})
    assert "unbalanced_control" in codes(doc)
