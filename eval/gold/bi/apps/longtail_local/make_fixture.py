"""Generate the longtail_local fixture: statements the rules cannot translate, with data and an independent oracle.

It tests the LLM pathway: the expected QVD contents and the reload log are computed here in plain Python from
Qlik's documented behaviour, never from SQL, so an AI translation is accepted only if it reproduces them.

    uv run python eval/gold/bi/apps/longtail_local/make_fixture.py
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

from pyqvd import QvdTable

HERE = Path(__file__).resolve().parent

SALES = [  # SaleID, Customer, SaleDate, AmountText, Path, Code
    (1, "acme", "2025-01-03", "1,234.50", "/eu/de/berlin", "AB-12-x"),
    (2, "acme", "2025-01-09", "310.00", "/eu/de/hamburg", "Q7-0-z"),
    (3, "bolt", "2025-01-04", "2,048.10", "/eu/fr/paris", "ZZ-905"),
    (4, "acme", "2025-02-11", "77.25", "/eu/de/berlin", "C-3"),
    (5, "bolt", "2025-02-01", "999.99", "/us/ny/nyc", "N-44-1"),
    (6, "corp", "2025-03-15", "15,000.00", "/apac/sg/sg", "S-8"),
    (7, "bolt", "2025-03-20", "12.40", "/eu/fr/lyon", "L-0-0"),
]

SCRIPT = """\
SET ThousandSep=',';
SET DecimalSep='.';
LIB CONNECT TO 'Dremio_Shop';

Sales:
LOAD SaleID, Customer, SaleDate,
     Num#(AmountText, '#,##0.00') AS Amount,
     SubField(Path, '/', 3) AS Country,
     KeepChar(Code, '0123456789') AS CodeDigits,
     Round(Num#(AmountText, '#,##0.00'), 10) AS AmountRounded,
     Class(Num#(AmountText, '#,##0.00'), 500) AS AmountBand;
SQL SELECT SaleID, Customer, SaleDate, AmountText, Path, Code FROM "shop"."raw"."sales";

Running:
NOCONCATENATE LOAD SaleID, Customer, SaleDate, Amount,
     If(Customer = Previous(Customer), RangeSum(Peek('RunningTotal'), Amount), Amount) AS RunningTotal
RESIDENT Sales ORDER BY Customer, SaleDate;

STORE Sales INTO [lib://QVD/Sales.qvd] (qvd);
STORE Running INTO [lib://QVD/Running.qvd] (qvd);
"""


def qlik_round(x: float, step: float) -> float:
    return math.floor(x / step + 0.5) * step  # Qlik rounds half up to a multiple of step


def qlik_class(x: float, width: float) -> str:
    lo = math.floor(x / width) * width
    return f"{lo:g} <= x < {lo + width:g}"


def qlik_semantics() -> tuple[list[dict], list[dict]]:
    sales = []
    for sid, cust, d, amount_text, path, code in SALES:
        amount = float(amount_text.replace(",", ""))
        parts = path.split("/")
        sales.append({"SaleID": sid, "Customer": cust, "SaleDate": d, "Amount": amount,
                      "Country": parts[2] if len(parts) > 2 else None,
                      "CodeDigits": "".join(c for c in code if c.isdigit()),
                      "AmountRounded": qlik_round(amount, 10), "AmountBand": qlik_class(amount, 500)})
    running, prev_customer, prev_total = [], None, None
    for r in sorted(sales, key=lambda r: (r["Customer"], r["SaleDate"])):
        total = (prev_total or 0) + r["Amount"] if r["Customer"] == prev_customer else r["Amount"]
        running.append({"SaleID": r["SaleID"], "Customer": r["Customer"], "SaleDate": r["SaleDate"],
                        "Amount": r["Amount"], "RunningTotal": round(total, 2)})
        prev_customer, prev_total = r["Customer"], total
    return sales, running


def reload_log(n_sales: int, n_running: int) -> str:
    ts = "20261001T030000.{:03d}+0000"
    lines = [f"{ts.format(0)} Execution started."]
    for i, ln in enumerate(SCRIPT.splitlines(), 1):
        if ln.strip():
            lines.append(f"{ts.format(i)} {i:04d}  {ln.rstrip(';')}")
        if ln.startswith("SQL SELECT"):
            lines.append(f"{ts.format(i)}       {n_sales:,} Lines fetched")
        if ln.startswith("RESIDENT Sales ORDER BY"):
            lines.append(f"{ts.format(i)}       {n_running:,} Lines fetched")
    lines.append(f"{ts.format(999)}       Execution finished.")
    return "\n".join(lines) + "\n"


def main() -> None:
    p = HERE / "sources" / "shop" / "raw" / "sales.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["SaleID", "Customer", "SaleDate", "AmountText", "Path", "Code"])
        w.writerows(SALES)
    (HERE / "script.qvs").write_text(SCRIPT, encoding="utf-8")
    sales, running = qlik_semantics()
    (HERE / "reload.log").write_text(reload_log(len(sales), len(running)), encoding="utf-8")
    (HERE / "qvd").mkdir(exist_ok=True)
    for name, rows in [("Sales", sales), ("Running", running)]:
        cols = list(rows[0])
        QvdTable.from_dict({"columns": cols, "data": [[r[c] for c in cols] for r in rows]}).to_qvd(
            str(HERE / "qvd" / f"{name}.qvd"))
    print("sales", len(sales), "running", len(running))


if __name__ == "__main__":
    main()
