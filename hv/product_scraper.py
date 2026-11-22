#!/usr/bin/env python3
"""
product_scraper.py
==================
Scrape EVERYTHING from product page URLs with Selenium: title, brand, price,
size, condition, colour, category, description, seller, attributes, breadcrumbs,
full page text -- and download all product images.

Works out of the box on:
  * Grailed   (grailed.com/listings/...)
  * Vinted    (vinted.*/items/...)      -- all country domains
  * Amazon    (amazon.*/dp/...)         -- all country domains
  * eBay      (ebay.*/itm/...)
  * Any other shop that exposes schema.org JSON-LD / OpenGraph / microdata
    (Shopify, WooCommerce, Magento, Depop, Etsy, Myntra, Flipkart, ...)

Usage
-----
  python product_scraper.py "https://www.vinted.nl/items/5043909102-..." "https://www.grailed.com/listings/..."
  python product_scraper.py --file urls.txt --out output
  python product_scraper.py --file urls.txt --headless --no-images
  python product_scraper.py URL --undetected      # use undetected-chromedriver (better vs Cloudflare)

Output
------
  output/<site>/<id-slug>/product.json   -- every detail for that product
  output/<site>/<id-slug>/images/01.jpg  -- product photos
  output/products.json / products.csv     -- all products of the run combined
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# ----------------------------------------------------------------------------
# Browser helpers (shared with category_scraper.py)
# ----------------------------------------------------------------------------

def make_driver(headless: bool = False, undetected: bool = False, proxy: str | None = None,
                user_data_dir: str | None = None, window: str = "1440,1000",
                chrome_binary: str | None = None, chromedriver: str | None = None,
                stealth: bool = False):
    """Create a Chrome driver.

    Backends (strongest first):
      stealth=True    -> SeleniumBase UC mode (open source). Patches chromedriver, hides automation flags,
                         disconnects the driver while pages load and can click Cloudflare Turnstile.
      undetected=True -> undetected-chromedriver (open source).
      default         -> plain Selenium with basic hardening.
    All three return a normal Selenium WebDriver, so the rest of the code works unchanged.
    """
    if stealth:
        try:
            from seleniumbase import Driver  # type: ignore
            kw = dict(uc=True, locale_code="en", proxy=proxy, user_data_dir=user_data_dir,
                      binary_location=chrome_binary)
            if headless:
                kw["headless2"] = True          # Chrome's new headless mode (less detectable)
            # (on a Linux server without a screen, SeleniumBase starts a virtual Xvfb display itself)
            d = Driver(**{k: v for k, v in kw.items() if v is not None})
            d.set_page_load_timeout(60)
            d.set_script_timeout(30)
            return d
        except ImportError:
            print("[!] SeleniumBase not installed -> pip install seleniumbase. Falling back.")
    if undetected:
        try:
            import undetected_chromedriver as uc  # type: ignore
            o = uc.ChromeOptions()
            o.add_argument(f"--window-size={window}")
            o.add_argument("--lang=en-US")
            if proxy:
                o.add_argument(f"--proxy-server={proxy}")
            if user_data_dir:
                o.add_argument(f"--user-data-dir={user_data_dir}")
            if chrome_binary:
                o.binary_location = chrome_binary
            d = uc.Chrome(options=o, headless=headless, use_subprocess=True)
            d.set_page_load_timeout(60)
            return d
        except ImportError:
            print("[!] undetected-chromedriver not installed -> pip install undetected-chromedriver. "
                  "Falling back to normal Selenium.")

    o = Options()
    if headless:
        o.add_argument("--headless=new")
    o.add_argument(f"--window-size={window}")
    o.add_argument("--lang=en-US,en")
    o.add_argument("--disable-blink-features=AutomationControlled")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--disable-gpu")
    o.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    o.add_experimental_option("useAutomationExtension", False)
    o.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
    if proxy:
        o.add_argument(f"--proxy-server={proxy}")
    if user_data_dir:
        o.add_argument(f"--user-data-dir={user_data_dir}")
    if chrome_binary:
        o.binary_location = chrome_binary

    service = None
    if chromedriver:
        from selenium.webdriver.chrome.service import Service
        service = Service(chromedriver)
    d = webdriver.Chrome(options=o, service=service) if service else webdriver.Chrome(options=o)

    # hide navigator.webdriver + fix "HeadlessChrome" user agent
    try:
        d.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": (
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
            "window.chrome = window.chrome || {runtime:{}};")})
        ua = d.execute_script("return navigator.userAgent") or DEFAULT_UA
        if "Headless" in ua:
            d.execute_cdp_cmd("Network.setUserAgentOverride",
                              {"userAgent": ua.replace("HeadlessChrome", "Chrome")})
    except Exception:
        pass
    d.set_page_load_timeout(60)
    d.set_script_timeout(30)
    return d


COOKIE_SELECTORS = [
    "#onetrust-accept-btn-handler", "#sp-cc-accept", "#accept-recommended-btn-handler",
    "button#gdpr-banner-accept", "[data-testid='uc-accept-all-button']",
    "button[data-testid='cookie-accept']", "button[data-cookiefirst-action='accept']",
    "#didomi-notice-agree-button", ".fc-cta-consent", "button[aria-label='Accept all']",
    "button[aria-label='Accept cookies']", "#truste-consent-button", ".cc-allow", ".cky-btn-accept",
]
COOKIE_TEXTS = ["Accept all", "Accept All", "Accept all cookies", "Accept", "I agree", "Agree",
                "Allow all", "Allow all cookies", "Got it", "OK", "Alles accepteren", "Accepteren",
                "Akkoord", "Alle akzeptieren", "Akzeptieren", "Tout accepter", "Accepter",
                "Aceptar todo", "Accetta tutto"]


def dismiss_popups(driver) -> None:
    """Click cookie banners and close modal popups (login prompts, newsletters...)."""
    for sel in COOKIE_SELECTORS:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    driver.execute_script("arguments[0].click()", el)
                    time.sleep(0.4)
                    return _close_modals(driver)
        except Exception:
            pass
    try:
        clicked = driver.execute_script("""
            const texts = arguments[0];
            const btns = [...document.querySelectorAll('button, a[role=button], div[role=button]')];
            for (const t of texts) {
              const b = btns.find(b => b.offsetParent && b.innerText && b.innerText.trim() === t);
              if (b) { b.click(); return t; }
            }
            return null;""", COOKIE_TEXTS)
        if clicked:
            time.sleep(0.4)
    except Exception:
        pass
    _close_modals(driver)


def _close_modals(driver) -> None:
    try:
        driver.execute_script("""
          const sels = ['[aria-label="Close"]','[aria-label="close"]','button[class*="close" i]',
                        '[data-testid*="close" i]','[data-testid="modal-close"]'];
          for (const s of sels) for (const el of document.querySelectorAll(s)) {
            const dlg = el.closest('[role=dialog],[class*="modal" i],[class*="Modal"]');
            if (dlg && el.offsetParent) { try { el.click(); } catch(e) {} }
          }""")
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass


BLOCK_MARKERS = ["captcha", "are you a robot", "verify you are human", "access denied",
                 "attention required", "just a moment", "unusual traffic", "pardon our interruption",
                 "please enable js and disable any ad blocker", "datadome", "px-captcha",
                 "enter the characters you see below", "robot check", "rush hour and traffic is piling up",
                 "503 - service unavailable", "make sure you're not a robot", "request blocked",
                 "access to this page has been denied", "something went wrong. please try again",
                 "please go back and try again", "reference #", "errors.edgesuite.net"]


def is_blocked(driver) -> bool:
    try:
        title = (driver.title or "").lower()
        body = driver.execute_script(
            "return (document.body && document.body.innerText || '').slice(0, 3000).toLowerCase()") or ""
        url = driver.current_url.lower()
    except Exception:
        return False
    if "validatecaptcha" in url or "/captcha" in url:
        return True
    short_page = len(body) < 1500
    return any(m in title for m in BLOCK_MARKERS) or (short_page and any(m in body for m in BLOCK_MARKERS))


def handle_block(driver, headless: bool, wait_for_user: bool | None = None, final: bool = True) -> bool:
    wait_for_user = BLOCK_WAIT_FOR_USER if wait_for_user is None else wait_for_user
    """If a bot check is shown, wait for the user to solve it (headful mode). Returns True if still blocked."""
    if not is_blocked(driver):
        return False
    # Cloudflare "just a moment" often clears on its own
    for _ in range(8):
        time.sleep(2)
        if not is_blocked(driver):
            return False
    if not headless and wait_for_user and sys.stdin.isatty():
        input(f"\n[!] Bot check / CAPTCHA on {driver.current_url}\n"
              f"    Solve it in the Chrome window, then press ENTER here to continue... ")
        time.sleep(1.5)
        return is_blocked(driver)
    if final:
        print(f"   [!] Blocked by bot protection at {driver.current_url} "
              f"(tip: run without --headless, or with --undetected / --profile)")
    else:
        print("   [~] bot check detected, retrying...")
    return True


def wait_for_page(driver, timeout: int = 25) -> None:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete")
    except TimeoutException:
        pass
    time.sleep(1.0 + random.random())


def scroll_page(driver, steps: int = 8, pause: float = 0.5) -> None:
    """Scroll gradually to trigger lazy-loaded images/sections, then back to top."""
    try:
        h = driver.execute_script("return document.body.scrollHeight") or 3000
        for i in range(1, steps + 1):
            driver.execute_script("window.scrollTo(0, arguments[0]);", int(h * i / steps))
            time.sleep(pause)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.3)
    except Exception:
        pass


OPEN_RETRIES = 2          # the harvester sets this to 0: never re-hit a page that showed a bot check
BLOCK_WAIT_FOR_USER = True


def open_url(driver, url: str, headless: bool, retries: int | None = None) -> bool:
    retries = OPEN_RETRIES if retries is None else retries
    for attempt in range(retries + 1):
        try:
            if hasattr(driver, "uc_open"):                      # SeleniumBase UC mode
                driver.uc_open(url)       # (uc_open_with_reconnect is stronger but can drop the session)
            else:
                driver.get(url)
            wait_for_page(driver)
            dismiss_popups(driver)
            if is_blocked(driver) and hasattr(driver, "uc_gui_click_captcha") and not headless:
                try:                                            # Cloudflare Turnstile checkbox
                    driver.uc_gui_click_captcha()
                    time.sleep(3)
                except Exception:
                    pass
            if handle_block(driver, headless, final=(attempt == retries)):
                if attempt < retries:
                    time.sleep(4 + attempt * 4)
                    continue
                return False
            return True
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
                return True
            except Exception:
                pass
        except WebDriverException as e:
            print(f"[!] Error loading {url}: {e.msg if hasattr(e, 'msg') else e}")
            time.sleep(3)
    return False


# ----------------------------------------------------------------------------
# Generic utilities
# ----------------------------------------------------------------------------

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY", "₩": "KRW",
                    "zł": "PLN", "kr": "SEK", "Kč": "CZK", "Rs": "INR", "Rs.": "INR", "R$": "BRL",
                    "C$": "CAD", "A$": "AUD", "US$": "USD", "CHF": "CHF", "Ft": "HUF", "lei": "RON"}
PRICE_RE = re.compile(
    r"(?P<pre>US\$|C\$|A\$|R\$|[$€£₹¥₩]|Rs\.?|USD|EUR|GBP|INR|CAD|AUD|CHF|PLN|SEK)?\s?"
    r"(?P<num>\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s?"
    r"(?P<post>€|zł|kr|Kč|Ft|lei|USD|EUR|GBP|INR|PLN|SEK|CHF)?")


def parse_price(text) -> tuple[float | None, str | None]:
    """'€ 1.234,50' -> (1234.5, 'EUR');  '$120' -> (120.0, 'USD')."""
    if text is None:
        return None, None
    if isinstance(text, (int, float)):
        return float(text), None
    text = str(text).replace("\xa0", " ").strip()
    for m in PRICE_RE.finditer(text):
        cur = m.group("pre") or m.group("post")
        if not cur and not re.search(r"\d", m.group("num") or ""):
            continue
        num = m.group("num").replace(" ", "")
        if re.search(r",\d{1,2}$", num):            # 1.234,56
            num = num.replace(".", "").replace(",", ".")
        else:                                        # 1,234.56
            num = num.replace(",", "")
        try:
            val = float(num)
        except ValueError:
            continue
        cur = CURRENCY_SYMBOLS.get(cur, cur) if cur else None
        if cur or len(text) < 25:
            return val, cur
    return None, None


def clean(s):
    if s is None:
        return None
    if isinstance(s, str):
        s = s.replace("‎", "").replace("‏", "").replace("\xa0", " ")
        s = re.sub(r"[ \t]+", " ", s)
        s = re.sub(r"\n\s*\n+", "\n", s).strip()
        return s or None
    return s


def slugify(s: str, maxlen: int = 80) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")
    return s[:maxlen] or hashlib.md5(s.encode()).hexdigest()[:10]


def site_name(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    host = re.sub(r"^(www\d?|m)\.", "", host)
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net") and len(parts[-1]) == 2:
        return parts[-3]           # amazon.co.uk -> amazon
    return parts[-2] if len(parts) >= 2 else host


def first(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def walk(obj):
    """Yield every dict inside a nested JSON structure."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


# ----------------------------------------------------------------------------
# One JavaScript pass that harvests everything from the DOM
# ----------------------------------------------------------------------------

HARVEST_JS = r"""
const out = {};
const txt = el => el ? (el.innerText || el.textContent || '').trim() : null;
const abs = u => { try { return u ? new URL(u, location.href).href : null } catch(e) { return null } };
const inChrome = el => !!el.closest('header, footer, nav, [role=navigation], [role=banner], [role=contentinfo]');

out.url = location.href;
out.title = document.title;
out.h1 = txt(document.querySelector('h1'));
out.lang = document.documentElement.lang || null;
const can = document.querySelector('link[rel=canonical]'); out.canonical = can ? can.href : null;

// meta tags
out.meta = {};
for (const m of document.querySelectorAll('meta[content]')) {
  const k = m.getAttribute('property') || m.getAttribute('name') || m.getAttribute('itemprop');
  if (!k) continue;
  const v = m.getAttribute('content');
  if (out.meta[k] === undefined) out.meta[k] = v;
  else if (Array.isArray(out.meta[k])) out.meta[k].push(v);
  else if (out.meta[k] !== v) out.meta[k] = [out.meta[k], v];
}

// structured data scripts
out.jsonld = [...document.querySelectorAll('script[type="application/ld+json"]')].map(s => s.textContent);
const nd = document.getElementById('__NEXT_DATA__'); out.next_data = nd ? nd.textContent : null;
out.json_scripts = [...document.querySelectorAll('script[type="application/json"]')]
   .map(s => ({id: s.id || null, name: s.dataset.componentName || s.dataset.name || null, text: s.textContent}))
   .filter(s => s.text && s.text.length < 3000000).slice(0, 30);

// microdata itemprop
out.itemprops = {};
for (const el of document.querySelectorAll('[itemprop]')) {
  const k = el.getAttribute('itemprop');
  const v = el.getAttribute('content') || el.getAttribute('href') || el.getAttribute('src') || txt(el);
  if (v && v.length < 3000 && !(k in out.itemprops)) out.itemprops[k] = v;
}

// breadcrumbs
const bc = document.querySelector('nav[aria-label*="readcrumb"], [class*="breadcrumb" i], [itemtype*="BreadcrumbList"], [data-testid*="breadcrumb" i]');
out.breadcrumbs = bc ? [...bc.querySelectorAll('a, li > span, [itemprop=name]')].map(txt).filter(Boolean) : [];

// key/value attribute pairs (dl, tables, labelled rows, "Label: value" list items)
const kv = [];
const push = (k, v, src) => { k = (k||'').replace(/[:\s‎‏]+$/,'').trim(); v = (v||'').trim();
  if (k && v && k.length <= 60 && v.length <= 800 && k !== v) kv.push([k, v, src]); };
for (const dl of document.querySelectorAll('dl')) {
  if (inChrome(dl)) continue;
  for (const dt of dl.querySelectorAll('dt')) { const dd = dt.nextElementSibling; if (dd && dd.tagName === 'DD') push(txt(dt), txt(dd), 'dl'); }
}
for (const tr of document.querySelectorAll('table tr')) {
  if (inChrome(tr)) continue;
  const c = tr.querySelectorAll('th, td'); if (c.length === 2) push(txt(c[0]), txt(c[1]), 'table');
}
for (const row of document.querySelectorAll('[data-testid^="item-attributes-"], .details-list__item, .ux-labels-values, [class*="attribute" i] > li, [class*="spec" i] li, [class*="detail" i] li')) {
  if (inChrome(row)) continue;
  const lab = row.querySelector('.ux-labels-values__labels, .details-list__item-title, [class*="label" i], [class*="title" i], dt, th');
  const val = row.querySelector('.ux-labels-values__values, [itemprop], [class*="value" i], dd, td');
  const lt = lab ? txt(lab) : '', vt = val ? txt(val) : '';
  if (lt && vt && lab !== val && lt !== vt && !vt.startsWith(lt) && !lab.contains(val) && !val.contains(lab)) { push(lt, vt, 'row'); continue; }
  const parts = (txt(row) || '').split('\n').map(s => s.trim()).filter(Boolean);
  if (parts.length >= 2 && parts.length <= 4) push(parts[0], parts.slice(1).join(' '), 'row');
}
for (const li of document.querySelectorAll('main li, #detailBullets_feature_div li, [id*="detail" i] li, [class*="product" i] li')) {
  if (inChrome(li)) continue;
  const t = (txt(li) || '').replace(/[‎‏]/g, '');
  const m = t.match(/^([^:\n]{1,50}?)\s*:\s*([^\n]{1,400})$/);
  if (m) push(m[1], m[2], 'li');
}
out.kv = kv;

// price / description candidates
out.price_candidates = [...document.querySelectorAll('[itemprop=price], [data-testid*="price" i], [class*="price" i], [id*="price" i]')]
   .filter(e => e.offsetParent && !inChrome(e)).map(txt).filter(t => t && t.length < 60 && /\d/.test(t)).slice(0, 15);
out.description_candidates = [...document.querySelectorAll('[itemprop=description], [data-testid*="description" i], #description, #productDescription, [class*="description" i], [class*="Description"]')]
   .filter(e => !inChrome(e)).map(txt).filter(t => t && t.length > 10 && t.length < 20000)
   .sort((a,b) => b.length - a.length).slice(0, 5);

// images (with every possible lazy-load attribute + srcset)
const bestSrcset = ss => { if (!ss) return null; let best = null, bw = -1;
  for (const part of ss.split(',')) { const [u, d] = part.trim().split(/\s+/); const w = parseFloat(d) || 1; if (u && w > bw) { bw = w; best = u; } }
  return abs(best); };
out.images = [];
for (const img of document.querySelectorAll('img')) {
  const r = img.getBoundingClientRect();
  const pic = img.closest('picture'); let picSet = null;
  if (pic) { const s = pic.querySelector('source[srcset]'); if (s) picSet = s.getAttribute('srcset'); }
  out.images.push({
    src: abs(img.currentSrc || img.src),
    best: bestSrcset(img.getAttribute('srcset') || img.getAttribute('data-srcset') || picSet),
    lazy: abs(img.getAttribute('data-zoom-src') || img.getAttribute('data-old-hires') || img.getAttribute('data-large') ||
              img.getAttribute('data-src') || img.getAttribute('data-lazy') || img.getAttribute('data-original')),
    dyn: img.getAttribute('data-a-dynamic-image'),
    alt: img.alt || null, testid: img.dataset.testid || null,
    w: img.naturalWidth, h: img.naturalHeight, rw: Math.round(r.width), rh: Math.round(r.height),
    top: Math.round(r.top + window.scrollY), chrome: inChrome(img),
    link: (img.closest('a[href]') || {}).href || null
  });
}
out.is_shopify = !!(window.Shopify && (window.Shopify.shop || window.Shopify.theme)) || !!document.querySelector('link[href*="cdn.shopify.com"], script[src*="cdn.shopify.com"]') || /\/cdn\/shop\//.test(document.documentElement.innerHTML.slice(0, 200000));
out.page_text = (document.body ? document.body.innerText : '').slice(0, 60000);
return out;
"""


# ----------------------------------------------------------------------------
# Structured data parsing
# ----------------------------------------------------------------------------

def pick_main_product(products: list, url: str = "", title: str = ""):
    """The JSON-LD product this page is about. A page often also describes the products it recommends
    (Sellpy: 54 products for one item), so prefer the one whose url/@id is this page, then the one named
    like the page's heading, then the first."""
    if not products:
        return None
    path = urlparse(url or "").path.rstrip("/")
    for p in products:
        for k in ("url", "@id"):
            v = p.get(k)
            if isinstance(v, str) and path and urlparse(v).path.rstrip("/") == path:
                return p
    t = (title or "").strip().lower()
    if t:
        for p in products:
            n = str(p.get("name") or "").strip().lower()
            if n and (n in t or t in n):
                return p
    return products[0]


def same_product(main: dict, other: dict) -> bool:
    """Another JSON-LD product node that is a variant of the main one (ProductGroup), not a recommendation."""
    if other is main:
        return True
    gid = main.get("productGroupID") or main.get("inProductGroupWithID")
    if gid and gid in (other.get("productGroupID"), other.get("inProductGroupWithID")):
        return True
    if "productgroup" in str(main.get("@type")).lower():
        return any(v is other for v in as_list(main.get("hasVariant")))
    return False


def parse_jsonld(raw_list):
    """Return (products, breadcrumbs, all_objects) from JSON-LD script texts."""
    objs, products, crumbs = [], [], []
    for raw in raw_list or []:
        try:
            data = json.loads(raw)
        except Exception:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except Exception:
                continue
        objs.append(data)
        for d in walk(data):
            t = d.get("@type")
            types = [x.lower() for x in as_list(t) if isinstance(x, str)]
            if any(x in ("product", "productgroup", "individualproduct", "car", "offer") for x in types) and d.get("name"):
                products.append(d)
            if "breadcrumblist" in types:
                for it in as_list(d.get("itemListElement")):
                    if isinstance(it, dict):
                        nm = it.get("name") or (it.get("item") or {}).get("name") if isinstance(it.get("item"), dict) else it.get("name")
                        if nm:
                            crumbs.append(nm)
    products.sort(key=lambda p: 0 if "product" in str(p.get("@type")).lower() else 1)
    return products, crumbs, objs


def jsonld_to_record(p: dict) -> dict:
    offers = p.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict) and offers.get("@type") == "AggregateOffer":
        offers = {**offers, "price": offers.get("lowPrice") or offers.get("price")}
    brand = p.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    elif isinstance(brand, list):
        brand = ", ".join(b.get("name", "") if isinstance(b, dict) else str(b) for b in brand)
    rating = p.get("aggregateRating") or {}
    imgs = []
    for im in as_list(p.get("image")):
        if isinstance(im, dict):
            im = im.get("contentUrl") or im.get("url")
        if isinstance(im, str):
            imgs.append(im)
    seller = offers.get("seller") if isinstance(offers, dict) else None
    if isinstance(seller, dict):
        seller = seller.get("name")
    cond = offers.get("itemCondition") or p.get("itemCondition") if isinstance(offers, dict) else p.get("itemCondition")
    avail = offers.get("availability") if isinstance(offers, dict) else None
    strip = lambda s: s.rsplit("/", 1)[-1] if isinstance(s, str) else s
    return {
        "title": p.get("name"), "brand": brand, "description": p.get("description"),
        "sku": first(p.get("sku"), p.get("mpn"), p.get("productID"), p.get("gtin13"), p.get("gtin")),
        "price": offers.get("price") if isinstance(offers, dict) else None,
        "currency": offers.get("priceCurrency") if isinstance(offers, dict) else None,
        "availability": strip(avail), "condition": strip(cond),
        "color": p.get("color"), "size": p.get("size"), "material": p.get("material"),
        "category": p.get("category") if isinstance(p.get("category"), str) else None,
        "rating": rating.get("ratingValue") if isinstance(rating, dict) else None,
        "review_count": first(rating.get("reviewCount"), rating.get("ratingCount")) if isinstance(rating, dict) else None,
        "seller": seller, "images": imgs,
    }


# ----------------------------------------------------------------------------
# Site specific extractors (each returns a partial record + raw data)
# ----------------------------------------------------------------------------

def extract_grailed(driver, page) -> dict:
    m = re.search(r"/listings/(\d+)", page["url"])
    lid = m.group(1) if m else None
    blobs = [page.get("next_data")] + [s["text"] for s in page.get("json_scripts", [])]
    listing = None
    for raw in blobs:
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for d in walk(data):
            if "title" in d and ("photos" in d or "designers" in d) and (lid is None or str(d.get("id")) == lid):
                listing = d
                break
        if listing:
            break
    if not listing:
        return {}
    g = lambda *keys: first(*[listing.get(k) for k in keys])
    designers = listing.get("designers") or []
    brand = g("designerNames", "designer_names") or ", ".join(
        x.get("name", "") for x in designers if isinstance(x, dict)) or None
    user = g("seller", "user") or {}
    user = user if isinstance(user, dict) else {}
    score = first(user.get("sellerScore"), user.get("seller_score")) or {}
    photos = [p.get("url") for p in listing.get("photos", []) if isinstance(p, dict) and p.get("url")]
    traits = {t.get("name"): t.get("value") for t in listing.get("traits", []) if isinstance(t, dict)}
    conditions = {"is_new": "New", "is_gently_used": "Gently used", "is_used": "Used",
                  "is_worn": "Very worn", "is_not_specified": "Not specified"}
    cond = g("condition")
    rec = {
        "id": listing.get("id"), "title": listing.get("title"), "brand": brand,
        "price": listing.get("price"), "currency": listing.get("currency") or "USD",
        "size": g("prettySize", "exactSize", "size"),
        "condition": conditions.get(cond, cond), "color": g("color") or traits.get("color"),
        "category": g("categoryPath", "category_path", "category"),
        "description": listing.get("description"), "images": photos,
        "seller": first(user.get("username"), user.get("name")),
        "location": first(listing.get("location"), user.get("location")),
        "sold": listing.get("sold"),
        "attributes": {k: v for k, v in {
            "department": g("department"), "subcategory": g("subcategory"),
            "size_raw": g("size"), "price_drops": g("priceDrops", "price_drops"),
            "sold_price": g("soldPrice", "sold_price"), "sold_at": g("soldAt", "sold_at"),
            "make_offer": g("makeOffer"), "buy_now": g("buyNow"), "shipping": g("shipping"),
            "created_at": g("createdAt", "created_at"), "price_updated_at": g("priceUpdatedAt"),
            "followers": g("followerCount"), "measurements": g("measurements"), "styles": g("styles"),
            "badges": g("badges"), "authenticated": g("wasDigitallyAuthenticated"),
            "seller_rating": first(score.get("ratingAverage"), score.get("rating_average")),
            "seller_rating_count": first(score.get("ratingCount"), score.get("rating_count")),
            "seller_sold_count": first(score.get("soldCount"), user.get("totalBoughtAndSold"),
                                       user.get("total_bought_and_sold")),
            **{f"trait_{k}": v for k, v in traits.items()},
        }.items() if v not in (None, "", [], {})},
    }
    return {"record": rec, "raw": listing}


VINTED_API_JS = r"""
const cb = arguments[arguments.length - 1], id = arguments[0];
(async () => {
  for (const u of [`/api/v2/items/${id}/details`, `/api/v2/items/${id}`]) {
    try {
      const r = await fetch(u, {credentials: 'include', headers: {'Accept': 'application/json'}});
      if (r.ok) { const j = await r.json(); if (j && j.item) return cb(j.item); }
    } catch (e) {}
  }
  cb(null);
})();
"""


def extract_vinted(driver, page) -> dict:
    m = re.search(r"/items/(\d+)", page["url"])
    rec, raw = {}, None
    if m:
        try:
            raw = driver.execute_async_script(VINTED_API_JS, m.group(1))
        except Exception:
            raw = None
    if raw:
        price = raw.get("price")
        cur = raw.get("currency")
        if isinstance(price, dict):
            cur = price.get("currency_code") or cur
            price = price.get("amount")
        total = raw.get("total_item_price")
        if isinstance(total, dict):
            total = total.get("amount")
        user = raw.get("user") or {}
        brand = raw.get("brand_dto") or {}
        photos = [p.get("full_size_url") or p.get("url") for p in raw.get("photos", []) if isinstance(p, dict)]
        rec = {
            "id": raw.get("id"), "title": raw.get("title"),
            "brand": brand.get("title") if isinstance(brand, dict) and brand else raw.get("brand"),
            "price": price, "currency": cur, "price_incl_buyer_protection": total,
            "size": raw.get("size_title") or raw.get("size"), "condition": raw.get("status"),
            "color": ", ".join(c for c in [raw.get("color1"), raw.get("color2")] if c) or None,
            "description": raw.get("description"), "images": [p for p in photos if p],
            "seller": user.get("login"),
            "location": ", ".join(x for x in [user.get("city"), user.get("country_title")] if x) or None,
            "attributes": {k: v for k, v in {
                "favourites": raw.get("favourite_count"), "views": raw.get("view_count"),
                "catalog_id": raw.get("catalog_id"), "material": raw.get("material"),
                "service_fee": raw.get("service_fee"), "is_reserved": raw.get("is_reserved"),
                "is_closed": raw.get("is_closed"), "created_at": raw.get("created_at_ts"),
                "seller_rating": user.get("feedback_reputation"), "seller_reviews": user.get("feedback_count"),
            }.items() if v not in (None, "", [], {})},
        }
    if m and not rec.get("id"):
        rec["id"] = m.group(1)
    # Only ~5 photos are in the page; opening the gallery (lightbox) renders all of them.
    if not rec.get("images"):
        gallery = []
        try:
            driver.execute_script(
                "const p=document.querySelector('[data-testid=\"item-photo-1\"]'); if (p) p.click();")
            time.sleep(1.5)
            gallery = driver.execute_script("""
              return [...document.querySelectorAll('img')].filter(i => {
                const t = i.closest('[data-testid]'); return t && t.dataset.testid.startsWith('image-carousel-image');
              }).map(i => i.currentSrc || i.src);""") or []
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass
        dom_photos = [i for i in page.get("images", []) if (i.get("testid") or "").startswith("item-photo-")]
        dom_photos.sort(key=lambda i: int(re.search(r"\d+", i["testid"]).group()) if re.search(r"\d+", i["testid"]) else 99)
        rec["images"] = list(dict.fromkeys(
            gallery + [i.get("best") or i.get("src") for i in dom_photos if i.get("src")]))
    try:
        seller = driver.execute_script("const e=document.querySelector('[data-testid=\"profile-username\"]');return e?e.innerText.trim():null")
        if seller and not rec.get("seller"):
            rec["seller"] = seller
    except Exception:
        pass
    return {"record": rec, "raw": raw} if rec else {}


AMAZON_JS = r"""
const q = s => document.querySelector(s), t = s => { const e = q(s); return e ? e.innerText.trim() : null; };
const r = {};
r.title = t('#productTitle'); r.brand = t('#bylineInfo');
r.price = t('#corePrice_feature_div .a-price .a-offscreen') || t('#corePriceDisplay_desktop_feature_div .a-price .a-offscreen') ||
          t('#apex_desktop .a-price .a-offscreen') || t('.a-price.priceToPay .a-offscreen') || t('#priceblock_ourprice') || t('#priceblock_dealprice');
r.mrp = t('#corePriceDisplay_desktop_feature_div .a-price.a-text-price .a-offscreen') || t('.basisPrice .a-offscreen');
r.discount = t('.savingsPercentage');
r.rating = (q('#acrPopover') || {}).title || t('#acrPopover .a-icon-alt');
r.review_count = t('#acrCustomerReviewText');
r.availability = t('#availability');
r.bullets = [...document.querySelectorAll('#feature-bullets li span.a-list-item, #productFactsDesktopExpander li')].map(e => e.innerText.trim()).filter(Boolean);
r.description = t('#productDescription') || t('#aplus_feature_div');
r.variations = {};
for (const c of document.querySelectorAll('[id^="variation_"], [id^="inline-twister-row-"]')) {
  const vals = [...c.querySelectorAll('li [title], li img[alt], option, li .swatch-title-text, li .a-button-text')]
    .map(e => (e.getAttribute('title') || e.getAttribute('alt') || e.innerText || '').replace(/^Click to select /,'').trim())
    .filter(v => v && v !== 'Select');
  if (vals.length) r.variations[c.id.replace(/^(variation_|inline-twister-row-)/,'')] = [...new Set(vals)];
}
r.seller = t('#sellerProfileTriggerId') || t('#merchant-info') || t('#tabular-buybox .tabular-buybox-text[tabular-attribute-name="Sold by"]');
return r;
"""


def extract_amazon(driver, page) -> dict:
    try:
        a = driver.execute_script(AMAZON_JS) or {}
    except Exception:
        a = {}
    src = driver.page_source
    imgs = re.findall(r'"hiRes":"(https://[^"]+)"', src) or re.findall(r'"large":"(https://[^"]+)"', src)
    if not imgs:
        for i in page.get("images", []):
            if i.get("dyn"):
                try:
                    dyn = json.loads(i["dyn"])
                    imgs.append(max(dyn.items(), key=lambda kv: kv[1][0] * kv[1][1])[0])
                except Exception:
                    pass
    asin = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", page["url"])
    price, cur = parse_price(a.get("price"))
    mrp, _ = parse_price(a.get("mrp"))
    rating, _ = parse_price((a.get("rating") or "").split(" ")[0]) if a.get("rating") else (None, None)
    brand = re.sub(r"^(Visit the |Brand: )|( Store)$", "", a.get("brand") or "").strip() or None
    rec = {"id": asin.group(1) if asin else None, "title": a.get("title"), "brand": brand,
           "price": price, "currency": cur, "original_price": mrp, "rating": rating,
           "review_count": re.sub(r"[^\d]", "", a.get("review_count") or "") or None,
           "availability": a.get("availability"), "description": a.get("description"),
           "seller": a.get("seller"), "images": list(dict.fromkeys(imgs)),
           "attributes": {k: v for k, v in {"discount": a.get("discount"), "bullet_points": a.get("bullets"),
                                            "variations": a.get("variations")}.items() if v}}
    return {"record": rec, "raw": a}


EBAY_JS = r"""
const t = s => { for (const x of s.split('||')) { const e = document.querySelector(x.trim()); if (e && e.innerText.trim()) return e.innerText.trim(); } return null; };
const lv = cls => t(`.ux-labels-values--${cls} .ux-labels-values__values`);
const specifics = {};
for (const row of document.querySelectorAll('.ux-layout-section-evo__item .ux-labels-values, .ux-layout-section--features .ux-labels-values, dl.ux-labels-values')) {
  const k = row.querySelector('.ux-labels-values__labels, dt'), v = row.querySelector('.ux-labels-values__values, dd');
  if (k && v) specifics[k.innerText.trim().replace(/:$/, '')] = v.innerText.trim();
}
return {
  title: t('.x-item-title__mainTitle || h1'),
  subtitle: t('.x-item-title__subTitle'),
  price: t('.x-price-primary || #prcIsum'),
  original_price: t('.x-additional-info__textual-display .ux-textspans--STRIKETHROUGH || .x-price-transparency--strikethrough'),
  condition: t('.x-item-condition-text .ux-textspans || .x-item-condition-value'),
  seller_notes: t('.x-item-condition-max-view .ux-section__item || [data-testid="x-item-condition"] .ux-expandable-textual-display-block-inline'),
  seller: t('.x-sellercard-atf__info__about-seller || .ux-seller-section__item--seller'),
  seller_feedback: t('.x-sellercard-atf__about-seller-item || .x-sellercard-atf__data-item'),
  quantity: t('#qtyAvailability || .x-quantity__availability'),
  watchers: t('.x-msku-evo__watching || [data-testid="x-watch-heart-btn"] .ux-textspans'),
  shipping: lv('shipping') || t('.ux-labels-values--deliveryto .ux-labels-values__values'),
  location: lv('itemLocation'),
  returns: lv('returns'),
  breadcrumbs: [...document.querySelectorAll('nav.breadcrumbs a span, .seo-breadcrumb-text span')].map(e => e.innerText.trim()).filter(Boolean),
  specifics,
  images: [...document.querySelectorAll('.ux-image-carousel-item img, .ux-image-filmstrip-carousel-item img, .ux-image-grid-item img')]
            .map(i => i.getAttribute('data-zoom-src') || i.getAttribute('data-src') || i.src).filter(Boolean),
  desc_iframe: (document.querySelector('iframe#desc_ifr') || {}).src || null
};
"""


def ebay_description(item_id: str, session: requests.Session | None = None) -> str | None:
    """The seller description lives on itm.ebaydesc.com - plain HTML, fetchable without a browser."""
    import html as _html
    try:
        r = (session or requests).get(f"https://itm.ebaydesc.com/itmdesc/{item_id}",
                                      headers={"User-Agent": DEFAULT_UA}, timeout=25)
        if r.status_code != 200:
            return None
        t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
        t = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", t, flags=re.I)
        t = _html.unescape(re.sub(r"<[^>]+>", " ", t))
        t = "\n".join(re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in t.splitlines())
        return re.sub(r"^eBay\s*", "", re.sub(r"\n{2,}", "\n", t).strip()) or None
    except Exception:
        return None


def extract_ebay(driver, page) -> dict:
    try:
        e = driver.execute_script(EBAY_JS) or {}
    except Exception:
        return {}
    imgs = [re.sub(r"s-l\d+\.(jpg|jpeg|png|webp)", r"s-l1600.\1", u.replace("/thumbs/images/", "/images/"))
            for u in e.get("images", [])]
    item = re.search(r"/itm/(?:[^/]+/)?(\d{9,})", page["url"])
    item_id = item.group(1) if item else None
    desc = ebay_description(item_id) if item_id else None
    if not desc and e.get("desc_iframe"):
        try:
            fr = driver.find_element(By.CSS_SELECTOR, "iframe#desc_ifr")
            driver.switch_to.frame(fr)
            desc = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            pass
        finally:
            driver.switch_to.default_content()
    price, cur = parse_price(e.get("price"))
    orig, _ = parse_price(e.get("original_price"))
    spec = e.get("specifics") or {}
    num = lambda s: int(re.sub(r"[^\d]", "", s)) if s and re.search(r"\d", s) else None
    qty = e.get("quantity") or ""
    sold = re.search(r"([\d,]+)\s+sold", qty)
    avail = re.search(r"([\d,]+)\s+available", qty)
    ship_cost = 0.0 if e.get("shipping") and re.search(r"free", e["shipping"], re.I) else parse_price(e.get("shipping"))[0]
    rec = {"id": item_id, "title": e.get("title"), "price": price, "currency": cur, "original_price": orig,
           "brand": spec.get("Brand"), "size": spec.get("Size"), "color": spec.get("Color") or spec.get("Colour"),
           "condition": e.get("condition"), "seller": e.get("seller"), "location": e.get("location"),
           "category": " > ".join(e.get("breadcrumbs") or []) or None,
           "description": desc, "images": list(dict.fromkeys(imgs)),
           "attributes": {**spec, **{k: v for k, v in {
               "subtitle": e.get("subtitle"), "seller_notes": e.get("seller_notes"),
               "seller_feedback": e.get("seller_feedback"), "shipping": e.get("shipping"),
               "shipping_cost": ship_cost, "returns": e.get("returns"),
               "quantity_available": num(avail.group(1)) if avail else None,
               "sold_count": num(sold.group(1)) if sold else None,
               "watchers": num(e.get("watchers"))}.items() if v not in (None, "", [])}}}
    return {"record": rec, "raw": e}


SHOPIFY_JS = r"""
const cb = arguments[arguments.length - 1];
const path = location.pathname.replace(/\/$/, '');
fetch(path + '.js', {headers: {'Accept': 'application/json'}}).then(r => r.ok ? r.json() : null)
  .then(j => cb(j)).catch(() => cb(null));
"""


def extract_shopify(driver, page) -> dict:
    """Any Shopify store: /products/<handle>.js returns all images, variants, sizes, stock."""
    if "/products/" not in page["url"]:
        return {}
    try:
        p = driver.execute_async_script(SHOPIFY_JS)
    except Exception:
        p = None
    if not p or not isinstance(p, dict) or not p.get("title"):
        return {}
    cents = lambda v: round(v / 100, 2) if isinstance(v, (int, float)) else v
    options = {o.get("name"): o.get("values") for o in p.get("options", []) if isinstance(o, dict)}
    variants = [{"title": v.get("title"), "sku": v.get("sku"), "price": cents(v.get("price")),
                 "compare_at_price": cents(v.get("compare_at_price")), "available": v.get("available"),
                 "options": [v.get("option1"), v.get("option2"), v.get("option3")]}
                for v in p.get("variants", [])]
    size_opt = next((vals for name, vals in options.items() if re.search(r"size|maat|größe|taille", str(name), re.I)), None)
    color_opt = next((vals for name, vals in options.items() if re.search(r"colou?r|kleur|farbe|couleur", str(name), re.I)), None)
    imgs = [("https:" + u if u.startswith("//") else u) for u in p.get("images", []) if isinstance(u, str)]
    desc = re.sub(r"<[^>]+>", "\n", p.get("description") or "")
    rec = {"id": p.get("id"), "title": p.get("title"), "brand": p.get("vendor"),
           "price": cents(p.get("price")), "original_price": cents(p.get("compare_at_price")),
           "category": p.get("type"), "description": desc, "images": imgs,
           "size": ", ".join(map(str, size_opt)) if size_opt else None,
           "color": ", ".join(map(str, color_opt)) if color_opt else None,
           "availability": "InStock" if p.get("available") else "OutOfStock",
           "attributes": {k: v for k, v in {
               "options": options, "variants": variants, "tags": p.get("tags"),
               "available_sizes": [v["title"] for v in variants if v["available"]] if size_opt else None,
               "handle": p.get("handle"), "published_at": p.get("published_at")}.items() if v}}
    return {"record": rec, "raw": p}


SITE_EXTRACTORS = {"grailed": extract_grailed, "vinted": extract_vinted,
                   "amazon": extract_amazon, "ebay": extract_ebay}


# ----------------------------------------------------------------------------
# Image selection & downloading
# ----------------------------------------------------------------------------

BAD_IMG = re.compile(r"logo|icon|sprite|avatar|badge|flag|pixel|spinner|loader|placeholder|blank\.|"
                     r"transparent|1x1|tracking|beacon|payment|rating|star|/ads?/|banner|emoji", re.I)


def upgrade_image_url(u: str) -> str:
    if not u:
        return u
    u = u.replace("&amp;", "&")
    if "media-amazon.com" in u or "ssl-images-amazon.com" in u:
        u = re.sub(r"\._[^/]*?_\.(jpg|jpeg|png|webp|gif)$", r".\1", u)
    if "ebayimg.com" in u:
        u = re.sub(r"s-l\d+\.(jpg|jpeg|png|webp)", r"s-l1600.\1", u)
    if "assets.myntassets.com" in u:
        u = re.sub(r"/h_\d+,q_\d+,w_\d+/", "/", u)
    if "cdn.shopify.com" in u:
        u = re.sub(r"_(\d+x\d*|\d*x\d+)(?=\.(jpg|jpeg|png|webp))", "", u)
    if "rukminim" in u:            # flipkart
        u = re.sub(r"/image/\d+/\d+/", "/image/1080/1080/", u)
    return u


def _links_elsewhere(link: str | None, page_url: str) -> bool:
    """An image inside a link to ANOTHER page is a tile of another product ("more from this seller",
    "you may also like" ...) - a product's own gallery images are not links to other pages."""
    if not link or link.startswith(("javascript:", "#")):
        return False
    a, b = urlparse(link), urlparse(page_url or "")
    if re.search(r"\.(jpe?g|png|webp|avif|gif)(\?|$)", a.path, re.I):       # a link to the image itself (zoom)
        return False
    return (a.netloc, a.path.rstrip("/")) != (b.netloc, b.path.rstrip("/"))


def pick_dom_images(page: dict, limit: int = 40) -> list[str]:
    """Fallback: pick large, non-decorative images from the top part of the page."""
    cands = []
    for i in page.get("images", []):
        url = i.get("lazy") or i.get("best") or i.get("src")
        if not url or url.startswith("data:") or url.endswith(".svg") or i.get("chrome"):
            continue
        if _links_elsewhere(i.get("link"), page.get("url") or ""):
            continue
        if BAD_IMG.search(url) or (i.get("alt") and BAD_IMG.search(i["alt"])):
            continue
        size = max(i.get("w") or 0, i.get("rw") or 0)
        if size < 250:
            continue
        cands.append((i.get("top", 0), url))
    cands.sort()
    # keep images in the product area (first ~2500px) when there are many
    top_area = [u for top, u in cands if top < 2500]
    chosen = top_area if len(top_area) >= 1 else [u for _, u in cands]
    return list(dict.fromkeys(chosen))[:limit]


def _norm_img_key(u: str) -> str:
    """Same photo served from another CDN host or at another size -> same key."""
    p = urlparse(u)
    path = re.sub(r"/image/\d+/\d+/", "/image/", p.path)                 # flipkart sizes
    path = re.sub(r"/(f\d+|s-l\d+|\d+x\d+|w_\d+[^/]*|h_\d+[^/]*)/", "/", path)
    path = re.sub(r"s-l\d+(\.\w+)$", r"s-l\1", path)                     # ebay s-l140.jpg vs s-l1600.jpg
    path = re.sub(r"^/fit-in(?=/)", "", path)                             # resizer prefix (Sellpy CDN vs S3)
    return path


def dedupe_images(urls):
    seen, out = set(), []
    for u in urls:
        if not u or not isinstance(u, str) or u.startswith("data:"):
            continue
        u = upgrade_image_url(u)
        k = _norm_img_key(u)
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out


def requests_session_from_driver(driver) -> requests.Session:
    s = requests.Session()
    try:
        ua = driver.execute_script("return navigator.userAgent").replace("HeadlessChrome", "Chrome")
    except Exception:
        ua = DEFAULT_UA
    s.headers.update({"User-Agent": ua, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                      "Accept-Language": "en-US,en;q=0.9"})
    try:
        for c in driver.get_cookies():
            s.cookies.set(c["name"], c["value"], domain=c.get("domain"))
    except Exception:
        pass
    return s


EXT_BY_TYPE = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp",
               "image/gif": ".gif", "image/avif": ".avif"}


def download_images(urls, folder, session: requests.Session, referer: str, driver=None, workers: int = 6):
    os.makedirs(folder, exist_ok=True)
    hashes = set()

    def fetch(idx_url):
        idx, url = idx_url
        for attempt in range(3):
            try:
                r = session.get(url, headers={"Referer": referer}, timeout=30)
                if r.status_code == 200 and r.content and len(r.content) > 1500:
                    return idx, url, r.content, r.headers.get("Content-Type", "").split(";")[0]
                if r.status_code in (403, 404):
                    break
            except requests.RequestException:
                time.sleep(1 + attempt)
        return idx, url, None, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(fetch, enumerate(urls, 1)))

    saved, failed = [], []
    for idx, url, content, ctype in results:
        if content is None and driver is not None:
            content, ctype = _download_in_browser(driver, url)
        if not content:
            failed.append(url)
            continue
        h = hashlib.md5(content).hexdigest()
        if h in hashes:
            continue
        hashes.add(h)
        ext = EXT_BY_TYPE.get(ctype) or os.path.splitext(urlparse(url).path)[1][:5] or ".jpg"
        path = os.path.join(folder, f"{len(saved) + 1:02d}{ext}")
        with open(path, "wb") as f:
            f.write(content)
        saved.append({"url": url, "file": path})
    return saved, failed


def _download_in_browser(driver, url):
    """Fallback for hot-link protected images: fetch inside the page (same cookies/origin)."""
    js = r"""
    const cb = arguments[arguments.length-1];
    fetch(arguments[0], {credentials:'include'}).then(r => r.ok ? r.blob() : null).then(b => {
      if (!b) return cb(null);
      const fr = new FileReader(); fr.onload = () => cb([fr.result, b.type]); fr.readAsDataURL(b);
    }).catch(() => cb(null));"""
    try:
        res = driver.execute_async_script(js, url)
        if res:
            import base64
            return base64.b64decode(res[0].split(",", 1)[1]), res[1]
    except Exception:
        pass
    return None, None


# ----------------------------------------------------------------------------
# Main product scrape
# ----------------------------------------------------------------------------

def scrape_product(driver, url: str, out_dir: str = "output", download: bool = True,
                   headless: bool = False, save_html: bool = False, screenshot: bool = False,
                   keep_raw: bool = True, already_open: bool = False) -> dict:
    """Open `url` and return a dict with every detail found. Saves JSON (+ images) under out_dir."""
    rec: dict = {"input_url": url, "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "site": site_name(url), "status": "ok"}
    if not already_open and not open_url(driver, url, headless):
        rec["status"] = "blocked_or_failed"
        rec["final_url"] = driver.current_url
        return rec

    # let galleries/lazy content load
    scroll_page(driver, steps=6, pause=0.4)
    dismiss_popups(driver)
    page = driver.execute_script(HARVEST_JS)
    rec["final_url"] = page["url"]
    rec["site"] = site_name(page["url"])

    # 1) site specific
    site_part = {}
    fn = SITE_EXTRACTORS.get(rec["site"]) or (extract_shopify if page.get("is_shopify") else None)
    if fn:
        try:
            site_part = fn(driver, page) or {}
        except Exception as e:
            print(f"   [!] {rec['site']} extractor error: {e}")
    srec = site_part.get("record", {})

    # 2) JSON-LD
    products, crumbs, ld_objs = parse_jsonld(page.get("jsonld"))
    main = pick_main_product(products, page.get("url") or url, page.get("h1") or "")
    jrec = jsonld_to_record(main) if main else {}

    # 3) meta / microdata / DOM
    meta, ip = page.get("meta", {}), page.get("itemprops", {})
    mprice = first(meta.get("product:price:amount"), meta.get("og:price:amount"), ip.get("price"))
    mcur = first(meta.get("product:price:currency"), meta.get("og:price:currency"), ip.get("priceCurrency"))
    dom_price, dom_cur = (None, None)
    for c in page.get("price_candidates", []):
        dom_price, dom_cur = parse_price(c)
        if dom_price is not None:
            break

    attributes: dict = {}
    for k, v, _src in page.get("kv", []):
        k, v = clean(k), clean(v)
        if k and v and k not in attributes and len(attributes) < 150:
            attributes[k] = v
    attributes.update({k: v for k, v in (srec.get("attributes") or {}).items()})

    def attr(*names):
        low = {k.lower(): v for k, v in attributes.items()}
        for n in names:
            for k, v in low.items():
                if k == n or k.startswith(n):
                    return v
        return None

    price = first(srec.get("price"), jrec.get("price"), mprice, dom_price)
    price_num, price_cur = parse_price(price) if not isinstance(price, (int, float)) else (float(price), None)

    title = first(srec.get("title"), jrec.get("title"), meta.get("og:title"), page.get("h1"), page.get("title"))
    description = first(srec.get("description"), jrec.get("description"),
                        (page.get("description_candidates") or [None])[0],
                        meta.get("og:description"), meta.get("description"))

    images = dedupe_images(
        as_list(srec.get("images")) + as_list(jrec.get("images")) +
        ([] if srec.get("images") else as_list(meta.get("og:image")) + as_list(meta.get("twitter:image"))))
    if not srec.get("images") or len(images) <= 1:
        # generic sites: JSON-LD often lists only 1 image -> add the gallery images found on the page
        images = dedupe_images(images + pick_dom_images(page))[:30]

    rec.update({
        "id": first(srec.get("id"), jrec.get("sku"), ip.get("sku"), ip.get("productID")),
        "title": clean(title),
        "brand": clean(first(srec.get("brand"), jrec.get("brand"), meta.get("product:brand"),
                             meta.get("og:brand"), ip.get("brand"), attr("brand", "merk", "marke", "designer"))),
        "price": price_num if price_num is not None else price,
        "currency": first(srec.get("currency"), jrec.get("currency"), mcur, price_cur, dom_cur),
        "original_price": srec.get("original_price"),
        "size": clean(first(srec.get("size"), jrec.get("size"), attr("size", "maat", "größe", "taille", "talla"))),
        "condition": clean(first(srec.get("condition"),
                                 attr("condition", "staat", "zustand", "état", "estado", "stan"),
                                 jrec.get("condition"))),
        "color": clean(first(srec.get("color"), jrec.get("color"), attr("color", "colour", "kleur", "farbe", "couleur"))),
        "material": clean(first(jrec.get("material"), attr("material", "materiaal", "fabric"))),
        "category": first(srec.get("category"), jrec.get("category"), meta.get("product:category")),
        "breadcrumbs": [clean(c) for c in (crumbs or page.get("breadcrumbs") or []) if clean(c)],
        "availability": clean(first(srec.get("availability"), jrec.get("availability"),
                                    meta.get("product:availability"))),
        "rating": first(srec.get("rating"), jrec.get("rating")),
        "review_count": first(srec.get("review_count"), jrec.get("review_count")),
        "seller": clean(first(srec.get("seller"), jrec.get("seller"))),
        "location": clean(srec.get("location")),
        "description": clean(description),
        "attributes": attributes,
        "images": images,
        "page_title": page.get("title"),
        "canonical_url": page.get("canonical"),
    })
    for extra in ("sold", "price_incl_buyer_protection"):
        if srec.get(extra) is not None:
            rec[extra] = srec[extra]
    if keep_raw:
        rec["raw"] = {"jsonld": ld_objs, "meta": meta, "itemprops": ip, "site_data": site_part.get("raw")}
    rec["page_text"] = page.get("page_text")

    # ---------- save ----------
    pid = str(rec.get("id") or "")
    path_tail = urlparse(page["url"]).path.rstrip("/").split("/")[-1]
    folder = os.path.join(out_dir, rec["site"], slugify(path_tail or pid or hashlib.md5(url.encode()).hexdigest()[:10]))
    os.makedirs(folder, exist_ok=True)
    rec["folder"] = folder

    if download and images:
        sess = requests_session_from_driver(driver)
        saved, failed = download_images(images, os.path.join(folder, "images"), sess, page["url"], driver)
        rec["image_files"] = [s["file"] for s in saved]
        if failed:
            rec["image_download_failed"] = failed
    if save_html:
        with open(os.path.join(folder, "page.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    if screenshot:
        try:
            driver.save_screenshot(os.path.join(folder, "screenshot.png"))
        except Exception:
            pass
    with open(os.path.join(folder, "product.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2, default=str)
    return rec


CSV_FIELDS = ["site", "id", "title", "brand", "price", "currency", "original_price", "size", "condition",
              "color", "material", "category", "breadcrumbs", "availability", "rating", "review_count",
              "seller", "location", "description", "images", "image_files", "attributes",
              "final_url", "input_url", "status", "scraped_at"]


def write_outputs(records, out_dir, name="products"):
    os.makedirs(out_dir, exist_ok=True)
    slim = [{k: v for k, v in r.items() if k not in ("raw", "page_text")} for r in records]
    with open(os.path.join(out_dir, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(out_dir, f"{name}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in slim:
            row = dict(r)
            for k in ("images", "image_files", "breadcrumbs"):
                if isinstance(row.get(k), list):
                    row[k] = " | ".join(map(str, row[k]))
            if isinstance(row.get("attributes"), dict):
                row["attributes"] = json.dumps(row["attributes"], ensure_ascii=False, default=str)
            w.writerow(row)


def add_browser_args(ap: argparse.ArgumentParser):
    g = ap.add_argument_group("browser")
    g.add_argument("--headless", action="store_true", help="run Chrome without a window (more likely to be blocked)")
    g.add_argument("--stealth", action="store_true",
                   help="use SeleniumBase UC mode - strongest open-source anti-detection (pip install seleniumbase)")
    g.add_argument("--undetected", action="store_true", help="use undetected-chromedriver (helps vs Cloudflare/DataDome)")
    g.add_argument("--profile", help="Chrome user-data-dir to reuse cookies/logins between runs, e.g. ./chrome-profile")
    g.add_argument("--proxy", help="proxy, e.g. http://user:pass@host:port")
    g.add_argument("--chrome-binary", help="path to a custom Chrome binary")
    g.add_argument("--chromedriver", help="path to chromedriver (default: auto via Selenium Manager)")
    g.add_argument("--delay", type=float, default=2.0, help="seconds to wait between pages (default 2)")


def main():
    ap = argparse.ArgumentParser(description="Scrape product pages (details + images) with Selenium.")
    ap.add_argument("urls", nargs="*", help="product page URLs")
    ap.add_argument("--file", "-f", help="text file with one URL per line")
    ap.add_argument("--out", "-o", default="output", help="output folder (default: output)")
    ap.add_argument("--no-images", action="store_true", help="do not download images")
    ap.add_argument("--save-html", action="store_true", help="also save the rendered HTML")
    ap.add_argument("--screenshot", action="store_true", help="also save a screenshot of each page")
    ap.add_argument("--no-raw", action="store_true", help="don't store raw JSON-LD/meta/site JSON in product.json")
    add_browser_args(ap)
    a = ap.parse_args()

    urls = list(a.urls)
    if a.file:
        with open(a.file, encoding="utf-8") as f:
            urls += [l.strip() for l in f if l.strip() and not l.startswith("#")]
    urls = list(dict.fromkeys(urls))
    if not urls:
        ap.error("give at least one URL or --file")

    driver = make_driver(a.headless, a.undetected, a.proxy, a.profile, chrome_binary=a.chrome_binary,
                         chromedriver=a.chromedriver, stealth=a.stealth)
    records = []
    try:
        for n, url in enumerate(urls, 1):
            print(f"[{n}/{len(urls)}] {url}")
            try:
                r = scrape_product(driver, url, a.out, download=not a.no_images, headless=a.headless,
                                   save_html=a.save_html, screenshot=a.screenshot, keep_raw=not a.no_raw)
            except Exception as e:
                print(f"   [!] failed: {e}")
                r = {"input_url": url, "status": f"error: {e}", "site": site_name(url)}
            records.append(r)
            if r.get("status") == "ok":
                print(f"   -> {r.get('title')!r} | {r.get('brand')} | {r.get('price')} {r.get('currency') or ''} "
                      f"| {len(r.get('images', []))} images, {len(r.get('image_files', []))} saved -> {r.get('folder')}")
            else:
                print(f"   -> {r.get('status')}")
            write_outputs(records, a.out)
            time.sleep(a.delay + random.random())
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    print(f"\nDone. {sum(r.get('status') == 'ok' for r in records)}/{len(records)} scraped. "
          f"Combined results: {os.path.join(a.out, 'products.json')} / products.csv")


if __name__ == "__main__":
    main()
