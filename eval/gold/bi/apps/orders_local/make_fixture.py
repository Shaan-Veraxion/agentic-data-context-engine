"""Generate the orders_local fixture: source data, the Qlik script, its reload log and the QVDs Qlik would write.

The expected QVD contents are computed here in plain Python, following Qlik's semantics by hand, so they are an
independent oracle for the SQL translation (they never come from the SQL being tested).

    uv run python eval/gold/bi/apps/orders_local/make_fixture.py
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from pyqvd import QvdTable

HERE = Path(__file__).resolve().parent

CUSTOMERS = [  # CustomerID, Name, Region
    (1, "alice", "N"), (2, "bob", "S"), (3, "carol", "E"), (4, "dave", "W"), (5, "erin", "N"),
]
ORDERS = [  # OrderID, CustomerID, OrderDate, Amount, Status, Region
    (101, 1, "2024-12-30", 500, "Shipped", "N"),
    (102, 1, "2025-01-05", 1500, "Shipped", "N"),
    (103, 2, "2025-02-10", 800, "Cancelled", "S"),
    (104, 2, "2025-02-11", 1200, "Shipped", "S"),
    (105, 3, "2025-03-01", 300, "Shipped", "E"),
    (106, 4, "2025-03-15", 2500, "Shipped", "W"),
    (107, 6, "2025-04-01", 90, "Shipped", "N"),
    (108, 5, "2025-04-02", 1000, "Shipped", None),
    (109, 1, "2025-05-20", 700, "Returned", "N"),
]
REGION_MAP = {"N": "North", "S": "South", "E": "East"}
START = "2025-01-01"

SCRIPT = """\
SET ThousandSep=',';
LET vStartDate = '2025-01-01';
LIB CONNECT TO 'Dremio_Sales';

Customers:
LOAD CustomerID, Upper(Name) AS CustomerName, Region;
SQL SELECT CustomerID, Name, Region FROM "sales"."crm"."customers";

RegionMap:
MAPPING LOAD * INLINE [
Region, RegionName
N, North
S, South
E, East
];

Orders:
LOAD OrderID, CustomerID, OrderDate, Amount,
     If(Amount > 1000, 'Large', 'Small') AS OrderSize,
     ApplyMap('RegionMap', Region, 'Unknown') AS RegionName
WHERE Status <> 'Cancelled';
SQL SELECT OrderID, CustomerID, OrderDate, Amount, Status, Region
FROM "sales"."erp"."orders_v2" WHERE OrderDate >= '$(vStartDate)';

LEFT JOIN (Orders) LOAD CustomerID, CustomerName RESIDENT Customers;

CustomerTotals:
LOAD CustomerID, Sum(Amount) AS TotalAmount, Count(OrderID) AS OrderCount
RESIDENT Orders GROUP BY CustomerID;

DROP TABLE Customers;
STORE Orders INTO [lib://QVD/Orders.qvd] (qvd);
STORE CustomerTotals INTO [lib://QVD/CustomerTotals.qvd] (qvd);
"""


def qlik_semantics() -> tuple[list[dict], list[dict], dict[str, int]]:
    """What Qlik does with this script, step by step, without SQL."""
    customers = [{"CustomerID": c, "CustomerName": n.upper(), "Region": r} for c, n, r in CUSTOMERS]
    fetched_orders = [o for o in ORDERS if o[2] >= START]  # rows the SQL statement returns
    orders = []
    for oid, cid, d, amt, status, region in fetched_orders:
        if status == "Cancelled":  # preceding LOAD WHERE
            continue
        orders.append({"OrderID": oid, "CustomerID": cid, "OrderDate": d, "Amount": amt,
                       "OrderSize": "Large" if amt > 1000 else "Small",
                       "RegionName": REGION_MAP.get(region, "Unknown") if region is not None else "Unknown"})
    names = {c["CustomerID"]: c["CustomerName"] for c in customers}  # LEFT JOIN on the shared field CustomerID
    for o in orders:
        o["CustomerName"] = names.get(o["CustomerID"])
    totals: dict[int, list[int]] = defaultdict(list)
    for o in orders:
        totals[o["CustomerID"]].append(o["Amount"])
    customer_totals = [{"CustomerID": k, "TotalAmount": sum(v), "OrderCount": len(v)} for k, v in totals.items()]
    counts = {"customers": len(customers), "regionmap": len(REGION_MAP), "orders_fetched": len(fetched_orders),
              "join_loaded": len(customers), "totals": len(customer_totals)}
    return orders, customer_totals, counts


def line_of(text: str, needle: str) -> int:
    for i, ln in enumerate(text.splitlines(), 1):
        if needle in ln:
            return i
    raise ValueError(needle)


def reload_log(counts: dict[str, int]) -> str:
    ts = "20261001T020000.{:03d}+0000"
    lines = [f"{ts.format(0)} Execution started."]
    for i, ln in enumerate(SCRIPT.replace("$(vStartDate)", START).splitlines(), 1):
        if ln.strip():
            lines.append(f"{ts.format(i)} {i:04d}  {ln.rstrip(';')}")
        fetched = {line_of(SCRIPT, 'FROM "sales"."crm"'): counts["customers"],
                   line_of(SCRIPT, "E, East") + 1: counts["regionmap"],
                   line_of(SCRIPT, 'FROM "sales"."erp"'): counts["orders_fetched"],
                   line_of(SCRIPT, "LEFT JOIN (Orders)"): counts["join_loaded"],
                   line_of(SCRIPT, "RESIDENT Orders GROUP BY"): counts["totals"]}.get(i)
        if fetched is not None:
            lines.append(f"{ts.format(i)}       {fetched:,} Lines fetched")
    lines.append(f"{ts.format(999)}       Execution finished.")
    return "\n".join(lines) + "\n"


def main() -> None:
    src = HERE / "sources"
    for rel, header, rows in [("sales/crm/customers.csv", ["CustomerID", "Name", "Region"], CUSTOMERS),
                              ("sales/erp/orders_v2.csv",
                               ["OrderID", "CustomerID", "OrderDate", "Amount", "Status", "Region"], ORDERS)]:
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(["" if v is None else v for v in r] for r in rows)
    (HERE / "script.qvs").write_text(SCRIPT, encoding="utf-8")
    orders, totals, counts = qlik_semantics()
    (HERE / "reload.log").write_text(reload_log(counts), encoding="utf-8")
    qvd = HERE / "qvd"
    qvd.mkdir(exist_ok=True)
    for name, rows in [("Orders", orders), ("CustomerTotals", totals)]:
        cols = list(rows[0])
        QvdTable.from_dict({"columns": cols, "data": [[r[c] for c in cols] for r in rows]}).to_qvd(
            str(qvd / f"{name}.qvd"))
    print("orders", len(orders), "totals", len(totals), counts)


if __name__ == "__main__":
    main()
