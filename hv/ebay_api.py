#!/usr/bin/env python3
"""
ebay_api.py - eBay products via the OFFICIAL eBay Browse API (no browser, no bot checks, no blocks).

1. Create a free developer account: https://developer.ebay.com/  ->  "Application Keys"
   -> Production keyset: copy the App ID (Client ID) and Cert ID (Client Secret).
2. export EBAY_CLIENT_ID=...   EBAY_CLIENT_SECRET=...      (Windows: set EBAY_CLIENT_ID=...)
3. python ebay_api.py --query "jacket" --limit 30 --images
   python ebay_api.py --query "men jeans" --limit 200 --marketplace EBAY_GB --category 11483
   python ebay_api.py --query "track pants" --condition used --max-price 100

Output (output/ebay_api_<query>_<time>/): products.json, products.csv, items/<id>/product.json + images/

Each product: title, brand, price, currency, original_price, condition, seller notes, size, color, material,
all item specifics, full description, every image, seller (name / feedback score / %), location, shipping
options and cost, returns, quantity available / sold, category path, URL.
Free tier: 5,000 calls/day.
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

API = "https://api.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope"


def get_token(client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(f"{API}/identity/v1/oauth2/token",
                      headers={"Authorization": f"Basic {basic}",
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "client_credentials", "scope": SCOPE}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


class Ebay:
    def __init__(self, token: str, marketplace: str = "EBAY_US"):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace,
                               "Accept": "application/json"})

    def _get(self, path, params=None):
        for attempt in range(4):
            r = self.s.get(f"{API}{path}", params=params, timeout=30)
            if r.status_code == 429:                       # rate limited -> back off
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def search(self, query, limit=50, category=None, filters=None, sort=None):
        """Yields item summaries, paging automatically (max 200 per call, 10,000 total)."""
        offset, got = 0, 0
        while got < limit:
            params = {"q": query, "limit": min(200, limit - got), "offset": offset}
            if category:
                params["category_ids"] = category
            if filters:
                params["filter"] = ",".join(filters)
            if sort:
                params["sort"] = sort
            data = self._get("/buy/browse/v1/item_summary/search", params)
            items = data.get("itemSummaries") or []
            if not items:
                return
            for it in items:
                yield it
                got += 1
                if got >= limit:
                    return
            offset += len(items)
            if offset >= data.get("total", 0):
                return

    def item(self, item_id):
        return self._get(f"/buy/browse/v1/item/{item_id}", {"fieldgroups": "PRODUCT"})


def strip_html(s):
    if not s:
        return None
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = "\n".join(re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in s.splitlines())
    return re.sub(r"\n{2,}", "\n", s).strip() or None


def money(m):
    return (float(m["value"]), m.get("currency")) if isinstance(m, dict) and m.get("value") else (None, None)


def normalize(d: dict, rank: int) -> dict:
    aspects = {a.get("name"): a.get("value") for a in d.get("localizedAspects", []) if a.get("name")}
    price, cur = money(d.get("price"))
    orig, _ = money((d.get("marketingPrice") or {}).get("originalPrice"))
    seller = d.get("seller") or {}
    loc = d.get("itemLocation") or {}
    ships = d.get("shippingOptions") or []
    ship_cost, _ = money(ships[0].get("shippingCost")) if ships else (None, None)
    avail = (d.get("estimatedAvailabilities") or [{}])[0]
    ret = d.get("returnTerms") or {}
    imgs = [(d.get("image") or {}).get("imageUrl")] + [i.get("imageUrl") for i in d.get("additionalImages", [])]
    imgs = [re.sub(r"s-l\d+\.(jpg|jpeg|png|webp)", r"s-l1600.\1", u) for u in imgs if u]
    return {
        "rank": rank, "item_id": d.get("legacyItemId") or d.get("itemId"), "title": d.get("title"),
        "subtitle": d.get("subtitle"), "brand": d.get("brand") or aspects.get("Brand"),
        "price": price, "currency": cur, "original_price": orig,
        "condition": d.get("condition"),
        "seller_notes": d.get("conditionDescription") or "; ".join(
            x.get("descriptorValues", [{}])[0].get("content", "") for x in d.get("conditionDescriptors", [])) or None,
        "size": d.get("size") or aspects.get("Size"), "color": d.get("color") or aspects.get("Color"),
        "material": d.get("material") or aspects.get("Outer Shell Material") or aspects.get("Material"),
        "category_path": d.get("categoryPath"),
        "seller": seller.get("username"), "seller_feedback_score": seller.get("feedbackScore"),
        "seller_positive_pct": seller.get("feedbackPercentage"),
        "location": ", ".join(x for x in [loc.get("city"), loc.get("stateOrProvince"), loc.get("country")] if x),
        "shipping_cost": ship_cost,
        "shipping_options": [{"service": s.get("shippingServiceCode"), "cost": money(s.get("shippingCost"))[0],
                              "min_days": s.get("minEstimatedDeliveryDate"),
                              "max_days": s.get("maxEstimatedDeliveryDate")} for s in ships],
        "returns": (("Accepted, %s %s" % ((ret.get("returnPeriod") or {}).get("value", ""),
                                           (ret.get("returnPeriod") or {}).get("unit", ""))).strip()
                    if ret.get("returnsAccepted") else "No returns"),
        "quantity_available": avail.get("estimatedAvailableQuantity"),
        "sold_count": avail.get("estimatedSoldQuantity"),
        "buying_options": d.get("buyingOptions"),
        "description": strip_html(d.get("description")) or d.get("shortDescription"),
        "item_specifics": aspects, "images": list(dict.fromkeys(imgs)),
        "url": d.get("itemWebUrl"),
    }


def download(urls, folder):
    os.makedirs(folder, exist_ok=True)
    files = []
    for i, u in enumerate(urls, 1):
        try:
            r = requests.get(u, timeout=30)
            if r.ok and len(r.content) > 1500:
                ext = {"image/png": ".png", "image/webp": ".webp"}.get(r.headers.get("Content-Type", ""), ".jpg")
                p = os.path.join(folder, f"{i:02d}{ext}")
                open(p, "wb").write(r.content)
                files.append(p)
        except requests.RequestException:
            pass
    return files


def main():
    ap = argparse.ArgumentParser(description="eBay products via the official Browse API")
    ap.add_argument("--query", "-q", required=True)
    ap.add_argument("--limit", "-n", type=int, default=30)
    ap.add_argument("--marketplace", default="EBAY_US", help="EBAY_US, EBAY_GB, EBAY_DE, EBAY_AU, EBAY_CA ...")
    ap.add_argument("--category", help="eBay category id(s), e.g. 57988 = Men's Coats, Jackets & Vests")
    ap.add_argument("--condition", choices=["new", "used"])
    ap.add_argument("--min-price", type=float)
    ap.add_argument("--max-price", type=float)
    ap.add_argument("--sort", choices=["price", "-price", "newlyListed", "endingSoonest"])
    ap.add_argument("--images", action="store_true", help="download every image")
    ap.add_argument("--out", "-o", default="output")
    a = ap.parse_args()

    cid, sec = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if not cid or not sec:
        sys.exit("Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (free keys: https://developer.ebay.com/my/keys)")

    filters = []
    if a.condition:
        filters.append("conditions:{%s}" % ("NEW" if a.condition == "new" else "USED"))
    if a.min_price is not None or a.max_price is not None:
        filters.append("price:[%s..%s]" % (a.min_price if a.min_price is not None else "",
                                           a.max_price if a.max_price is not None else ""))
        currency = {"EBAY_US": "USD", "EBAY_GB": "GBP", "EBAY_DE": "EUR", "EBAY_FR": "EUR", "EBAY_IT": "EUR",
                    "EBAY_ES": "EUR", "EBAY_AU": "AUD", "EBAY_CA": "CAD"}.get(a.marketplace)
        if currency:
            filters.append(f"priceCurrency:{currency}")
    filters = [f for f in filters if f]

    eb = Ebay(get_token(cid, sec), a.marketplace)
    summaries = list(eb.search(a.query, a.limit, a.category, filters, a.sort))
    print(f"[+] {len(summaries)} search results, fetching full details...")
    with ThreadPoolExecutor(6) as ex:
        details = list(ex.map(lambda s: eb.item(s["itemId"]), summaries))

    run = os.path.join(a.out, f"ebay_api_{re.sub(r'[^A-Za-z0-9]+', '-', a.query)}_{datetime.now():%Y%m%d_%H%M%S}")
    rows = []
    for rank, d in enumerate(details, 1):
        r = normalize(d, rank)
        folder = os.path.join(run, "items", str(r["item_id"]))
        os.makedirs(folder, exist_ok=True)
        if a.images:
            r["image_files"] = download(r["images"], os.path.join(folder, "images"))
        json.dump(r, open(os.path.join(folder, "product.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        rows.append(r)
        print(f"  {rank:>3}. {r['title'][:70]} | {r['price']} {r['currency']} | {len(r['images'])} images")

    json.dump(rows, open(os.path.join(run, "products.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    with open(os.path.join(run, "products.csv"), "w", newline="", encoding="utf-8-sig") as f:
        cols = list(dict.fromkeys(k for r in rows for k in r))
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                        for k, v in r.items()})
    print(f"[+] saved -> {run}")


if __name__ == "__main__":
    main()
