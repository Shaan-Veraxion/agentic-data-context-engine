"""Read the front end of a Qlik Sense app: master measures and dimensions, charts and tables, sheets, variables.

Input is the app's object JSON as Qlik produces it: the output of `qlik app unbuild` (measures.json,
dimensions.json, variables.json, objects/*.json), or objects exported through the Engine API (GetProperties).
The reader walks every JSON document and recognises objects by their Qlik properties, so file names and nesting
do not matter:
- qMeasure        a master measure        (qInfo.qId, qMeasure.qLabel, qMeasure.qDef)
- qDim            a master dimension      (qDim.qFieldDefs, qDim.qFieldLabels)
- qHyperCubeDef   a chart or table        (qDimensions, qMeasures, each inline or a qLibraryId reference)
- qType "sheet"   a sheet; its cells name the objects placed on it
- qName + qDefinition  a variable
QlikView documents: their -prj folder XML is read by prj.py (attach_prj below).
"""
from __future__ import annotations

import json
from typing import Any

from adce_core.bi_ir import BIApp, Issue, MasterDimension, MasterMeasure, Visual, VisualDimension, VisualMeasure

from .variables import expand


def _issue(code: str, message: str) -> Issue:
    return Issue(statement=None, code=code, message=message)


def _walk(node: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        out.append(node)
        for v in node.values():
            out += _walk(v)
    elif isinstance(node, list):
        for v in node:
            out += _walk(v)
    return out


def _id(obj: dict[str, Any], fallback: str) -> str:
    info = obj.get("qInfo") or {}
    return str(info.get("qId") or obj.get("id") or fallback)


def _title(obj: dict[str, Any]) -> str:
    t = obj.get("title") or (obj.get("qMetaDef") or {}).get("title") or ""
    if isinstance(t, dict):  # title given as an expression: {"qStringExpression": {"qExpr": "..."}}
        t = (t.get("qStringExpression") or {}).get("qExpr", "")
    return str(t)


def read_frontend(documents: dict[str, str]) -> tuple[list[MasterMeasure], list[MasterDimension], list[Visual],
                                                      dict[str, str], list[Issue]]:
    """documents: file name -> JSON text. Returns measures, dimensions, visuals (unexpanded), variables, issues."""
    measures: dict[str, MasterMeasure] = {}
    dimensions: dict[str, MasterDimension] = {}
    visuals: dict[str, Visual] = {}
    variables: dict[str, str] = {}
    sheet_of: dict[str, str] = {}
    issues: list[Issue] = []
    for name, text in sorted(documents.items()):
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            issues.append(_issue("frontend_parse_error", f"{name}: {e}"))
            continue
        for i, obj in enumerate(_walk(doc)):
            if "qMeasure" in obj and isinstance(obj["qMeasure"], dict):
                m = obj["qMeasure"]
                mid = _id(obj, f"{name}#{i}")
                measures[mid] = MasterMeasure(id=mid, label=str(m.get("qLabel") or _title(obj) or mid),
                                              expression=str(m.get("qDef") or ""),
                                              description=str((obj.get("qMetaDef") or {}).get("description", "")))
            elif "qDim" in obj and isinstance(obj["qDim"], dict):
                d = obj["qDim"]
                did = _id(obj, f"{name}#{i}")
                labels = d.get("qFieldLabels") or []
                dimensions[did] = MasterDimension(id=did, label=str((labels[0] if labels and labels[0] else None)
                                                                    or _title(obj) or did),
                                                  fields=[str(f) for f in d.get("qFieldDefs") or []])
            elif "qHyperCubeDef" in obj and isinstance(obj["qHyperCubeDef"], dict):
                vid = _id(obj, f"{name}#{i}")
                visuals[vid] = _visual(obj, vid)
            elif (obj.get("qInfo") or {}).get("qType") == "sheet":
                for cell in obj.get("cells") or []:
                    if isinstance(cell, dict) and cell.get("name"):
                        sheet_of[str(cell["name"])] = _title(obj) or _id(obj, "")
            elif "qName" in obj and "qDefinition" in obj:
                variables[str(obj["qName"])] = str(obj["qDefinition"])
    for vid, v in visuals.items():
        v.sheet = sheet_of.get(vid)
    return list(measures.values()), list(dimensions.values()), list(visuals.values()), variables, issues


def _visual(obj: dict[str, Any], vid: str) -> Visual:
    hc = obj["qHyperCubeDef"]
    v = Visual(id=vid, type=str((obj.get("qInfo") or {}).get("qType") or obj.get("visualization") or "chart"),
               title=_title(obj))
    for d in hc.get("qDimensions") or []:
        qdef = d.get("qDef") or {}
        fields = qdef.get("qFieldDefs") or []
        labels = qdef.get("qFieldLabels") or []
        field_def = str(fields[0]) if fields else ""
        v.dimensions.append(VisualDimension(label=str((labels[0] if labels and labels[0] else "") or field_def),
                                            field=field_def, library_id=d.get("qLibraryId") or None))
    for k, m in enumerate(hc.get("qMeasures") or []):
        qdef = m.get("qDef") or {}
        v.measures.append(VisualMeasure(id=str(qdef.get("cId") or f"{vid}.m{k + 1}"),
                                        label=str(qdef.get("qLabel") or ""), expression=str(qdef.get("qDef") or ""),
                                        library_id=m.get("qLibraryId") or None))
    return v


def attach_frontend(app: BIApp, documents: dict[str, str]) -> BIApp:
    """Read the front end into the app: library references resolved, $(variables) expanded with the script's
    variables and the app's variables."""
    measures, dimensions, visuals, fe_vars, issues = read_frontend(documents)
    env: dict[str, str | None] = {k: v.value for k, v in app.variables.items()}
    env.update({k: v for k, v in fe_vars.items() if k not in env or env[k] is None})
    lib_m = {m.id: m for m in measures}
    lib_d = {d.id: d for d in dimensions}

    def exp_text(text: str, where: str) -> str:
        out, unresolved = expand(text, env)
        if unresolved:
            issues.append(_issue("unresolved_variable", f"{where}: unresolved {', '.join(unresolved)}"))
        return out

    for m in measures:
        m.expression = exp_text(m.expression, f"measure {m.label}")
    for v in visuals:
        for d in v.dimensions:
            if d.library_id:
                lib = lib_d.get(d.library_id)
                if lib is None:
                    issues.append(_issue("library_item_missing", f"{v.id}: dimension {d.library_id}"))
                    continue
                d.field = lib.fields[0] if lib.fields else d.field
                d.label = d.label or lib.label
            d.field = exp_text(d.field, f"{v.id} dimension")
        for m in v.measures:
            if m.library_id:
                lib_measure = lib_m.get(m.library_id)
                if lib_measure is None:
                    issues.append(_issue("library_item_missing", f"{v.id}: measure {m.library_id}"))
                    continue
                m.expression = lib_measure.expression
                m.label = m.label or lib_measure.label
            else:
                m.expression = exp_text(m.expression, f"{v.id} measure {m.label}")
            m.label = m.label or m.expression
    app.measures, app.dimensions, app.visuals = measures, dimensions, visuals
    app.issues += issues
    return app


def attach_prj(app: BIApp, documents: dict[str, str]) -> BIApp:
    """Read a QlikView -prj folder's objects (prj.read_prj) into the app: charts and their expressions, with
    $(variables) expanded from the script's and the document's variables. Adds to any visuals already read."""
    from .prj import read_prj

    visuals, doc_vars, issues = read_prj(documents)
    env: dict[str, str | None] = {k: v.value for k, v in app.variables.items()}
    env.update({k: v for k, v in doc_vars.items() if k not in env or env[k] is None})
    for v in visuals:
        for d in v.dimensions:
            d.field, unresolved = expand(d.field, env)
            if unresolved:
                issues.append(_issue("unresolved_variable", f"{v.id} dimension: unresolved {', '.join(unresolved)}"))
        for m in v.measures:
            m.expression, unresolved = expand(m.expression, env)
            if unresolved:
                issues.append(_issue("unresolved_variable", f"{v.id} measure {m.label}: unresolved "
                                     f"{', '.join(unresolved)}"))
    app.visuals = [*app.visuals, *visuals]
    app.issues += issues
    return app
