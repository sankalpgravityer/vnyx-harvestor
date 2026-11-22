"""Site adapters. Each picks the *least intrusive* way to get complete product data:

  shopify  : /products.json (250 products per request, all images/variants/sizes/stock) - no page scraping at all
  ebay_api : official eBay Browse API (needs free keys) - never bot-checked
  generic  : search pages via stealth browser -> product pages via HTTP (schema.org JSON-LD) when that gives
             complete data, otherwise via the stealth browser (full DOM extraction from product_scraper.py)
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import time
from urllib.parse import urljoin, urlparse

from . import category_scraper as cs
from . import product_scraper as ps
from .guard import DomainBlocked, domain_of
from .net import looks_blocked

log = logging.getLogger("harvester")


def strip_html(s):
    if not s:
        return None
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", s, flags=re.I)
    s = _html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = "\n".join(re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in s.splitlines())
    return re.sub(r"\n{2,}", "\n", s).strip() or None


def words_match(text: str, query: str) -> bool:
    t = text.lower()
    return all(w in t for w in re.findall(r"\w+", query.lower()))


class HttpChallenge(Exception):
    """The plain HTTP client got a challenge page; a real browser may still be welcome."""


class Base:
    name = "base"

    def __init__(self, ctx, job):
        self.ctx, self.job = ctx, job
        self.site = job["site"].rstrip("/")
        if "://" not in self.site:
            self.site = "https://" + self.site
        self.domain = domain_of(self.site)

    # helpers --------------------------------------------------------------
    def http(self, url, sleep=True, expect_json=False, strike=True):
        g = self.ctx.guard
        if not g.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        g.before(url, sleep=sleep)
        status, text = self.ctx.http.get(url)
        reason = looks_blocked(status, text if not expect_json else "")
        if reason:
            if not strike:
                raise HttpChallenge(reason)
            g.after_block(domain_of(url), reason)
            raise DomainBlocked(reason)
        g.after_ok(domain_of(url))
        return status, text

    def discover(self, need: int) -> list[dict]:
        """Return complete product records (API-style adapters) or [] after enqueueing URLs."""
        raise NotImplementedError

    def scrape(self, qrow: dict) -> tuple[dict, str]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
class Shopify(Base):
    name = "shopify"

    @staticmethod
    def detect(ctx, site) -> bool:
        try:
            url = site.rstrip("/") + "/products.json?limit=1"
            ctx.guard.before(url, sleep=False)
            st, txt = ctx.http.get(url)
            return st == 200 and '"products"' in txt[:200]
        except Exception:
            return False

    def discover(self, need):
        cur = self.job.get("currency")
        if not cur:                       # /cart.js is disallowed by Shopify's robots.txt -> read the homepage
            try:
                st, txt = self.http(self.site + "/")
                m = (re.search(r'Shopify\.currency\s*=\s*\{"active":"([A-Z]{3})"', txt)
                     or re.search(r'"currency(?:Code)?":"([A-Z]{3})"', txt))
                cur = m.group(1) if m else None
            except Exception:
                pass
        out, q = [], self.job.get("category", "")
        for page in range(1, int(self.job.get("max_pages", 6)) + 1):
            st, txt = self.http(f"{self.site}/products.json?limit=250&page={page}", expect_json=True)
            prods = json.loads(txt).get("products", [])
            if not prods:
                break
            for p in prods:
                hay = " ".join([p.get("title", ""), p.get("product_type", ""), p.get("handle", ""),
                                " ".join(p.get("tags", []) if isinstance(p.get("tags"), list) else [str(p.get("tags"))])])
                if q and not words_match(hay, q):
                    continue
                url = f"{self.site}/products/{p['handle']}"
                if self.ctx.db.x("SELECT 1 FROM products WHERE url=?", (url,)).fetchone():
                    continue
                out.append(self._record(p, url, cur))
                if len(out) >= need:
                    return out
        return out

    def _record(self, p, url, cur):
        variants = p.get("variants", [])
        prices = [float(v["price"]) for v in variants if v.get("price")]
        compare = [float(v["compare_at_price"]) for v in variants if v.get("compare_at_price")]
        options = {o.get("name"): o.get("values") for o in p.get("options", [])}
        size = next((v for k, v in options.items() if re.search(r"size|maat|größe|taille", k or "", re.I)), None)
        color = next((v for k, v in options.items() if re.search(r"colou?r|kleur|farbe", k or "", re.I)), None)
        return {
            "url": url, "site": ps.site_name(url), "id": p.get("id"), "title": p.get("title"),
            "brand": p.get("vendor"), "category": p.get("product_type"),
            "price": min(prices) if prices else None, "currency": cur,
            "original_price": max(compare) if compare else None,
            "size": ", ".join(map(str, size)) if size else None,
            "color": ", ".join(map(str, color)) if color else None,
            "availability": "InStock" if any(v.get("available") for v in variants) else "OutOfStock",
            "description": strip_html(p.get("body_html")),
            "images": [i.get("src") for i in p.get("images", []) if i.get("src")],
            "attributes": {"tags": p.get("tags"), "options": options,
                           "variants": [{"title": v.get("title"), "sku": v.get("sku"), "price": v.get("price"),
                                         "compare_at_price": v.get("compare_at_price"),
                                         "available": v.get("available")} for v in variants],
                           "published_at": p.get("published_at"), "updated_at": p.get("updated_at")},
        }


# ---------------------------------------------------------------------------
class EbayApi(Base):
    name = "ebay_api"

    def discover(self, need):
        from . import ebay_api as ea
        e = self.ctx.cfg.get("ebay", {})
        cid, sec = e.get("client_id"), e.get("client_secret")
        if not cid or not sec:
            raise RuntimeError("ebay_api job needs ebay.client_id / client_secret (free: developer.ebay.com)")
        if not getattr(self.ctx, "_ebay", None):
            self.ctx._ebay = ea.Ebay(ea.get_token(cid, sec), e.get("marketplace", "EBAY_US"))
        eb, out = self.ctx._ebay, []
        filters = self.job.get("filters") or []
        for s in eb.search(self.job["category"], need * 3, self.job.get("category_id"), filters):
            url = s.get("itemWebUrl", "").split("?")[0]
            if not url or self.ctx.db.x("SELECT 1 FROM products WHERE url=?", (url,)).fetchone():
                continue
            time.sleep(0.5)                                         # official API: light pacing is enough
            r = ea.normalize(eb.item(s["itemId"]), 0)
            r.update({"url": url, "site": "ebay", "id": r.get("item_id"),
                      "attributes": {"item_specifics": r.pop("item_specifics", {}),
                                     **{k: r.get(k) for k in ("shipping_cost", "returns", "seller_notes",
                                                              "quantity_available", "sold_count",
                                                              "seller_feedback_score", "seller_positive_pct")}}})
            out.append(r)
            if len(out) >= need:
                break
        return out


# ---------------------------------------------------------------------------
JSONLD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
META_RE = re.compile(r'<meta[^>]+(?:property|name)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']', re.I)


class Generic(Base):
    name = "generic"

    def __init__(self, ctx, job):
        super().__init__(ctx, job)
        self.mode = job.get("fetch", "auto")              # auto | http | browser
        self.min_images = int(job.get("min_images", 2))

    def discover(self, need):
        drv = self.ctx.browser.get(self.domain)
        self.ctx.guard.before(self.site)
        delay = self.ctx.guard.min_delay
        rows = cs.scrape_category(drv, self.site, self.job["category"], max_products=need * 2,
                                  max_pages=int(self.job.get("max_pages", 3)),
                                  search_url=self.job.get("search_url"), headless=True, delay=delay)
        if not rows:
            if ps.is_blocked(drv):
                self.ctx.guard.after_block(self.domain, "bot-check on search page")
                raise DomainBlocked("bot-check on search page")
            shot = self._debug_snapshot(drv, "no-products")
            log.warning("[%s] no products found on search page (check selectors / search_url) - "
                        "what the browser saw: %s", self.job["name"], shot or "no snapshot")
            return []
        self.ctx.guard.after_ok(self.domain)
        added = sum(self.ctx.db.enqueue(r["url"], self.job["name"], r["site"], r) for r in rows)
        log.info("[%s] discovered %d new product URLs (%d listed)", self.job["name"], added, len(rows))
        return []

    def _debug_snapshot(self, drv, why: str) -> str | None:
        """A screenshot + the HTML of the page the browser is on, in data/debug/, to see what a site served."""
        try:
            d = os.path.join(self.ctx.cfg["storage"]["dir"], "debug")
            os.makedirs(d, exist_ok=True)
            base = os.path.join(d, f"{self.job['name']}-{why}-{time.strftime('%Y%m%d-%H%M%S')}")
            drv.save_screenshot(base + ".png")
            with open(base + ".html", "w", encoding="utf-8") as f:
                f.write(f"<!-- {drv.current_url} | title: {drv.title} -->\n" + drv.page_source)
            return base + ".png (+ .html)"
        except Exception:
            return None

    # --- product page ---
    def scrape(self, qrow):
        url = qrow["url"]
        listing = json.loads(qrow.get("listing") or "{}")
        prefer_browser = self.ctx.db.domain(self.domain).get("tier") == "browser"
        if self.mode == "http" or (self.mode == "auto" and not prefer_browser):
            try:
                rec = self._http_product(url)
            except HttpChallenge as e:
                log.info("[%s] %s challenges plain HTTP clients (%s) -> using the stealth browser for this site",
                         self.job["name"], self.domain, e)
                self.ctx.db.update_domain(self.domain, tier="browser")
                rec = None
            if rec and (self.mode == "http" or self._complete(rec)):
                return self._fill(rec, listing), "http"
            if self.mode == "auto" and rec is not None:
                misses = self.ctx.tier_memory.get(self.domain + ":misses", 0) + 1
                self.ctx.tier_memory[self.domain + ":misses"] = misses
                if misses >= 2:                      # HTTP data keeps being incomplete -> browser from now on
                    self.ctx.db.update_domain(self.domain, tier="browser")
        return self._fill(self._browser_product(url), listing), "browser"

    def _complete(self, r):
        return bool(r.get("title") and r.get("price") is not None and len(r.get("images") or []) >= self.min_images)

    def _fill(self, rec, listing):
        for k in ("title", "brand", "price", "currency"):
            if rec.get(k) in (None, "") and listing.get(k) not in (None, ""):
                rec[k] = listing[k]
        return rec

    def _http_product(self, url):
        st, page = self.http(url, strike=(self.mode == "http"))
        if st != 200:
            return None
        # Shopify product page on a non-Shopify-configured job -> use its JSON
        if "/products/" in url and "cdn.shopify.com" in page[:300000]:
            try:
                st2, js = self.ctx.http.get(url.split("?")[0].rstrip("/") + ".js")
                if st2 == 200:
                    return Shopify(self.ctx, self.job)._record(json.loads(js), url, None)
            except Exception:
                pass
        prods, crumbs, _ = ps.parse_jsonld(JSONLD_RE.findall(page))
        if not prods:
            return None
        meta = {}
        for k, v in META_RE.findall(page):
            meta.setdefault(k.lower(), _html.unescape(v))
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
        main = ps.pick_main_product(prods, url, strip_html(h1.group(1)) if h1 else meta.get("og:title", ""))
        r = ps.jsonld_to_record(main)
        imgs = r.get("images") or []
        # Variants of THIS product only (ProductGroup). The other products a page describes are its
        # recommendations - their photos are not this product's (Sellpy, 22 Nov 2026: 53 other items).
        for p in prods:
            if p is not main and ps.same_product(main, p):
                imgs += ps.jsonld_to_record(p).get("images") or []
        if meta.get("og:image"):
            imgs.append(meta["og:image"])
        price, cur = r.get("price"), r.get("currency")
        if price is None:
            price = meta.get("product:price:amount") or meta.get("og:price:amount")
            cur = cur or meta.get("product:price:currency") or meta.get("og:price:currency")
        p_num, p_cur = ps.parse_price(price) if not isinstance(price, (int, float)) else (float(price), None)
        r.update({"url": url, "site": ps.site_name(url), "price": p_num, "currency": cur or p_cur,
                  "images": ps.dedupe_images([urljoin(url, i) for i in imgs if isinstance(i, str)]),
                  "breadcrumbs": crumbs, "description": strip_html(r.get("description")) or meta.get("og:description"),
                  "id": r.get("sku") or ps.slugify(urlparse(url).path.rstrip("/").split("/")[-1])})
        return r

    def _browser_product(self, url):
        self.ctx.guard.before(url)
        reason = self.ctx.browser.open(url)
        if reason:
            if "bot-check" in reason:
                self.ctx.guard.after_block(self.domain, reason)
                raise DomainBlocked(reason)
            raise RuntimeError(reason)
        self.ctx.guard.after_ok(self.domain)
        drv = self.ctx.browser.driver
        rec = ps.scrape_product(drv, url, out_dir=self.ctx.pages_dir, download=False, headless=True,
                                keep_raw=False, already_open=True)
        rec.pop("page_text", None)
        rec["url"] = url
        if not rec.get("title"):
            raise RuntimeError("no product data found on page")
        return rec


def make_adapter(ctx, job):
    t = job.get("type", "auto")
    if t == "shopify":
        return Shopify(ctx, job)
    if t == "ebay_api":
        return EbayApi(ctx, job)
    if t == "auto":
        site = job["site"] if "://" in job["site"] else "https://" + job["site"]
        if re.search(r"(^|\.)ebay\.", urlparse(site).netloc) and ctx.cfg.get("ebay", {}).get("client_id"):
            return EbayApi(ctx, job)
        if Shopify.detect(ctx, site):
            log.info("[%s] Shopify store detected -> using /products.json", job["name"])
            return Shopify(ctx, job)
    return Generic(ctx, job)


