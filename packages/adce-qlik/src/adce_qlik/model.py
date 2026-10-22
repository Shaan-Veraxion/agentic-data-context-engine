"""Build the BI IR (BIApp) from a classified script: tables, their load chains, joins, stores and sources.

Qlik semantics implemented here:
- A LOAD without FROM/RESIDENT/INLINE/AUTOGENERATE is a preceding LOAD: it reads the next LOAD or SQL statement.
  The label and prefixes of the topmost statement apply to the whole chain.
- JOIN / KEEP / CONCATENATE without a table name apply to the last loaded table. Plain JOIN is an outer join.
- A load whose field set equals an existing table's field set is auto-concatenated, unless NOCONCATENATE.
- MAPPING loads create mapping tables (used by ApplyMap / MAP USING, not part of the data model).
- Unlabelled tables get Qlik-style default names, flagged as inferred.
- Statements inside SUB definitions are not executed until CALL; they are flagged (expanded in P1.4).
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

import sqlglot
from adce_core.bi_ir import (
    AccessTable,
    BIApp,
    Coverage,
    FieldOp,
    Issue,
    LoadOp,
    LoadSource,
    LogicalSource,
    QlikTable,
    ScriptDocument,
    Statement,
    StatementKind as K,
    StoreOp,
    TableAction,
    TableStep,
)
from sqlglot import exp
from sqlglot.errors import ParseError

from .load_parser import LoadParseError, ParsedLoad, parse_load, unquote_name
from .sql_dialect import detect as detect_dialect

_NAME = r"(\[[^\]]+\]|\"[^\"]+\"|'[^']+'|[^\s,;()]+)"
CONNECT_RX = re.compile(rf"(?is)connect\s+(?:to\s+)?{_NAME}")
STORE_RX = re.compile(rf"(?is)^store\s+(?:.+?\s+from\s+)?{_NAME}\s+into\s+{_NAME}\s*(?:\(\s*(\w+)\s*\))?")
DROP_TABLES_RX = re.compile(r"(?is)^drop\s+tables?\s+(.+)$")
DROP_FIELDS_RX = re.compile(r"(?is)^drop\s+fields?\s+(.+?)(?:\s+from\s+(.+))?$")
RENAME_TABLE_RX = re.compile(rf"(?is)^rename\s+tables?\s+{_NAME}\s+to\s+{_NAME}")
RENAME_FIELD_RX = re.compile(r"(?is)^rename\s+fields?\s+(.+)$")
JOIN_RX = re.compile(r"(?i)^(left|right|inner|outer)?\s*join\s*(?:\((.+)\))?$")
KEEP_RX = re.compile(r"(?i)^(left|right|inner)\s+keep\s*(?:\((.+)\))?$")
CONCAT_RX = re.compile(r"(?i)^concatenate\s*(?:\((.+)\))?$")
TIERED = {"crosstable": "T2", "generic": "T3", "hierarchy": "T3", "hierarchybelongsto": "T3",
          "intervalmatch": "T2", "semantic": "T3"}
LOOP_KINDS = {K.FOR, K.DO}
OPENERS = {K.IF: K.END_IF, K.FOR: K.NEXT, K.DO: K.LOOP, K.SWITCH: K.END_SWITCH, K.SUB: K.END_SUB}


def _names(text: str) -> list[str]:
    return [unquote_name(n.strip()) for n in re.split(r",(?![^\[]*\])", text) if n.strip()]


# Section Access system fields: everything else in an access table reduces the data model
ACCESS_SYSTEM_FIELDS = {"ACCESS", "USERID", "PASSWORD", "NTNAME", "NTDOMAINSID", "NTSID", "SERIAL", "GROUP", "OMIT",
                        "USER.EMAIL", "EMAIL"}


class _Builder:
    def __init__(self, doc: ScriptDocument, sql_dialect: str | None, qvd_columns: dict[str, list[str]] | None = None,
                 connection_dialects: dict[str, str] | None = None) -> None:
        self.doc = doc
        self.qvd_columns = {k.lower(): v for k, v in (qvd_columns or {}).items()}
        self.dialect = sql_dialect  # None: detected per statement
        self.configured_dialects = connection_dialects or {}
        self.app = BIApp(app_id=doc.app_id, variables=doc.variables, reload_time=doc.reload_time)
        self.connection: str | None = None
        self.pending: list[tuple[Statement, ParsedLoad]] = []
        self.last_table: str | None = None
        self.stack: list[Statement] = []
        self.inferred_counter: dict[str, int] = {}
        self.failed_sources: set[int] = set()
        self.section_access = False  # between Section Access and Section Application
        self.qualify_rules: list[tuple[str, bool]] = []  # (field pattern, qualified?) in script order; last wins

    # ------------------------------------------------------------------ helpers
    def issue(self, st: Statement | None, code: str, message: str) -> None:
        self.app.issues.append(Issue(statement=st.index if st else None, code=code, message=message,
                                     span=st.span if st else None))

    def add_source(self, src: LogicalSource) -> None:
        if all(s.key != src.key for s in self.app.sources):
            self.app.sources.append(src)

    def in_sub(self) -> bool:
        return any(s.kind == K.SUB for s in self.stack)

    # ------------------------------------------------------------------ main loop
    def build(self) -> BIApp:
        # The executed stream already has control flow applied (interpreter); a document without it (built by
        # hand in tests) falls back to the statements as written.
        stream = self.doc.executed or self.doc.statements
        for st in stream:
            if self.pending and st.kind not in (K.LOAD, K.SQL):
                self._flush_dangling(st)
            if self.in_sub() and st.kind != K.END_SUB:
                continue
            self._statement(st)
        if self.pending:
            self._flush_dangling(None)
        failed = len(self.failed_sources)
        cov = self.doc.coverage
        self.app.coverage = Coverage(total=cov.total, understood=cov.understood - failed, other=cov.other + failed,
                                     unresolved_variables=cov.unresolved_variables)
        self.app.issues = list(self.doc.issues) + self.app.issues
        seen: set[tuple[int, str]] = set()
        for st in stream:
            src = st.source_index if st.source_index is not None else st.index
            if st.unresolved_variables and (src, "u") not in seen:
                seen.add((src, "u"))
                self.issue(st, "unresolved_variable", f"unresolved: {', '.join(st.unresolved_variables)}")
        for st in self.doc.statements:
            if st.kind == K.OTHER:
                self.issue(st, "unknown_statement", st.text[:120])
        return self.app

    def _statement(self, st: Statement) -> None:
        k = st.kind
        text = st.expanded_text or st.text
        if k in OPENERS:
            self.stack.append(st)
            if k == K.SUB:
                self.issue(st, "sub_not_expanded", "SUB body is recorded but not executed until CALL (P1.4)")
        elif k in OPENERS.values():
            for i in range(len(self.stack) - 1, -1, -1):
                if OPENERS[self.stack[i].kind] == k:
                    del self.stack[i:]
                    break
            else:
                self.issue(st, "unbalanced_control", f"{k.value} without opener")
        elif k == K.CONNECT:
            m = CONNECT_RX.search(text)
            self.connection = unquote_name(m.group(1)) if m else None
            if self.connection and self.connection not in self.app.connections:
                self.app.connections.append(self.connection)
        elif k == K.DISCONNECT:
            self.connection = None
        elif k == K.LOAD:
            self._load(st, text)
        elif k == K.SQL:
            self._sql(st, text)
        elif k == K.STORE:
            self._store(st, text)
        elif k == K.DROP:
            self._drop(st, text)
        elif k == K.RENAME:
            self._rename(st, text)
        elif k == K.CALL:
            self.issue(st, "call_not_expanded", "CALL of a SUB is not expanded yet (P1.4)")
        elif k in (K.QUALIFY, K.UNQUALIFY):
            body = re.sub(r"(?is)^\s*(un)?qualify\b", "", text).strip()
            for name in _split_names(body):
                self.qualify_rules.append((name, k == K.QUALIFY))
        elif k == K.SECTION:
            self.section_access = bool(re.match(r"(?i)\s*section\s+access\b", text))

    # ------------------------------------------------------------------ loads
    def _load(self, st: Statement, text: str) -> None:
        try:
            parsed = parse_load(text)
        except LoadParseError as e:
            self.failed_sources.add(st.source_index if st.source_index is not None else st.index)
            self.issue(st, "parse_error", str(e))
            parsed = ParsedLoad(fields=[], source=LoadSource(kind="unknown"))
            self._finalize(self.pending + [(st, parsed)], parse_error=str(e))
            self.pending = []
            return
        if parsed.source.kind == "preceding":
            self.pending.append((st, parsed))
            return
        chain = self.pending + [(st, parsed)]
        self.pending = []
        self._finalize(chain)

    def _sql(self, st: Statement, text: str) -> None:
        sql = re.sub(r"(?is)^sql\s+", "", text).strip()
        if self.dialect:
            dialect = self.dialect
        else:
            dialect, how = detect_dialect(sql, self.connection, configured=self.configured_dialects,
                                          learned=self.app.connection_dialects)
            if self.connection and how in ("configured", "sql text", "connection name"):
                self.app.connection_dialects.setdefault(self.connection, dialect)
        src = LoadSource(kind="sql", ref=sql, connection=self.connection, dialect=dialect)
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
            src.tables = list(dict.fromkeys(_table_path(t) for t in tree.find_all(exp.Table)))
            if isinstance(tree, exp.Select):
                cols = [_output_name(e, dialect) for e in tree.expressions]
                src.columns = [] if any(isinstance(e, exp.Star) or c == "*" for e, c in zip(tree.expressions, cols,
                                                                                             strict=True)) else cols
        except ParseError as e:
            self.issue(st, "sql_parse_error", f"({dialect}) {str(e).splitlines()[0]}")
            src.tables = re.findall(r"(?i)\bfrom\s+([\w.\"\[\]]+)", sql)
        for t in src.tables:
            self.add_source(LogicalSource(connection=self.connection, path=t, kind="sql_table"))
        parsed = ParsedLoad(fields=[], source=src)
        chain = self.pending + [(st, parsed)]
        self.pending = []
        self._finalize(chain, sql=True)

    def _flush_dangling(self, at: Statement | None) -> None:
        st0 = self.pending[0][0]
        self.issue(st0, "preceding_without_source", "preceding LOAD is not followed by LOAD or SELECT")
        chain = self.pending
        self.pending = []
        self._finalize(chain)

    def _finalize(self, chain: list[tuple[Statement, ParsedLoad]], *, sql: bool = False,
                  parse_error: str | None = None) -> None:
        head = chain[0][0]
        if head.label is None and (label := next((s.label for s, _ in chain if s.label), None)):
            # Qlik takes a label written further down a preceding-LOAD chain: "LOAD a; [T]: SQL SELECT ..."
            head = head.model_copy(update={"label": label})
        ops: list[LoadOp] = []
        for i, (st, p) in enumerate(chain):
            is_sql = sql and i == len(chain) - 1
            if p.source.kind == "qvd" and p.source.ref and not p.source.columns:
                cols = self.qvd_columns.get(PurePosixPath(p.source.ref.replace("\\", "/")).name.lower())
                if cols:
                    p.source.columns = list(cols)  # LOAD * resolved from the QVD header
            if p.source.kind == "resident" and p.source.ref in self.app.tables and not p.source.columns:
                known = [f for f in self.app.tables[p.source.ref].fields if f != "*"]
                if known and "*" not in self.app.tables[p.source.ref].fields:
                    p.source.columns = known  # LOAD * RESIDENT T: T's fields at this point
            ops.append(LoadOp(statement=st.index, span=st.span, kind="sql" if is_sql else "load", fields=p.fields,
                              distinct=p.distinct, where=p.where, while_=p.while_, group_by=p.group_by,
                              order_by=p.order_by, order_desc=p.order_desc,
                              source=p.source, text=st.expanded_text or st.text,
                              parse_error=parse_error if i == len(chain) - 1 else None))
            if p.source.kind in ("qvd", "file") and p.source.ref:
                self.add_source(LogicalSource(connection=None, path=p.source.ref, kind=p.source.kind))
        fields = self._chain_fields(head, ops)
        if self.section_access:  # an access table: a row-level security requirement, not a data table
            name = head.label or f"access_{len(self.app.section_access) + 1}"
            reduction = [f for f in fields if f.upper() not in ACCESS_SYSTEM_FIELDS and f != "*"]
            self.app.section_access.append(AccessTable(name=name, fields=fields, reduction_fields=reduction,
                                                       span=head.span))
            self.issue(head, "section_access", f"Section Access table {name}: row-level security on "
                       f"{', '.join(reduction) or 'no reduction field'}; recorded as a requirement, not translated")
            return
        action, join_kind, target, constructs, noconcat, cargs = self._prefixes(head)
        if "crosstable" in constructs and "*" not in fields:
            fields = _crosstable_fields(fields, cargs.get("crosstable", []))
        context = list(head.context) or [s.text.splitlines()[0][:80] for s in self.stack]
        if any(s.kind in LOOP_KINDS for s in self.stack):
            self.issue(head, "inside_loop", "load runs inside a loop; repeated execution is not modelled yet")
        for c in constructs:
            self.issue(head, "construct_tier", f"{c} prefix (tier {TIERED[c]})")
        step = TableStep(action=action, join_kind=join_kind, ops=ops, fields=fields, constructs=constructs,
                         construct_args=cargs, context=context)

        if action in (TableAction.JOIN, TableAction.CONCATENATE):
            name = target or self.last_table
            fields = self._qualify(step, name or self._new_name(head, ops[-1].source))
            if name is None or name not in self.app.tables or self.app.tables[name].dropped_at is not None:
                self.issue(head, "unknown_target", f"{action.value} target {name!r} not found")
                name = self._new_name(head, ops[-1].source)
                step.action = TableAction.CREATE
                self.app.tables[name] = QlikTable(name=name, name_inferred=head.label is None)
            t = self.app.tables[name]
            t.steps.append(step)
            t.fields = _union(t.fields, fields)
            self.last_table = name
            return

        name = self._new_name(head, ops[-1].source)
        if action == TableAction.MAPPING:
            self.app.tables[name] = QlikTable(name=name, name_inferred=head.label is None, mapping=True,
                                              steps=[step], fields=fields)
            return
        fields = self._qualify(step, name)
        if action == TableAction.KEEP:
            step.other_table = target or self.last_table
        elif not noconcat and not constructs and "*" not in fields:  # tiered prefixes reshape fields
            same = next((t for t in self.app.data_tables() if t.fields and set(t.fields) == set(fields)), None)
            if same is not None:
                step.action = TableAction.AUTO_CONCATENATE
                same.steps.append(step)
                self.last_table = same.name
                return
        old = self.app.tables.get(name)
        if old is not None and old.dropped_at is not None and not old.mapping:
            # the name was freed by DROP TABLE: Qlik creates a new table with the same name
            t = self.app.tables[name]
            t.earlier_drops.append(old.dropped_at)
            t.dropped_at = None
            t.steps.append(step)
            t.fields = fields
            t.name_inferred = head.label is None
            self.last_table = name
            return
        if name in self.app.tables:
            n = 1
            while f"{name}-{n}" in self.app.tables:
                n += 1
            self.issue(head, "duplicate_table_name", f"table {name!r} exists; Qlik names the new one {name}-{n}")
            name = f"{name}-{n}"
        self.app.tables[name] = QlikTable(name=name, name_inferred=head.label is None, steps=[step], fields=fields)
        self.last_table = name

    def _qualify(self, step: TableStep, table: str) -> list[str]:
        """QUALIFY: fields matching the rules in force are renamed Table.Field (UNQUALIFY exempts; the last rule that
        matches a field wins). Records the renaming on the step (compose applies it) and returns the new fields."""
        if not self.qualify_rules:
            return step.fields
        renamed = {}
        for f in step.fields:
            decision = None
            for pattern, q in self.qualify_rules:
                if fnmatch.fnmatchcase(f.lower(), pattern.lower()):
                    decision = q
            if decision and f != "*":
                renamed[f] = f"{table}.{f}"
        step.qualify = renamed
        step.fields = [renamed.get(f, f) for f in step.fields]
        return step.fields

    def _chain_fields(self, head: Statement, ops: list[LoadOp]) -> list[str]:
        below: list[str] = list(ops[-1].source.columns)
        unresolved_star = False
        for op in reversed(ops):
            if op.kind == "sql":
                continue  # SQL output columns are the source columns
            out: list[str] = []
            for f in op.fields:
                if f.star:
                    if below:
                        out = _union(out, below)
                    else:
                        unresolved_star = True
                        out = _union(out, ["*"])
                else:
                    out = _union(out, [f.name])
            below = out
        if unresolved_star:
            self.issue(head, "star_unresolved",
                       "LOAD * over a source with unknown columns; resolved with the QVD header or the catalog (PD)")
        return below

    def _prefixes(self, st: Statement
                  ) -> tuple[TableAction, str | None, str | None, list[str], bool, dict[str, list[str]]]:
        action, join_kind, target, noconcat = TableAction.CREATE, None, None, False
        constructs: list[str] = []
        args: dict[str, list[str]] = {}
        for p in st.prefixes:
            low = p.lower()
            if m := JOIN_RX.match(p):
                action, join_kind = TableAction.JOIN, (m.group(1) or "outer").lower()
                target = unquote_name(m.group(2).strip()) if m.group(2) else None
            elif m := KEEP_RX.match(p):
                action, join_kind = TableAction.KEEP, m.group(1).lower()
                target = unquote_name(m.group(2).strip()) if m.group(2) else None
            elif m := CONCAT_RX.match(p):
                action = TableAction.CONCATENATE
                target = unquote_name(m.group(1).strip()) if m.group(1) else None
            elif re.match(r"(?i)no\s*concatenate", p):
                noconcat = True
            elif low == "mapping":
                action = TableAction.MAPPING
            else:
                word = re.match(r"[a-z]+", low)
                if word and word.group(0) in TIERED:
                    constructs.append(word.group(0))
                    inner = re.search(r"\((.*)\)", p, re.S)
                    args[word.group(0)] = _names(inner.group(1)) if inner else []
        return action, join_kind, target, constructs, noconcat, args

    def _new_name(self, head: Statement, src: LoadSource) -> str:
        if head.label:
            return head.label
        if src.kind in ("qvd", "file") and src.ref:
            return PurePosixPath(src.ref.replace("\\", "/")).stem
        if src.kind == "sql" and src.tables:
            return src.tables[-1].split(".")[-1]
        if src.kind == "resident" and src.ref:
            return src.ref
        base = src.kind.upper()
        self.inferred_counter[base] = self.inferred_counter.get(base, 0) + 1
        return f"{base}{self.inferred_counter[base]}"

    # ------------------------------------------------------------------ other statements
    def _store(self, st: Statement, text: str) -> None:
        m = STORE_RX.match(text)
        if not m:
            self.issue(st, "parse_error", "STORE statement not understood")
            self.failed_sources.add(st.source_index if st.source_index is not None else st.index)
            return
        table, target, fmt = unquote_name(m.group(1)), unquote_name(m.group(2)), m.group(3)
        self.app.stores.append(StoreOp(statement=st.index, table=table, target=target, format=fmt))
        if table in self.app.tables:
            self.app.tables[table].stored_to.append(target)
        else:
            self.issue(st, "unknown_target", f"STORE of unknown table {table!r}")

    def _drop(self, st: Statement, text: str) -> None:
        if m := DROP_TABLES_RX.match(text):
            for name in _names(m.group(1)):
                if name in self.app.tables:
                    self.app.tables[name].dropped_at = st.index
                else:
                    self.issue(st, "unknown_target", f"DROP of unknown table {name!r}")
        elif m := DROP_FIELDS_RX.match(text):
            fields = set(_names(m.group(1)))
            tables = _names(m.group(2)) if m.group(2) else list(self.app.tables)
            for name in tables:
                if name in self.app.tables:
                    t = self.app.tables[name]
                    gone = [f for f in t.fields if f in fields]
                    if gone and t.dropped_at is None and not t.mapping:
                        t.field_ops.append(FieldOp(statement=st.index, kind="drop", drops=gone))
                    t.fields = [f for f in t.fields if f not in fields]

    def _rename(self, st: Statement, text: str) -> None:
        if m := RENAME_TABLE_RX.match(text):
            old, new = unquote_name(m.group(1)), unquote_name(m.group(2))
            if old in self.app.tables:
                prev = self.app.tables.get(new)
                if prev is not None:
                    if prev.dropped_at is None:
                        self.issue(st, "rename_conflict", f"RENAME TABLE {old} TO {new}: {new!r} already exists")
                        return
                    # the dropped table keeps its history under a distinct key
                    n = 1
                    while f"{new}~{n}" in self.app.tables:
                        n += 1
                    prev.qlik_name, prev.name = new, f"{new}~{n}"
                    self.app.tables[prev.name] = self.app.tables.pop(new)
                t = self.app.tables.pop(old)
                t.renamed_from.append(old)
                t.renamed_at.append(st.index)
                t.name = new
                self.app.tables[new] = t
                if self.last_table == old:
                    self.last_table = new
        elif m := RENAME_FIELD_RX.match(text):
            pairs = [re.split(r"(?i)\s+to\s+", p.strip()) for p in re.split(r",(?![^\[]*\])", m.group(1))]
            mapping = {unquote_name(a.strip()): unquote_name(b.strip()) for a, b in (p for p in pairs if len(p) == 2)}
            for t in self.app.tables.values():
                hit = {a: b for a, b in mapping.items() if a in t.fields}
                if hit and t.dropped_at is None and not t.mapping:
                    t.field_ops.append(FieldOp(statement=st.index, kind="rename", renames=hit))
                t.fields = [mapping.get(f, f) for f in t.fields]


def _split_names(text: str) -> list[str]:
    """QUALIFY / UNQUALIFY lists: names separated by commas, quoted or bracketed, '*' and '?' as wildcards."""
    out = []
    for part in re.split(r",(?=(?:[^\"']*[\"'][^\"']*[\"'])*[^\"']*$)", text):
        p = part.strip().strip(";").strip()
        if p:
            out.append(unquote_name(p))
    return out


def _table_path(t: exp.Table) -> str:
    parts = [p.name for p in t.parts]
    return ".".join(parts)


def _crosstable_fields(fields: list[str], args: list[str]) -> list[str]:
    """CROSSTABLE(attribute, data[, n]): the first n fields (default 1) stay, the others become rows."""
    if len(args) < 2:
        return fields
    n = int(args[2]) if len(args) > 2 and args[2].strip().isdigit() else 1
    return fields[:n] + [args[0], args[1]]


def _union(a: list[str], b: list[str]) -> list[str]:
    return a + [x for x in b if x not in a]


# databases that fold unquoted identifiers: Qlik receives the column names as the database reports them
_FOLD_UPPER = {"oracle", "snowflake", "db2", "exasol"}
_FOLD_LOWER = {"postgres", "redshift"}


def _output_name(e: exp.Expr, dialect: str | None) -> str:
    """The name a SELECT expression's column gets in the result set: an unquoted alias or column name is folded
    the way the database folds identifiers (Oracle: upper case), a quoted one is kept."""
    name = e.alias_or_name
    ident = e.args.get("alias") if isinstance(e, exp.Alias) else e.this if isinstance(e, exp.Column) else None
    if not isinstance(ident, exp.Identifier) or ident.quoted:
        return name
    if dialect in _FOLD_UPPER:
        return name.upper()
    if dialect in _FOLD_LOWER:
        return name.lower()
    return name


def build_app(doc: ScriptDocument, *, sql_dialect: str | None = None,
              qvd_columns: dict[str, list[str]] | None = None,
              connection_dialects: dict[str, str] | None = None) -> BIApp:
    """sql_dialect: force one dialect for all embedded SQL; by default it is detected per connection and statement
    (sql_dialect.detect), with connection_dialects (connection name -> sqlglot dialect) taking precedence.
    qvd_columns: QVD file name -> field names from its header, used to resolve LOAD * FROM x.qvd."""
    return _Builder(doc, sql_dialect, qvd_columns, connection_dialects).build()
