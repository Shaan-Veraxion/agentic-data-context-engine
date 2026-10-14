"""Charts and exported reports on the orders_local fixture: figures computed by hand (make_fixture.py) must match."""
from pathlib import Path

from adce_reversesql.cli import _load, _venue
from adce_reversesql.compose import compose
from adce_reversesql.figures import check_exports

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "orders_local"


def test_exports_reach_v5_including_fan_out_and_total():
    _, app = _load(APP)
    t = compose(app)  # type: ignore[arg-type]
    venue, _, _ = _venue(APP, app)
    exports = {p.name: p.read_bytes() for p in sorted((APP / "exports").glob("*.xlsx"))}
    results = check_exports(app, t, venue, exports)  # type: ignore[arg-type]
    assert len(results) == 2
    for r in results:
        assert r.check is not None and r.check.ok, (r.file, r.problems, r.check and r.check.diffs)
