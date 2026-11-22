"""Politeness + IP protection.

Every request to a site goes through Guard.before(url) / Guard.after_ok(domain) / Guard.after_block(domain).

What it enforces, per domain:
  * random human-like gap between requests (min_delay..max_delay seconds, plus occasional longer pauses)
  * a hard daily request cap
  * robots.txt rules (optional but on by default)
  * circuit breaker: the first sign of a bot check / 403 / 429 stops ALL traffic to that domain
    for cooldown_hours[strike] (6h -> 24h -> 72h). Strikes reset after a clean day.
    Hammering a site that already pushed back is how a soft, temporary block becomes a permanent IP ban.
"""
from __future__ import annotations

import logging
import random
import time
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

log = logging.getLogger("harvester")


class DomainBlocked(Exception):
    pass


def domain_of(url: str) -> str:
    h = urlparse(url).netloc.lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


class Guard:
    def __init__(self, db, cfg: dict, http_get=None, ua: str = "Mozilla/5.0"):
        self.db = db
        p = cfg.get("politeness", {})
        self.min_delay = float(p.get("min_delay", 12))
        self.max_delay = float(p.get("max_delay", 35))
        self.long_pause_every = int(p.get("long_pause_every", 15))      # after ~N requests take a break
        self.long_pause = p.get("long_pause_seconds", [60, 180])
        self.daily_cap = int(p.get("max_requests_per_domain_per_day", 150))
        self.respect_robots = bool(p.get("respect_robots", True))
        self.cooldowns = [float(h) for h in p.get("cooldown_hours", [6, 24, 72])]
        self.http_get = http_get                 # used to fetch robots.txt with the same TLS fingerprint
        self.ua = ua
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        # Per-site pace (22 Nov 2026: Grailed raised a bot check after ~30 requests at the default pace).
        # cfg["site_politeness"] = {domain: {min_delay, max_delay, long_pause_every, long_pause_seconds,
        # max_requests_per_domain_per_day}}; built by harvester.load_config from each job's `politeness:`.
        self.site = {k.lower().removeprefix("www."): v for k, v in (cfg.get("site_politeness") or {}).items()}
        self._count: dict[str, int] = {}

    # ---------- status ----------
    def cooldown_left(self, domain: str) -> float:
        d = self.db.domain(domain)
        if not d["cooldown_until"]:
            return 0
        left = (datetime.fromisoformat(d["cooldown_until"]) - datetime.now(timezone.utc)).total_seconds()
        return max(0, left)

    def _site(self, domain: str, key: str, default):
        for dom, conf in self.site.items():
            if domain == dom or domain.endswith("." + dom):
                if key in conf:
                    return conf[key]
        return default

    def cap(self, domain: str) -> int:
        return int(self._site(domain, "max_requests_per_domain_per_day", self.daily_cap))

    def usable(self, domain: str) -> bool:
        d = self.db.domain(domain)
        return self.cooldown_left(domain) == 0 and d["requests_today"] < self.cap(domain)

    def next_allowed_in(self, domain: str) -> float:
        d = self.db.domain(domain)
        if not d["last_request"]:
            return 0
        gap = random.uniform(float(self._site(domain, "min_delay", self.min_delay)),
                             float(self._site(domain, "max_delay", self.max_delay)))
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(d["last_request"])).total_seconds()
        return max(0, gap - elapsed)

    # ---------- robots ----------
    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        dom = domain_of(url)
        if dom not in self._robots:
            rp = None
            try:
                base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
                status, text = self.http_get(base + "/robots.txt") if self.http_get else (0, "")
                if status == 200 and text:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(text.splitlines())
            except Exception:
                rp = None
            self._robots[dom] = rp
        rp = self._robots[dom]
        return True if rp is None else (rp.can_fetch(self.ua, url) and rp.can_fetch("*", url))

    # ---------- request lifecycle ----------
    def before(self, url: str, sleep: bool = True):
        dom = domain_of(url)
        left = self.cooldown_left(dom)
        if left > 0:
            raise DomainBlocked(f"{dom} cooling down for {left / 3600:.1f}h more")
        d = self.db.domain(dom)
        if d["requests_today"] >= self.cap(dom):
            raise DomainBlocked(f"{dom} reached its daily cap of {self.cap(dom)} requests")
        if sleep:
            wait = self.next_allowed_in(dom)
            self._count[dom] = self._count.get(dom, 0) + 1
            every = int(self._site(dom, "long_pause_every", self.long_pause_every))
            if every and self._count[dom] % every == 0:
                wait += random.uniform(*self._site(dom, "long_pause_seconds", self.long_pause))
            if wait > 0:
                log.debug("waiting %.0fs before %s", wait, dom)
                time.sleep(wait)
        self.db.update_domain(dom, last_request=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              requests_today=d["requests_today"] + 1)

    def after_ok(self, domain: str):
        d = self.db.domain(domain)
        if d["strikes"] and d["cooldown_until"]:
            # a successful request after a cooldown: forgive one strike
            self.db.update_domain(domain, strikes=max(0, d["strikes"] - 1), cooldown_until=None)

    def after_block(self, domain: str, reason: str):
        d = self.db.domain(domain)
        strikes = (d["strikes"] or 0) + 1
        hours = self.cooldowns[min(strikes - 1, len(self.cooldowns) - 1)]
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        self.db.update_domain(domain, strikes=strikes, cooldown_until=until.isoformat(timespec="seconds"),
                              last_block_reason=reason[:200])
        self.db.event(domain, "block", f"strike {strikes}: {reason} -> pause {hours:g}h")
        log.warning("BLOCK SIGNAL on %s (%s) -> no traffic to this site for %gh (strike %d)",
                    domain, reason, hours, strikes)
