"""Variable resolution: SET/LET in script order, $(…) expansion, and values recovered from reload logs.

Limits of this first version (P0): branches of IF/SWITCH are not evaluated (the last assignment in text order wins),
LET is evaluated only for literals and '&' concatenation, and $(=expr) / $(name(args)) stay unresolved. Unresolved
values are recovered by aligning the script statement with its expanded echo in the reload log.
"""
from __future__ import annotations

import re

from adce_core.bi_ir import Statement, StatementKind as K, Variable, VariableOrigin

from .reload_log import ReloadLog, strip_label

ASSIGN = re.compile(r"(?is)^(?:(set|let)\s+)?(?P<name>[^\s=]+)\s*=\s*(?P<rhs>.*)$")
REF = re.compile(r"\$\((?P<name>[A-Za-z_][\w.]*)\)")
ANY_DOLLAR = re.compile(r"\$\((?P<body>[^()]*(?:\([^()]*\)[^()]*)*)\)")
MAX_DEPTH = 10


def expand(text: str, env: dict[str, str | None]) -> tuple[str, list[str]]:
    """Expand $(name) references. Returns the expanded text and names that could not be expanded."""
    unresolved: list[str] = []
    for _ in range(MAX_DEPTH):
        changed = False

        def sub(m: re.Match[str]) -> str:
            nonlocal changed
            v = env.get(m.group("name"))
            if v is None:
                return m.group(0)
            changed = True
            return v

        text = REF.sub(sub, text)
        if not changed:
            break
    for m in ANY_DOLLAR.finditer(text):
        name = m.group("body")
        if name not in unresolved:
            unresolved.append(name)
    return text, unresolved


def _eval_let(rhs: str) -> str | None:
    """Evaluate literal LET expressions: 'text', numbers, and '&' concatenations of those."""
    parts = re.split(r"\s*&\s*", rhs.strip())
    out = []
    for p in parts:
        if re.fullmatch(r"'(?:[^']|'')*'", p):
            out.append(p[1:-1].replace("''", "'"))
        elif re.fullmatch(r"-?\d+(?:\.\d+)?", p):
            out.append(p)
        else:
            return None
    return "".join(out)


def _strip_set(rhs: str) -> str:
    rhs = rhs.strip()
    if len(rhs) >= 2 and rhs[0] == rhs[-1] == "'":
        return rhs[1:-1].replace("''", "'")
    return rhs


def resolve(statements: list[Statement], log: ReloadLog | None = None) -> dict[str, Variable]:
    """Walk statements in order, record variables, and fill expanded_text / unresolved_variables in place."""
    env: dict[str, str | None] = {}
    variables: dict[str, Variable] = {}
    for st in statements:
        expanded, unresolved = expand(st.text, env)
        if unresolved and log is not None and log.echo:
            recovered = _recover_from_log(expanded, _candidates(st, log))
            for name, value in recovered.items():
                env[name] = value
                variables[name] = Variable(name=name, value=value, origin=VariableOrigin.RELOAD_LOG, span=st.span,
                                           raw=variables[name].raw if name in variables else None)
            if recovered:
                expanded, unresolved = expand(st.text, env)
        st.expanded_text = expanded if expanded != st.text else None
        st.unresolved_variables = unresolved
        if st.kind in (K.SET, K.LET):
            m = ASSIGN.match(expanded)
            if not m:
                continue
            name, rhs = m.group("name"), m.group("rhs")
            if st.kind == K.SET:
                value, origin = _strip_set(rhs), VariableOrigin.SCRIPT_SET
            else:
                value, origin = _eval_let(rhs), VariableOrigin.SCRIPT_LET
            env[name] = value
            variables[name] = Variable(name=name, value=value, origin=origin, raw=rhs.strip(), span=st.span)
    return variables


def _candidates(st: Statement, log: ReloadLog) -> list[str]:
    """Logged (expanded) texts that may correspond to this statement: by script line range, else every echo line."""
    t = log.text_for_lines(st.span.start_line, st.span.end_line)
    texts = [t] if t else [e.text for e in log.echo]
    return [_norm(strip_label(x)) for x in texts]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().rstrip(";").strip()


def _recover_from_log(template: str, logged_norm: list[str]) -> dict[str, str]:
    """Align a statement that still contains $(name) with an expanded statement from the reload log."""
    t = _norm(template)
    names: list[str] = []
    pattern = ""
    pos = 0
    for m in REF.finditer(t):
        pattern += re.escape(t[pos:m.start()])
        name = m.group("name")
        if name in names:
            pattern += f"(?P={_group(name)})"
        else:
            names.append(name)
            pattern += f"(?P<{_group(name)}>.+?)"
        pos = m.end()
    if not names:
        return {}
    pattern += re.escape(t[pos:])
    rx = re.compile(pattern.replace(r"\ ", r"\s+"), re.IGNORECASE)
    for line in logged_norm:
        m = rx.fullmatch(line)
        if m:
            return {n: m.group(_group(n)) for n in names}
    return {}


def _group(name: str) -> str:
    return "v_" + re.sub(r"\W", "_", name)
