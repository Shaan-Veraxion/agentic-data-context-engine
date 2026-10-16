"""QlikView project folders (-prj): the script from LoadScript.txt, charts and variables from the object XML.
The fixture is synthetic, in the shape of QlikView 11/12 project files."""
from pathlib import Path

from adce_qlik.document import read_script
from adce_qlik.frontend import attach_prj
from adce_qlik.model import build_app
from adce_qlik.prj import is_project_file, read_prj

PRJ = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "qlikview_prj" / "Sales-prj"


def _xml() -> dict[str, str]:
    return {f"Sales-prj/{p.name}": p.read_text(encoding="utf-8") for p in sorted(PRJ.glob("*.xml"))}


def test_charts_and_variables_are_read_from_the_project_xml():
    visuals, variables, issues = read_prj(_xml())
    assert issues == []
    assert variables == {"vLabel": "Sales"}
    (v,) = visuals
    assert (v.id, v.title) == ("CH01", "Sales by region")
    assert [(d.label, d.field) for d in v.dimensions] == [("Region", "Region")]
    assert [(m.label, m.expression) for m in v.measures] == [("Sales this year", "Sum({<Year={$(vYear)}>} Amount)")]


def test_the_app_gets_the_charts_with_the_scripts_variables_expanded():
    doc = read_script("prj", {"LoadScript.txt": (PRJ / "LoadScript.txt").read_text(encoding="utf-8")})
    app = attach_prj(build_app(doc), _xml())
    assert sorted(app.tables) == ["Sales"]
    assert app.visuals[0].measures[0].expression == "Sum({<Year={2026}>} Amount)"


def test_project_files_are_recognised_by_their_folder():
    assert is_project_file("Sales-prj/CH01.xml") and is_project_file("x/Sales-prj/LoadScript.txt")
    assert not is_project_file("objects/CH01.xml")


def test_an_object_that_is_not_understood_is_reported():
    _, _, issues = read_prj({"Sales-prj/CH02.xml": "<GraphProperties><Other/></GraphProperties>",
                             "Sales-prj/broken.xml": "<oops"})
    assert {i.code for i in issues} == {"prj_object_not_understood", "prj_parse_error"}
