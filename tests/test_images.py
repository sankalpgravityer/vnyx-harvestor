"""Product photos: only this product's, never the tiles of other products on the page (22 Nov 2026)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hv import product_scraper as ps  # noqa: E402

PAGE = "https://www.sellpy.nl/item/lzw0wO2Cny"


def img(src, top, link=None, w=600):
    return {"src": src, "top": top, "w": w, "rw": w, "link": link}


def test_gallery_images_are_kept_and_other_products_tiles_are_not():
    page = {"url": PAGE, "images": [
        img("https://prod.images.sellpy.net/fit-in/768x4096/a/lzw0wO2Cny-e451-0.jpg", 113),
        img("https://prod.images.sellpy.net/fit-in/768x4096/a/lzw0wO2Cny-e451-180.jpg", 113),
        img("https://prod.images.sellpy.net/x/zoom.jpg", 113, link="https://prod.images.sellpy.net/x/zoom.jpg"),
        img("https://prod.images.sellpy.net/fit-in/400x4096/b/2rkjzRRI1q-1.jpg", 1650, link="https://www.sellpy.nl/item/2rkjzRRI1q"),
        img("https://prod.images.sellpy.net/fit-in/400x4096/c/pIyysFweN4-1.jpg", 2422, link="https://www.sellpy.nl/item/pIyysFweN4"),
        img("https://prod.images.sellpy.net/self.jpg", 300, link=PAGE + "/"),
    ]}
    got = ps.pick_dom_images(page)
    assert got == ["https://prod.images.sellpy.net/fit-in/768x4096/a/lzw0wO2Cny-e451-0.jpg",
                   "https://prod.images.sellpy.net/fit-in/768x4096/a/lzw0wO2Cny-e451-180.jpg",
                   "https://prod.images.sellpy.net/x/zoom.jpg", "https://prod.images.sellpy.net/self.jpg"]


def test_the_main_json_ld_product_is_this_page_s_and_recommendations_are_not_its_variants():
    rec1 = {"@type": "Product", "name": "Wool coat", "url": "https://www.sellpy.nl/item/OTHER1", "image": "o1.jpg"}
    main = {"@type": "Product", "name": "Strellson, Jas, Maat: L", "url": PAGE, "image": "main.jpg"}
    rec2 = {"@type": "Product", "name": "Shirt", "url": "https://www.sellpy.nl/item/OTHER2", "image": "o2.jpg"}
    assert ps.pick_main_product([rec1, main, rec2], PAGE) is main
    assert ps.pick_main_product([rec1, {"@type": "Product", "name": "Strellson, Jas, Maat: L"}],
                                "https://x/y", "Strellson, Jas, Maat: L")["name"] == "Strellson, Jas, Maat: L"
    assert not ps.same_product(main, rec1)
    v1 = {"@type": "Product", "name": "Pants S", "inProductGroupWithID": "PG1"}
    grp = {"@type": "ProductGroup", "name": "Pants", "productGroupID": "PG1", "hasVariant": [v1]}
    assert ps.same_product(grp, v1)


def test_the_same_photo_on_two_hosts_or_sizes_is_kept_once():
    got = ps.dedupe_images([
        "https://sellpy-parse-prod-files.s3.amazonaws.com/photoRobot-standard-7-l-1/lzw0wO2Cny-e451-0.jpg",
        "https://prod.images.sellpy.net/fit-in/1024x4096/photoRobot-standard-7-l-1/lzw0wO2Cny-e451-0.jpg",
        "https://prod.images.sellpy.net/fit-in/1024x4096/phone-images/lzw0wO2Cny-311f.jpg"])
    assert len(got) == 2
