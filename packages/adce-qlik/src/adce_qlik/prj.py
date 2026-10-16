"""Read a QlikView project folder (`<App>-prj/`): the files QlikView writes next to a .qvw when "Always use project
files" is on.

What the folder holds (QlikView 11/12):
- LoadScript.txt         the load script (read by the intake as a script, like a .qvs)
- DocProperties.xml,     document settings; variables appear as elements with a name and a value
  AllProperties.xml
- <ID>.xml per object    sheets (SH..), charts (CH..), straight and pivot tables, list boxes (LB..), ...; a chart
                         lists its dimensions and its expressions (definition and label)

The XML element names differ between versions and object types, so the reader is tolerant rather than strict:
- a file whose root names a chart or table (…GraphProperties, …TableProperties, ChartProperties) or whose name is
  a chart id (CH01.xml) is a visual; its id is the ObjectId (Document\\CH01 → CH01) or the file name;
- a dimension is an element whose tag ends with "Dimension" (not the "Dimensions" list); its field is the first
  Field / FieldName / Name / Definition inside it, its label the first Label;
- an expression is an element whose tag ends with "ExpressionData" (or is "Expression") that holds a Definition;
  disabled expressions (Enable 0/false) are skipped;
- a variable is an element whose tag contains "Variable" with a Name and a Value / RawValue / Definition inside.
Text values may be wrapped in <v> elements (QlikView writes many strings that way); the whole text of an element
is read. Anything not recognised is reported as an issue, never guessed.

Checked on hand-built samples of this shape only: confirm on the client's first real -prj folder.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

from adce_core.bi_ir import Issue, Visual, VisualDimension, VisualMeasure

CHART_ROOT = re.compile(r"(?i)(graph|chart|straighttable|pivottable|table)properties$")
CHART_FILE = re.compile(r"(?i)^(CH|TB|ST|PT)\d+$")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _first(el: ET.Element, names: tuple[str, ...]) -> ET.Element | None:
    for d in el.iter():
        if d is not el and _local(d.tag).lower() in names:
            return d
    return None


def _issue(code: str, message: str) -> Issue:
    return Issue(statement=None, code=code, message=message)


def is_project_file(name: str) -> bool:
    return any(p.lower().endswith("-prj") for p in PurePosixPath(name.replace("\\", "/")).parts[:-1])


def read_prj(documents: dict[str, str]) -> tuple[list[Visual], dict[str, str], list[Issue]]:
    """documents: file name -> XML text of a -prj folder. Returns visuals (variables unexpanded), variables, issues."""
    visuals: list[Visual] = []
    variables: dict[str, str] = {}
    issues: list[Issue] = []
    for name, text in sorted(documents.items()):
        stem = PurePosixPath(name.replace("\\", "/")).stem
        try:
            root = ET.fromstring(text.lstrip("﻿"))
        except ET.ParseError as e:
            issues.append(_issue("prj_parse_error", f"{name}: {e}"))
            continue
        for el in root.iter():
            tag = _local(el.tag).lower()
            if "variable" in tag and tag != "variables":
                n = _text(_first(el, ("name",)))
                v = _first(el, ("value", "rawvalue", "definition"))
                if n and v is not None:
                    variables[n] = _text(v)
        if CHART_ROOT.search(_local(root.tag)) or CHART_FILE.match(stem):
            visuals.append(_visual(root, stem, name, issues))
    return visuals, variables, issues


def _visual(root: ET.Element, stem: str, name: str, issues: list[Issue]) -> Visual:
    oid = _text(_first(root, ("objectid",)))
    vid = (oid.replace("\\", "/").split("/")[-1] if oid else "") or stem
    v = Visual(id=vid, type=_local(root.tag).removesuffix("Properties").lower() or "chart",
               title=_text(_first(root, ("title", "caption"))))
    for el in root.iter():
        tag = _local(el.tag)
        low = tag.lower()
        if low.endswith("dimension") and low != "dimensions":
            field = _first(el, ("field", "fieldname", "name", "definition"))
            f = _text(field)
            if f:
                label = _text(_first(el, ("label",)))
                v.dimensions.append(VisualDimension(label=label or f, field=f))
        elif (low.endswith("expressiondata") or low == "expression") and _first(el, ("definition",)) is not None:
            enable = _text(_first(el, ("enable", "enabled")))
            if enable.lower() in ("0", "false"):
                continue
            definition = _text(_first(el, ("definition",)))
            if not definition or any(m.expression == definition for m in v.measures):
                continue  # nested ExpressionData elements repeat the same expression
            label = _text(_first(el, ("label",)))
            v.measures.append(VisualMeasure(id=f"{vid}.m{len(v.measures) + 1}", label=label or definition,
                                            expression=definition))
    if not v.dimensions and not v.measures:
        issues.append(_issue("prj_object_not_understood", f"{name}: no dimensions or expressions recognised"))
    return v
