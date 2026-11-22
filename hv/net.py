"""Two fetch tiers.

HTTP tier    : curl_cffi with a real Chrome TLS/HTTP2 fingerprint (open source: github.com/lexiforest/curl_cffi).
               Cheap, fast, and indistinguishable from Chrome at the network level. Used for JSON endpoints,
               robots.txt and product pages that carry schema.org JSON-LD.
Browser tier : SeleniumBase UC mode (open source: github.com/seleniumbase/SeleniumBase) - patched chromedriver,
               no automation flags. On a screenless server the harvester starts a virtual display (Xvfb) and
               runs normal Chrome inside it - not Chrome's "headless" mode, which anti-bot systems fingerprint.
               The real mode is verified from Chrome's command line after every launch.
               mode: auto (default) = Xvfb on Linux, a normal visible window on macOS/Windows;
               headless is available too.

One identity per site: each domain gets its own sticky HTTP session (cookies kept) and its own Chrome profile
folder, and - if you configure proxies - always the same proxy. Rotating identities on every request looks
far more bot-like than one consistent "visitor".
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import time

from curl_cffi import requests as creq

from . import product_scraper as ps
from .guard import domain_of

log = logging.getLogger("harvester")

BLOCK_STATUS = {401, 403, 407, 429, 503}
BLOCK_TEXT = re.compile(r"captcha|are you a robot|verify you are human|access denied|attention required|"
                        r"just a moment|unusual traffic|pardon our interruption|robot check|"
                        r"request blocked|please go back and try again|px-captcha|datadome", re.I)


def looks_blocked(status: int, text: str) -> str | None:
    if status in BLOCK_STATUS:
        cf = " (Cloudflare challenge)" if text and re.search(r"cf-chl|challenge-platform|cloudflare", text[:30000], re.I) else ""
        return f"HTTP {status}{cf}"
    if status == 202 and len(text or "") < 10000:
        return "HTTP 202 JavaScript challenge"
    if text and len(text) < 20000 and BLOCK_TEXT.search(text[:20000]):
        return "bot-check page"
    return None


def pick_proxy(proxies: list[str], domain: str) -> str | None:
    if not proxies:
        return None
    return proxies[int(hashlib.md5(domain.encode()).hexdigest(), 16) % len(proxies)]   # sticky per domain


class Http:
    def __init__(self, cfg):
        self.proxies = [p for p in (cfg.get("proxies") or []) if p]
        self.sessions: dict[str, creq.Session] = {}
        self.impersonate = cfg.get("http", {}).get("impersonate", "chrome")

    def session(self, domain):
        if domain not in self.sessions:
            s = creq.Session(impersonate=self.impersonate, timeout=30)
            s.headers.update({"Accept-Language": "en-US,en;q=0.9"})
            p = pick_proxy(self.proxies, domain)
            if p:
                s.proxies = {"http": p, "https": p}
            self.sessions[domain] = s
        return self.sessions[domain]

    def get(self, url, **kw) -> tuple[int, str]:
        r = self.session(domain_of(url)).get(url, **kw)
        return r.status_code, r.text


class Browser:
    """One Chrome at a time (keeps RAM low on a small VPS); restarted per site and every N pages."""

    def __init__(self, cfg, profiles_dir):
        b = cfg.get("browser", {})
        self.mode = b.get("mode", "auto")              # auto | xvfb | visible | headless
        if self.mode == "auto":                        # Linux server -> virtual display, Mac/Windows -> real window
            self.mode = "xvfb" if sys.platform.startswith("linux") else "visible"
        self.restart_every = int(b.get("restart_every", 40))
        self.proxies = [p for p in (cfg.get("proxies") or []) if p]
        self.profiles_dir = profiles_dir
        self.driver, self.domain, self.pages = None, None, 0
        self.display = None

    def _ensure_display(self):
        """xvfb mode on a screenless server: start a virtual X display ourselves and point Chrome at it.
        (SeleniumBase's Driver() would otherwise silently switch to --headless when there is no screen.)"""
        if self.mode != "xvfb" or os.environ.get("DISPLAY") or self.display:
            return
        try:
            from sbvirtualdisplay import Display          # ships with SeleniumBase
        except ImportError:
            from pyvirtualdisplay import Display          # pip install pyvirtualdisplay
        self.display = Display(visible=False, size=(1920, 1080))
        self.display.start()                              # sets $DISPLAY for this process
        if not os.environ.get("DISPLAY"):
            raise RuntimeError("Xvfb did not start - install it: sudo apt-get install -y xvfb")
        log.info("virtual display started on %s (Chrome runs normally inside it - not headless)",
                 os.environ["DISPLAY"])

    def _verify_mode(self, profile):
        """Read Chrome's real command line (Linux) and make sure the mode is what the config asked for."""
        if not sys.platform.startswith("linux"):
            return
        headless = False
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="ignore")
            except OSError:
                continue
            if profile in cmd and "--headless" in cmd:
                headless = True
                break
        actual = "headless" if headless else "xvfb"
        if actual != self.mode:
            raise RuntimeError(f"browser.mode is '{self.mode}' but Chrome started as '{actual}'")
        log.info("Chrome running in %s mode", "HEADLESS" if headless else f"virtual display {os.environ.get('DISPLAY')}")

    def get(self, domain):
        if self.driver and (self.domain != domain or self.pages >= self.restart_every):
            self.close()
        if not self.driver:
            from seleniumbase import Driver
            proxy = pick_proxy(self.proxies, domain)
            kw = dict(uc=True, locale_code="en",
                      user_data_dir=os.path.join(self.profiles_dir, re.sub(r"[^a-z0-9.-]", "_", domain)))
            if proxy:
                kw["proxy"] = re.sub(r"^\w+://", "", proxy)
            if self.mode == "headless":
                kw["headless2"] = True
            else:
                self._ensure_display()
            self.driver = Driver(**kw)
            self._verify_mode(kw["user_data_dir"])
            self.driver.set_page_load_timeout(60)
            self.driver.set_script_timeout(30)
            self.domain, self.pages = domain, 0
        return self.driver

    def open(self, url) -> str | None:
        """Open a page once. Returns a block reason or None. Never retries a blocked page."""
        d = self.get(domain_of(url))
        self.pages += 1
        try:
            d.uc_open(url) if hasattr(d, "uc_open") else d.get(url)
        except Exception as e:
            return f"load error: {str(e)[:80]}"
        ps.wait_for_page(d)
        ps.dismiss_popups(d)
        if ps.is_blocked(d):
            time.sleep(10)                             # Cloudflare's JS check often clears by itself
            if ps.is_blocked(d):
                return "bot-check page (browser)"
        return None

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.driver, self.domain, self.pages = None, None, 0

    def shutdown(self):
        self.close()
        if self.display:
            try:
                self.display.stop()
            except Exception:
                pass
            self.display = None
