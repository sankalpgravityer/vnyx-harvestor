# Testing the harvester locally on a Mac

About 10 minutes from unzip to your first products JSON. Works on Apple Silicon (M1–M4) and Intel Macs.

## 1. What you need

| | How to get it |
|---|---|
| **Google Chrome** | <https://www.google.com/chrome/> (or `brew install --cask google-chrome`) |
| **Python 3.10+** | `python3 --version`. If it's older, install [Homebrew](https://brew.sh) then `brew install python@3.12` |

You don't need Xvfb or systemd. Those are only for the Ubuntu server.

## 2. Install (one time)

```bash
cd ~/Downloads/harvester          # wherever you unzipped it
bash setup_mac.sh
```

The script checks for Chrome and Python, creates a `.venv`, installs the Python packages and copies
`config.local.example.yaml` → `config.local.yaml`.

<details><summary>Manual install instead of the script</summary>

```bash
cd ~/Downloads/harvester
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.local.example.yaml config.local.yaml
```
</details>

## 3. Run a test

Open a terminal in the folder and activate the environment (do this in every new terminal):
```bash
source .venv/bin/activate
```

**a) Pre-flight check:** is each site reachable from your Mac?
```bash
python harvester.py check -c config.local.yaml
python harvester.py check -c config.local.yaml --browser     # also opens each site in the stealth Chrome
```

**b) Scrape 2 products per site**
```bash
python harvester.py run -c config.local.yaml --max 2
```
A Chrome window will open and navigate by itself. **Don't click in it or close it.** It closes on its own.
Expect ~1–3 minutes per site because of the polite delays.

One site only:
```bash
python harvester.py run -c config.local.yaml --job vinted-jackets --max 3
```

**c) Look at the result**
```bash
open data-local/exports/$(date +%F)/products.json              # opens in your default app
python -m json.tool data-local/exports/$(date +%F)/products.json | less
python harvester.py status -c config.local.yaml                # counts, requests per site, cooldowns
```

The JSON is rewritten after every product, so you can open it while a run is still going.

## 4. What the JSON looks like

Images are **not downloaded**. Each product only stores the image URLs from the site's own CDN
(full-size versions where the site allows it):

```json
[
  {
    "title": "LEVI'S Slim Men Blue Jeans",
    "brand": "LEVI'S",
    "price": 806.0,
    "currency": "INR",
    "color": "Mid Indigo",
    "condition": "NewCondition",
    "category": "jean",
    "availability": "InStock",
    "description": "…",
    "image_urls": [
      "https://rukmini1.flixcart.com/image/1500/1500/xif0q/jean/p/g/z/-original-imahpw5byemphngz.jpeg?q=70",
      "https://rukmini1.flixcart.com/image/1500/1500/xif0q/jean/9/b/d/-original-imahf7auexxmg2yb.jpeg?q=70"
    ],
    "image_count": 2,
    "rating": 3.9,
    "item_id": "JEAHDPF2TEKFRXV2",
    "site": "flipkart",
    "url": "https://www.flipkart.com/levi-s-slim-men-blue-jeans/p/itm37bfc51389502?pid=JEAHDPF2TEKFRXV2",
    "job": "flipkart-jeans",
    "source": "http",
    "scraped_at": "2026-11-22T10:38:29+00:00"
  }
]
```
Fields that a site doesn't provide are left out. Site-specific extras (Vinted size/condition, Shopify variants
and stock, item specifics…) are in `attributes`.

What's on disk in `data-local/`:
```
data-local/
├── exports/<date>/products.json   ← the output
├── exports/<date>/products.csv    ← same data for Excel (image_urls joined with " | ")
├── harvester.db                   ← internal state: queue, de-duplication, per-site cooldowns
├── logs/harvester.log
└── profiles/                      ← one Chrome profile per site (cookies, so you look like a returning visitor)
```

## 5. Testing your own sites

Add a job to `config.local.yaml`:
```yaml
  - name: myntra-shirts
    site: https://www.myntra.com
    category: shirts
    per_day: 10
```
Then `python harvester.py run -c config.local.yaml --job myntra-shirts --max 2`.
If it says "no products found on search page", add the site's search URL:
`search_url: "https://www.example.com/search?q={query}&page={page}"`.

## 6. Good to know

- **Your Mac's IP is a home (residential) IP.** Sites like eBay and Amazon that block *server* IPs usually work
  from home, so a site working on your Mac does **not** mean it will work from the Ubuntu server. Always run
  `python harvester.py check --browser` on the server too.
- Keep local runs small (`--max 2`…`5`). The local config uses shorter delays (6–12 s) to save time.
- The circuit breaker is active locally too. If a site shows a bot check, it pauses that site for 6 h
  (`status` shows it). To reset during testing: `rm -rf data-local` (this also clears de-duplication).
- `browser.mode: headless` in `config.local.yaml` hides the Chrome window, but it's more likely to get bot checks.
- Run again the same day and you'll get *new* products: already-scraped URLs are skipped.

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| `command not found: python` | Run `source .venv/bin/activate` first (or use `.venv/bin/python`). |
| "chromedriver cannot be opened because the developer cannot be verified" | `xattr -dr com.apple.quarantine .venv` then run again. |
| Chrome opens and closes immediately / version mismatch | Update Chrome (Chrome menu → About Google Chrome), then `rm -rf .venv && bash setup_mac.sh`. |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Run `/Applications/Python\ 3.12/Install\ Certificates.command` (python.org installs only). |
| A job is "skipped: cooling down" | That site showed a bot check. Wait, or `rm -rf data-local` to reset while testing. |
| "another run is still active" | Another terminal is already running the harvester. Wait for it to finish (the lock is released automatically, even after a crash). |

## 8. Moving to the server

Same code, different config. Copy the folder to the server and follow the README's **Deploy** section
(`bash install.sh` → `config.yaml` → `harvester.py check --browser` → enable the timer).
