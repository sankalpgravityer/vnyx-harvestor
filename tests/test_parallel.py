"""harvester.py run --parallel: one thread per site, never two on the same site."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harvester as H  # noqa: E402


class FakeBrowser:
    mode = "visible"

    def close(self):
        pass

    def shutdown(self):
        pass


class FakeDB:
    class _C:
        def close(self):
            pass

    c = _C()

    def count_today(self, job=None):
        return 0


class FakeCtx:
    def __init__(self, cfg):
        self.cfg, self.db, self.browser = cfg, FakeDB(), FakeBrowser()

    def worker(self):
        return FakeCtx(self.cfg)


JOBS = [{"name": "vinted-jackets", "site": "https://www.vinted.nl"},
        {"name": "vinted-coats", "site": "https://www.vinted.nl"},
        {"name": "grailed-jackets", "site": "https://www.grailed.com"},
        {"name": "pangaia-track-pants", "site": "https://pangaia.com"}]


def test_jobs_are_grouped_by_site():
    g = H.group_by_site(JOBS)
    assert list(g) == ["vinted.nl", "grailed.com", "pangaia.com"]
    assert [j["name"] for j in g["vinted.nl"]] == ["vinted-jackets", "vinted-coats"]


def test_sites_run_together_and_one_site_never_twice_at_once(monkeypatch, tmp_path):
    active, peak, per_site_overlap = {}, [0], []
    lock = threading.Lock()

    def fake_run_job(ctx, job, budget):
        dom = H.domain_of(job["site"])
        with lock:
            if active.get(dom):
                per_site_overlap.append(dom)
            active[dom] = True
            peak[0] = max(peak[0], sum(active.values()))
        time.sleep(0.3)
        with lock:
            active[dom] = False
        return 2

    monkeypatch.setattr(H, "run_job", fake_run_job)
    monkeypatch.setattr(H, "cmd_export", lambda *a, **k: None)
    monkeypatch.setattr(H.random, "shuffle", lambda x: None)
    cfg = {"storage": {"dir": str(tmp_path)}, "jobs": JOBS, "daily_total_limit": 200, "stock": {"auto_push": False}}
    ctx = FakeCtx(cfg)
    t0 = time.time()
    H.cmd_run(ctx, argparse.Namespace(job=None, max=None, parallel=3))
    took = time.time() - t0
    assert peak[0] == 3 and not per_site_overlap          # 3 sites at once; the two Vinted jobs one after another
    assert took < 0.9                                      # ~0.6 s (vinted twice) instead of 1.2 s in a row


def test_parallel_1_is_the_old_one_after_another_run(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(H, "run_job", lambda ctx, job, budget: order.append(job["name"]) or 1)
    monkeypatch.setattr(H, "cmd_export", lambda *a, **k: None)
    monkeypatch.setattr(H.random, "shuffle", lambda x: None)
    cfg = {"storage": {"dir": str(tmp_path)}, "jobs": JOBS, "stock": {"auto_push": False}}
    H.cmd_run(FakeCtx(cfg), argparse.Namespace(job=None, max=None, parallel=1))
    assert order == [j["name"] for j in JOBS]


def test_rescrape_requeues_browser_products_and_the_push_resends_them(tmp_path):
    import json as _json
    from hv.db import DB
    from hv import stock_push as SP

    class Ctx:
        pass

    ctx = Ctx()
    ctx.db = DB(str(tmp_path / "h.db"))
    ctx.cfg = {"jobs": [{"name": "sellpy-jassen", "site": "https://www.sellpy.nl"},
                        {"name": "pangaia-track-pants", "site": "https://pangaia.com"}]}
    ctx.db.save_product({"url": "https://www.sellpy.nl/item/A", "site": "sellpy", "title": "t", "brand": "B",
                         "images": ["bad.jpg"]}, "sellpy-jassen", "browser")
    ctx.db.save_product({"url": "https://pangaia.com/products/x", "site": "pangaia", "title": "t", "brand": "Pangaia",
                         "images": []}, "pangaia-track-pants", "shopify")
    SP.ensure_table(ctx.db)
    SP.record(ctx.db, "https://www.sellpy.nl/item/A", "sellpy", "2026-11-22T10:00:00+00:00", "created", "PRD_1")
    H.cmd_rescrape(ctx, argparse.Namespace(job=None))
    assert [p["url"] for p in ctx.db.products()] == ["https://pangaia.com/products/x"]      # shopify left alone
    assert [q["url"] for q in ctx.db.pending("sellpy-jassen", 10)] == ["https://www.sellpy.nl/item/A"]
    # read again -> newer scraped_at than the push record -> sent again (updates PRD_1)
    ctx.db.save_product({"url": "https://www.sellpy.nl/item/A", "site": "sellpy", "title": "t", "brand": "B",
                         "images": ["good.jpg"]}, "sellpy-jassen", "browser")
    ctx.db.x("UPDATE products SET scraped_at='2999-01-01T00:00:00+00:00' WHERE url=?", ("https://www.sellpy.nl/item/A",))
    assert "https://www.sellpy.nl/item/A" in [p["url"] for p in SP.to_push(ctx.db)]
