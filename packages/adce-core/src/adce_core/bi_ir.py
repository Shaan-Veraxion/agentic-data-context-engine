"""BI IR: the tool-neutral model of a BI application.

Qlik is the first tool. Tool-specific readers (adce-qlik) produce these models, and the translators (adce-reversesql)
consume them. Every element keeps the span of script text it came from.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class SourceSpan(BaseModel):
    """Where something came from in an input file. Lines are 1-based and inclusive; offsets are 0-based."""

    file: str
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int


class StatementKind(StrEnum):
    LOAD = "load"
    SQL = "sql"  # SQL SELECT ... passed to the data connection
    SET = "set"
    LET = "let"
    CONNECT = "connect"  # LIB CONNECT TO, CONNECT, ODBC/OLEDB CONNECT
    DISCONNECT = "disconnect"
    STORE = "store"
    DROP = "drop"
    RENAME = "rename"
    QUALIFY = "qualify"
    UNQUALIFY = "unqualify"
    IF = "if"
    ELSEIF = "elseif"
    ELSE = "else"
    END_IF = "end_if"
    FOR = "for"
    NEXT = "next"
    SUB = "sub"
    END_SUB = "end_sub"
    CALL = "call"
    DO = "do"
    LOOP = "loop"
    EXIT = "exit"
    SWITCH = "switch"
    CASE = "case"
    DEFAULT = "default"
    END_SWITCH = "end_switch"
    TRACE = "trace"
    SLEEP = "sleep"
    BINARY = "binary"
    SECTION = "section"
    MAP_USING = "map_using"
    INCLUDE = "include"  # $(Include=...) / $(Must_Include=...)
    DIRECTIVE = "directive"  # other script-level directives (e.g. SEARCH, TAG, COMMENT FIELD)
    OTHER = "other"  # not understood: kept with its text and span, counted in coverage


CONTROL_KINDS = frozenset({
    StatementKind.IF, StatementKind.ELSEIF, StatementKind.ELSE, StatementKind.END_IF, StatementKind.FOR,
    StatementKind.NEXT, StatementKind.SUB, StatementKind.END_SUB, StatementKind.CALL, StatementKind.DO,
    StatementKind.LOOP, StatementKind.EXIT, StatementKind.SWITCH, StatementKind.CASE, StatementKind.DEFAULT,
    StatementKind.END_SWITCH,
})


class Statement(BaseModel):
    """One script statement, as split from the source text."""

    index: int
    kind: StatementKind
    text: str  # statement text without the label and the terminating semicolon
    span: SourceSpan
    label: str | None = None  # "Orders:" label in front of a LOAD/SQL
    prefixes: list[str] = Field(default_factory=list)  # e.g. ["LEFT JOIN (Orders)", "NOCONCATENATE"]
    expanded_text: str | None = None  # text after $(variable) expansion, when it differs
    unresolved_variables: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)  # executed stream: enclosing CALL / FOR iteration / IF branch
    source_index: int | None = None  # executed stream: index of the statement as written


class VariableOrigin(StrEnum):
    SCRIPT_SET = "script_set"
    SCRIPT_LET = "script_let"
    RELOAD_LOG = "reload_log"
    REVIEWER = "reviewer"


class Variable(BaseModel):
    name: str
    value: str | None  # None when it could not be evaluated
    origin: VariableOrigin
    raw: str | None = None  # the right-hand side as written
    span: SourceSpan | None = None


class LogicalSource(BaseModel):
    """A source as the script names it. Resolved to a Dremio dataset in phase PD."""

    connection: str | None  # Qlik data connection (LIB CONNECT TO name), None for files
    path: str  # table path as written in SQL, or a file/QVD path
    kind: str  # "sql_table" | "qvd" | "file" | "inline" | "resident"

    @property
    def key(self) -> str:
        return f"{self.connection or ''}:{self.path}"


class Coverage(BaseModel):
    total: int = 0
    understood: int = 0
    other: int = 0
    unresolved_variables: int = 0

    @property
    def ratio(self) -> float:
        return self.understood / self.total if self.total else 1.0


class Expr(BaseModel):
    """ExprAST: the shared expression form for script expressions and chart measures (Qlik first, other BI later).

    kinds: field | number | string | call | binop | unop | star
    """

    kind: str
    name: str | None = None  # field name, function name, or operator
    value: str | int | float | None = None  # literal value
    args: list[Expr] = Field(default_factory=list)
    modifier: str | None = None  # DISTINCT / TOTAL / ALL in aggregations
    text: str = ""  # exact source text of this expression

    def fields(self) -> list[str]:
        out: list[str] = [self.name] if self.kind == "field" and self.name else []
        for a in self.args:
            out += [f for f in a.fields() if f not in out]
        return out

    def calls(self) -> list[str]:
        out = [self.name.lower()] if self.kind == "call" and self.name else []
        for a in self.args:
            out += a.calls()
        return out


class FieldDef(BaseModel):
    name: str  # output field name (alias, field name, or the expression text when unaliased)
    expr: Expr | None  # None for '*'
    star: bool = False


class LoadSource(BaseModel):
    kind: str  # sql | resident | file | qvd | inline | autogenerate | extension | preceding
    ref: str | None = None  # table name, file path, or SQL text
    connection: str | None = None
    dialect: str | None = None  # sqlglot dialect of embedded SQL (None: Dremio)
    format: str | None = None
    file_table: str | None = None  # file sources: the sheet or table inside the file ("table is Sheet1")
    tables: list[str] = Field(default_factory=list)  # tables referenced by embedded SQL
    columns: list[str] = Field(default_factory=list)  # known output columns of the source, if any
    inline_rows: list[list[str]] = Field(default_factory=list)


class LoadOp(BaseModel):
    """One LOAD or SQL statement in a table's chain (preceding LOADs first, the reading statement last)."""

    statement: int
    span: SourceSpan
    kind: str  # load | sql
    fields: list[FieldDef] = Field(default_factory=list)
    distinct: bool = False
    where: Expr | None = None
    while_: Expr | None = None
    group_by: list[Expr] = Field(default_factory=list)
    source: LoadSource
    parse_error: str | None = None
    text: str = ""


class TableAction(StrEnum):
    CREATE = "create"
    CONCATENATE = "concatenate"  # explicit CONCATENATE prefix
    AUTO_CONCATENATE = "auto_concatenate"  # same field set as an existing table
    JOIN = "join"
    KEEP = "keep"
    MAPPING = "mapping"


class TableStep(BaseModel):
    """A chain of load ops and what it does to its target table."""

    action: TableAction
    join_kind: str | None = None  # inner | left | right | outer (JOIN/KEEP)
    ops: list[LoadOp]
    fields: list[str]
    other_table: str | None = None  # KEEP: the table this load is kept against
    constructs: list[str] = Field(default_factory=list)  # e.g. crosstable, generic (tier T2/T3)
    context: list[str] = Field(default_factory=list)  # enclosing IF / FOR / SUB headers
    qualified: dict[str, str] = Field(default_factory=dict)  # QUALIFY in force: field -> "Table.field"


class QlikTable(BaseModel):
    name: str
    name_inferred: bool = False  # True when Qlik's default naming was applied (no label)
    mapping: bool = False
    steps: list[TableStep] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    dropped_at: int | None = None
    renamed_from: list[str] = Field(default_factory=list)
    stored_to: list[str] = Field(default_factory=list)


class StoreOp(BaseModel):
    statement: int
    table: str
    target: str
    format: str | None = None


class Issue(BaseModel):
    statement: int | None
    code: str  # e.g. parse_error, unresolved_variable, star_unresolved, inside_loop, sub_not_expanded
    message: str
    span: SourceSpan | None = None


class BIApp(BaseModel):
    """The BI IR for one application."""

    app_id: str
    tool: str = "qlik"
    tables: dict[str, QlikTable] = Field(default_factory=dict)
    stores: list[StoreOp] = Field(default_factory=list)
    connections: list[str] = Field(default_factory=list)
    sources: list[LogicalSource] = Field(default_factory=list)
    variables: dict[str, Variable] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)

    def data_tables(self) -> list[QlikTable]:
        return [t for t in self.tables.values() if not t.mapping and t.dropped_at is None]


class ScriptDocument(BaseModel):
    """A split and classified script: the first stage of the BI IR for one application."""

    app_id: str
    files: list[str]
    statements: list[Statement]  # as written (includes spliced in), classified
    executed: list[Statement] = Field(default_factory=list)  # as run: control flow applied, SUBs called, loops unrolled
    variables: dict[str, Variable] = Field(default_factory=dict)
    coverage: Coverage = Field(default_factory=Coverage)
    issues: list[Issue] = Field(default_factory=list)
