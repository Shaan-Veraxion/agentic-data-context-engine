"""Query-checker BI semantics rules: fan-out on the SQL itself, set-modifier fields, cross joins in scripts."""
from pathlib import Path

from adce_qlik.document import read_script
from adce_qlik.model import build_app
from adce_reversesql.charts import translate_charts
from adce_reversesql.cli import _load
from adce_reversesql.compose import compose
from adce_reversesql.semantics import chart_rules, fan_out, script_rules

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "orders_local"


def test_a_sum_over_a_plain_join_is_fan_out_and_the_bridge_shape_is_not():
    naive = ('SELECT c.region, SUM(o.amount) FROM orders o JOIN order_lines l ON l.order_id = o.order_id '
             'JOIN customers c ON c.customer_id = o.customer_id GROUP BY c.region')
    assert fan_out(naive) and "SUM" in fan_out(naive)[0]
    pre_aggregated = ('WITH oa AS (SELECT order_id, customer_id, SUM(amount) AS amount FROM orders GROUP BY 1, 2) '
                      'SELECT c.region, SUM(oa.amount) FROM oa JOIN customers c ON c.customer_id = oa.customer_id '
                      'GROUP BY c.region')
    assert fan_out(pre_aggregated)  # still a join before the outer SUM: the rule asks for the bridge shape
    bridge = ('SELECT b."d1", SUM(m."Amount") AS "v" FROM (SELECT DISTINCT t1."Region" AS "d1", t0."__rid" FROM '
              '(SELECT ROW_NUMBER() OVER (ORDER BY "OrderID") AS "__rid", "OrderID", "Amount" FROM orders) AS t0 '
              'LEFT JOIN customers AS t1 ON t0."CustomerID" = t1."CustomerID") AS b JOIN (SELECT ROW_NUMBER() OVER '
              '(ORDER BY "OrderID") AS "__rid", "OrderID", "Amount" FROM orders) AS m ON m."__rid" = b."__rid" '
              'GROUP BY b."d1"')
    assert fan_out(bridge) == []
    assert fan_out("SELECT region, SUM(amount) FROM orders GROUP BY region") == []


def test_the_charts_the_translator_builds_pass_the_fan_out_rule():
    _, app = _load(APP)
    t = compose(app)  # type: ignore[arg-type]
    charts = [c for c in translate_charts(app, t) if c.sql]  # type: ignore[arg-type]
    assert charts
    for c in charts:
        rules = {r.name: r for r in chart_rules(app, t, c.visual_id, c.sql)}  # type: ignore[arg-type]
        assert rules["fan_out"].ok, (c.visual_id, rules["fan_out"].message)
        assert rules["set_fields"].ok


def test_a_join_with_no_shared_field_is_flagged_in_the_script():
    script = ("A:\nLOAD * INLINE [\nx, y\n1, 2\n];\n"
              "JOIN (A) LOAD * INLINE [\nz\n3\n];\n"
              "B:\nLOAD * INLINE [\nk\n1\n];\nJOIN (B) LOAD * INLINE [\nk, w\n1, 2\n];\n")
    app = build_app(read_script("x", {"s.qvs": script}))
    a = script_rules(app, "A")[0]
    assert a.name == "join_fields" and not a.ok and not a.blocking and "cartesian" in a.message
    assert script_rules(app, "B")[0].ok
