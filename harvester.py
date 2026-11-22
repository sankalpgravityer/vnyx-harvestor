#!/usr/bin/env python3
"""
Product harvester - headless, scheduled, polite multi-site product scraper for an Ubuntu server.

  python harvester.py check            # pre-flight: is each site reachable from THIS server's IP?
  python harvester.py run              # scrape today's share of every job (run by the systemd timer)
  python harvester.py run --job vinted-jackets --max 5
  python harvester.py run --parallel 3 # one thread per site (each site still sees the same polite pace)
  python harvester.py export           # today's products -> data/exports/<day>/products.csv/.json
  python harvester.py status           # quotas, cooldowns, block events, what reached Stock
  python harvester.py push --dry-run   # send products to the Stock API (bulk upload) - dry run: validate only
  python harvester.py push             # send products not sent yet (also runs after every `run`)

Configuration: config.yaml (see config.example.yaml).
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import logging
import math
import os
import random
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler

import yaml

from hv import product_scraper as ps
from hv import stock_push
from hv.adapters import Generic, Shopify, make_adapter
from hv.db import DB, today
from hv.guard import DomainBlocked, Guard, domain_of
from hv.net import Browser, Http, looks_blocked

ROOT = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("harvester")

# The scraper modules must never wait for a human or re-hit a blocked page on a server.
ps.OPEN_RETRIES = 0
ps.BLOCK_WAIT_FOR_USER = False


# ---------------------------------------------------------------------------
def load_config(path):
    raw = open(path, encoding="utf-8").read()
    raw = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), raw)   # ${ENV_VAR} support
    cfg = yaml.safe_load(raw) or {}
    st = cfg.setdefault("storage", {})
    for k, d in (("dir", "data"),):
        st.setdefault(k, d)
    base = st["dir"] if os.path.isabs(st["dir"]) else os.path.join(ROOT, st["dir"])
    st["dir"] = base
    for sub in ("exports", "profiles", "logs"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    return cfg


def setup_logging(cfg, verbose):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)-10s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = RotatingFileHandler(os.path.join(cfg["storage"]["dir"], "logs", "harvester.log"),
                             maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]


class Ctx:
    def __init__(self, cfg):
        self.cfg = cfg
        d = cfg["storage"]["dir"]
        self.db = DB(os.path.join(d, "harvester.db"))
        self.http = Http(cfg)
        self.guard = Guard(self.db, cfg, http_get=self.http.get,
                           ua="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome")
        self.browser = Browser(cfg, os.path.join(d, "profiles"))
        self.pages_dir = os.path.join(tempfile.gettempdir(), "harvester-pages")   # scratch, not kept
        self.tier_memory: dict = {}

    def worker(self) -> "Ctx":
        """A context for one site thread: its own SQLite connection (WAL lets them write in turn), HTTP sessions,
        guard and browser. The virtual display is the main context's, shared by every thread."""
        w = object.__new__(Ctx)
        d = self.cfg["storage"]["dir"]
        w.cfg, w.pages_dir, w.tier_memory = self.cfg, self.pages_dir, {}
        w.db = DB(os.path.join(d, "harvester.db"))
        w.http = Http(self.cfg)
        w.guard = Guard(w.db, self.cfg, http_get=w.http.get, ua=self.guard.ua)
        w.browser = Browser(self.cfg, os.path.join(d, "profiles"))
        return w


# ---------------------------------------------------------------------------
def clean_image_urls(rec):
    """Images are NOT downloaded - only their URLs (full-size where the site allows) go into the JSON."""
    urls = [u for u in (rec.get("images") or []) if isinstance(u, str) and u.startswith(("http://", "https://"))]
    rec["images"] = ps.dedupe_images(urls)
    rec.pop("image_files", None)
    return rec


OUTPUT_ORDER = ["title", "brand", "price", "currency", "original_price", "size", "color", "condition", "material",
                "category", "breadcrumbs", "availability", "description", "image_urls", "image_count", "seller",
                "location", "rating", "review_count", "attributes", "item_id", "site", "url", "job", "source",
                "scraped_at"]
DROP = {"raw", "page_text", "folder", "image_files", "images", "input_url", "final_url", "page_title", "status",
        "canonical_url", "id"}


def to_output(row) -> dict:
    """One clean JSON record per product."""
    d = json.loads(row["data"])
    d.update({"image_urls": d.get("images") or [], "image_count": len(d.get("images") or []),
              "item_id": str(d.get("id") or d.get("item_id") or "") or None,
              "job": row["job"], "source": row["source"], "scraped_at": row["scraped_at"]})
    out = {k: d[k] for k in OUTPUT_ORDER if d.get(k) not in (None, "", [], {})}
    out.update({k: v for k, v in d.items() if k not in out and k not in DROP and v not in (None, "", [], {})})
    return out


_write_lock = threading.Lock()


def write_json(ctx, day):
    """data/exports/<day>/products.json - rewritten after every product, so it is always up to date."""
    with _write_lock:                          # site threads write the same file
        return _write_json(ctx, day)


def _write_json(ctx, day):
    rows = ctx.db.products(None if day == "all" else day)
    out = os.path.join(ctx.cfg["storage"]["dir"], "exports", day)
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "products.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([to_output(r) for r in rows], f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)                      # atomic: a crash never leaves a half-written file
    return path, rows


def run_job(ctx, job, budget):
    name = job["name"]
    per_day = int(job.get("per_day", 30))
    runs = int(ctx.cfg.get("schedule", {}).get("runs_per_day", 5))
    done_today = ctx.db.count_today(name)
    remaining = per_day - done_today
    # spread the day's quota evenly over the scheduled runs (the timer fires runs_per_day times)
    target = min(remaining, budget, job.get("_run_max") or math.ceil(per_day / runs))
    if target <= 0:
        log.info("[%s] daily quota reached (%d/%d)", name, done_today, per_day)
        return 0
    dom = domain_of(job["site"] if "://" in job["site"] else "https://" + job["site"])
    if not ctx.guard.usable(dom):
        log.info("[%s] skipped: %s is cooling down or at its daily cap", name, dom)
        return 0
    try:
        ad = make_adapter(ctx, job)
    except Exception as e:
        log.error("[%s] adapter error: %s", name, e)
        return 0
    log.info("[%s] %s via %s - target this run: %d (today %d/%d)", name, dom, ad.name, target, done_today, per_day)
    saved = 0
    try:
        if isinstance(ad, Generic):
            if len(ctx.db.pending(name, target)) < target:
                ad.discover(target)
            for q in ctx.db.pending(name, target):
                if saved >= target:
                    break
                try:
                    rec, src = ad.scrape(q)
                except (DomainBlocked, PermissionError):
                    raise
                except Exception as e:
                    log.warning("[%s] failed %s: %s", name, q["url"], e)
                    ctx.db.mark(q["url"], "pending")
                    continue
                ctx.db.save_product(clean_image_urls(rec), name, src)
                write_json(ctx, today())
                saved += 1
                log.info("[%s] %d/%d %s | %s %s | %d image urls [%s]", name, saved, target,
                         (rec.get("title") or "")[:60], rec.get("price"), rec.get("currency") or "",
                         len(rec["images"]), src)
        else:
            for rec in ad.discover(target):
                ctx.db.save_product(clean_image_urls(rec), name, ad.name)
                write_json(ctx, today())
                saved += 1
                log.info("[%s] %d/%d %s | %s %s | %d image urls [%s]", name, saved, target,
                         (rec.get("title") or "")[:60], rec.get("price"), rec.get("currency") or "",
                         len(rec["images"]), ad.name)
    except DomainBlocked as e:
        log.warning("[%s] stopped: %s", name, e)
    except PermissionError as e:
        log.warning("[%s] stopped: %s", name, e)
    except Exception as e:
        log.error("[%s] stopped by an error: %s", name, e)
    return saved


def cmd_run(ctx, a):
    lock = open(os.path.join(ctx.cfg["storage"]["dir"], ".run.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another run is still active - exiting")
        return
    jobs = [j for j in ctx.cfg.get("jobs", []) if j.get("enabled", True) and (not a.job or j["name"] == a.job)]
    if a.max:
        for j in jobs:
            j["_run_max"] = a.max
    random.shuffle(jobs)                                   # no fixed visiting pattern
    total_cap = int(ctx.cfg.get("daily_total_limit", 200))
    t0, total = time.time(), 0
    groups = group_by_site(jobs)
    workers = min(len(groups), max(1, int(a.parallel or ctx.cfg.get("parallel_sites", 1))))
    try:
        if workers <= 1:
            for j in jobs:
                total += run_site(ctx, [j], total_cap)
        else:
            # One thread per site, never two on the same site: each site still sees one patient visitor,
            # so this changes the run time (about the slowest site instead of the sum), not the IP risk.
            if ctx.browser.mode == "xvfb":
                ctx.browser._ensure_display()             # one virtual display, shared by every thread
            log.info("running %d sites in parallel (%d threads)", len(groups), workers)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="site") as ex:
                futures = [ex.submit(run_site_thread, ctx, g, total_cap) for g in groups.values()]
                total = sum(f.result() for f in futures)
    finally:
        ctx.browser.shutdown()
    log.info("run finished: %d products in %.1f min (today total %d)", total, (time.time() - t0) / 60,
             ctx.db.count_today())
    cmd_export(ctx, argparse.Namespace(day=today(), quiet=True))
    scfg = stock_push.stock_cfg(ctx.cfg)
    if scfg["auto_push"] and scfg["api_key"]:
        cmd_push(ctx, argparse.Namespace(dry_run=False, retry_failed=False, day=None))


def group_by_site(jobs) -> dict:
    """Jobs grouped by domain, in their (shuffled) order: one thread per group."""
    groups: dict = {}
    for j in jobs:
        dom = domain_of(j["site"] if "://" in j["site"] else "https://" + j["site"])
        groups.setdefault(dom, []).append(j)
    return groups


def run_site(ctx, jobs, total_cap) -> int:
    """This site's jobs, one after the other."""
    saved = 0
    for j in jobs:
        budget = total_cap - ctx.db.count_today()
        if budget <= 0:
            log.info("daily_total_limit (%d) reached", total_cap)
            break
        saved += run_job(ctx, j, budget)
        ctx.browser.close()
    return saved


def run_site_thread(ctx, jobs, total_cap) -> int:
    w = ctx.worker()
    try:
        return run_site(w, jobs, total_cap)
    except Exception as e:                                # one site's crash never stops the others
        log.error("[%s] site thread stopped: %s", jobs[0]["name"], e)
        return 0
    finally:
        w.browser.shutdown()                              # its Chrome; the display is the main context's
        w.db.c.close()


def cmd_check(ctx, a):
    print(f"\nPre-flight check from this server's IP - {datetime.now():%Y-%m-%d %H:%M}\n")
    print(f"{'job':24} {'site':28} {'result':10} detail")
    print("-" * 100)
    for j in ctx.cfg.get("jobs", []):
        site = j["site"] if "://" in j["site"] else "https://" + j["site"]
        dom = domain_of(site)
        detail, verdict = "", "OK"
        try:
            ctx.guard.before(site, sleep=False)
            st, txt = ctx.http.get(site)
            reason = looks_blocked(st, txt)
            kind = j.get("type") or "generic"
            if not reason and Shopify.detect(ctx, site):
                kind = "shopify"
            if reason or (a.browser and kind == "generic"):
                ctx.guard.before(site, sleep=False)
                r = ctx.browser.open(site)
                if r and reason:
                    verdict = "BLOCKED"
                    detail = (f"HTTP: {reason}; browser: {r} -> this site blocks the server's IP. "
                              f"Use a residential proxy or an official API.")
                    if re.search(r"(^|\.)ebay\.", dom):
                        detail += " (eBay: type: ebay_api)"
                elif r:
                    verdict, detail = "BROWSER-BLOCKED", f"HTTP {st} ok but browser: {r}"
                elif reason:
                    detail = f"HTTP: {reason} -> stealth browser OK (product pages will use the browser)"
                    ctx.db.update_domain(dom, tier="browser")
                else:
                    detail = f"HTTP {st}, browser ok, adapter: {kind}"
            else:
                detail = f"HTTP {st}, adapter: {kind}"
            if verdict == "OK" and not ctx.guard.allowed(site + "/"):
                verdict, detail = "ROBOTS", "robots.txt disallows crawling this site"
        except Exception as e:
            verdict, detail = "ERROR", str(e)[:90]
        if ctx.guard.cooldown_left(dom):
            detail += f"  [cooling down {ctx.guard.cooldown_left(dom) / 3600:.1f}h]"
        print(f"{j['name'][:24]:24} {dom[:28]:28} {verdict:10} {detail}")
    ctx.browser.shutdown()
    print()


def cmd_export(ctx, a):
    day = a.day or today()
    path, rows = write_json(ctx, day)
    cols = ["job", "site", "item_id", "title", "brand", "price", "currency", "condition", "size", "color", "category",
            "description", "image_urls", "url", "source", "scraped_at"]
    with open(os.path.join(os.path.dirname(path), "products.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["image_urls"] = " | ".join(json.loads(r["images"] or "[]"))
            w.writerow(r)
    if not getattr(a, "quiet", False):
        print(f"{len(rows)} products -> {path} (+ products.csv)")


def cmd_push(ctx, a):
    """Products -> Stock API Product Master (POST /products/bulk). See hv/stock_push.py."""
    r = stock_push.push(ctx.db, ctx.cfg, dry_run=a.dry_run, retry_failed=a.retry_failed, day=a.day)
    mode = "DRY RUN (Stock validated, nothing written)" if a.dry_run else "pushed"
    if a.dry_run:
        counts = f"would create {r['would_create']}, would update {r['would_update']}"
    else:
        counts = f"created {r['created']}, updated {r['updated']}"
    log.info("stock push %s: %d products, %d sent - %s, refused %d, not sendable %d",
             mode, r["products"], r["sent"], counts, r["failed"], r["skipped"])
    for e in r["errors"]:
        log.warning("stock push: %s", e)
    for f in r["failures"][:30]:
        log.info("  not saved: %s - %s%s", f["url"][:90], f["why"], f" (brand {f['brand']})" if f.get("brand") else "")
    if len(r["failures"]) > 30:
        log.info("  ... %d more", len(r["failures"]) - 30)
    return r


def cmd_status(ctx, a):
    print(f"\nToday ({today()}): {ctx.db.count_today()} / {ctx.cfg.get('daily_total_limit', 200)} products\n")
    for j in ctx.cfg.get("jobs", []):
        pend = ctx.db.x("SELECT COUNT(*) FROM queue WHERE job=? AND status='pending'", (j["name"],)).fetchone()[0]
        print(f"  {j['name']:26} {ctx.db.count_today(j['name']):>4} / {j.get('per_day', 30):<4} queued {pend}")
    print("\nDomains:")
    for d in ctx.db.x("SELECT * FROM domains ORDER BY domain"):
        cd = ctx.guard.cooldown_left(d["domain"])
        print(f"  {d['domain']:28} requests today {d['requests_today'] if d['day'] == today() else 0:>4}"
              f"  strikes {d['strikes']}  {'COOLDOWN %.1fh (%s)' % (cd / 3600, d['last_block_reason']) if cd else 'ok'}")
    sp = stock_push.summary(ctx.db)
    print(f"\nStock API: created {sp.get('created', 0)}, updated {sp.get('updated', 0)}, "
          f"refused {sp.get('failed', 0)}, not sent yet {sp.get('not_sent_yet', 0)}"
          f"{'' if stock_push.stock_cfg(ctx.cfg)['api_key'] else '  (STOCK_API_KEY not set)'}")
    ev = ctx.db.x("SELECT * FROM events ORDER BY ts DESC LIMIT 10").fetchall()
    if ev:
        print("\nRecent block events:")
        for e in ev:
            print(f"  {e['ts']}  {e['domain']:24} {e['detail']}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Polite headless multi-site product harvester")
    ap.add_argument("command", choices=["run", "check", "export", "status", "push"])
    ap.add_argument("--config", "-c", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--job", help="only this job")
    ap.add_argument("--max", type=int, help="at most N products per job in this run")
    ap.add_argument("--parallel", type=int, help="run: sites at the same time (default: parallel_sites, 1)")
    ap.add_argument("--day", help="export: YYYY-MM-DD or 'all' (default today)")
    ap.add_argument("--browser", action="store_true", help="check: also test the stealth browser per site")
    ap.add_argument("--dry-run", action="store_true", help="push: Stock validates and matches, writes nothing")
    ap.add_argument("--retry-failed", action="store_true", help="push: also resend rows Stock refused before")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config)
    setup_logging(cfg, a.verbose)
    ctx = Ctx(cfg)
    {"run": cmd_run, "check": cmd_check, "export": cmd_export, "status": cmd_status,
     "push": cmd_push}[a.command](ctx, a)


if __name__ == "__main__":
    main()
