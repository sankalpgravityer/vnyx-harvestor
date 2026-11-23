"""Harvested products -> the Stock API's Product Master (POST /products/bulk).

The harvester keeps what it scrapes in its own SQLite (hv/db.py). This module sends those products to the
Stock API through its existing bulk upload - no direct database access, nothing to change on Stock's side:

  * one Stock source per website, the same one Stock's other writers use: "vinted" / "Vinted", "ebay" / "eBay"
    (23 Nov 2026; was upload:harvester-<site> "Harvester - vinted"). Harvested rows still carry the tag
    "harvester", so they can be found, filtered or removed later;
  * keyed on `externalId` = <site>:<item id> (else the product URL): a product scraped again updates its row
    instead of making a second one;
  * prices go in as price evidence: resale sites (Vinted, Grailed, eBay used ...) as RESALE_ASKING, shops
    (Shopify stores, Flipkart, Amazon ...) as RETAIL_CURRENT, a struck-through price as RETAIL_ORIGINAL;
  * image URLs only (up to 20) - images are never downloaded.

Stock refuses a row whose brand is not in its Brand Master (code BRAND_NOT_FOUND). That row is recorded as
failed with the reason and is not sent again unless `--retry-failed` is given (e.g. after adding the brand).

What was sent, when, and Stock's answer (PRD code or error) is kept in the harvester's own SQLite, table
`stock_push`. A product is sent again only when it was scraped again since.

Config (config.yaml):
  stock:
    url: ${STOCK_API_URL}          # default https://api-stock.vnyx.ai
    api_key: ${STOCK_API_KEY}      # one of Stock's PRICING_API_KEYS
    auto_push: true                # push at the end of every run
    batch: 200                     # rows per call (Stock allows 500)
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

log = logging.getLogger("harvester")

DEFAULT_URL = "https://api-stock.vnyx.ai"
MAX_IMAGES = 20
MAX_VARIANTS = 200
MAX_ATTRIBUTES = 100

# Second-hand marketplaces: their price is a seller's asking price, not a shop's retail price.
RESALE_SITES = {"vinted", "grailed", "depop", "vestiairecollective", "poshmark", "marktplaats", "2dehands",
                "kleinanzeigen", "wallapop", "subito", "tise", "thredup", "mercari", "sellpy", "vide-dressing",
                "klekt", "stockx"}
USED_WORDS = re.compile(r"used|pre-?owned|gebraucht|gebruikt|occasion|second|worn|very good|good|fair|"
                        r"heel goed|goed|gently", re.I)
CURRENCY_MARKET = {"EUR": "EU", "GBP": "GB", "USD": "US", "INR": "IN", "CAD": "CA", "AUD": "AU", "SEK": "SE",
                   "PLN": "PL", "CHF": "CH", "DKK": "DK", "CZK": "CZ", "JPY": "JP"}
TLD_MARKET = {"uk": "GB"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_push (
    url TEXT PRIMARY KEY, site TEXT, scraped_at TEXT, status TEXT,   -- created | updated | failed
    stock_code TEXT, code TEXT, error TEXT, pushed_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stock_cfg(cfg: dict) -> dict:
    s = dict(cfg.get("stock") or {})
    s["url"] = (s.get("url") or DEFAULT_URL).rstrip("/")
    s["api_key"] = (s.get("api_key") or "").strip()
    s["auto_push"] = bool(s.get("auto_push", True))
    s["batch"] = max(1, min(500, int(s.get("batch") or 200)))
    return s


# ---------------------------------------------------------------------------
# The Brand Master: a listing's brand, as Stock knows it (22 Nov 2026)
# ---------------------------------------------------------------------------
# Words that are brand names but mostly appear in titles as ordinary words - never taken from a title alone.
TITLE_STOPWORDS = {"only", "basic", "basics", "vintage", "classic", "original", "new", "sport", "sports", "active",
                   "blue", "black", "white", "red", "green", "pink", "grey", "gray", "brown", "jeans", "denim",
                   "jacket", "coat", "jas", "jacke", "giacca", "veste", "shirt", "dress", "shoes", "kids", "men",
                   "women", "home", "love", "style", "free", "nature", "maat", "size", "taglia", "groesse"}


def norm_brand(s: str) -> str:
    """'Arc'teryx' / 'Arcteryx' / 'ARC TERYX' -> 'arcteryx'; accents dropped."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


class BrandIndex:
    """Stock's Brand Master (names + aliases), read once per push through GET /brands (API key)."""

    def __init__(self, names: dict[str, str]):
        self.names = names                         # normalized name/alias -> the brand's name in Stock

    def __len__(self):
        return len(self.names)

    def match(self, brand: str | None) -> str | None:
        n = norm_brand(brand or "")
        return self.names.get(n) if n else None

    def in_title(self, title: str | None) -> str | None:
        """The longest Brand Master name (1-4 words) that appears in the title, or None."""
        words = re.findall(r"[\w'&.+-]+", title or "", re.U)
        best = None
        for size in (4, 3, 2, 1):
            for i in range(0, max(0, len(words) - size + 1)):
                phrase = " ".join(words[i:i + size])
                n = norm_brand(phrase)
                if len(n) < 3 or (size == 1 and (len(n) < 4 or n in TITLE_STOPWORDS)):
                    continue
                hit = self.names.get(n)
                if hit and (best is None or len(n) > len(norm_brand(best))):
                    best = hit
            if best:
                return best
        return None


def load_brands(scfg: dict, session=None) -> BrandIndex | None:
    """GET /brands, all pages. None when Stock does not let the key read it (then brands go as scraped)."""
    http = session or requests
    names: dict[str, str] = {}
    skip, take = 0, 100
    try:
        while True:
            r = http.get(f"{scfg['url']}/brands", params={"skip": skip, "take": take}, timeout=60,
                         headers={"x-api-key": scfg["api_key"], "accept": "application/json"})
            if r.status_code >= 400:
                log.warning("stock push: Brand Master not readable with this key (HTTP %s) - brands go as "
                            "scraped, no title matching", r.status_code)
                return None
            data = (r.json() or {}).get("data") or {}
            items = data.get("items") or []
            for b in items:
                name = b.get("name")
                if not name:
                    continue
                for v in [name, b.get("slug")] + [a.get("value") for a in b.get("aliases") or []]:
                    n = norm_brand(v or "")
                    if n:
                        names.setdefault(n, name)
            skip += len(items)
            if not items or skip >= int(data.get("total") or 0):
                break
    except Exception as e:  # noqa: BLE001
        log.warning("stock push: could not read the Brand Master (%s) - brands go as scraped", e)
        return None
    log.info("stock push: Brand Master loaded - %d names and aliases", len(names))
    return BrandIndex(names)


def resolve_brand(rec: dict, brands: BrandIndex | None) -> tuple[str | None, str]:
    """-> (brand to send, where it came from: page | page-matched | title | none)."""
    raw = _text(rec.get("brand"), 120)
    if brands is None:
        return raw, "page" if raw else "none"
    if raw:
        hit = brands.match(raw)
        if hit:
            return hit, "page" if hit == raw else "page-matched"
        # "Calvin Klein Jeans" -> "Calvin Klein": the page's brand with a known brand inside it
        inside = brands.in_title(raw)
        if inside:
            return inside, "page-matched"
        return raw, "page"                         # Stock refuses it and says so (BRAND_NOT_FOUND)
    found = brands.in_title(rec.get("title"))
    return (found, "title") if found else (None, "none")


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------
LETTER_SIZE = re.compile(r"(?<![A-Z0-9])(XXXS|XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|5XL)(?![A-Z0-9])")
EU_SIZE = re.compile(r"(?:\bEU\s*[:.]?\s*(\d{2}(?:[.,]5)?)\b)|(?:\b(\d{2}(?:[.,]5)?)\s*EU\b)", re.I)
PLAIN_EU = re.compile(r"^\s*(?:maat|taglia|gr(?:ö|oe)?(?:ss|ß)e|taille|talla|size)?\s*[:.]?\s*(3[0-9]|4[0-9]|5[0-8])\s*$", re.I)


def split_size(size: str | None) -> dict:
    """'L - INT (Geschatte maat)' -> {internationalSize: 'L'}; 'EU 38' / 'Maat 38' -> {euSize: '38'}."""
    s = str(size or "")
    out = {}
    m = LETTER_SIZE.search(s.upper())
    if m:
        out["internationalSize"] = m.group(1).replace("2XL", "XXL").replace("3XL", "XXXL")
    e = EU_SIZE.search(s) or PLAIN_EU.search(s)
    if e:
        out["euSize"] = next(g for g in e.groups() if g).replace(",", ".")
    return out


# ---------------------------------------------------------------------------
# Mapping one harvested product to a Stock bulk row
# ---------------------------------------------------------------------------
def _text(v, n):
    if v is None:
        return None
    if isinstance(v, (list, dict)):
        v = ", ".join(map(str, v)) if isinstance(v, list) else json.dumps(v, ensure_ascii=False)
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s[:n] or None


def _num(v):
    try:
        f = float(str(v).replace(",", ".")) if v not in (None, "") else None
        return round(f, 2) if f and f > 0 else None
    except (TypeError, ValueError):
        return None


def site_slug(site: str | None, url: str) -> str:
    s = site or urlparse(url).netloc.lower().removeprefix("www.").split(".")[0]
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-") or "site"


def market_of(url: str, currency: str | None) -> str:
    tld = urlparse(url).netloc.lower().rsplit(".", 1)[-1]
    if tld in TLD_MARKET:
        return TLD_MARKET[tld]
    if len(tld) == 2 and tld.isalpha() and tld not in ("co", "io", "ai"):
        return tld.upper()
    return CURRENCY_MARKET.get((currency or "").upper(), "EU")


def is_resale(site: str, rec: dict) -> bool:
    if site in RESALE_SITES:
        return True
    if site == "ebay":                    # eBay sells both; the item's condition decides
        return bool(USED_WORDS.search(str(rec.get("condition") or "")))
    return False


def gender_of(rec: dict, job: dict | None) -> str | None:
    hay = " ".join(str(x) for x in (rec.get("title"), rec.get("category"), " ".join(rec.get("breadcrumbs") or []),
                                    (job or {}).get("category")) if x).lower()
    if re.search(r"\b(kid|kids|child|children|girl|girls|boy|boys|baby|junior)\b", hay):
        return "KIDS"
    women = bool(re.search(r"\b(women|womens|women's|ladies|female|dames|damen|femme)\b", hay))
    men = bool(re.search(r"\b(men|mens|men's|male|heren|herren|homme)\b", hay))
    if women and men or re.search(r"\bunisex\b", hay):
        return "UNISEX"
    return "WOMEN" if women else "MEN" if men else None


def category_of(rec: dict, job: dict | None) -> str | None:
    c = rec.get("category")
    if isinstance(c, str) and c.strip() and not c.strip().startswith(("{", "[")):
        if re.search(r"[._/>]", c) and " " not in c.strip():          # Grailed: "bottoms.casual_pants"
            c = " ".join(w[:1].upper() + w[1:] for w in re.split(r"[._/> ]+", c.replace("_", " ")) if w)
        return _text(c, 120)
    crumbs = [b for b in (rec.get("breadcrumbs") or []) if isinstance(b, str) and 1 < len(b) <= 60]
    if len(crumbs) >= 2:
        return _text(crumbs[-2], 120)
    return _text((job or {}).get("category"), 120)


def to_row(rec: dict, job: dict | None = None,
           brands: BrandIndex | None = None) -> tuple[dict | None, str | None]:
    """One harvested record as a Stock bulk row. -> (row, None) or (None, why it cannot be one).
    With the Brand Master (`brands`), the brand is Stock's own spelling, and a listing without one gets the
    brand its title names - only a brand Stock already has, never a guess."""
    url = rec.get("url") or ""
    if not url.startswith(("http://", "https://")):
        return None, "no product URL"
    title = _text(rec.get("title"), 300)
    brand, brand_from = resolve_brand(rec, brands)
    if not brand:
        return None, ("no brand on the page, none of the Brand Master's brands in the title" if brands is not None
                      else "no brand on the page")
    if not title or len(title) < 2:
        return None, "no title"
    site = site_slug(rec.get("site"), url)
    item_id = _text(rec.get("item_id") or rec.get("id") or rec.get("sku"), 150)
    currency = (_text(rec.get("currency"), 3) or "").upper()
    currency = currency if re.fullmatch(r"[A-Z]{3}", currency) else None
    market = market_of(url, currency)
    observed = rec.get("scraped_at") or now()
    condition = _text(rec.get("condition"), 60)
    resale = is_resale(site, rec)

    prices = []
    price = _num(rec.get("price"))
    if price and currency:
        prices.append({"amount": price, "currency": currency,
                       "evidenceType": "RESALE_ASKING" if resale else "RETAIL_CURRENT",
                       "market": market, "url": url, "observedAt": observed,
                       **({"condition": condition} if condition else {}),
                       "note": f"Harvester · {site}{' · sold' if rec.get('sold') else ''}"[:300]})
    orig = _num(rec.get("original_price"))
    if orig and currency and (not price or orig > price):
        prices.append({"amount": orig, "currency": currency, "evidenceType": "RETAIL_ORIGINAL",
                       "market": market, "url": url, "observedAt": observed,
                       "note": f"Harvester · {site} · struck-through price"})

    variants = []
    for v in ((rec.get("attributes") or {}).get("variants") or [])[:MAX_VARIANTS]:
        if not isinstance(v, dict) or not _text(v.get("title"), 80):
            continue
        vp = _num(v.get("price"))
        variants.append({k: x for k, x in {
            "sizeLabel": _text(v.get("title"), 80), "sku": _text(v.get("sku"), 80),
            "price": vp, "originalPrice": _num(v.get("compare_at_price")),
            "currency": currency if vp else None,
            "inStock": v.get("available") if isinstance(v.get("available"), bool) else None,
        }.items() if x is not None})
    if not variants and _text(rec.get("size"), 80) and len(str(rec.get("size"))) <= 80:
        variants.append({"sizeLabel": _text(rec.get("size"), 80),
                         **({"inStock": not rec.get("sold")} if rec.get("sold") is not None else {})})

    attrs = {}
    for k in ("site", "condition", "availability", "seller", "location", "rating", "review_count", "sold",
              "item_id", "job", "source"):
        v = rec.get(k) if k not in ("site", "item_id") else (site if k == "site" else item_id)
        if v in (None, "", [], {}):
            continue
        attrs[f"harvester_{k}"] = v if isinstance(v, (int, float, bool)) else _text(v, 2000)
    if brand_from in ("title", "page-matched"):
        attrs["harvester_brand_from"] = brand_from
        if rec.get("brand") and _text(rec.get("brand"), 120) != brand:
            attrs["harvester_brand_scraped"] = _text(rec.get("brand"), 120)
    attrs = dict(list(attrs.items())[:MAX_ATTRIBUTES])

    images = [u for u in (rec.get("image_urls") or rec.get("images") or [])
              if isinstance(u, str) and u.startswith(("http://", "https://")) and len(u) <= 2000][:MAX_IMAGES]
    material = _text(rec.get("material"), 60)
    row = {
        "brand": brand, "title": title,
        "externalId": _text(f"{site}:{item_id}" if item_id else url.split("#")[0], 200),
        "url": url[:2000], "market": market,
        "category": category_of(rec, job), "gender": gender_of(rec, job),
        "color": _text(rec.get("color"), 60), "materials": [material] if material else None,
        "description": _text(rec.get("description"), 10_000),
        "sizeOnLabel": _text(rec.get("size"), 80),
        **split_size(rec.get("size")),
        "tags": [t for t in ("harvester", site, _text(rec.get("job"), 60), "resale" if resale else "retail") if t],
        "status": "ACTIVE", "images": images or None,
        "variants": variants or None, "prices": prices or None, "attributes": attrs or None,
    }
    return {k: v for k, v in row.items() if v not in (None, "", [], {})}, None


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------
# The website's name as Stock shows its source. Stock maps the key to its one source per site
# (stock-vnyx-api src/services/source-canonical.ts); an unknown site keeps its own.
SITE_NAMES = {
    "vinted": "Vinted", "grailed": "Grailed", "depop": "Depop", "ebay": "eBay", "poshmark": "Poshmark",
    "vestiairecollective": "Vestiaire Collective", "vestiaire": "Vestiaire Collective",
    "kleinanzeigen": "Kleinanzeigen", "marktplaats": "Marktplaats", "2dehands": "2dehands", "sellpy": "Sellpy",
    "subito": "Subito", "pangaia": "Pangaia", "zalando": "Zalando", "etsy": "Etsy", "mercari": "Mercari",
    "thredup": "thredUP", "wallapop": "Wallapop", "tise": "Tise", "stockx": "StockX", "klekt": "Klekt",
}


def stock_source(site: str) -> dict:
    """The Stock source of a site: {"key": "vinted", "name": "Vinted"}."""
    key = re.sub(r"[^a-z0-9]+", "-", (site or "").lower()).strip("-") or "harvester"
    return {"key": key[:60], "name": (SITE_NAMES.get(key) or site.strip().title() or "Harvester")[:120]}


def post_bulk(scfg: dict, site: str, rows: list[dict], dry_run: bool, session=None) -> dict:
    """POST /products/bulk for one site's rows. Retries a network error / 5xx three times; a 4xx is final.
    -> {"ok": True, "data": {...}} or {"ok": False, "error": "..."}."""
    http = session or requests
    body = {"products": rows, "dryRun": dry_run,
            "source": stock_source(site)}
    last = ""
    for attempt in range(4):
        try:
            r = http.post(f"{scfg['url']}/products/bulk", json=body, timeout=180,
                          headers={"x-api-key": scfg["api_key"], "accept": "application/json"})
            try:
                js = r.json()
            except ValueError:
                js = {}
            if r.status_code < 400:
                return {"ok": True, "data": js.get("data", js)}
            last = f"HTTP {r.status_code}: {js.get('message') or r.text[:300]}"
            if r.status_code < 500:
                return {"ok": False, "error": last}
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(5 * (attempt + 1))
    return {"ok": False, "error": last}


def ensure_table(db) -> None:
    db.c.executescript(SCHEMA)


def to_push(db, retry_failed: bool = False, day: str | None = None) -> list[dict]:
    """Products never sent, scraped again since they were sent, or (with retry_failed) refused before."""
    ensure_table(db)
    q = """SELECT p.* FROM products p LEFT JOIN stock_push s ON s.url = p.url
           WHERE (s.url IS NULL OR p.scraped_at > s.scraped_at {failed})
           {day} ORDER BY p.id"""
    args: list = []
    q = q.format(failed="OR s.status = 'failed'" if retry_failed else "",
                 day="AND p.day = ?" if day else "")
    if day:
        args.append(day)
    return [dict(r) for r in db.x(q, args)]


def record(db, url, site, scraped_at, status, stock_code=None, code=None, error=None) -> None:
    db.x("""INSERT OR REPLACE INTO stock_push(url, site, scraped_at, status, stock_code, code, error, pushed_at)
            VALUES (?,?,?,?,?,?,?,?)""", (url, site, scraped_at, status, stock_code, code,
                                          (error or "")[:500] or None, now()))


def push(db, cfg: dict, *, dry_run: bool = False, retry_failed: bool = False, day: str | None = None,
         poster=None, brand_loader=None) -> dict:
    """Send the products to Stock, one call per site per batch. -> report with counts and failures."""
    scfg = stock_cfg(cfg)
    report = {"dry_run": dry_run, "products": 0, "sent": 0, "created": 0, "updated": 0, "would_create": 0,
              "would_update": 0, "failed": 0, "skipped": 0, "errors": [], "failures": []}
    if not scfg["api_key"]:
        report["errors"].append("stock.api_key / STOCK_API_KEY is not set - nothing sent")
        return report
    poster = poster or post_bulk
    jobs = {j.get("name"): j for j in cfg.get("jobs", [])}
    brands = brand_loader(scfg) if brand_loader else load_brands(scfg)
    by_site: dict[str, list[tuple[dict, dict]]] = {}
    for p in to_push(db, retry_failed, day):
        report["products"] += 1
        rec = json.loads(p["data"])
        rec.update({"url": p["url"], "job": p["job"], "source": p["source"], "scraped_at": p["scraped_at"],
                    "image_urls": json.loads(p.get("images") or "[]") or rec.get("images")})
        row, why = to_row(rec, jobs.get(p["job"]), brands)
        site = site_slug(rec.get("site") or p.get("site"), p["url"])
        if not row:
            report["skipped"] += 1
            report["failures"].append({"url": p["url"], "why": why})
            if not dry_run:
                record(db, p["url"], site, p["scraped_at"], "failed", code="NOT_SENT", error=why)
            continue
        by_site.setdefault(site, []).append((p, row))

    for site, items in by_site.items():
        for i in range(0, len(items), scfg["batch"]):
            chunk = items[i:i + scfg["batch"]]
            res = poster(scfg, site, [r for _, r in chunk], dry_run)
            if not res.get("ok"):
                report["errors"].append(f"{site}: {res.get('error')}")
                log.warning("stock push: %s batch of %d not sent - %s", site, len(chunk), res.get("error"))
                continue
            report["sent"] += len(chunk)
            for r in (res.get("data") or {}).get("results") or []:
                idx = r.get("index")
                if not isinstance(idx, int) or idx >= len(chunk):
                    continue
                p, row = chunk[idx]
                status = r.get("status") or "failed"
                if status in report:
                    report[status] += 1
                if status == "failed":
                    why = f"{r.get('code') or 'ERROR'}: {r.get('error') or ''}".strip()
                    report["failures"].append({"url": p["url"], "brand": row.get("brand"), "why": why})
                if dry_run:
                    continue
                record(db, p["url"], site, p["scraped_at"],
                       status if status in ("created", "updated") else "failed",
                       stock_code=r.get("stockCode"), code=r.get("code"),
                       error=r.get("error") if status == "failed" else None)
            log.info("stock push: %s %d rows -> %s", site, len(chunk),
                     json.dumps((res.get("data") or {}).get("summary") or {}))
    return report


def summary(db) -> dict:
    ensure_table(db)
    rows = db.x("SELECT status, COUNT(*) n FROM stock_push GROUP BY status").fetchall()
    out = {r["status"]: r["n"] for r in rows}
    out["not_sent_yet"] = len(to_push(db))
    return out
