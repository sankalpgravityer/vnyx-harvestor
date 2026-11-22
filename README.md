# Product Harvester

A headless, scheduled product scraper for an Ubuntu server. You list sites and categories in `config.yaml`, and
it collects **100–200 products per day**: title, brand, price, currency, size, colour, condition, category,
description, attributes and **image URLs** (images are never downloaded). The output is a daily JSON file
(plus CSV). SQLite is used only for internal state.

It is built so your server's IP stays clean: it behaves like one patient visitor per site, never hammers a
site that pushes back, and tells you up front which sites block your server no matter what.

```
harvester/
├── harvester.py            # CLI: check | run | export | status | push
├── config.example.yaml     # copy to config.yaml
├── install.sh              # one-time Ubuntu setup (Chrome, Xvfb, venv, systemd timer)
├── setup_mac.sh            # local test setup on macOS - see MAC_SETUP.md
├── config.local.example.yaml  # fast local-test config
├── tests/                  # pytest: the Stock push
├── deploy/                 # harvester.service + harvester.timer (5 runs/day, randomised)
└── hv/
    ├── guard.py            # rate limits, robots.txt, daily caps, circuit breaker  <- IP protection
    ├── net.py              # HTTP tier (curl_cffi) + stealth browser tier (SeleniumBase UC)
    ├── adapters.py         # shopify | ebay_api | generic
    ├── db.py               # SQLite: queue, products, per-site state, block events
    ├── product_scraper.py  # product page extraction (also usable standalone)
    ├── category_scraper.py # search-result discovery (also usable standalone)
    ├── ebay_api.py         # official eBay Browse API client
    └── stock_push.py       # harvested products -> Stock API Product Master (POST /products/bulk)
```

## Test locally on a Mac first

See **[MAC_SETUP.md](MAC_SETUP.md)**: `bash setup_mac.sh`, then `python harvester.py run -c config.local.yaml --max 2`.

## Deploy (Ubuntu 22.04 / 24.04, 2 GB RAM is enough)

```bash
scp -r harvester/ user@server:~/          # or git clone
ssh user@server
cd ~/harvester && bash install.sh          # Chrome, Xvfb, Python venv, systemd units
nano config.yaml                           # your sites / categories / per_day
.venv/bin/python harvester.py check --browser   # <- run this before scheduling
.venv/bin/python harvester.py run --max 2       # small test run
sudo systemctl enable --now harvester.timer     # start the schedule
```

Daily use:
```bash
.venv/bin/python harvester.py status       # today's counts, per-site requests, cooldowns, block events
.venv/bin/python harvester.py export       # data/exports/<today>/products.csv + products.json
.venv/bin/python harvester.py export --day all
tail -f data/logs/harvester.log
systemctl list-timers harvester.timer
```

Output (no images or HTML pages are ever saved):
- `data/exports/<day>/products.json`: **the result**, one record per product with `image_urls` (the site's own
  CDN links, full-size where possible). It's rewritten after every product, so it's always current.
- `data/exports/<day>/products.csv`: same data for Excel
- `data/harvester.db`: internal state only (queue, de-duplication, per-site cooldowns)

```json
{
  "title": "LEVI'S Slim Men Blue Jeans", "brand": "LEVI'S", "price": 806.0, "currency": "INR",
  "color": "Mid Indigo", "condition": "NewCondition", "availability": "InStock", "description": "...",
  "image_urls": ["https://rukmini1.flixcart.com/image/1500/1500/xif0q/jean/p/g/z/-original-imahpw5byemphngz.jpeg?q=70", "..."],
  "image_count": 6, "rating": 3.9, "item_id": "JEAHDPF2TEKFRXV2", "site": "flipkart",
  "url": "https://www.flipkart.com/levi-s-slim-men-blue-jeans/p/itm37bfc51389502?pid=JEAHDPF2TEKFRXV2",
  "job": "flipkart-jeans", "source": "http", "scraped_at": "2026-11-22T10:38:29+00:00"
}
```

## Into the Stock DB (Product Master)

Every run ends by sending the day's new products to the **Stock API** through its bulk upload
(`POST /products/bulk`, see `hv/stock_push.py`). No direct database access; nothing to change on Stock's side.

```bash
# /etc/harvester.env (systemd) - or export them for a manual run
STOCK_API_URL=https://api-stock.vnyx.ai
STOCK_API_KEY=...                       # one of Stock's PRICING_API_KEYS

.venv/bin/python harvester.py push --dry-run      # Stock validates and matches, writes nothing
.venv/bin/python harvester.py push                # send what has not been sent yet
.venv/bin/python harvester.py push --retry-failed # resend rows Stock refused (e.g. after adding a brand)
.venv/bin/python harvester.py status              # ... "Stock API: created N, updated N, refused N"
```

| Harvested | Stock |
|---|---|
| site + item id (else URL) | `externalId` - a product scraped again updates its row |
| brand, title, description, category, colour, size | the same fields (size -> `sizeOnLabel`) |
| image URLs | `images` (up to 20, never downloaded) |
| price | resale sites (Vinted, Grailed, eBay used ...) -> `RESALE_ASKING`; shops -> `RETAIL_CURRENT`; struck-through price -> `RETAIL_ORIGINAL` |
| Shopify variants | `variants` (size, price, in stock) |
| condition, seller, rating, job | `attributes` (`harvester_*`) |

Each site is its own Stock source, `upload:harvester-<site>`. **A product whose brand is not in Stock's Brand
Master is refused** (`BRAND_NOT_FOUND`), logged, and not sent again until `--retry-failed`. What reached Stock (PRD
code or the reason it did not) is kept in `data/harvester.db`, table `stock_push`. `stock.auto_push: false` in
`config.yaml` turns the end-of-run push off.

Tests: `.venv/bin/pip install pytest && .venv/bin/python -m pytest -q tests`

## How it avoids getting your IP blocked

| Measure | What it does |
|---|---|
| **Tiny, spread-out volume** | 100–200 products/day split over 5 runs at random times (timer jitter up to 45 min). Each site sees a few dozen requests a day, less than one shopper. |
| **Human pacing per site** | 12–35 s random gap between requests to the same site, plus a 1–3 min break about every 15 requests. Daily request cap per site (default 150). |
| **Least-intrusive source first** | Shopify stores: `/products.json` gives 250 products in one request, with no page scraping. eBay: official API. Other sites: one plain HTTP request per product when the page has schema.org data. The browser is used only when needed. |
| **Real fingerprints** | HTTP tier uses **curl_cffi** (Chrome's exact TLS/HTTP2 handshake). Browser tier uses **SeleniumBase UC mode** (patched chromedriver, no automation flags) in a **virtual display (Xvfb)**. It's headless as far as your server is concerned, but Chrome isn't in "headless mode", which is the most common thing bot detection looks for. |
| **One consistent identity per site** | Each site keeps its own cookies, Chrome profile and (if configured) proxy across runs. Constantly rotating identities looks *more* bot-like. |
| **Circuit breaker** | The first 403/429/CAPTCHA/bot-check from a site stops **all** traffic to it for 6 h, then 24 h, then 72 h on repeat. It never retries a blocked page. Retrying is what turns a temporary flag into a permanent IP ban. |
| **robots.txt respected** | Disallowed paths are skipped (e.g. Shopify's `/cart`). |
| **No CAPTCHA solving** | A CAPTCHA means the site has noticed you. The right response is to back off, not to push through. |

### What no scraper can promise

Some sites (eBay, Amazon on some regions, some Akamai/Cloudflare-protected shops) block **entire datacenter IP
ranges** (AWS, DigitalOcean, Hetzner…) on the very first request, before any behaviour is seen. No code can
change that, only *where the request comes from*:

- `harvester.py check --browser` detects it per site (`BLOCKED`). Don't schedule those sites from a bare VPS.
- Fix options: the site's **official API** (eBay: `type: ebay_api`, free keys), or **residential/mobile
  proxies** in `config.yaml → proxies:`. Each site gets one sticky proxy.

Tested from a cloud server while building this: pangaia.com (Shopify), Vinted, Flipkart, Grailed and Amazon.in
all worked. eBay was IP-blocked at the first request and was correctly paused.

### Headless or not? How the browser runs on the server

Your server has no screen, and nothing ever opens on it. With `browser.mode: auto` (the default) on Linux:

1. The harvester starts **Xvfb**, a virtual screen that exists only in memory (installed by `install.sh`).
2. Chrome runs as a **normal (non-headless) browser** inside that virtual screen. To websites it looks like
   ordinary desktop Chrome. Chrome's `--headless` mode is the most commonly fingerprinted bot signal.
3. When the run ends, Chrome and Xvfb are shut down.

Every run logs what actually happened, read from Chrome's real launch flags:
```
virtual display started on :1001 (Chrome runs normally inside it - not headless)
Chrome running in virtual display :1001 mode
```
If Chrome ever starts in a different mode than configured, the job stops with an error instead of silently
running headless. Set `browser.mode: headless` to use true headless Chrome instead (no Xvfb, slightly less RAM,
easier to detect). Many products never need a browser at all: Shopify feeds, the eBay API and plain-HTTP product
pages don't use one.

## config.yaml

```yaml
daily_total_limit: 200
schedule: {runs_per_day: 5}          # keep in sync with deploy/harvester.timer
politeness: {min_delay: 12, max_delay: 35, max_requests_per_domain_per_day: 150, cooldown_hours: [6, 24, 72]}
browser: {mode: auto}                # auto = Xvfb on Linux, visible window on Mac | xvfb | visible | headless
proxies: []                          # optional residential proxies
jobs:
  - name: vinted-jackets
    site: https://www.vinted.nl
    category: jacket
    per_day: 40
    # type: auto|shopify|ebay_api|generic     fetch: auto|http|browser
    # search_url: "https://site/find?q={query}&page={page}"   (only for unusual search URLs)
    # min_images: 2   max_pages: 3   enabled: false
```

Adding a site is just adding a job. Built-in search settings exist for Amazon, eBay, Flipkart, Myntra, Ajio,
Meesho, Snapdeal, Nykaa Fashion, Walmart, Target, Etsy, ASOS, H&M, Zalando, Vinted, Grailed, Depop and Poshmark.
Shopify stores are auto-detected, and any other site falls back to search-box and product-card detection.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check` says BLOCKED | The site blocks your server's IP range: use `type: ebay_api` (eBay) or add proxies, or drop the site. |
| A site keeps landing in COOLDOWN | Increase `min_delay`/`max_delay`, lower its `per_day`, keep `browser.mode: xvfb`. |
| "no products found on search page" | Give the job a `search_url` with `{query}` (and `{page}`). |
| Chrome crashes on a 1 GB VPS | Use 2 GB, or set `browser.mode: headless` (lighter). |

Scrape only public data, keep the default pacing, and check each site's terms of service.
