"""The validation ladder on orders_local, whose QVDs are an independent oracle (make_fixture.py)."""
from pathlib import Path

import pytest
from adce_qlik import parse_reload_log, read_qvd_header, read_script
from adce_qlik.model import build_app
from adce_reversesql.compose import compose
from adce_reversesql.local import LocalVenue, read_qvd
from adce_reversesql.reconcile import Level, Reconciler

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "orders_local"
TABLES = ("Orders", "CustomerTotals")


def reconciler(contents_override=None) -> Reconciler:
    log = parse_reload_log((APP / "reload.log").read_text(encoding="utf-8"))
    app = build_app(read_script("orders_local", {"script.qvs": (APP / "script.qvs").read_text(encoding="utf-8")}, log))
    venue = LocalVenue()
    venue.add_source_dir(APP / "sources")
    heads = {f"lib://QVD/{n}.qvd": read_qvd_header(APP / "qvd" / f"{n}.qvd") for n in TABLES}
    conts = {f"lib://QVD/{n}.qvd": read_qvd(APP / "qvd" / f"{n}.qvd") for n in TABLES}
    conts.update(contents_override or {})
    return Reconciler(compose(app), venue, log, heads, conts)


@pytest.mark.parametrize("table", TABLES)
def test_tables_reach_v4_against_the_oracle(table):
    tv = reconciler().validate(table)
    assert tv.level == Level.V4, [c for c in tv.comparisons if not c.ok]
    assert tv.venue == "local"
    checks = {c.check for c in tv.comparisons}
    assert {"rows_step", "rows_store", "field_aggregate"} <= checks


def test_row_count_basis_is_recorded():
    tv = reconciler().validate("Orders")
    steps = {c.subject.split()[0]: c.basis for c in tv.comparisons if c.check == "rows_step"}
    assert steps == {"create": "rows_read", "join": "rows_read"}
    totals = reconciler().validate("CustomerTotals")
    assert [c.basis for c in totals.comparisons if c.check == "rows_step"] == ["rows_produced"]


def test_a_wrong_figure_stops_at_v3():
    cols, data = read_qvd(APP / "qvd" / "Orders.qvd")
    i = cols.index("Amount")
    tampered = [tuple(v + 1 if j == i and k == 0 else v for j, v in enumerate(r)) for k, r in enumerate(data)]
    tv = reconciler({"lib://QVD/Orders.qvd": (cols, tampered)}).validate("Orders")
    assert tv.level == Level.V3
    bad = [c.subject for c in tv.comparisons if not c.ok]
    assert "lib://QVD/Orders.qvd.Amount.sum" in bad


class Failing:
    """The local venue, but full queries (run) or counts (scalar) fail, as an engine does mid-check."""

    def __init__(self, venue, fail: str) -> None:
        self.inner, self.fail, self.venue = venue, fail, venue.venue

    def run(self, sql, label, max_rows=100_000):
        if self.fail == "run":
            raise RuntimeError("Conversion Error: could not parse date\nLINE 1")
        return self.inner.run(sql, label, max_rows)

    def scalar(self, sql, label):
        if self.fail == "scalar":
            raise RuntimeError("Binder Error: column not found")
        return self.inner.scalar(sql, label)


def test_v2_runs_the_whole_result_not_a_count():
    # a count may skip a column that fails to compute; V2 needs the whole result to run
    r = reconciler()
    r.venue = Failing(r.venue, "run")  # type: ignore[assignment]
    tv = r.validate("Orders")
    assert tv.level == Level.V1 and tv.errors == ["execution: Conversion Error: could not parse date"]


def test_a_failure_after_v2_is_reported_not_raised():
    r = reconciler()
    r.venue = Failing(r.venue, "scalar")  # type: ignore[assignment]
    tv = r.validate("Orders")
    assert tv.level == Level.V2 and tv.errors == ["execution: Binder Error: column not found"]
