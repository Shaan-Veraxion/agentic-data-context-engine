"""Generate the rulefix_local fixture: a statement the rules translate into SQL that runs but is wrong.

Qlik writes a number inside text without trailing zeros ('P' & 20 -> 'P20'); CAST(20.0 AS VARCHAR) gives '20.0'.
The rule translation therefore runs (V2) and matches row counts (V3) but not the QVD contents (V4). The LLM
diagnosis step must find the statement and correct it. The oracle is computed here by hand, never from SQL.

    uv run python eval/gold/bi/apps/rulefix_local/make_fixture.py
"""
from __future__ import annotations

import csv
from pathlib import Path

from pyqvd import QvdTable

HERE = Path(__file__).resolve().parent

PRODUCTS = [(1, "bolt", 10.5), (2, "nut", 20.0), (3, "gear", 7.25), (4, "shaft", 100.0), (5, "pin", 0.5)]

SCRIPT = """\
LIB CONNECT TO 'Dremio_Shop';

Products:
LOAD ID, Name, Price, 'P' & Price AS PriceTag;
SQL SELECT ID, Name, Price FROM "shop"."raw"."products";

STORE Products INTO [lib://QVD/Products.qvd] (qvd);
"""


def qlik_number_text(x: float) -> str:
    return f"{x:g}"  # Qlik: shortest form, no trailing zeros, no '.0' for whole numbers


def main() -> None:
    p = HERE / "sources" / "shop" / "raw" / "products.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ID", "Name", "Price"])
        w.writerows(PRODUCTS)
    (HERE / "script.qvs").write_text(SCRIPT, encoding="utf-8")
    rows = [{"ID": i, "Name": n, "Price": pr, "PriceTag": "P" + qlik_number_text(pr)} for i, n, pr in PRODUCTS]
    ts = "20261001T040000.{:03d}+0000"
    log = [f"{ts.format(0)} Execution started."]
    for i, ln in enumerate(SCRIPT.splitlines(), 1):
        if ln.strip():
            log.append(f"{ts.format(i)} {i:04d}  {ln.rstrip(';')}")
        if ln.startswith("SQL SELECT"):
            log.append(f"{ts.format(i)}       {len(rows)} Lines fetched")
    log.append(f"{ts.format(999)}       Execution finished.")
    (HERE / "reload.log").write_text("\n".join(log) + "\n", encoding="utf-8")
    (HERE / "qvd").mkdir(exist_ok=True)
    cols = list(rows[0])
    QvdTable.from_dict({"columns": cols, "data": [[r[c] for c in cols] for r in rows]}).to_qvd(
        str(HERE / "qvd" / "Products.qvd"))
    print("products", len(rows))


if __name__ == "__main__":
    main()
