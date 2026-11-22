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
