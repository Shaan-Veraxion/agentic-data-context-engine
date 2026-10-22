"""QVD chains across applications (ADR-Q4 §3): producers inlined, chosen, ordered."""
import sqlglot
from adce_reversesql.chains import Producer, choose, inline_producers, order_apps, qvd_key
from sqlglot import exp

ORDERS = Producer("extract", "Orders", "lib://QVD/Orders.qvd",
                  'WITH orders_sql AS (SELECT "id", "amount" FROM "erp"."orders") SELECT * FROM orders_sql')
TOTALS = Producer("transform", "Totals", "lib://QVD/Totals.qvd",
                  'WITH t_qvd AS (SELECT "id", "amount" FROM "__qvd__"."lib://QVD/Orders.qvd"), '
                  't AS (SELECT "id", SUM("amount") AS "s" FROM t_qvd GROUP BY "id") SELECT * FROM t')
CONSUMER = ('WITH c_qvd AS (SELECT "id", "s" FROM "__qvd__"."lib://Shared/totals.qvd"), '
            'm AS (SELECT * FROM "__qvd__"."lib://QVD/Missing.qvd") SELECT * FROM c_qvd JOIN m ON c_qvd."id" = m."id"')


def test_qvd_key_matches_by_file_name():
    assert qvd_key("lib://QVD/Orders.qvd") == qvd_key("[lib://Shared/orders.QVD]") == "orders.qvd"


def test_a_chain_of_producers_is_inlined_and_reads_only_database_sources():
    r = inline_producers(CONSUMER, {"orders.qvd": ORDERS, "totals.qvd": TOTALS})
    tree = sqlglot.parse_one(r.sql, read="dremio")
    ctes = [c.alias_or_name for c in tree.find_all(exp.CTE)]
    # the producers' CTEs, renamed under their app, come before the consumer's, in dependency order
    assert ctes.index("extract__orders_sql") < ctes.index("qvd__orders") < ctes.index("transform__t_qvd") \
        < ctes.index("transform__t") < ctes.index("qvd__totals") < ctes.index("c_qvd")
    assert '"erp"."orders"' in r.sql and "lib://Shared/totals.qvd" not in r.sql
    assert [(i["qvd"], i["app_id"], i["depth"]) for i in r.inlined] == [("orders.qvd", "extract", 1),
                                                                        ("totals.qvd", "transform", 0)]
    assert r.missing == ["missing.qvd"]  # no producer: stays a placeholder, reported


def test_a_mapped_qvd_is_left_to_the_mapping_and_a_cycle_stops():
    r = inline_producers(CONSUMER, {"totals.qvd": TOTALS}, mapped={"qvd:lib://Shared/totals.qvd"})
    assert r.inlined == [] and "lib://Shared/totals.qvd" in r.sql
    loop = Producer("a", "T", "x.qvd", 'SELECT * FROM "__qvd__"."x.qvd"')
    r = inline_producers('SELECT * FROM "__qvd__"."x.qvd"', {"x.qvd": loop})
    assert len(r.inlined) == 1 and r.missing == ["x.qvd"]  # the producer reads itself: inlined once, then stops


def test_choosing_between_producers():
    other = Producer("sales_copy", "Orders", "lib://QVD/Orders.qvd", "SELECT 1")
    chosen, amb = choose({"orders.qvd": [ORDERS, other]})
    assert chosen == {} and amb == {"orders.qvd": ["extract", "sales_copy"]}
    chosen, amb = choose({"orders.qvd": [ORDERS, other]}, creators={"orders.qvd": r"C:\Qlik\Extract.qvw"})
    assert chosen["orders.qvd"].app_id == "extract" and amb == {}
    chosen, _ = choose({"orders.qvd": [ORDERS, other]}, confirmed={"orders.qvd": "sales_copy"})
    assert chosen["orders.qvd"].app_id == "sales_copy"
    r = inline_producers(CONSUMER, {}, ambiguous={"totals.qvd": ["a", "b"]})
    assert r.ambiguous == {"totals.qvd": ["a", "b"]} and r.missing == ["missing.qvd"]


def test_producers_run_first():
    produced = {"extract": {"orders.qvd"}, "transform": {"totals.qvd"}}
    reads = {"transform": {"orders.qvd"}, "report": {"totals.qvd"}}
    assert order_apps(["report", "transform", "extract"], produced, reads) == ["extract", "transform", "report"]
    assert order_apps(["b", "a"], {"a": {"x"}, "b": {"y"}}, {"a": {"y"}, "b": {"x"}}) == ["a", "b"]  # a cycle
