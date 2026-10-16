"""Run a classified script's control flow statically: the 'executed' statement stream the BI IR is built from.

- $(Include=…) / $(Must_Include=…): the named file is split, classified and spliced in (spans keep its own name)
- SUB … END SUB: recorded; the body runs at each CALL, with parameters bound as variables
- FOR i = a TO b [STEP s] and FOR EACH v IN list: unrolled when the bounds/items are constants (max 200 iterations)
- IF / ELSEIF / ELSE and SWITCH / CASE: the condition is evaluated when it is constant after expansion; otherwise
  every branch is kept, tagged with its condition, and flagged branch_not_evaluated
- loops whose items are not constant (FOR EACH f IN FileList(...), bounds from data) are unrolled with the values
  the reload log shows, one per echoed iteration (flagged loop_unrolled_from_log)
- DO … LOOP: the body runs once and is flagged loop_not_unrolled
- EXIT SCRIPT: stops execution (when reached unconditionally)
Variables are expanded statement by statement; values Qlik computes at reload time are recovered from the log.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from adce_core.bi_ir import Issue, Statement, StatementKind as K, Variable, VariableOrigin

from .classify import classify
from .evaluate import NotEvaluable, as_text, evaluate_text, truthy
from .libraries import library_call, library_of_include
from .reload_log import ReloadLog, strip_label
from .splitter import RawStatement, split_script
from .variables import ASSIGN, _candidates, _eval_let, _norm, _recover_from_log, _strip_set, expand

MAX_ITER = 200
MAX_INCLUDE_DEPTH = 10
INCLUDE_RX = re.compile(r"(?is)^\$\(\s*(must_)?include\s*=\s*(.+?)\s*\)\s*$")
FOR_RX = re.compile(r"(?is)^for\s+([\w.]+)\s*=\s*(.+?)\s+to\s+(.+?)(?:\s+step\s+(.+))?$")
FOR_EACH_RX = re.compile(r"(?is)^for\s+each\s+([\w.]+)\s+in\s+(.+)$")
IF_RX = re.compile(r"(?is)^(?:else)?if\s+(.+?)\s+then\s*$")
SUB_RX = re.compile(r"(?is)^sub\s+([\w.]+)\s*(?:\((.*)\))?\s*$")
CALL_RX = re.compile(r"(?is)^call\s+([\w.]+)\s*(?:\((.*)\))?\s*$")
SWITCH_RX = re.compile(r"(?is)^switch\s+(.+)$")
CASE_RX = re.compile(r"(?is)^case\s+(.+)$")


# ---------------------------------------------------------------------- structure
@dataclass
class Simple:
    st: Statement


@dataclass
class Branch:
    head: Statement | None  # None for ELSE / DEFAULT
    body: list[Node]


@dataclass
class IfNode:
    branches: list[Branch]
    switch: Statement | None = None  # SWITCH header when this came from SWITCH/CASE


@dataclass
class LoopNode:
    head: Statement
    body: list[Node]


@dataclass
class SubNode:
    head: Statement
    body: list[Node]


Node = Simple | IfNode | LoopNode | SubNode


@dataclass
class _Parser:
    stmts: list[Statement]
    issues: list[Issue]
    i: int = 0

    def block(self, until: set[K]) -> list[Node]:
        out: list[Node] = []
        while self.i < len(self.stmts):
            st = self.stmts[self.i]
            if st.kind in until:
                return out
            self.i += 1
            if st.kind == K.IF:
                out.append(self._if(st))
            elif st.kind == K.SWITCH:
                out.append(self._switch(st))
            elif st.kind in (K.FOR, K.DO):
                body = self.block({K.NEXT if st.kind == K.FOR else K.LOOP})
                self._expect(st, K.NEXT if st.kind == K.FOR else K.LOOP)
                out.append(LoopNode(st, body))
            elif st.kind == K.SUB:
                body = self.block({K.END_SUB})
                self._expect(st, K.END_SUB)
                out.append(SubNode(st, body))
            elif st.kind in (K.NEXT, K.LOOP, K.END_IF, K.END_SUB, K.END_SWITCH, K.ELSE, K.ELSEIF, K.CASE, K.DEFAULT):
                self.issues.append(Issue(statement=st.index, code="unbalanced_control",
                                         message=f"{st.kind.value} without opener", span=st.span))
            else:
                out.append(Simple(st))
        return out

    def _expect(self, opener: Statement, kind: K) -> None:
        if self.i < len(self.stmts) and self.stmts[self.i].kind == kind:
            self.i += 1
        else:
            self.issues.append(Issue(statement=opener.index, code="unbalanced_control",
                                     message=f"{opener.kind.value} without {kind.value}", span=opener.span))

    def _if(self, head: Statement) -> IfNode:
        branches = [Branch(head, self.block({K.ELSEIF, K.ELSE, K.END_IF}))]
        while self.i < len(self.stmts) and self.stmts[self.i].kind in (K.ELSEIF, K.ELSE):
            st = self.stmts[self.i]
            self.i += 1
            branches.append(Branch(st if st.kind == K.ELSEIF else None, self.block({K.ELSEIF, K.ELSE, K.END_IF})))
        self._expect(head, K.END_IF)
        return IfNode(branches)

    def _switch(self, head: Statement) -> IfNode:
        branches: list[Branch] = []
        self.block({K.CASE, K.DEFAULT, K.END_SWITCH})  # anything before the first CASE is ignored by Qlik
        while self.i < len(self.stmts) and self.stmts[self.i].kind in (K.CASE, K.DEFAULT):
            st = self.stmts[self.i]
            self.i += 1
            branches.append(Branch(st if st.kind == K.CASE else None, self.block({K.CASE, K.DEFAULT, K.END_SWITCH})))
        self._expect(head, K.END_SWITCH)
        return IfNode(branches, switch=head)


# ---------------------------------------------------------------------- includes
def splice_includes(statements: list[Statement], files: dict[str, str], issues: list[Issue],
                    env: dict[str, str | None], depth: int = 0) -> list[Statement]:
    out: list[Statement] = []
    by_name = {PurePosixPath(n.replace("\\", "/")).name.lower(): n for n in files}
    for st in statements:
        if st.kind in (K.SET, K.LET):
            m = ASSIGN.match(expand(st.text, env)[0])
            if m:
                env[m.group("name")] = _strip_set(m.group("rhs")) if st.kind == K.SET else _eval_let(m.group("rhs"))
        if st.kind != K.INCLUDE:
            out.append(st)
            continue
        m = INCLUDE_RX.match(expand(st.text, env)[0])
        target = m.group(2).strip().strip("'\"[]") if m else ""
        name = by_name.get(PurePosixPath(target.replace("\\", "/")).name.lower()) if target else None
        if name is None and (lib := library_of_include(target)):
            issues.append(Issue(statement=st.index, code="library_include", span=st.span,
                                message=f"{target!r} is a known library ({lib}); its routines are modelled"))
            out.append(st)
            continue
        if name is None or depth >= MAX_INCLUDE_DEPTH:
            code = "include_missing" if name is None else "include_too_deep"
            issues.append(Issue(statement=st.index, code=code, span=st.span,
                                message=f"{'Must_Include' if m and m.group(1) else 'Include'} {target!r} not provided"))
            out.append(st)
            continue
        inner = [classify(r, 0) for r in split_script(files[name], file=name)]
        out.extend(splice_includes(inner, files, issues, env, depth + 1))
    for i, st in enumerate(out):
        st.index = i
    return out


# ---------------------------------------------------------------------- execution
@dataclass
class _Runner:
    log: ReloadLog | None
    issues: list[Issue]
    env: dict[str, str | None] = field(default_factory=dict)
    variables: dict[str, Variable] = field(default_factory=dict)
    subs: dict[str, SubNode] = field(default_factory=dict)
    out: list[Statement] = field(default_factory=list)
    stopped: bool = False
    call_depth: int = 0

    def run(self, nodes: list[Node], context: list[str], conditional: bool = False) -> None:
        for n in nodes:
            if self.stopped:
                return
            if isinstance(n, Simple):
                self._simple(n.st, context, conditional)
            elif isinstance(n, SubNode):
                m = SUB_RX.match(n.head.text)
                if m:
                    self.subs[m.group(1).lower()] = n
            elif isinstance(n, LoopNode):
                self._loop(n, context, conditional)
            else:
                self._branches(n, context, conditional)

    def _expanded(self, st: Statement) -> tuple[str, list[str]]:
        text, unresolved = expand(st.text, self.env)
        if unresolved and self.log is not None and self.log.echo:
            for name, value in _recover_from_log(text, _candidates(st, self.log)).items():
                self.env[name] = value
                self.variables[name] = Variable(name=name, value=value, origin=VariableOrigin.RELOAD_LOG,
                                                span=st.span, raw=self.variables[name].raw
                                                if name in self.variables else None)
            text, unresolved = expand(st.text, self.env)
        return text, unresolved

    def _simple(self, st: Statement, context: list[str], conditional: bool) -> None:
        text, unresolved = self._expanded(st)
        if st.kind == K.CALL:
            self._call(st, text, context, conditional)
            return
        if st.kind == K.EXIT and re.match(r"(?i)exit\s+script", text):
            if conditional:
                self.issues.append(Issue(statement=st.index, code="exit_in_branch", span=st.span,
                                         message="EXIT SCRIPT inside an unevaluated branch"))
            else:
                self.stopped = True
            return
        label = expand(st.label, self.env)[0].strip('[]"') if st.label else None
        prefixes = [expand(p, self.env)[0] for p in st.prefixes]
        ex = st.model_copy(update={"index": len(self.out), "source_index": st.index, "context": list(context),
                                   "expanded_text": text if text != st.text else None, "label": label,
                                   "prefixes": prefixes, "unresolved_variables": unresolved}, deep=True)
        self.out.append(ex)
        if st.kind in (K.SET, K.LET):
            m = ASSIGN.match(text)
            if m:
                name, rhs = m.group("name"), m.group("rhs")
                value = _strip_set(rhs) if st.kind == K.SET else _eval_let(rhs)
                if value is None and st.kind == K.LET:
                    try:
                        value = as_text(evaluate_text(rhs))
                    except NotEvaluable:
                        value = None
                if value is None and name in self.env and self.env[name] is not None:
                    value = self.env[name]  # keep a value recovered from the reload log or given by a reviewer
                self.env[name] = value
                origin = VariableOrigin.SCRIPT_SET if st.kind == K.SET else VariableOrigin.SCRIPT_LET
                prev = self.variables.get(name)
                if not (value is not None and prev is not None
                        and prev.origin in (VariableOrigin.RELOAD_LOG, VariableOrigin.REVIEWER)
                        and prev.value == value):
                    self.variables[name] = Variable(name=name, value=value, origin=origin, raw=rhs.strip(),
                                                    span=st.span)

    def _call(self, st: Statement, text: str, context: list[str], conditional: bool) -> None:
        m = CALL_RX.match(text)
        sub = self.subs.get(m.group(1).lower()) if m else None
        if sub is None and m and (lib := library_call(m.group(1), _split_args(m.group(2) or ""))):
            self.issues.append(Issue(statement=st.index, code="library_call", span=st.span,
                                     message=lib.note))
            ctx = [*context, f"CALL {m.group(1)} (library model)"]
            for text_ in lib.statements:
                raw = RawStatement(text=text_, raw=text_, span=st.span, terminated=True)
                self._simple(classify(raw, st.index), ctx, conditional)
            return
        if sub is None or self.call_depth >= 20:
            self.issues.append(Issue(statement=st.index, code="call_unresolved", span=st.span,
                                     message=f"CALL target not defined before the call: {text[:80]}"))
            return
        hm = SUB_RX.match(sub.head.text)
        params = [p.strip() for p in (hm.group(2) or "").split(",") if p.strip()] if hm else []
        args = _split_args(m.group(2) or "") if m else []
        saved = {p: self.env.get(p) for p in params}
        for p, a in zip(params, args, strict=False):
            try:
                self.env[p] = as_text(evaluate_text(a))
            except NotEvaluable:
                self.env[p] = None
                self.issues.append(Issue(statement=st.index, code="argument_not_evaluated", span=st.span,
                                         message=f"argument {p}={a!r} is not a constant"))
        self.call_depth += 1
        self.run(sub.body, [*context, f"CALL {m.group(1) if m else '?'}({', '.join(args)})"], conditional)
        self.call_depth -= 1
        self.env.update(saved)

    def _loop(self, n: LoopNode, context: list[str], conditional: bool) -> None:
        text, _ = self._expanded(n.head)
        items: list[str] | None = None
        var = None
        if n.head.kind == K.FOR:
            if m := FOR_EACH_RX.match(text):
                var = m.group(1)
                try:
                    items = [as_text(evaluate_text(a)) for a in _split_args(m.group(2))]
                except NotEvaluable:
                    items = None
            elif m := FOR_RX.match(text):
                var = m.group(1)
                try:
                    a, b = float(str(evaluate_text(m.group(2)))), float(str(evaluate_text(m.group(3))))
                    step = float(str(evaluate_text(m.group(4)))) if m.group(4) else 1.0
                    if step == 0:
                        raise NotEvaluable("step 0")
                    items, x = [], a
                    while (x <= b if step > 0 else x >= b) and len(items) <= MAX_ITER:
                        items.append(as_text(x))
                        x += step
                except (NotEvaluable, ValueError):
                    items = None
        if items is None and var is not None and self.log is not None and self.log.echo:
            items = self._items_from_log(n, var)  # e.g. FOR EACH f IN FileList(...): the log shows each iteration
            if items:
                self.issues.append(Issue(statement=n.head.index, code="loop_unrolled_from_log", span=n.head.span,
                                         message=f"loop unrolled with the {len(items)} values of {var} the reload log "
                                                 f"shows: {', '.join(items[:5])}{' ...' if len(items) > 5 else ''}"))
        if items is None or var is None or len(items) > MAX_ITER:
            self.issues.append(Issue(statement=n.head.index, code="loop_not_unrolled", span=n.head.span,
                                     message=f"loop body modelled once: {text[:80]}"))
            self.run(n.body, [*context, f"{text.splitlines()[0][:60]} (once, not unrolled)"], conditional)
            return
        saved = self.env.get(var)
        for it in items:
            self.env[var] = it
            self.run(n.body, [*context, f"{text.splitlines()[0][:60]} ({var}={it})"], conditional)
            if self.stopped:
                break
        self.env[var] = saved

    def _items_from_log(self, n: LoopNode, var: str) -> list[str] | None:
        """The values a loop variable took, read from the reload log: Qlik echoes the loop's body once per
        iteration with the variable expanded, so each echo of a body statement that uses $(var) gives one value
        (in the order the iterations ran). None when the log shows none."""
        assert self.log is not None
        target = next((b.st for b in n.body if isinstance(b, Simple) and f"$({var})" in b.st.text), None)
        if target is None:
            return None
        start, end = target.span.start_line, target.span.end_line
        runs: list[list[str]] = []  # one run of echo lines per iteration
        for e in self.log.echo:
            if not start <= e.line_no <= end:
                continue
            if e.line_no == start or not runs:
                runs.append([])
            runs[-1].append(e.text)
        others = {k: v for k, v in self.env.items() if k != var}
        template, _ = expand(target.text, others)
        values: list[str] = []
        for run in runs:
            got = _recover_from_log(template, [_norm(strip_label("\n".join(run)))]).get(var)
            if got is not None and got not in values:
                values.append(got)
        return values or None

    def _branches(self, n: IfNode, context: list[str], conditional: bool) -> None:
        subject = None
        if n.switch is not None:
            sm = SWITCH_RX.match(self._expanded(n.switch)[0])
            subject = sm.group(1) if sm else None
        for b in n.branches:
            if b.head is None:  # ELSE / DEFAULT: reached only when no earlier condition was true
                self.run(b.body, [*context, "ELSE" if n.switch is None else "DEFAULT"], conditional)
                return
            text = self._expanded(b.head)[0]
            cond = _condition(text, subject)
            try:
                if cond is None:
                    raise NotEvaluable(text)
                if truthy(evaluate_text(cond)):
                    self.run(b.body, [*context, f"{text[:60]} (true)"], conditional)
                    return
            except NotEvaluable:
                self.issues.append(Issue(statement=b.head.index, code="branch_not_evaluated", span=b.head.span,
                                         message=f"condition not constant, branch kept: {text[:80]}"))
                self.run(b.body, [*context, f"{text[:60]} (not evaluated)"], True)
                # later branches stay possible too
        return


def _condition(text: str, switch_subject: str | None) -> str | None:
    if switch_subject is not None:
        m = CASE_RX.match(text)
        if not m:
            return None
        values = _split_args(m.group(1))
        return " OR ".join(f"({switch_subject}) = ({v})" for v in values)
    m = IF_RX.match(text)
    return m.group(1) if m else None


def _split_args(text: str) -> list[str]:
    out, depth, cur, q = [], 0, "", ""
    for ch in text:
        if q:
            cur += ch
            if ch == q:
                q = ""
            continue
        if ch in "'\"":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def execute(statements: list[Statement], files: dict[str, str], log: ReloadLog | None,
            seed: dict[str, str] | None = None
            ) -> tuple[list[Statement], list[Statement], dict[str, Variable], list[Issue]]:
    """Returns (statements as written with includes spliced, executed stream, variables, issues)."""
    issues: list[Issue] = []
    written = splice_includes(statements, files, issues, {})
    tree = _Parser(written, issues).block(set())
    runner = _Runner(log=log, issues=issues)
    for name, value in (seed or {}).items():  # values given by reviewers (Ask decisions)
        runner.env[name] = value
        runner.variables[name] = Variable(name=name, value=value, origin=VariableOrigin.REVIEWER)
    runner.run(tree, [])
    return written, runner.out, runner.variables, issues
