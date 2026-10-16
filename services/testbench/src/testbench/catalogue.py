"""The generated test cases: one Qlik construct per case, with source data and an oracle computed by hand.

Each spec gives the script, the source tables, the tables Qlik would hold after the reload (computed here in plain
Python from Qlik's documented behaviour, never from SQL) and the "Lines fetched" of every statement that reads
rows. generate.py turns a spec into a case folder: script, sources, reload log, QVDs of the stored tables, the
expected rows, and case.yaml with the expectations.

Constructs the rules do not cover yet are still written with their correct expected result and marked
status="known_gap", so the bench shows them as expected failures until they are fixed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

FULL_CHAIN = [1, 27, 28, 24, 25, 26, 20]
DATA_VARIANTS = ["full", "no_qvd", "no_log", "script_only"]


@dataclass
class Table:
    columns: list[str]
    rows: list[tuple[Any, ...]]


@dataclass
class Spec:
    id: str  # <suite>/<name>
    title: str
    covers: list[str]
    script: str
    sources: dict[str, Table] = field(default_factory=dict)  # "shop/raw/orders" or "files/prices" (file loads)
    outputs: dict[str, Table] = field(default_factory=dict)  # tables after the reload, and each stored snapshot
    fetched: list[int] = field(default_factory=list)  # reload log "Lines fetched", one per row-reading statement
    expect: dict[str, Any] = field(default_factory=dict)
    levels: dict[str, str] | None = None  # default: V4 for stored tables, V3 for the others
    status: str = "active"  # active | known_gap
    gap: str = ""
    chain: list[int] = field(default_factory=lambda: list(FULL_CHAIN))
    variants: dict[str, list[str]] = field(default_factory=lambda: {"ai": ["off"], "data": list(DATA_VARIANTS)})
    oracle_note: str = ""
    when: list[dict[str, Any]] = field(default_factory=list)  # variant-specific expectations (cases.Case.when)


HEAD = "LIB CONNECT TO 'Dremio_Shop';\n\n"

ORDERS = Table(["OrderID", "Customer", "Qty", "Price"],
               [(1, "acme", 2, 10.5), (2, "bolt", 1, 99.0), (3, "acme", 5, 3.5), (4, "zed", 3, 7.0),
                (5, "bolt", 10, 1.25)])
ORDERS2 = Table(["OrderID", "CustomerID", "Qty"], [(1, "C1", 2), (2, "C2", 1), (3, "C1", 5), (4, "C9", 3)])
CUSTOMERS = Table(["CustomerID", "CustomerName", "Region"],
                  [("C1", "Acme", "EU"), ("C2", "Bolt", "US"), ("C3", "Zed", "EU")])
CUSTOMERS_DUP = Table(["CustomerID", "CustomerName", "Region"],
                      [("C1", "Acme", "EU"), ("C1", "Acme", "APAC"), ("C2", "Bolt", "US")])

SQL_ORDERS = 'SQL SELECT OrderID, Customer, Qty, Price FROM "shop"."raw"."orders";\n'
SQL_ORDERS2 = 'SQL SELECT OrderID, CustomerID, Qty FROM "shop"."raw"."orders2";\n'
SQL_CUSTOMERS = 'SQL SELECT CustomerID, CustomerName, Region FROM "shop"."raw"."customers";\n'


# llm cases the rules now translate on their own (they stay in the llm suite: with a model, nothing is asked)
RULES_TRANSLATE = {"llm/previous_change_flag"}  # Previous() over ORDER BY -> LAG (2026-10-16)


def _store(*names: str) -> str:
    return "".join(f"STORE {n} INTO [lib://QVD/{n}.qvd] (qvd);\n" for n in names)


def _join(left: Table, right: Table, key: str, how: str) -> Table:
    """Qlik JOIN on the shared field: inner, left, right or outer (plain JOIN); duplicates fan out."""
    cols = left.columns + [c for c in right.columns if c not in left.columns]
    li, ri = left.columns.index(key), right.columns.index(key)
    extra = [right.columns.index(c) for c in right.columns if c not in left.columns]
    rows: list[tuple[Any, ...]] = []
    matched_right: set[int] = set()
    for lr in left.rows:
        hits = [(j, rr) for j, rr in enumerate(right.rows) if rr[ri] == lr[li]]
        for j, rr in hits:
            matched_right.add(j)
            rows.append(lr + tuple(rr[k] for k in extra))
        if not hits and how in ("left", "outer"):
            rows.append(lr + (None,) * len(extra))
    if how in ("right", "outer"):
        for j, rr in enumerate(right.rows):
            if j not in matched_right:
                row = [None] * len(cols)
                for c in right.columns:
                    row[cols.index(c)] = rr[right.columns.index(c)]
                rows.append(tuple(row))
    return Table(cols, rows)


def qlik_round(x: float, step: float = 1) -> float:
    return math.floor(x / step + 0.5) * step  # half up, to a multiple of step


def specs() -> list[Spec]:
    out: list[Spec] = []

    # ---------------------------------------------------------------- loads and expressions
    out.append(Spec(
        "loads/sql_preceding_load", "SQL source with a preceding LOAD computing fields",
        ["preceding_load", "sql_select", "arithmetic", "upper"],
        HEAD + "Orders:\nLOAD OrderID, Customer, Qty, Price, Qty * Price AS Amount, Upper(Customer) AS CustomerUpper;\n"
        + SQL_ORDERS + "\n" + _store("Orders"),
        sources={"shop/raw/orders": ORDERS},
        outputs={"Orders": Table(ORDERS.columns + ["Amount", "CustomerUpper"],
                                 [r + (r[2] * r[3], r[1].upper()) for r in ORDERS.rows])},
        fetched=[5],
        expect={"extract": {"tables": ["Orders"], "fields": {"Orders": ORDERS.columns + ["Amount", "CustomerUpper"]},
                            "sources": ["Dremio_Shop:shop.raw.orders"]},
                "synthesize": {"sql_contains": {"Orders": ['"shop"."raw"."orders"', "UPPER("]}}}))

    out.append(Spec(
        "loads/where_and_set_variable", "WHERE with a SET variable expanded by $()",
        ["where", "set_variable", "dollar_expansion"],
        "SET vMinQty = 3;\n" + HEAD + "BigOrders:\nLOAD OrderID, Customer, Qty\nWHERE Qty >= $(vMinQty);\n"
        'SQL SELECT OrderID, Customer, Qty FROM "shop"."raw"."orders";\n\n' + _store("BigOrders"),
        sources={"shop/raw/orders": ORDERS},
        outputs={"BigOrders": Table(["OrderID", "Customer", "Qty"],
                                    [(r[0], r[1], r[2]) for r in ORDERS.rows if r[2] >= 3])},
        fetched=[5],  # Qlik counts the rows the SQL returned; the WHERE runs after
        oracle_note="lines fetched = rows read from SQL (5); rows kept = 3"))

    out.append(Spec(
        "loads/let_variable_arithmetic", "LET variable used inside an expression",
        ["let_variable", "dollar_expansion", "arithmetic"],
        "LET vRate = 0.5;\n" + HEAD + "Orders:\nLOAD OrderID, Price, Price * (1 + $(vRate)) AS Gross;\n"
        'SQL SELECT OrderID, Price FROM "shop"."raw"."orders";\n\n' + _store("Orders"),
        sources={"shop/raw/orders": ORDERS},
        outputs={"Orders": Table(["OrderID", "Price", "Gross"], [(r[0], r[3], r[3] * 1.5) for r in ORDERS.rows])},
        fetched=[5]))

    out.append(Spec(
        "loads/inline_if_nested", "INLINE load with nested If",
        ["inline", "if", "nested_if"],
        "Scores:\nLOAD Name, Score, If(Score >= 50, 'pass', 'fail') AS Result,\n"
        "     If(Score >= 80, 'A', If(Score >= 50, 'B', 'C')) AS Grade\nINLINE [\nName, Score\nann, 91\nbob, 50\n"
        "cid, 49\ndan, 75\n];\n\n" + _store("Scores"),
        outputs={"Scores": Table(["Name", "Score", "Result", "Grade"],
                                 [(n, s, "pass" if s >= 50 else "fail", "A" if s >= 80 else "B" if s >= 50 else "C")
                                  for n, s in [("ann", 91), ("bob", 50), ("cid", 49), ("dan", 75)]])},
        fetched=[4]))

    codes = [("  Alpha ", "AB-123-X"), ("Beta", "CD-45-YY"), (" gamma", "EF-6789-Z"), ("delta  ", "GH9")]

    def mid(s: str, start: int, n: int) -> str:
        return s[start - 1:start - 1 + n]

    out.append(Spec(
        "expressions/string_functions", "Trim, Len, Left, Right, Mid, Replace, Index, Lower",
        ["trim", "len", "left", "right", "mid", "replace", "index", "lower"],
        HEAD + "Codes:\nLOAD Trim(Name) AS Clean, Len(Trim(Name)) AS NameLen, Lower(Trim(Name)) AS NameLower,\n"
        "     Left(Code, 2) AS Prefix, Right(Code, 2) AS Suffix, Mid(Code, 4, 3) AS Middle,\n"
        "     Replace(Code, '-', '') AS Compact, Index(Code, '-') AS DashPos;\n"
        'SQL SELECT Name, Code FROM "shop"."raw"."codes";\n\n' + _store("Codes"),
        sources={"shop/raw/codes": Table(["Name", "Code"], codes)},
        outputs={"Codes": Table(["Clean", "NameLen", "NameLower", "Prefix", "Suffix", "Middle", "Compact", "DashPos"],
                                [(n.strip(), len(n.strip()), n.strip().lower(), c[:2], c[-2:], mid(c, 4, 3),
                                  c.replace("-", ""), c.find("-") + 1) for n, c in codes])},
        fetched=[4],
        oracle_note="Mid is 1-based; Index returns 0 when absent; Mid past the end gives an empty text"))

    nums = [(1, 2.5), (2, -2.5), (3, 7.25), (4, -0.4)]
    out.append(Spec(
        "expressions/numeric_functions", "Floor, Ceil, Round (half up, with step), Fabs, Mod, Div",
        ["floor", "ceil", "round", "fabs", "mod", "div"],
        HEAD + "Nums:\nLOAD Id, X, Floor(X) AS F, Ceil(X) AS C, Round(X) AS R, Round(X, 0.5) AS RHalf,\n"
        "     Fabs(X) AS A, Mod(Id * 7, 3) AS M, Div(Id * 7, 2) AS D,\n"
        "     Mod(Id * 7 - 20, 3) AS MNeg, Div(Id * 7 - 20, 3) AS DNeg;\n"
        'SQL SELECT Id, X FROM "shop"."raw"."nums";\n\n' + _store("Nums"),
        sources={"shop/raw/nums": Table(["Id", "X"], nums)},
        outputs={"Nums": Table(["Id", "X", "F", "C", "R", "RHalf", "A", "M", "D", "MNeg", "DNeg"],
                               [(i, x, math.floor(x), math.ceil(x) + 0, qlik_round(x), qlik_round(x, 0.5), abs(x),
                                 (i * 7) % 3, (i * 7) // 2, (i * 7 - 20) % 3, math.trunc((i * 7 - 20) / 3))
                                for i, x in nums])},
        fetched=[4],
        oracle_note="Qlik Round rounds half up: Round(-2.5) = -2; Round(x, 0.5) = Floor(x/0.5 + 0.5) * 0.5. "
                    "Mod is never negative for a positive divisor (Mod(-13, 3) = 2); Div truncates towards zero"))

    disc = [(1, 5.0), (2, None), (3, 0.0), (4, None)]
    out.append(Spec(
        "expressions/null_handling", "IsNull (-1/0 booleans), Alt, If on NULL",
        ["isnull", "alt", "null", "qlik_boolean"],
        HEAD + "Disc:\nLOAD Id, Discount, IsNull(Discount) AS NoDiscount, Alt(Discount, 0) AS DiscountOrZero,\n"
        "     If(IsNull(Discount), 'missing', 'ok') AS Flag;\n"
        'SQL SELECT Id, Discount FROM "shop"."raw"."discounts";\n\n' + _store("Disc"),
        sources={"shop/raw/discounts": Table(["Id", "Discount"], disc)},
        outputs={"Disc": Table(["Id", "Discount", "NoDiscount", "DiscountOrZero", "Flag"],
                               [(i, d, -1 if d is None else 0, 0 if d is None else d,
                                 "missing" if d is None else "ok") for i, d in disc])},
        fetched=[4],
        oracle_note="Qlik true is -1, false is 0"))

    regions = [("EU", "AB-1"), ("US", "XZ"), ("APAC", "ABZ"), ("eu", "Q")]

    def wild(s: str) -> int:
        if s.upper().startswith("AB"):
            return 1
        return 2 if s.upper().endswith("Z") else 0

    out.append(Spec(
        "expressions/match_wildmatch", "Match (case-sensitive), MixMatch, WildMatch",
        ["match", "mixmatch", "wildmatch"],
        HEAD + "R:\nLOAD Region, Code, Match(Region, 'EU', 'US') AS M, MixMatch(Region, 'eu', 'us') AS MM,\n"
        "     WildMatch(Code, 'AB*', '*Z') AS W;\n"
        'SQL SELECT Region, Code FROM "shop"."raw"."regions";\n\n' + _store("R"),
        sources={"shop/raw/regions": Table(["Region", "Code"], regions)},
        outputs={"R": Table(["Region", "Code", "M", "MM", "W"],
                            [(r, c, {"EU": 1, "US": 2}.get(r, 0), {"eu": 1, "us": 2}.get(r.lower(), 0), wild(c))
                             for r, c in regions])},
        fetched=[4],
        oracle_note="Match is case-sensitive; MixMatch and WildMatch are not; first matching position wins"))

    events = [(1, 20240115), (2, 20241231), (3, 20250201)]
    out.append(Spec(
        "expressions/dates_from_number_text", "Date# on a YYYYMMDD number, Year, Month, Date formatting",
        ["date#", "year", "month", "date_format"],
        HEAD + "Events:\nLOAD EventID, Year(Date#(EventDay, 'YYYYMMDD')) AS Yr,\n"
        "     Num(Month(Date#(EventDay, 'YYYYMMDD'))) AS Mo,\n"
        "     Text(Date(Date#(EventDay, 'YYYYMMDD'), 'DD/MM/YYYY')) AS DayText;\n"
        'SQL SELECT EventID, EventDay FROM "shop"."raw"."events";\n\n' + _store("Events"),
        sources={"shop/raw/events": Table(["EventID", "EventDay"], events)},
        outputs={"Events": Table(["EventID", "Yr", "Mo", "DayText"],
                                 [(i, d // 10000, d // 100 % 100, f"{d % 100:02d}/{d // 100 % 100:02d}/{d // 10000}")
                                  for i, d in events])},
        fetched=[3],
        oracle_note="Date# reads the number's text 20240115 with the format; Text() keeps only the formatted text"))

    import calendar
    import datetime as dt

    def add_months(d: dt.date, n: int) -> dt.date:  # Qlik AddMonths mode 0: the month's last day when shorter
        y, m = divmod(d.month - 1 + n, 12)
        y, m = d.year + y, m + 1
        return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))

    parts = [(1, "2024-01-31", "a/b/c", 2024, 2, 9), (2, "2025-03-15", "root", 2025, 12, 31),
             (3, "2024-12-30", "x//y", 2023, 1, 1)]
    iso = "YYYY-MM-DD"
    out.append(Spec(
        "expressions/date_parts_and_counts", "SubStringCount, MakeDate, AddMonths (month end), Week (ISO)",
        ["substringcount", "makedate", "addmonths", "week"],
        HEAD + "Parts:\nLOAD Id, SubStringCount(Path, '/') AS Depth,\n"
        f"     Text(Date(MakeDate(Y, M, Dd), '{iso}')) AS Made,\n"
        f"     Text(Date(AddMonths(Date#(Day, '{iso}'), 1), '{iso}')) AS NextMonth,\n"
        f"     Text(Date(AddMonths(Date#(Day, '{iso}'), -1), '{iso}')) AS PrevMonth,\n"
        f"     Week(Date#(Day, '{iso}')) AS Wk;\n"
        'SQL SELECT Id, Day, Path, Y, M, Dd FROM "shop"."raw"."parts";\n\n' + _store("Parts"),
        sources={"shop/raw/parts": Table(["Id", "Day", "Path", "Y", "M", "Dd"], parts)},
        outputs={"Parts": Table(["Id", "Depth", "Made", "NextMonth", "PrevMonth", "Wk"], [
            (i, path.count("/"), dt.date(y, m, dd).isoformat(),
             add_months(dt.date.fromisoformat(day), 1).isoformat(),
             add_months(dt.date.fromisoformat(day), -1).isoformat(),
             dt.date.fromisoformat(day).isocalendar()[1])
            for i, day, path, y, m, dd in parts])},
        fetched=[3],
        oracle_note="AddMonths keeps the day, or the month's last day (31 Jan + 1 = 29 Feb 2024); Week is the ISO "
                    "week with Qlik's default settings (30 Dec 2024 is week 1)"))

    # ---------------------------------------------------------------- resident, aggregation, distinct
    totals: dict[str, list[Any]] = {}
    for _, c, q, p in ORDERS.rows:
        t = totals.setdefault(c, [0, 0, 0.0])
        t[0] += q
        t[1] += 1
        t[2] = max(t[2], p)
    out.append(Spec(
        "resident/group_by", "RESIDENT load with GROUP BY: Sum, Count, Max; source table dropped",
        ["resident", "group_by", "sum", "count", "max", "drop_table"],
        HEAD + "Orders:\nLOAD OrderID, Customer, Qty, Price;\n" + SQL_ORDERS + "\nCustomerTotals:\n"
        "LOAD Customer, Sum(Qty) AS TotalQty, Count(OrderID) AS Orders, Max(Price) AS MaxPrice\n"
        "RESIDENT Orders GROUP BY Customer;\n\nDROP TABLE Orders;\n" + _store("CustomerTotals"),
        sources={"shop/raw/orders": ORDERS},
        outputs={"CustomerTotals": Table(["Customer", "TotalQty", "Orders", "MaxPrice"],
                                         [(c, *v) for c, v in totals.items()])},
        fetched=[5, 3],
        expect={"extract": {"tables": ["Orders", "CustomerTotals"]}}))

    stats: dict[str, list[Any]] = {}
    for _, cid, q in ORDERS2.rows:
        stats.setdefault("C9" if cid == "C9" else "known", []).append((cid, q))
    out.append(Spec(
        "resident/aggregations_more", "Avg, Min, Count(DISTINCT) by a computed group",
        ["avg", "min", "count_distinct", "group_by_expression"],
        HEAD + "O:\nLOAD OrderID, CustomerID, Qty, If(CustomerID = 'C9', 'C9', 'known') AS Grp;\n" + SQL_ORDERS2 +
        "\nG:\nLOAD Grp, Avg(Qty) AS AvgQty, Min(Qty) AS MinQty, Count(DISTINCT CustomerID) AS Customers\n"
        "RESIDENT O GROUP BY Grp;\n\nDROP TABLE O;\n" + _store("G"),
        sources={"shop/raw/orders2": ORDERS2},
        outputs={"G": Table(["Grp", "AvgQty", "MinQty", "Customers"],
                            [(g, sum(q for _, q in v) / len(v), min(q for _, q in v), len({c for c, _ in v}))
                             for g, v in stats.items()])},
        fetched=[4, 2]))

    out.append(Spec(
        "resident/distinct", "LOAD DISTINCT from a resident table",
        ["distinct", "resident"],
        HEAD + "Orders:\nLOAD OrderID, Customer;\n" + 'SQL SELECT OrderID, Customer FROM "shop"."raw"."orders";\n'
        "\nCustomers:\nNOCONCATENATE LOAD DISTINCT Customer RESIDENT Orders;\n\nDROP TABLE Orders;\n"
        + _store("Customers"),
        sources={"shop/raw/orders": ORDERS},
        outputs={"Customers": Table(["Customer"], [(c,) for c in dict.fromkeys(r[1] for r in ORDERS.rows)])},
        fetched=[5, 3]))

    out.append(Spec(
        "resident/autogenerate_recno", "AUTOGENERATE with RecNo",
        ["autogenerate", "recno"],
        "Seq:\nLOAD RecNo() AS N, RecNo() * 2 AS Double AUTOGENERATE 5;\n\n" + _store("Seq"),
        outputs={"Seq": Table(["N", "Double"], [(n, n * 2) for n in range(1, 6)])},
        fetched=[5]))

    # ---------------------------------------------------------------- joins and keep
    for how, kw, title in [("left", "LEFT JOIN", "LEFT JOIN: unmatched orders keep NULL customer fields"),
                           ("inner", "INNER JOIN", "INNER JOIN: only matched orders remain"),
                           ("outer", "JOIN", "plain JOIN is a full outer join in Qlik")]:
        out.append(Spec(
            f"joins/{how}_join", title, [f"{how}_join", "join"],
            HEAD + "Orders:\nLOAD OrderID, CustomerID, Qty;\n" + SQL_ORDERS2 + f"\n{kw} (Orders)\n"
            "LOAD CustomerID, CustomerName, Region;\n" + SQL_CUSTOMERS + "\n" + _store("Orders"),
            sources={"shop/raw/orders2": ORDERS2, "shop/raw/customers": CUSTOMERS},
            outputs={"Orders": _join(ORDERS2, CUSTOMERS, "CustomerID", how)},
            fetched=[4, 3]))

    out.append(Spec(
        "joins/left_join_fan_out", "LEFT JOIN with a duplicated key multiplies rows",
        ["left_join", "fan_out"],
        HEAD + "Orders:\nLOAD OrderID, CustomerID, Qty;\n" + SQL_ORDERS2 + "\nLEFT JOIN (Orders)\n"
        "LOAD CustomerID, CustomerName, Region;\n" + SQL_CUSTOMERS.replace("customers", "customers_dup") + "\n"
        + _store("Orders"),
        sources={"shop/raw/orders2": ORDERS2, "shop/raw/customers_dup": CUSTOMERS_DUP},
        outputs={"Orders": _join(ORDERS2, CUSTOMERS_DUP, "CustomerID", "left")},
        fetched=[4, 3],
        oracle_note="C1 has two customer rows, so both C1 orders appear twice: 6 rows"))

    kept = Table(CUSTOMERS.columns, [r for r in CUSTOMERS.rows if r[0] in {o[1] for o in ORDERS2.rows}])
    out.append(Spec(
        "joins/left_keep", "LEFT KEEP filters the second table to keys of the first, tables stay separate",
        ["left_keep", "keep"],
        HEAD + "Orders:\nLOAD OrderID, CustomerID, Qty;\n" + SQL_ORDERS2 + "\nCustomers:\nLEFT KEEP (Orders)\n"
        "LOAD CustomerID, CustomerName, Region;\n" + SQL_CUSTOMERS + "\n" + _store("Orders", "Customers"),
        sources={"shop/raw/orders2": ORDERS2, "shop/raw/customers": CUSTOMERS},
        outputs={"Orders": ORDERS2, "Customers": kept},
        fetched=[4, 3]))

    exists = Table(ORDERS2.columns, [r for r in ORDERS2.rows if r[1] in {c[0] for c in CUSTOMERS.rows}])
    out.append(Spec(
        "joins/where_exists", "WHERE Exists(field) keeps rows whose key was already loaded",
        ["exists", "where"],
        HEAD + "Customers:\nLOAD CustomerID, CustomerName, Region;\n" + SQL_CUSTOMERS + "\nOrders:\n"
        "LOAD OrderID, CustomerID, Qty WHERE Exists(CustomerID);\n" + SQL_ORDERS2 + "\n"
        + _store("Customers", "Orders"),
        sources={"shop/raw/orders2": ORDERS2, "shop/raw/customers": CUSTOMERS},
        outputs={"Customers": CUSTOMERS, "Orders": exists},
        fetched=[3, 4]))

    out.append(Spec(
        "joins/not_exists_dedup", "WHERE NOT Exists(key) on the key being loaded keeps the first row per key",
        ["exists", "not_exists", "dedup", "load_order"],
        HEAD + "Customers:\nLOAD CustomerID, CustomerName, Region WHERE NOT Exists(CustomerID);\n"
        + SQL_CUSTOMERS.replace("customers", "customers_dup") + "\n" + _store("Customers"),
        sources={"shop/raw/customers_dup": CUSTOMERS_DUP},
        outputs={"Customers": Table(CUSTOMERS_DUP.columns, [CUSTOMERS_DUP.rows[0], CUSTOMERS_DUP.rows[2]])},
        fetched=[3],
        status="known_gap",
        gap="needs the source's row order (first row per key); SQL sources have no order unless the script gives one",
        variants={"ai": ["off"], "data": ["full"]}))

    # ---------------------------------------------------------------- concatenation
    out.append(Spec(
        "concat/explicit_concatenate", "CONCATENATE with an extra field fills NULLs",
        ["concatenate", "inline"],
        "Sales:\nLOAD * INLINE [\nRegion, Amount\nEU, 100\nUS, 200\n];\n\nCONCATENATE (Sales)\n"
        "LOAD * INLINE [\nRegion, Amount, Channel\nAPAC, 50, web\n];\n\n" + _store("Sales"),
        outputs={"Sales": Table(["Region", "Amount", "Channel"],
                                [("EU", 100, None), ("US", 200, None), ("APAC", 50, "web")])},
        fetched=[2, 1]))

    out.append(Spec(
        "concat/auto_concatenate", "Same field set: the second label is concatenated into the first table",
        ["auto_concatenate", "inline"],
        "A:\nLOAD * INLINE [\nRegion, Amount\nEU, 100\nUS, 200\n];\n\nB:\nLOAD * INLINE [\nRegion, Amount\n"
        "APAC, 50\nLATAM, 25\n];\n\n" + _store("A"),
        outputs={"A": Table(["Region", "Amount"], [("EU", 100), ("US", 200), ("APAC", 50), ("LATAM", 25)])},
        fetched=[2, 2],
        expect={"extract": {"tables": ["A"]}},
        oracle_note="B never exists: Qlik appends it to A because the field sets are equal"))

    out.append(Spec(
        "concat/noconcatenate", "NOCONCATENATE keeps two tables with the same fields apart",
        ["noconcatenate", "inline"],
        "A:\nLOAD * INLINE [\nRegion, Amount\nEU, 100\nUS, 200\n];\n\nB:\nNOCONCATENATE LOAD * INLINE [\n"
        "Region, Amount\nAPAC, 50\nLATAM, 25\n];\n\n" + _store("A", "B"),
        outputs={"A": Table(["Region", "Amount"], [("EU", 100), ("US", 200)]),
                 "B": Table(["Region", "Amount"], [("APAC", 50), ("LATAM", 25)])},
        fetched=[2, 2],
        expect={"extract": {"tables": ["A", "B"]}}))

    # ---------------------------------------------------------------- mapping, rename, drop, qvd round trip
    regions2 = Table(["CustomerID", "Region"], [("C1", "EU"), ("C2", "US"), ("C3", "APAC")])
    names = {"EU": "Europe", "US": "United States"}
    out.append(Spec(
        "mapping/applymap_default", "MAPPING LOAD and ApplyMap with a default",
        ["mapping", "applymap"],
        "RegionMap:\nMAPPING LOAD * INLINE [\nCode, Name\nEU, Europe\nUS, United States\n];\n\n" + HEAD +
        "Customers:\nLOAD CustomerID, ApplyMap('RegionMap', Region, 'Unknown') AS RegionName;\n"
        'SQL SELECT CustomerID, Region FROM "shop"."raw"."customers2";\n\n' + _store("Customers"),
        sources={"shop/raw/customers2": regions2},
        outputs={"Customers": Table(["CustomerID", "RegionName"],
                                    [(c, names.get(r, "Unknown")) for c, r in regions2.rows])},
        fetched=[2, 3],
        expect={"extract": {"tables": ["RegionMap", "Customers"]}}))

    out.append(Spec(
        "model/rename_and_drop_fields", "RENAME FIELD and DROP FIELD after the load",
        ["rename_field", "drop_field"],
        HEAD + "Orders:\nLOAD OrderID, Customer, Qty, Price;\n" + SQL_ORDERS +
        "\nRENAME FIELD Qty TO Quantity;\nDROP FIELD Price;\n\n" + _store("Orders"),
        sources={"shop/raw/orders": ORDERS},
        outputs={"Orders": Table(["OrderID", "Customer", "Quantity"], [r[:3] for r in ORDERS.rows])},
        fetched=[5]))

    out.append(Spec(
        "model/rename_table", "RENAME TABLE before the STORE",
        ["rename_table"],
        HEAD + "Orders:\nLOAD OrderID, Customer;\n" + 'SQL SELECT OrderID, Customer FROM "shop"."raw"."orders";\n'
        "\nRENAME TABLE Orders TO Facts;\n\n" + _store("Facts"),
        sources={"shop/raw/orders": ORDERS},
        outputs={"Facts": Table(["OrderID", "Customer"], [r[:2] for r in ORDERS.rows])},
        fetched=[5]))

    big = Table(ORDERS.columns, [r for r in ORDERS.rows if r[2] > 2])
    out.append(Spec(
        "model/store_drop_reload_qvd", "STORE, DROP, then read the stored QVD back with a filter",
        ["store", "drop_table", "qvd_load", "where"],
        HEAD + "Orders:\nLOAD OrderID, Customer, Qty, Price;\n" + SQL_ORDERS + "\n" + _store("Orders") +
        "DROP TABLE Orders;\n\nBig:\nLOAD * FROM [lib://QVD/Orders.qvd] (qvd) WHERE Qty > 2;\n\n" + _store("Big"),
        sources={"shop/raw/orders": ORDERS},
        outputs={"Orders": ORDERS, "Big": big},
        fetched=[5, 3]))

    prices = Table(["Sku", "ListPrice"], [("A1", 10.0), ("B2", 25.5), ("C3", 4.25)])
    out.append(Spec(
        "loads/file_csv", "Load from a delimited file (lib:// path) with a field computed",
        ["file_load", "csv"],
        "Prices:\nLOAD Sku, ListPrice, ListPrice * 2 AS DoublePrice\nFROM [lib://Files/prices.csv]\n"
        "(txt, utf8, embedded labels, delimiter is ',');\n\n" + _store("Prices"),
        sources={"files/prices": prices},
        outputs={"Prices": Table(["Sku", "ListPrice", "DoublePrice"], [(s, p, p * 2) for s, p in prices.rows])},
        fetched=[3]))

    out.append(Spec(
        "model/crosstable", "CROSSTABLE turns month columns into rows",
        ["crosstable"],
        "Budget:\nCROSSTABLE(Month, Amount)\nLOAD * INLINE [\nRegion, Jan, Feb\nEU, 10, 20\nUS, 30, 40\n];\n\n"
        + _store("Budget"),
        outputs={"Budget": Table(["Region", "Month", "Amount"],
                                 [("EU", "Jan", 10), ("EU", "Feb", 20), ("US", "Jan", 30), ("US", "Feb", 40)])},
        fetched=[4],
        oracle_note="lines fetched after CROSSTABLE counts the unpivoted rows"))

    out += llm_specs()

    # ---------------------------------------------------------------- intake (agent 1 alone)
    out.append(Spec(
        "intake/classify_bundle", "Classify a typical upload bundle", ["intake", "classification"],
        HEAD + "T:\nLOAD OrderID;\n" + 'SQL SELECT OrderID FROM "shop"."raw"."orders";\n',
        sources={"shop/raw/orders": ORDERS},
        fetched=[5],
        chain=[1],
        variants={"ai": ["off"], "data": ["full"]},
        expect={"intake": {"classes": {"script.qvs": "script", "reload.log": "reload_log",
                                       "sources/shop/raw/orders.csv": "source_extract"}}}))
    return out


def llm_specs() -> list[Spec]:
    """Statements the rules cannot translate (or translate wrongly): the LLM pathway's evaluation set. In CI they
    run with AI off (expected: not translated); `adce-bench eval` runs them live, with and without tools."""
    out: list[Spec] = []
    yield_specs = out
    off = {"ai": ["off"], "data": ["full"]}

    pays = [(1, "1.234,50"), (2, "99,00"), (3, "12.000,75"), (4, "0,5")]
    out.append(Spec(
        "llm/num_text_settings", "Num# without a format follows the script's ThousandSep and DecimalSep",
        ["llm", "num#", "script_settings"],
        "SET ThousandSep='.';\nSET DecimalSep=',';\n" + HEAD + "Pay:\nLOAD PayID, Num#(AmountText) AS Amount;\n"
        'SQL SELECT PayID, AmountText FROM "shop"."raw"."payments";\n\n' + _store("Pay"),
        sources={"shop/raw/payments": Table(["PayID", "AmountText"], pays)},
        outputs={"Pay": Table(["PayID", "Amount"],
                              [(i, float(t.replace(".", "").replace(",", "."))) for i, t in pays])},
        fetched=[4], variants=off))

    labels = [(1, "order [EU] 12", "A-1/2"), (2, "[US] item", "B--7"), (3, "x [APAC]", "C/9-9")]
    out.append(Spec(
        "llm/textbetween_purgechar", "TextBetween and PurgeChar",
        ["llm", "textbetween", "purgechar"],
        HEAD + "L:\nLOAD Id, TextBetween(Label, '[', ']') AS Tag, PurgeChar(Code, '-/') AS CleanCode;\n"
        'SQL SELECT Id, Label, Code FROM "shop"."raw"."labels";\n\n' + _store("L"),
        sources={"shop/raw/labels": Table(["Id", "Label", "Code"], labels)},
        outputs={"L": Table(["Id", "Tag", "CleanCode"],
                            [(i, lb[lb.index("[") + 1:lb.index("]")], "".join(ch for ch in c if ch not in "-/"))
                             for i, lb, c in labels])},
        fetched=[3], variants=off))

    paths = [(1, "/eu/de/berlin"), (2, "a/b"), (3, "root")]
    out.append(Spec(
        "llm/subfield_last_count", "SubField with a negative index (from the end) and SubStringCount",
        ["llm", "subfield", "substringcount"],
        HEAD + "P:\nLOAD Id, SubField(Path, '/', -1) AS Leaf, SubStringCount(Path, '/') AS Depth;\n"
        'SQL SELECT Id, Path FROM "shop"."raw"."paths";\n\n' + _store("P"),
        sources={"shop/raw/paths": Table(["Id", "Path"], paths)},
        outputs={"P": Table(["Id", "Leaf", "Depth"], [(i, p.split("/")[-1], p.count("/")) for i, p in paths])},
        fetched=[3], variants=off,
        oracle_note="SubField(s, d, -1) is the last part; without the delimiter it is the whole text"))

    events = [(3, "closed"), (1, "open"), (5, "open"), (2, "open"), (4, "closed")]
    ordered = sorted(events)
    changed = [(i, s, 1 if k == 0 or s != ordered[k - 1][1] else 0) for k, (i, s) in enumerate(ordered)]
    out.append(Spec(
        "llm/previous_change_flag", "Previous() over ORDER BY flags each change of status",
        ["llm", "previous", "order_by", "resident"],
        HEAD + "Events:\nLOAD EventID, Status;\n" + 'SQL SELECT EventID, Status FROM "shop"."raw"."events";\n\n'
        "Flags:\nNOCONCATENATE LOAD EventID, Status,\n"
        "     If(IsNull(Previous(Status)) or Status <> Previous(Status), 1, 0) AS Changed\n"
        "RESIDENT Events ORDER BY EventID;\n\nDROP TABLE Events;\n" + _store("Flags"),
        sources={"shop/raw/events": Table(["EventID", "Status"], events)},
        outputs={"Flags": Table(["EventID", "Status", "Changed"], changed)},
        fetched=[5, 5], variants=off,
        oracle_note="rows in EventID order; the first row has no previous row, so it is flagged"))

    lines = [(1, "1 Main St"), (2, "22 Hill Rd"), (3, "3 Stone Ave")]
    abbr = [("St", "Street"), ("Rd", "Road")]

    def map_sub(s: str) -> str:
        out_s, i = "", 0
        while i < len(s):  # left to right, every occurrence, replaced text is not scanned again
            hit = next(((f, t) for f, t in abbr if s.startswith(f, i)), None)
            if hit:
                out_s += hit[1]
                i += len(hit[0])
            else:
                out_s += s[i]
                i += 1
        return out_s

    out.append(Spec(
        "llm/mapsubstring_inline", "MapSubString with a mapping table loaded earlier in the script",
        ["llm", "mapsubstring", "mapping", "script_context"],
        "Abbr:\nMAPPING LOAD * INLINE [\nFrom, To\nSt, Street\nRd, Road\n];\n\n" + HEAD +
        "Addr:\nLOAD Id, MapSubString('Abbr', Line) AS FullLine;\n"
        'SQL SELECT Id, Line FROM "shop"."raw"."addresses";\n\n' + _store("Addr"),
        sources={"shop/raw/addresses": Table(["Id", "Line"], lines)},
        outputs={"Addr": Table(["Id", "FullLine"], [(i, map_sub(s)) for i, s in lines])},
        fetched=[2, 3], variants=off,
        oracle_note="every occurrence is replaced, also inside words: '3 Stone Ave' becomes '3 Streetone Ave'"))

    months = "jan;feb;mar;apr;mai;jun;jul;aug;sep;okt;nov;des"
    days = [(1, "05 mai 2024"), (2, "17 okt 2023"), (3, "01 jan 2025")]
    names: list[str] = [str(m) for m in months.split(";")]
    out.append(Spec(
        "llm/date_custom_monthnames", "Date# with month names set by the script (MonthNames)",
        ["llm", "date#", "monthnames", "script_settings"],
        f"SET MonthNames='{months}';\n" + HEAD + "D:\nLOAD Id, Num(Month(Date#(Day, 'DD MMM YYYY'))) AS Mo,\n"
        "     Year(Date#(Day, 'DD MMM YYYY')) AS Yr;\n"
        'SQL SELECT Id, Day FROM "shop"."raw"."days";\n\n' + _store("D"),
        sources={"shop/raw/days": Table(["Id", "Day"], days)},
        outputs={"D": Table(["Id", "Mo", "Yr"],
                            [(i, names.index(d.split()[1]) + 1, int(d.split()[2])) for i, d in days])},
        fetched=[3], variants=off))
    sales = [("bolt", 3, 5), ("acme", 2, 10), ("acme", 1, 7), ("bolt", 1, 4), ("acme", 3, 1), ("zed", 1, 9)]
    run_rows, prev, acc = [], None, 0
    for c, d, a in sorted(sales):
        acc = acc + a if c == prev else a
        run_rows.append((c, d, a, acc))
        prev = c
    out.append(Spec(
        "llm/running_total_by_group", "Running total per customer: Previous, Peek and RangeSum over ORDER BY",
        ["llm", "previous", "peek", "rangesum", "order_by", "resident"],
        HEAD + "Sales:\nLOAD Cust, Day, Amt;\n" + 'SQL SELECT Cust, Day, Amt FROM "shop"."raw"."sales";\n\n'
        "Run:\nNOCONCATENATE LOAD Cust, Day, Amt,\n"
        "     If(Cust = Previous(Cust), RangeSum(Peek('RunAmt'), Amt), Amt) AS RunAmt\n"
        "RESIDENT Sales ORDER BY Cust, Day;\n\nDROP TABLE Sales;\n" + _store("Run"),
        sources={"shop/raw/sales": Table(["Cust", "Day", "Amt"], sales)},
        outputs={"Run": Table(["Cust", "Day", "Amt", "RunAmt"], run_rows)},
        fetched=[6, 6], variants=off,
        oracle_note="the total restarts at each customer; Peek('RunAmt') is the previous output row's total"))

    buys = [("acme", "nut", 3), ("acme", "bolt", 9), ("acme", "gear", 5), ("bolt", "cog", 2), ("zed", "pin", 4),
            ("zed", "nut", 1)]
    by_cust: dict[str, list[tuple[str, int]]] = {}
    for c, p, d in buys:
        by_cust.setdefault(c, []).append((p, d))
    out.append(Spec(
        "llm/firstsortedvalue_group", "FirstSortedValue per group: the first and the latest product per customer",
        ["llm", "firstsortedvalue", "group_by"],
        HEAD + "Firsts:\nLOAD Cust, FirstSortedValue(Prod, Day) AS FirstProd,\n"
        "     FirstSortedValue(Prod, -Day) AS LastProd, Count(Prod) AS Buys\nGROUP BY Cust;\n"
        'SQL SELECT Cust, Prod, Day FROM "shop"."raw"."buys";\n\n' + _store("Firsts"),
        sources={"shop/raw/buys": Table(["Cust", "Prod", "Day"], buys)},
        outputs={"Firsts": Table(["Cust", "FirstProd", "LastProd", "Buys"],
                                 [(c, min(v, key=lambda x: x[1])[0], max(v, key=lambda x: x[1])[0], len(v))
                                  for c, v in sorted(by_cust.items())])},
        fetched=[6], variants=off,
        oracle_note="sort weight -Day takes the highest Day; no ties in this data (a tie gives NULL in Qlik)"))

    custs = [(1, "Alice"), (2, "Bob")]
    orders = [(10, 1), (11, 2), (12, 3), (13, 1)]
    names_by_id = dict(custs)
    out.append(Spec(
        "llm/lookup_other_table", "Lookup() reads a field of another table loaded earlier",
        ["llm", "lookup", "script_context"],
        "Cust:\nLOAD * INLINE [\nCId, CName\n1, Alice\n2, Bob\n];\n\n" + HEAD +
        "Orders:\nLOAD OrderId, CustId, Lookup('CName', 'CId', CustId, 'Cust') AS Name;\n"
        'SQL SELECT OrderId, CustId FROM "shop"."raw"."orders";\n\n' + _store("Orders"),
        sources={"shop/raw/orders": Table(["OrderId", "CustId"], orders)},
        outputs={"Orders": Table(["OrderId", "CustId", "Name"], [(o, c, names_by_id.get(c)) for o, c in orders])},
        fetched=[2, 4], variants=off,
        oracle_note="Lookup gives the first match in load order; no match gives NULL (order 12)"))
    for sp in yield_specs:  # with AI off the rules leave the table untranslated: nothing to run or compare
        if sp.id in RULES_TRANSLATE:
            continue
        table = next(iter(sp.outputs))
        sp.when = [{"if": {"ai": "off"}, "levels": {table: "V0"},
                    "expect": {"check": {"failing": {table: ["translated", "composed"]}}, "rows": False}}]
    return out
