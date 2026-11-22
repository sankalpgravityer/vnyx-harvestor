"""SQLite storage: discovered-URL queue, scraped products, per-domain state, event log."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    url TEXT PRIMARY KEY, job TEXT, site TEXT, listing JSON,
    status TEXT DEFAULT 'pending',            -- pending | done | failed | skipped
    attempts INTEGER DEFAULT 0, discovered_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS queue_job ON queue(job, status);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, job TEXT, site TEXT, source TEXT,
    item_id TEXT, title TEXT, brand TEXT, price REAL, currency TEXT, condition TEXT, size TEXT, color TEXT,
    category TEXT, description TEXT, images JSON, image_files JSON, data JSON, day TEXT, scraped_at TEXT
);
CREATE INDEX IF NOT EXISTS products_day ON products(day, job);
CREATE TABLE IF NOT EXISTS domains (
    domain TEXT PRIMARY KEY, cooldown_until TEXT, strikes INTEGER DEFAULT 0,
    last_request TEXT, day TEXT, requests_today INTEGER DEFAULT 0, last_block_reason TEXT,
    tier TEXT
);
CREATE TABLE IF NOT EXISTS events (ts TEXT, domain TEXT, kind TEXT, detail TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


class DB:
    def __init__(self, path: str):
        self.c = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)
        try:
            self.c.execute("ALTER TABLE domains ADD COLUMN tier TEXT")
        except sqlite3.OperationalError:
            pass
        self.lock = threading.Lock()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur

    # ---------- queue ----------
    def enqueue(self, url, job, site, listing=None) -> bool:
        if self.x("SELECT 1 FROM products WHERE url=?", (url,)).fetchone():
            return False
        cur = self.x("INSERT OR IGNORE INTO queue(url, job, site, listing, discovered_at, updated_at) "
                     "VALUES (?,?,?,?,?,?)", (url, job, site, json.dumps(listing or {}, default=str), now(), now()))
        return cur.rowcount > 0

    def pending(self, job, limit):
        return [dict(r) for r in self.x("SELECT * FROM queue WHERE job=? AND status='pending' AND attempts < 3 "
                                        "ORDER BY discovered_at LIMIT ?", (job, limit))]

    def mark(self, url, status):
        self.x("UPDATE queue SET status=?, attempts=attempts+1, updated_at=? WHERE url=?", (status, now(), url))

    # ---------- products ----------
    def save_product(self, rec: dict, job: str, source: str):
        imgs = rec.get("images") or []
        self.x("""INSERT OR REPLACE INTO products(url, job, site, source, item_id, title, brand, price, currency,
                  condition, size, color, category, description, images, image_files, data, day, scraped_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (rec["url"], job, rec.get("site"), source, str(rec.get("id") or rec.get("item_id") or ""),
                rec.get("title"), rec.get("brand"), _num(rec.get("price")), rec.get("currency"),
                rec.get("condition"), _s(rec.get("size")), _s(rec.get("color")), _s(rec.get("category")),
                rec.get("description"), json.dumps(imgs), json.dumps(rec.get("image_files") or []),
                json.dumps(rec, ensure_ascii=False, default=str), today(), now()))
        self.x("UPDATE queue SET status='done', updated_at=? WHERE url=?", (now(), rec["url"]))

    def count_today(self, job=None) -> int:
        if job:
            return self.x("SELECT COUNT(*) FROM products WHERE day=? AND job=?", (today(), job)).fetchone()[0]
        return self.x("SELECT COUNT(*) FROM products WHERE day=?", (today(),)).fetchone()[0]

    def products(self, day=None):
        q, a = ("SELECT * FROM products WHERE day=? ORDER BY id", (day,)) if day else ("SELECT * FROM products ORDER BY id", ())
        return [dict(r) for r in self.x(q, a)]

    # ---------- domains / events ----------
    def domain(self, d) -> dict:
        r = self.x("SELECT * FROM domains WHERE domain=?", (d,)).fetchone()
        if not r:
            self.x("INSERT INTO domains(domain, day) VALUES (?,?)", (d, today()))
            r = self.x("SELECT * FROM domains WHERE domain=?", (d,)).fetchone()
        r = dict(r)
        if r["day"] != today():
            self.x("UPDATE domains SET day=?, requests_today=0 WHERE domain=?", (today(), d))
            r["day"], r["requests_today"] = today(), 0
        return r

    def update_domain(self, d, **kw):
        self.domain(d)
        sets = ", ".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE domains SET {sets} WHERE domain=?", (*kw.values(), d))

    def event(self, domain, kind, detail=""):
        self.x("INSERT INTO events VALUES (?,?,?,?)", (now(), domain, kind, str(detail)[:500]))


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _s(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v
