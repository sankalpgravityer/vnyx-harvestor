#!/usr/bin/env python3
"""
category_scraper.py
===================
Give it a website + a product category/keyword and it scrapes the product listings
(search results) with Selenium -- e.g. all "jeans" on Amazon.

  python category_scraper.py --site amazon.in --category jeans
  python category_scraper.py --site https://www.ebay.com --category "track pants" --max-products 100
  python category_scraper.py --site myntra.com --category "men jeans" --details          # + open every product page
  python category_scraper.py --site vinted.nl --category "pangaia" --details --images     # + download all photos
  python category_scraper.py --site https://some-shop.com --category jeans               # any other site (auto)
  python category_scraper.py --site x.com --category jeans --search-url "https://x.com/find?term={query}&pg={page}"

What you get (in output/<site>_<category>_<time>/):
  listings.json / listings.csv   one row per product from the results pages
                                 (url, title, brand, price, mrp, discount, rating, reviews, image, card text...)
  products.json / products.csv   with --details: FULL product data from each product page
                                 (via product_scraper.py: description, sizes, attributes, all images...)
  products/<site>/<id>/          with --details: product.json + images/ per product
  thumbnails/                    with --thumbnails: the listing images

Built-in support: amazon, ebay, flipkart, myntra, ajio, meesho, walmart, target, etsy, asos, hm,
vinted, grailed, depop, poshmark, zalando, nykaa fashion, snapdeal. Any other site falls back to
automatic search-box detection + product-card detection.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from urllib.parse import quote, quote_plus, urlparse, urlunparse

from selenium.webdriver.common.keys import Keys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:                                   # imported as part of the hv package (harvester)
    from .product_scraper import (  # noqa: E402
        add_browser_args, dismiss_popups, download_images, make_driver, open_url, parse_price,
        requests_session_from_driver, scrape_product, site_name, slugify, upgrade_image_url, wait_for_page,
        write_outputs)
except ImportError:                    # run as a standalone script
  from product_scraper import (add_browser_args, dismiss_popups, download_images, make_driver,  # noqa: E402
                               open_url, parse_price, requests_session_from_driver, scrape_product,
                               site_name, slugify, upgrade_image_url, wait_for_page, write_outputs)

# ----------------------------------------------------------------------------
# Site configurations
#   search   : search URL template ({host}, {q}=url-encoded, {qslug}=dash-slug, {page})
#   paging   : "page" (uses {page} in URL) or "scroll" (infinite scroll / load-more)
#   card     : CSS selector of one product card (optional -> automatic card detection)
#   link     : CSS selector of the product link inside a card (optional)
#   link_pattern : regex a product URL must match
#   title/brand/price/mrp/image/rating/reviews/subtitle : CSS selectors inside the card (optional)
# ----------------------------------------------------------------------------
SITES = {
    "amazon": dict(
        match=r"(^|\.)amazon\.", search="https://{host}/s?k={q}&page={page}", paging="page", page_start=1,
        card='div[data-component-type="s-search-result"]',
        link='a.a-link-normal[href*="/dp/"], h2 a, a.a-link-normal.s-no-outline',
        link_pattern=r"/dp/|/gp/product/|/sspa/click",
        title='[data-cy="title-recipe"] a h2 span, h2 a span, h2 span, h2',
        brand='[data-cy="title-recipe"] h2.a-size-mini span, h5 span, .s-line-clamp-1 span',
        price='.a-price:not(.a-text-price) .a-offscreen', mrp='.a-price.a-text-price .a-offscreen',
        image="img.s-image", rating=".a-icon-alt",
        reviews='a[href*="customerReviews"] span, [data-csa-c-content-id*="ratings-count"] span, span.a-size-base.s-underline-text',
        id_attr="data-asin"),
    "ebay": dict(
        match=r"(^|\.)ebay\.", search="https://{host}/sch/i.html?_nkw={q}&_pgn={page}", paging="page", page_start=1,
        card="li.s-item, li.s-card, li.brwrvr__item-card, .brw-product-card", link_pattern=r"/itm/",
        link='a.s-item__link, a.su-link, a.bsig__title__wrapper, a[href*="/itm/"]',
        title=".s-item__title span[role=heading], .s-item__title, .s-card__title, .bsig__title__text, h3",
        price=".s-item__price, .s-card__price, .bsig__price--displayprice, .textual-display.bsig__price",
        subtitle=".s-item__subtitle, .s-card__subtitle, .bsig__listingCondition",
        image="img", reviews=".s-item__reviews-count span", rating=".x-star-rating span"),
    "flipkart": dict(
        match=r"(^|\.)flipkart\.com", search="https://www.flipkart.com/search?q={q}&page={page}",
        paging="page", page_start=1, link_pattern=r"/p/itm", brand_first_line=True),
    "myntra": dict(
        match=r"(^|\.)myntra\.com", search="https://www.myntra.com/{qslug}?rawQuery={q}&p={page}",
        paging="page", page_start=1, card="li.product-base", link="a[href]",
        brand=".product-brand", title=".product-product", price=".product-discountedPrice, .product-price span",
        mrp=".product-strike", image="picture img, img.img-responsive, img",
        rating=".product-ratingsContainer span", reviews=".product-ratingsCount"),
    "ajio": dict(
        match=r"(^|\.)ajio\.com", search="https://www.ajio.com/search/?text={q}", paging="scroll",
        card=".item.rilrtl-products-list__item, .item", link="a.rilrtl-products-list__link, a[href*='/p/']",
        brand=".brand", title=".nameCls", price=".price", mrp=".orginal-price", link_pattern=r"/p/"),
    "meesho": dict(
        match=r"(^|\.)meesho\.com", search="https://www.meesho.com/search?q={q}", paging="scroll",
        link_pattern=r"/p/\w+"),
    "snapdeal": dict(
        match=r"(^|\.)snapdeal\.com", search="https://www.snapdeal.com/search?keyword={q}", paging="scroll",
        card=".product-tuple-listing", link="a.dp-widget-link", title=".product-title",
        price=".product-price", mrp=".product-desc-price", link_pattern=r"/product/"),
    "nykaafashion": dict(
        match=r"nykaafashion\.com", search="https://www.nykaafashion.com/catalogsearch/result/?q={q}&p={page}",
        paging="page", page_start=1, link_pattern=r"/p/\d+"),
    "walmart": dict(
        match=r"(^|\.)walmart\.", search="https://{host}/search?q={q}&page={page}", paging="page",
        page_start=1, card='[data-item-id]', link='a[href*="/ip/"]', link_pattern=r"/ip/",
        title='[data-automation-id="product-title"], span.w_iUH7', price='[data-automation-id="product-price"]'),
    "target": dict(
        match=r"(^|\.)target\.com", search="https://www.target.com/s?searchTerm={q}", paging="scroll",
        card='[data-test="@web/site-top-of-funnel/ProductCardWrapper"]', link_pattern=r"/-/A-\d+",
        title='[data-test="product-title"]', price='[data-test="current-price"]'),
    "etsy": dict(
        match=r"(^|\.)etsy\.com", search="https://www.etsy.com/search?q={q}&page={page}", paging="page",
        page_start=1, card="[data-listing-id]", link="a.listing-link, a[href*='/listing/']",
        link_pattern=r"/listing/\d+", title="h3", price=".currency-value", image="img"),
    "asos": dict(
        match=r"(^|\.)asos\.com", search="https://www.asos.com/search/?q={q}&page={page}", paging="page",
        page_start=1, card="article[id^='product-']", link="a[href*='/prd/']", link_pattern=r"/prd/\d+",
        title="h2, p[class*='productDescription']", price="[class*='price']"),
    "hm": dict(
        match=r"(^|\.)hm\.com", search="https://{host}/en_in/search-results.html?q={q}&page={page}",
        paging="page", page_start=1, link_pattern=r"productpage\.\d+"),
    "zalando": dict(
        match=r"(^|\.)zalando\.", search="https://{host}/catalog/?q={q}&p={page}", paging="page",
        page_start=1, card="article", link_pattern=r"\.html"),
    "vinted": dict(
        match=r"(^|\.)vinted\.", search="https://{host}/catalog?search_text={q}&page={page}", paging="page",
        page_start=1, card='[data-testid="grid-item"]', link='a[href*="/items/"]', link_pattern=r"/items/\d+",
        brand='[data-testid$="--description-title"]', subtitle='[data-testid$="--description-subtitle"]',
        price='[data-testid$="--price-text"]', image="img"),
    "grailed": dict(
        match=r"(^|\.)grailed\.com", search="https://www.grailed.com/shop?query={q}", paging="scroll",
        link_pattern=r"/listings/\d+", brand_first_line=True),
    "depop": dict(
        match=r"(^|\.)depop\.com", search="https://www.depop.com/search/?q={q}", paging="scroll",
        link_pattern=r"/products/[\w-]+"),
    "poshmark": dict(
        match=r"(^|\.)poshmark\.", search="https://{host}/search?query={q}", paging="scroll",
        card="[data-et-prop-listing_id], .card", link_pattern=r"/listing/"),
    # Resale sites the vnyx resale lookup reads (22 Nov 2026). Search URL + what a product link looks like;
    # cards are found automatically around those links.
    "vestiairecollective": dict(
        match=r"(^|\.)vestiairecollective\.", search="https://{host}/search/?q={q}&page={page}", paging="page",
        page_start=1, link_pattern=r"-\d{6,}\.shtml"),
    "marktplaats": dict(
        match=r"(^|\.)marktplaats\.nl", search="https://www.marktplaats.nl/q/{qslug}/p/{page}/", paging="page",
        page_start=1, link_pattern=r"/v/[^/]+/[^/]+/[am]\d+"),
    "2dehands": dict(
        match=r"(^|\.)2dehands\.be", search="https://www.2dehands.be/q/{qslug}/p/{page}/", paging="page",
        page_start=1, link_pattern=r"/v/[^/]+/[^/]+/[am]\d+"),
    "kleinanzeigen": dict(
        match=r"(^|\.)kleinanzeigen\.de", search="https://www.kleinanzeigen.de/s-seite:{page}/{qslug}/k0",
        paging="page", page_start=1, link_pattern=r"/s-anzeige/"),
    "wallapop": dict(
        match=r"(^|\.)wallapop\.com", search="https://{host}/app/search?keywords={q}", paging="scroll",
        link_pattern=r"/item/[\w-]+"),
    "subito": dict(
        match=r"(^|\.)subito\.it", search="https://www.subito.it/annunci-italia/vendita/usato/?q={q}&o={page}",
        paging="page", page_start=1, link_pattern=r"-\d{6,}\.htm"),
    "tise": dict(
        match=r"(^|\.)tise\.com", search="https://tise.com/search?q={q}", paging="scroll",
        link_pattern=r"/p/|/product/"),
    "thredup": dict(
        match=r"(^|\.)thredup\.com", search="https://www.thredup.com/products?search_text={q}&page={page}",
        paging="page", page_start=1, link_pattern=r"/product/"),
    "mercari": dict(
        match=r"(^|\.)mercari\.com", search="https://www.mercari.com/search/?keyword={q}", paging="scroll",
        link_pattern=r"/item/m\d+"),
    "sellpy": dict(
        match=r"(^|\.)sellpy\.", search="https://{host}/search?query={q}", paging="scroll",
        link_pattern=r"/item/[\w-]{6,}"),
}

GENERIC_SEARCH_PATHS = ["/search?q={q}", "/search?type=product&q={q}", "/search?query={q}",
                        "/catalogsearch/result/?q={q}", "/?s={q}&post_type=product", "/search/?text={q}",
                        "/s?k={q}", "/shop?query={q}", "/search?keyword={q}", "/catalog?search_text={q}"]

# ----------------------------------------------------------------------------
# JavaScript: extract product cards from a results page
# ----------------------------------------------------------------------------
CARDS_JS = r"""
const cfg = arguments[0] || {};
const abs = u => { try { return new URL(u, location.href).href } catch(e) { return null } };
const txt = e => e ? (e.innerText || e.textContent || '').replace(/\s+\n/g, '\n').trim() : null;
const q = (root, sel) => { if (!sel) return null; try { return root.querySelector(sel) } catch(e) { return null } };
const imgUrl = img => {
  if (!img) return null;
  const ss = img.getAttribute('srcset') || img.getAttribute('data-srcset');
  let best = null, bw = -1;
  if (ss) for (const p of ss.split(',')) { const [u, d] = p.trim().split(/\s+/); const w = parseFloat(d) || 1; if (u && w > bw) { bw = w; best = u; } }
  return abs(best || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.currentSrc || img.src);
};
const priceRe = /(US\$|C\$|A\$|R\$|[$€£₹¥]|Rs\.?|INR|USD|EUR|GBP)\s?\d[\d.,]*|\d[\d.,]*\s?(€|zł|kr|Kč|EUR|USD)/g;
const priceRe1 = new RegExp(priceRe.source);
const linkRe = cfg.link_pattern ? new RegExp(cfg.link_pattern, 'i')
  : /\/(dp|gp\/product|p|product|products|item|items|itm|listing|listings|ip|prd|pd|buy|goods)\/|[-_\/]p[-_]?\d{4,}|\/\d{6,}(?:[-\/.?]|$)|-\d{5,}\.html|\.html?(\?|$)/i;
const base = location.hostname.split('.').slice(-2).join('.');
const sameSite = a => { try { return new URL(a.href).hostname.endsWith(base) } catch(e) { return false } };
const chrome = el => !!el.closest('header, footer, nav, [role=navigation], [role=banner], [role=contentinfo]');
const keyOf = h => h.split('#')[0];

let pairs = [];
if (cfg.card) {
  for (const c of document.querySelectorAll(cfg.card)) {
    let a = q(c, cfg.link);
    if (!a || !a.href) a = [...c.querySelectorAll('a[href]')].find(x => linkRe.test(x.href)) || (c.tagName === 'A' ? c : null);
    if (a && a.href && !a.href.startsWith('javascript')) pairs.push([c, a]);
  }
}
if (!pairs.length) {
  // automatic card detection: product-looking links, card = biggest ancestor holding only that product
  const anchors = [...document.querySelectorAll('a[href]')].filter(a => sameSite(a) && !chrome(a) && a.href !== location.href);
  const cand = anchors.filter(a => linkRe.test(new URL(a.href).pathname + new URL(a.href).search) || a.querySelector('img'));
  const candKeys = new Set(cand.map(a => keyOf(a.href)));
  const seen = new Set();
  for (const a of cand) {
    const k = keyOf(a.href); if (seen.has(k)) continue;
    let card = a, depth = 0;
    while (card.parentElement && depth < 8) {
      const p = card.parentElement;
      const ks = new Set([...p.querySelectorAll('a[href]')].map(x => keyOf(x.href)).filter(x => candKeys.has(x)));
      const pt = p.innerText || '';
      if (pt.length > 1500) break;
      if (ks.size > 1) {
        // colour swatches / variant links: keep climbing only while we still haven't reached the price
        const hasPriceYet = ((card.innerText || '').match(priceRe) || []).length > 0;
        const pPrices = (pt.match(priceRe) || []).length;
        if (hasPriceYet || pPrices > 2 || pt.length > 700 || ks.size > 8) break;
      }
      card = p; depth++;
    }
    const t = card.innerText || '';
    const hasImg = !!card.querySelector('img');
    const hasPrice = (t.match(priceRe) || []).length > 0;
    const looks = linkRe.test(new URL(a.href).pathname + new URL(a.href).search);
    if (hasImg && (hasPrice || looks) && t.length < 1500) { seen.add(k); pairs.push([card, a]); }
  }
}

const out = [], done = new Set();
for (const [c, a] of pairs) {
  const url = abs(a.href); const k = keyOf(url);
  if (!url || done.has(k)) continue; done.add(k);
  const text = txt(c) || '';
  const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
  const prices = text.match(priceRe) || [];
  const img = q(c, cfg.image) || c.querySelector('img');
  let title = txt(q(c, cfg.title)) || a.getAttribute('title') || a.getAttribute('aria-label') ||
              (img && img.alt) || lines.filter(l => l.length >= 6 && l.length <= 200 && !priceRe1.test(l)).sort((x, y) => y.length - x.length)[0] || null;
  const strike = c.querySelector('s, del, strike, [class*="strike" i], [class*="original" i], [class*="was" i], [class*="mrp" i], .a-text-price');
  const rat = q(c, cfg.rating) || c.querySelector('[aria-label*="out of 5" i], [aria-label*="stars" i], [class*="rating" i]');
  out.push({
    url, title,
    brand: txt(q(c, cfg.brand)),
    subtitle: txt(q(c, cfg.subtitle)),
    price_text: txt(q(c, cfg.price)) || prices[0] || null,
    mrp_text: txt(q(c, cfg.mrp)) || (strike ? txt(strike) : null) || (prices.length > 1 ? prices[1] : null),
    rating_text: rat ? (rat.getAttribute('aria-label') || txt(rat)) : null,
    reviews_text: txt(q(c, cfg.reviews)),
    image: imgUrl(img), image_alt: img ? img.alt : null,
    item_id: cfg.id_attr ? c.getAttribute(cfg.id_attr) : null,
    sponsored: /\bSponsored\b|\bGesponsord\b|\bAnzeige\b/.test(text),
    card_text: text.slice(0, 800)
  });
}
return out;
"""

NEXT_PAGE_JS = r"""
const sels = ['a[rel=next]', 'a[aria-label*="next" i]', 'a.s-pagination-next', 'a[class*="pagination-next" i]',
              'a[class*="next" i]', 'button[aria-label*="next" i]', 'li[class*="next" i] a', '[class*="pagination" i] a[class*="next" i]'];
for (const s of sels) { const el = [...document.querySelectorAll(s)].find(e => e.offsetParent && !e.disabled && e.getAttribute('aria-disabled') !== 'true'); if (el) { el.scrollIntoView({block:'center'}); el.click(); return s; } }
const words = ['next', 'next page', '›', '»', '→', 'load more', 'show more', 'view more', 'see more', 'meer laden', 'volgende', 'weiter', 'mehr laden', 'suivant'];
const els = [...document.querySelectorAll('a, button')].filter(e => e.offsetParent && !e.disabled);
for (const w of words) { const el = els.find(e => (e.innerText || e.getAttribute('aria-label') || '').trim().toLowerCase() === w); if (el) { el.scrollIntoView({block:'center'}); el.click(); return w; } }
return null;
"""

FIND_SEARCH_JS = r"""
const vis = e => e && e.offsetParent !== null && !e.disabled && e.type !== 'hidden';
const sels = ['input[type=search]', 'input[name=q]', 'input[name=k]', 'input[name=query]', 'input[name=search]',
  'input[name=searchTerm]', 'input[name=text]', 'input[name=keyword]', 'input[name=search_text]', 'input[name*="search" i]',
  'input[placeholder*="search" i]', 'input[aria-label*="search" i]', 'input[id*="search" i]', 'input[class*="search" i]',
  'input[placeholder*="zoek" i]', 'input[placeholder*="such" i]', 'input[placeholder*="cherch" i]', 'input[placeholder*="busca" i]'];
for (const s of sels) { const el = [...document.querySelectorAll(s)].find(vis); if (el) return el; }
return null;
"""

OPEN_SEARCH_JS = r"""
const el = [...document.querySelectorAll('button, a, [role=button]')].find(e => e.offsetParent &&
  /search|zoek|such/i.test((e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '') + ' ' + (e.className || '') + ' ' + (e.dataset.testid || '')));
if (el) { el.click(); return true; } return false;
"""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def normalize_site(site: str) -> tuple[str, str]:
    """'amazon' -> ('https', 'www.amazon.com'); 'amazon.in' -> ('https','www.amazon.in')."""
    s = site.strip()
    if "://" not in s:
        if "." not in s:
            s = f"www.{s}.com"
        s = "https://" + s
    p = urlparse(s)
    host = p.netloc
    if host.count(".") == 1 or re.match(r"^[^.]+\.co\.[a-z]{2}$", host):
        host = "www." + host
    return p.scheme or "https", host


def find_config(host: str):
    for name, cfg in SITES.items():
        if re.search(cfg["match"], host):
            return name, cfg
    return None, None


def js_cfg(cfg: dict | None) -> dict:
    keys = ["card", "link", "link_pattern", "title", "brand", "subtitle", "price", "mrp", "image",
            "rating", "reviews", "id_attr"]
    return {k: (cfg or {}).get(k) for k in keys}


ID_PATTERNS = [r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})", r"/itm/(?:[^/]+/)?(\d{9,})", r"/items/(\d+)",
               r"/listings/(\d+)", r"/listing/(\d+)", r"/ip/(?:[^/]+/)?(\d+)", r"pid=([A-Z0-9]+)",
               r"/(\d{6,})/buy", r"/p/([\w]+)", r"/prd/(\d+)", r"productpage\.(\d+)", r"/A-(\d+)"]


def product_id(url: str) -> str | None:
    for pat in ID_PATTERNS:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def clean_url(url: str) -> str:
    """Strip tracking junk so the same product isn't listed twice."""
    p = urlparse(url)
    if re.search(r"amazon\.", p.netloc):
        m = re.search(r"/dp/([A-Z0-9]{10})", p.path) or re.search(r"/gp/product/([A-Z0-9]{10})", p.path)
        if m:
            return urlunparse((p.scheme, p.netloc, f"/dp/{m.group(1)}", "", "", ""))
        m = re.search(r"url=([^&]+)", p.query)          # sponsored redirect
        if m:
            from urllib.parse import unquote
            inner = unquote(m.group(1))
            m2 = re.search(r"/dp/([A-Z0-9]{10})", inner)
            if m2:
                return urlunparse((p.scheme, p.netloc, f"/dp/{m2.group(1)}", "", "", ""))
    if re.search(r"ebay\.", p.netloc) and "/itm/" in p.path:
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    if "flipkart.com" in p.netloc:
        pid = re.search(r"pid=([A-Z0-9]+)", p.query)
        return urlunparse((p.scheme, p.netloc, p.path, "", f"pid={pid.group(1)}" if pid else "", ""))
    if re.search(r"vinted\.|grailed\.|depop\.|etsy\.|myntra\.|asos\.|walmart\.", p.netloc):
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    return url.split("#")[0]


def normalize_card(c: dict, site: str, category: str, page_no: int, rank: int,
                   cfg: dict | None = None) -> dict | None:
    url = clean_url(c["url"])
    # A site with a known product-link shape: anything else on the page (store, profile, category links that
    # also carry a picture) is not a product (Sellpy, 22 Nov 2026: a store link was taken as the first item).
    if (cfg or {}).get("link_pattern") and not re.search(cfg["link_pattern"], url, re.I):
        return None
    title = (c.get("title") or "").strip() or None
    if not c.get("brand") and (cfg or {}).get("brand_first_line"):
        lines = [l.strip() for l in (c.get("card_text") or "").split("\n") if l.strip()]
        if lines and len(lines[0]) <= 40 and lines[0] != title and not re.search(r"\d", lines[0][:1]) \
                and parse_price(lines[0])[0] is None:
            c["brand"] = lines[0]
    if site == "ebay" and title and title.lower().startswith("shop on ebay"):
        return None
    if title:   # Vinted & co put "title, brand: X, condition: Y, size: Z, price" in the alt/aria text
        title = re.split(r",\s*(?:merk|brand|marke|marque|marca|marka|staat|condition|zustand|état|estado|"
                         r"maat|size|größe|taille|talla)\s*:", title, maxsplit=1, flags=re.I)[0].strip()
    price, cur = parse_price(c.get("price_text"))
    mrp, _ = parse_price(c.get("mrp_text"))
    if mrp is not None and price is not None and mrp <= price:
        mrp = None
    rating = None
    if c.get("rating_text"):
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:out of|von|sur|van|/)\s*5", c["rating_text"]) or \
            re.search(r"^(\d(?:[.,]\d)?)\b", c["rating_text"].strip())
        if m:
            rating = float(m.group(1).replace(",", "."))
    reviews = None
    if c.get("reviews_text"):
        d = re.sub(r"[^\d]", "", c["reviews_text"].split("(")[-1])
        reviews = int(d) if d else None
    return {
        "rank": rank, "page": page_no, "site": site, "category": category,
        "id": c.get("item_id") or product_id(url), "title": title, "brand": c.get("brand"),
        "subtitle": c.get("subtitle"), "price": price, "currency": cur, "mrp": mrp,
        "discount_pct": round((1 - price / mrp) * 100) if price and mrp else None,
        "rating": rating, "reviews": reviews, "sponsored": c.get("sponsored"),
        "image": upgrade_image_url(c.get("image")) if c.get("image") else None,
        "url": url, "card_text": c.get("card_text"),
    }


def collect_cards(driver, cfg) -> list[dict]:
    try:
        cards = driver.execute_script(CARDS_JS, js_cfg(cfg)) or []
    except Exception as e:
        print(f"   [!] card extraction error: {e}")
        cards = []
    if not cards and (cfg or {}).get("link_pattern"):
        try:
            cards = embedded_cards(driver.page_source, driver.current_url, cfg["link_pattern"])
            if cards:
                print(f"   (product links read from the page's data: {len(cards)})")
        except Exception:
            pass
    return cards


def embedded_cards(html: str, page_url: str, link_pattern: str, limit: int = 200) -> list[dict]:
    """Product URLs a page carries in its own data (JSON in <script>) when its tiles are not rendered as links
    (Vestiaire, 22 Nov 2026: 10,000 hits, 64 product URLs in the page data, no product anchors)."""
    from urllib.parse import urljoin
    text = html.replace("\\u002F", "/").replace("\\/", "/")
    host = urlparse(page_url).netloc
    base_dom = ".".join(host.split(".")[-2:])
    out, seen = [], set()
    for m in re.finditer(r'(?:https?:)?//[^\s"\'<>\\]+|/[A-Za-z0-9][^\s"\'<>\\]{3,300}', text):
        u = m.group(0)
        u = ("https:" + u) if u.startswith("//") else urljoin(page_url, u)
        p = urlparse(u)
        if not p.netloc.endswith(base_dom) or not re.search(link_pattern, p.path + ("?" + p.query if p.query else ""), re.I):
            continue
        u = u.split("#")[0]
        if u in seen:
            continue
        seen.add(u)
        slug = p.path.rstrip("/").rsplit("/", 1)[-1]
        title = re.sub(r"[-_]+", " ", re.sub(r"(-\d{5,})?\.\w+$", "", slug)).strip().capitalize() or None
        out.append({"url": u, "title": title, "brand": None, "subtitle": None, "price_text": None, "mrp_text": None,
                    "rating_text": None, "reviews_text": None, "image": None, "image_alt": None, "item_id": None,
                    "sponsored": False, "card_text": ""})
        if len(out) >= limit:
            break
    return out


def light_scroll(driver, rounds=6, pause=0.6):
    for i in range(rounds):
        driver.execute_script("window.scrollBy(0, Math.max(600, window.innerHeight * 0.9));")
        time.sleep(pause)


# ----------------------------------------------------------------------------
# Main category scrape
# ----------------------------------------------------------------------------

def scrape_category(driver, site: str, category: str, max_products: int = 60, max_pages: int = 5,
                    search_url: str | None = None, headless: bool = False, delay: float = 2.0) -> list[dict]:
    scheme, host = normalize_site(site)
    name, cfg = find_config(host)
    sname = name or site_name(f"{scheme}://{host}")
    q, qslug = quote_plus(category), quote(re.sub(r"\s+", "-", category.strip().lower()))
    results: dict[str, dict] = {}

    def add(cards, page_no):
        new = 0
        for c in cards:
            rec = normalize_card(c, sname, category, page_no, len(results) + 1, cfg)
            if rec and rec["url"] not in results and len(results) < max_products:
                results[rec["url"]] = rec
                new += 1
        return new

    def is_full():
        return len(results) >= max_products

    template = search_url or (cfg or {}).get("search")
    if template:
        template = template.replace("{query}", "{q}")
        paging = "page" if "{page}" in template else "scroll"
        print(f"[*] {sname}: searching '{category}' ({paging} mode, config: {name or 'custom'})")
        if paging == "page":
            start = (cfg or {}).get("page_start", 1)
            for page_no in range(start, start + max_pages):
                url = template.format(host=host, q=q, qslug=qslug, page=page_no)
                print(f"   page {page_no}: {url}")
                if not open_url(driver, url, headless):
                    break
                light_scroll(driver)
                new = add(collect_cards(driver, cfg), page_no)
                print(f"     +{new} products (total {len(results)})")
                if new == 0 or len(results) >= max_products:
                    break
                time.sleep(delay + random.random())
        else:
            url = template.format(host=host, q=q, qslug=qslug, page=1)
            print(f"   {url}")
            if open_url(driver, url, headless):
                _scroll_collect(driver, cfg, add, is_full, rounds=max_pages * 4)
    else:
        print(f"[*] {host}: no built-in config -> automatic search")
        if not _generic_search(driver, scheme, host, category, q, headless):
            print("[!] Could not find a search box or search URL on this site. "
                  "Pass one with --search-url 'https://site/search?q={query}&page={page}'")
            return []
        print(f"   results page: {driver.current_url}")
        page_no = 1
        while page_no <= max_pages and len(results) < max_products:
            light_scroll(driver)
            new = add(collect_cards(driver, cfg), page_no)
            print(f"   page {page_no}: +{new} products (total {len(results)})")
            if len(results) >= max_products:
                break
            before = driver.current_url
            clicked = driver.execute_script(NEXT_PAGE_JS)
            if not clicked:
                _scroll_collect(driver, cfg, add, is_full, rounds=max_pages * 3)
                break
            time.sleep(delay)
            wait_for_page(driver)
            if driver.current_url == before and new == 0:
                break
            page_no += 1
    return list(results.values())


def _scroll_collect(driver, cfg, add, is_full, rounds=20):
    """Infinite scroll / 'load more' sites: scroll, collect, repeat until nothing new appears."""
    stale = 0
    for r in range(rounds):
        new = add(collect_cards(driver, cfg), r + 1)
        print(f"   scroll {r + 1}: +{new} products")
        if is_full():
            break
        if new == 0:
            stale += 1
            if stale >= 3:
                break
            driver.execute_script(NEXT_PAGE_JS)        # click "load more" if present
        else:
            stale = 0
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5 + random.random())


def _generic_search(driver, scheme, host, category, q, headless) -> bool:
    home = f"{scheme}://{host}/"
    if open_url(driver, home, headless):
        box = driver.execute_script(FIND_SEARCH_JS)
        if box is None and driver.execute_script(OPEN_SEARCH_JS):
            time.sleep(1)
            box = driver.execute_script(FIND_SEARCH_JS)
        if box is not None:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", box)
                box.clear()
                for ch in category:
                    box.send_keys(ch)
                    time.sleep(0.05)
                box.send_keys(Keys.ENTER)
                time.sleep(2)
                wait_for_page(driver)
                dismiss_popups(driver)
                if driver.current_url.rstrip("/") != home.rstrip("/") or len(collect_cards(driver, None)) >= 3:
                    return True
            except Exception as e:
                print(f"   [!] search box failed: {e}")
    for path in GENERIC_SEARCH_PATHS:
        url = f"{scheme}://{host}{path.format(q=q)}"
        print(f"   trying {url}")
        if open_url(driver, url, headless) and len(collect_cards(driver, None)) >= 3:
            return True
    return False


def write_listings(rows, folder):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "listings.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    if rows:
        fields = list(dict.fromkeys(k for r in rows for k in r.keys()))
        with open(os.path.join(folder, "listings.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Scrape products of a category/keyword from a website with Selenium.")
    ap.add_argument("--site", "-s", required=True, help="website, e.g. amazon.in, https://www.ebay.com, myntra.com")
    ap.add_argument("--category", "-c", required=True, help='category or keyword, e.g. jeans, "men track pants"')
    ap.add_argument("--max-products", "-n", type=int, default=60, help="max products to collect (default 60)")
    ap.add_argument("--pages", "-p", type=int, default=5, help="max result pages / scroll rounds (default 5)")
    ap.add_argument("--details", action="store_true", help="open every product page and scrape full details")
    ap.add_argument("--images", action="store_true", help="with --details: download all product images")
    ap.add_argument("--thumbnails", action="store_true", help="download the listing thumbnail of each product")
    ap.add_argument("--search-url", help="custom search URL template with {query} and optional {page}")
    ap.add_argument("--out", "-o", default="output", help="output root folder (default: output)")
    add_browser_args(ap)
    a = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _, host = normalize_site(a.site)
    run_dir = os.path.join(a.out, f"{site_name('https://' + host)}_{slugify(a.category)}_{stamp}")

    driver = make_driver(a.headless, a.undetected, a.proxy, a.profile, chrome_binary=a.chrome_binary,
                         chromedriver=a.chromedriver, stealth=a.stealth)
    try:
        rows = scrape_category(driver, a.site, a.category, a.max_products, a.pages, a.search_url,
                               a.headless, a.delay)
        write_listings(rows, run_dir)
        print(f"\n[+] {len(rows)} listings -> {os.path.join(run_dir, 'listings.csv')}")

        if a.thumbnails and rows:
            sess = requests_session_from_driver(driver)
            imgs = [r["image"] for r in rows if r.get("image")]
            saved, failed = download_images(imgs, os.path.join(run_dir, "thumbnails"), sess, driver.current_url)
            by_url = {s["url"]: s["file"] for s in saved}
            for r in rows:
                r["thumbnail_file"] = by_url.get(r.get("image"))
            write_listings(rows, run_dir)
            print(f"[+] {len(saved)} thumbnails saved")

        if a.details and rows:
            details = []
            for i, r in enumerate(rows, 1):
                print(f"[{i}/{len(rows)}] details: {r['url']}")
                try:
                    d = scrape_product(driver, r["url"], os.path.join(run_dir, "products"),
                                       download=a.images, headless=a.headless)
                except Exception as e:
                    d = {"input_url": r["url"], "status": f"error: {e}"}
                # fill gaps from the listing card
                for k in ("title", "brand", "price", "currency"):
                    if not d.get(k) and r.get(k):
                        d[k] = r[k]
                d.update({"rank": r["rank"], "category": a.category, "listing_mrp": r.get("mrp"),
                          "listing_rating": r.get("rating"), "listing_reviews": r.get("reviews")})
                details.append(d)
                print(f"   -> {d.get('status')} | {str(d.get('title'))[:70]} | {d.get('price')} | "
                      f"{len(d.get('images') or [])} images")
                write_outputs(details, run_dir, "products")
                time.sleep(a.delay + random.random())
            print(f"[+] full details -> {os.path.join(run_dir, 'products.csv')}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
