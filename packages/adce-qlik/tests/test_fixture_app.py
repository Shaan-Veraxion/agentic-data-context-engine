"""End-to-end reading of the sales_overview fixture app (eval/gold/bi/apps/sales_overview)."""
from pathlib import Path

import pytest
from adce_core.bi_ir import StatementKind as K, VariableOrigin
from adce_qlik import parse_reload_log, read_qvd_header, read_script

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "sales_overview"


@pytest.fixture(scope="module")
def log():
    return parse_reload_log((APP / "reload.log").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def doc(log):
    return read_script("sales_overview", {"script.qvs": (APP / "script.qvs").read_text(encoding="utf-8")}, log)


def test_statements_and_kinds(doc):
    kinds = [s.kind for s in doc.statements]
    assert kinds == [K.SET, K.SET, K.LET, K.LET, K.CONNECT, K.LOAD, K.SQL, K.LOAD, K.LOAD, K.SQL, K.IF, K.TRACE,
                     K.END_IF, K.LOAD, K.DROP, K.STORE]
    assert doc.coverage.total == 16 and doc.coverage.other == 0


def test_labels_prefixes_and_spans(doc):
    by_label = {s.label: s for s in doc.statements if s.label}
    assert set(by_label) == {"Customers", "RegionMap", "Orders"}
    assert by_label["RegionMap"].prefixes == ["MAPPING"]
    orders = by_label["Orders"]
    assert (orders.span.start_line, orders.span.end_line) == (22, 27)
    join = doc.statements[13]
    assert join.prefixes == ["LEFT JOIN (Orders)"] and join.span.start_line == 35


def test_variables_from_script_and_reload_log(doc):
    v = doc.variables
    assert v["vStartDate"].value == "2025-01-01" and v["vStartDate"].origin == VariableOrigin.SCRIPT_LET
    assert v["ThousandSep"].value == ","
    assert v["vReloadDate"].value == "2026-10-01" and v["vReloadDate"].origin == VariableOrigin.RELOAD_LOG
    sql = doc.executed[9]
    assert sql.kind == K.SQL and "'2025-01-01'" in (sql.expanded_text or "")
    assert all(not s.unresolved_variables for s in doc.executed[5:])  # everything after the LETs resolves
    # the IF condition is constant after expansion, so only its true branch runs: TRACE is executed, IF is gone
    assert [s.kind for s in doc.executed].count(K.TRACE) == 1 and K.IF not in [s.kind for s in doc.executed]


def test_reload_log_counts(log):
    assert log.started_at == "20261001T021400.000+0000" and log.finished_at == "20261001T021421.000+0000"
    assert [(f.table, f.lines, f.line_no) for f in log.fetches] == [
        ("Customers", 3120, 13), ("RegionMap", 2, 20), ("Orders", 48213, 29), (None, 3120, 35)]
    assert log.errors == []


def test_qvd_header(tmp_path):
    data = (APP / "Orders.qvd.header.xml").read_bytes() + b"\r\n\x00" + bytes(range(256)) * 4
    path = tmp_path / "Orders.qvd"
    path.write_bytes(data)
    h = read_qvd_header(path)
    assert (h.table_name, h.records, h.creator_doc) == ("Orders", 47902, "Sales Overview.qvf")
    assert [f.name for f in h.fields][:3] == ["OrderID", "CustomerID", "OrderDate"] and len(h.fields) == 8
    assert h.lineage[0].discriminator == "Dremio_Sales" and "orders_v2" in (h.lineage[0].statement or "")
