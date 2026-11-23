"""Per-site settings (22 Nov 2026): product links from page data, a site's own proxy, a site's own pace."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hv import category_scraper as cs  # noqa: E402
from hv.net import pick_proxy  # noqa: E402
from hv.guard import Guard  # noqa: E402


def test_product_links_are_read_from_the_page_data_when_tiles_are_not_links():
    html = ('<a href="/sell-clothes-online.shtml">sell</a><script id="__NEXT_DATA__">{"items":['
            '{"link":"https:\\/\\/www.vestiairecollective.com\\/men-clothing\\/jackets\\/moncler\\/black-moncler-jacket-73962299.shtml"},'
            '{"link":"\\u002Fwomen-clothing\\u002Fjackets\\u002Fgucci\\u002Fbrown-wool-gucci-jacket-72852709.shtml"},'
            '{"link":"https://other.example/x-12345678.shtml"}]}</script>')
    _, cfg = cs.find_config("us.vestiairecollective.com")
    cards = cs.embedded_cards(html, "https://us.vestiairecollective.com/search/?q=jacket", cfg["link_pattern"])
    assert [c["url"] for c in cards] == [
        "https://www.vestiairecollective.com/men-clothing/jackets/moncler/black-moncler-jacket-73962299.shtml",
        "https://us.vestiairecollective.com/women-clothing/jackets/gucci/brown-wool-gucci-jacket-72852709.shtml"]
    assert cards[0]["title"] == "Black moncler jacket"


def test_a_site_uses_its_own_proxy_others_go_direct_or_shared():
    per_site = {"etsy.com": "http://user:pw@resi.example:7000"}
    assert pick_proxy([], "etsy.com", per_site) == "http://user:pw@resi.example:7000"
    assert pick_proxy([], "www2.etsy.com", per_site) == "http://user:pw@resi.example:7000"
    assert pick_proxy([], "vinted.nl", per_site) is None                       # the server's own IP
    assert pick_proxy(["http://shared:1"], "vinted.nl", per_site) == "http://shared:1"


class _DB:
    def __init__(self):
        self.rows = {}

    def domain(self, d):
        return self.rows.setdefault(d, {"domain": d, "cooldown_until": None, "strikes": 0, "last_request": None,
                                        "day": "x", "requests_today": 0})

    def update_domain(self, d, **kw):
        self.domain(d).update(kw)

    def event(self, *a):
        pass


def test_a_site_gets_its_own_pace_and_daily_cap():
    cfg = {"politeness": {"min_delay": 12, "max_delay": 35, "max_requests_per_domain_per_day": 150},
           "site_politeness": {"grailed.com": {"min_delay": 40, "max_delay": 90, "max_requests_per_domain_per_day": 25}}}
    g = Guard(_DB(), cfg)
    assert g.cap("grailed.com") == 25 and g.cap("vinted.nl") == 150
    assert g._site("grailed.com", "min_delay", 12) == 40 and g._site("vinted.nl", "min_delay", 12) == 12
    g.db.update_domain("grailed.com", requests_today=25)
    assert not g.usable("grailed.com")


def test_jobs_proxy_and_politeness_become_site_settings(tmp_path):
    import harvester as H
    p = tmp_path / "c.yaml"
    p.write_text(f"""storage: {{dir: {tmp_path}/data}}
jobs:
  - {{name: etsy, site: https://www.etsy.com, category: x, proxy: "http://resi:1"}}
  - {{name: grailed, site: https://www.grailed.com, category: x, politeness: {{min_delay: 40, max_delay: 90}}}}
""")
    cfg = H.load_config(str(p))
    assert cfg["site_proxies"] == {"etsy.com": "http://resi:1"}
    assert cfg["site_politeness"] == {"grailed.com": {"min_delay": 40, "max_delay": 90}}


def test_browser_caches_are_deleted_and_cookies_kept(tmp_path):
    from hv.net import purge_profile_cache
    prof = tmp_path / "vinted.nl"
    for p, size in [("Default/Cache/Cache_Data/f_000001", 4000), ("Default/Code Cache/js/abc", 3000),
                    ("Default/Service Worker/CacheStorage/x/y", 2000), ("Default/Cookies", 100),
                    ("Default/Local Storage/leveldb/000003.log", 50), ("GrShaderCache/data_0", 1000)]:
        f = prof / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * size)
    assert purge_profile_cache(str(prof)) == 10000
    assert (prof / "Default/Cookies").exists() and (prof / "Default/Local Storage/leveldb/000003.log").exists()
    assert not (prof / "Default/Cache").exists() and not (prof / "GrShaderCache").exists()
    assert purge_profile_cache(str(tmp_path / "missing")) == 0
