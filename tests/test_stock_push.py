"""hv/stock_push.py: harvested products -> Stock's POST /products/bulk.

    pip install pytest && python -m pytest -q tests
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hv import stock_push as SP  # noqa: E402
from hv.db import DB  # noqa: E402

SHOPIFY = {"url": "https://pangaia.com/products/365-track-pants", "site": "pangaia", "id": 7712,
           "title": "365 Midweight Track Pants", "brand": "Pangaia", "category": "Track Pants",
           "price": 120.0, "currency": "EUR", "original_price": 150.0, "color": "Sand",
           "description": "Organic cotton.", "images": ["https://cdn.shopify.com/a.jpg", "https://cdn.shopify.com/b.jpg"],
           "attributes": {"variants": [{"title": "S", "sku": "PG-S", "price": "120.00", "compare_at_price": "150.00",
                                        "available": True},
                                       {"title": "M", "sku": "PG-M", "price": "120.00", "available": False}]}}
NO_BRANDS = lambda scfg: None          # noqa: E731 - push without reading Stock's Brand Master

VINTED = {"url": "https://www.vinted.nl/items/5043909102-track-pants", "site": "vinted", "id": 5043909102,
          "title": "Pangaia track pants women", "brand": "Pangaia", "price": 45, "currency": "EUR",
          "size": "M", "condition": "Heel goed", "seller": "anna_k", "images": ["https://images1.vinted.net/x.webp"]}


def test_a_shop_product_is_retail_with_its_variants_and_struck_through_price():
    row, why = SP.to_row({**SHOPIFY, "job": "pangaia-track-pants", "scraped_at": "2026-11-22T10:00:00+00:00"})
    assert why is None
    assert row["brand"] == "Pangaia" and row["externalId"] == "pangaia:7712" and row["market"] == "EU"
    assert row["category"] == "Track Pants" and row["status"] == "ACTIVE"
    assert row["images"] == SHOPIFY["images"]
    assert [p["evidenceType"] for p in row["prices"]] == ["RETAIL_CURRENT", "RETAIL_ORIGINAL"]
    assert row["prices"][0] == {"amount": 120.0, "currency": "EUR", "evidenceType": "RETAIL_CURRENT",
                                "market": "EU", "url": SHOPIFY["url"], "observedAt": "2026-11-22T10:00:00+00:00",
                                "note": "Harvester · pangaia"}
    assert row["variants"] == [{"sizeLabel": "S", "sku": "PG-S", "price": 120.0, "originalPrice": 150.0,
                                "currency": "EUR", "inStock": True},
                               {"sizeLabel": "M", "sku": "PG-M", "price": 120.0, "currency": "EUR", "inStock": False}]
    assert "retail" in row["tags"] and "harvester" in row["tags"]


def test_a_resale_listing_is_an_asking_price_with_market_and_gender():
    row, _ = SP.to_row(VINTED)
    assert row["prices"][0]["evidenceType"] == "RESALE_ASKING" and row["prices"][0]["condition"] == "Heel goed"
    assert row["market"] == "NL" and row["gender"] == "WOMEN" and row["sizeOnLabel"] == "M"
    assert row["variants"] == [{"sizeLabel": "M"}]
    assert row["attributes"]["harvester_seller"] == "anna_k" and "resale" in row["tags"]
    # eBay: the condition decides
    used = SP.to_row({**VINTED, "url": "https://www.ebay.com/itm/123456789012", "site": "ebay",
                      "condition": "Pre-owned", "currency": "USD"})[0]
    new = SP.to_row({**VINTED, "url": "https://www.ebay.com/itm/123456789013", "site": "ebay",
                     "condition": "New with tags", "currency": "USD"})[0]
    assert used["prices"][0]["evidenceType"] == "RESALE_ASKING" and used["market"] == "US"
    assert new["prices"][0]["evidenceType"] == "RETAIL_CURRENT"
    # INR shop on a .com domain -> market from the currency
    assert SP.to_row({**SHOPIFY, "url": "https://www.flipkart.com/x/p/itm1", "site": "flipkart",
                      "currency": "INR"})[0]["market"] == "IN"


def test_a_product_stock_cannot_take_is_not_sent():
    assert SP.to_row({**VINTED, "brand": None}) == (None, "no brand on the page")
    assert SP.to_row({**VINTED, "url": ""}) == (None, "no product URL")
    assert SP.to_row({**VINTED, "price": None})[0].get("prices") is None      # still a product, no price


def _db(tmp_path):
    db = DB(str(tmp_path / "h.db"))
    for i, rec in enumerate([SHOPIFY, VINTED, {**VINTED, "url": "https://www.vinted.nl/items/9-x", "id": 9,
                                               "brand": "Nobrand"}, {**VINTED, "url": "https://www.vinted.nl/items/10-y",
                                                                     "id": 10, "brand": None}]):
        db.save_product(rec, "pangaia-track-pants" if i == 0 else "vinted-jackets", "http")
    return db


def test_push_sends_by_site_records_answers_and_resends_only_what_changed(tmp_path):
    db = _db(tmp_path)
    calls = []

    def poster(scfg, site, rows, dry_run):
        calls.append((site, [r["externalId"] for r in rows], dry_run))
        results = []
        for i, r in enumerate(rows):
            if r["brand"] == "Nobrand":
                results.append({"index": i, "status": "failed", "code": "BRAND_NOT_FOUND",
                                "error": 'Brand "Nobrand" is not in the Brand Master'})
            else:
                results.append({"index": i, "status": "would_create" if dry_run else "created",
                                "stockCode": f"PRD_{i}"})
        return {"ok": True, "data": {"results": results, "summary": {"total": len(rows)}}}

    cfg = {"stock": {"api_key": "stk_test"}, "jobs": [{"name": "pangaia-track-pants", "category": "track pants"}]}
    dry = SP.push(db, cfg, dry_run=True, poster=poster, brand_loader=NO_BRANDS)
    assert dry["would_create"] == 2 and dry["failed"] == 1 and dry["skipped"] == 1
    assert SP.to_push(db) and len(SP.to_push(db)) == 4                    # a dry run records nothing

    r = SP.push(db, cfg, poster=poster, brand_loader=NO_BRANDS)
    assert (r["created"], r["failed"], r["skipped"]) == (2, 1, 1)
    assert sorted(c[0] for c in calls[-2:]) == ["pangaia", "vinted"]
    assert any("BRAND_NOT_FOUND" in f["why"] for f in r["failures"])
    assert SP.to_push(db) == []                                           # nothing new
    assert len(SP.to_push(db, retry_failed=True)) == 2                    # the refused and the unsendable

    # scraped again -> sent again
    db.save_product({**VINTED, "price": 40}, "vinted-jackets", "http")
    db.x("UPDATE products SET scraped_at='2999-01-01T00:00:00+00:00' WHERE url=?", (VINTED["url"],))
    assert [p["url"] for p in SP.to_push(db)] == [VINTED["url"]]
    assert SP.summary(db)["created"] == 2


def test_push_without_a_key_sends_nothing_and_a_refused_batch_is_reported(tmp_path):
    db = _db(tmp_path)
    assert SP.push(db, {"stock": {}}, poster=lambda *a: 1 / 0, brand_loader=NO_BRANDS)["errors"][0].startswith("stock.api_key")
    r = SP.push(db, {"stock": {"api_key": "k"}}, poster=lambda *a: {"ok": False, "error": "HTTP 401: bad key"},
                brand_loader=NO_BRANDS)
    assert r["sent"] == 0 and any("401" in e for e in r["errors"])
    assert len(SP.to_push(db)) == 3            # the batch is retried later; the brand-less one is noted as not sendable
    assert SP.category_of({"category": "bottoms.casual_pants"}, None) == "Bottoms Casual Pants"


def test_post_bulk_sends_the_key_source_and_rows():
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"success": True, "data": {"summary": {"total": 1}, "results": []}}

    class Session:
        def post(self, url, json=None, timeout=None, headers=None):
            seen.update(url=url, body=json, headers=headers)
            return Resp()

    r = SP.post_bulk({"url": "https://stock.test", "api_key": "stk"}, "vinted", [{"brand": "x"}], True, Session())
    assert r["ok"] and seen["url"] == "https://stock.test/products/bulk" and seen["headers"]["x-api-key"] == "stk"
    assert seen["body"]["source"] == {"key": "harvester-vinted", "name": "Harvester - vinted"}
    assert seen["body"]["dryRun"] is True


BRANDS = SP.BrandIndex({SP.norm_brand(v): name for name, vals in {
    "Arc'teryx": ["Arc'teryx", "arcteryx"], "The North Face": ["The North Face", "TNF"],
    "Calvin Klein": ["Calvin Klein"], "Only": ["Only"], "Colmar": ["Colmar"], "Strellson": ["Strellson"],
}.items() for v in vals})


def test_brand_is_stocks_spelling_and_comes_from_the_title_when_the_listing_has_none():
    assert SP.resolve_brand({"brand": "Arcteryx"}, BRANDS) == ("Arc'teryx", "page-matched")
    assert SP.resolve_brand({"brand": "ARC TERYX"}, BRANDS) == ("Arc'teryx", "page-matched")
    assert SP.resolve_brand({"brand": "Strellson"}, BRANDS) == ("Strellson", "page")
    assert SP.resolve_brand({"brand": "Calvin Klein Jeans"}, BRANDS) == ("Calvin Klein", "page-matched")
    assert SP.resolve_brand({"brand": "Nobrand"}, BRANDS) == ("Nobrand", "page")          # Stock refuses + says so
    assert SP.resolve_brand({"title": "Giacca invernale The North Face taglia S"}, BRANDS) == ("The North Face", "title")
    assert SP.resolve_brand({"title": "Giacca di lana Colmar"}, BRANDS) == ("Colmar", "title")
    # a stop-word brand is never taken from a title alone; an unknown title gives nothing
    assert SP.resolve_brand({"title": "Only one owner leather jacket"}, BRANDS) == (None, "none")
    assert SP.resolve_brand({"title": "Ferrari - Jas"}, BRANDS) == (None, "none")
    # without the Brand Master: as before
    assert SP.resolve_brand({"title": "The North Face jas"}, None) == (None, "none")
    row, _ = SP.to_row({**VINTED, "brand": None, "title": "Giacca invernale The North Face taglia S",
                        "url": "https://www.subito.it/abbigliamento-accessori/giacca-123456789.htm", "site": "subito"},
                       None, BRANDS)
    assert row["brand"] == "The North Face" and row["attributes"]["harvester_brand_from"] == "title"
    assert SP.to_row({**VINTED, "brand": None, "title": "Ferrari - Jas"}, None, BRANDS)[1].startswith("no brand")


def test_size_is_split_into_international_and_eu():
    assert SP.split_size("L - INT (Geschatte maat)") == {"internationalSize": "L"}
    assert SP.split_size("EU 38") == {"euSize": "38"}
    assert SP.split_size("38 EU / M") == {"internationalSize": "M", "euSize": "38"}
    assert SP.split_size("Maat 40") == {"euSize": "40"}
    assert SP.split_size("US 36 / EU 52") == {"euSize": "52"}
    assert SP.split_size("2XL") == {"internationalSize": "XXL"}
    assert SP.split_size("One size") == {} and SP.split_size(None) == {}
    row, _ = SP.to_row({**VINTED, "size": "L - INT (Geschatte maat)"})
    assert row["internationalSize"] == "L" and row["sizeOnLabel"] == "L - INT (Geschatte maat)"


def test_the_brand_master_is_read_page_by_page_and_a_refusal_means_none():
    pages = [{"items": [{"name": "Arc'teryx", "slug": "arcteryx", "aliases": [{"value": "Arcteryx"}]}], "total": 2},
             {"items": [{"name": "Colmar", "slug": "colmar", "aliases": []}], "total": 2}]

    class R:
        def __init__(self, code, body):
            self.status_code, self.body = code, body

        def json(self):
            return self.body

    class S:
        calls = []

        def get(self, url, params=None, timeout=None, headers=None):
            self.calls.append((url, params, headers["x-api-key"]))
            return R(200, {"data": pages[len(self.calls) - 1]})

    idx = SP.load_brands({"url": "https://stock.test", "api_key": "stk"}, S())
    assert len(S.calls) == 2 and S.calls[0] == ("https://stock.test/brands", {"skip": 0, "take": 100}, "stk")
    assert idx.match("ARC'TERYX") == "Arc'teryx" and idx.match("colmar") == "Colmar"

    class Deny:
        def get(self, *a, **k):
            return R(401, {})

    assert SP.load_brands({"url": "https://stock.test", "api_key": "stk"}, Deny()) is None
