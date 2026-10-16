"""Dremio names for the sources a translation reads: mapped ones are rewritten, the rest are listed in a comment."""
from adce_reversesql.resolve import apply_mappings, mapping_key

SQL = ('WITH f AS (SELECT x FROM "__file__"."$(vLoadfile)"), g AS (SELECT "Date" FROM "__generated__"."Cal(1)"), '
       'b AS (SELECT x FROM tblinvoicehead AS t JOIN dbo.TblY AS y ON t.x = y.x) SELECT * FROM b')


def test_mapped_sources_are_rewritten_and_the_rest_listed():
    r = apply_mappings(SQL, {"source::$(vLoadfile)": "lake.fx.monthly",
                             mapping_key("source:Pluto Prod:TblInvoiceHead"): '"Pluto Prod".dbo.tblinvoicehead'})
    assert 'FROM "lake"."fx"."monthly"' in r.sql and 'FROM "Pluto Prod"."dbo"."tblinvoicehead" AS t' in r.sql
    assert r.unmapped == ["generated:Cal(1)"] and r.unqualified == ["dbo.tbly"]
    assert r.sql.startswith("-- NOT RUNNABLE IN DREMIO YET")


def test_nothing_to_map_leaves_no_comment():
    r = apply_mappings('SELECT x FROM "shop"."raw"."orders"', {"sql:shop.raw.orders": "shop.raw.orders"})
    assert r.unmapped == [] and r.unqualified == [] and 'FROM "shop"."raw"."orders"' in r.sql
