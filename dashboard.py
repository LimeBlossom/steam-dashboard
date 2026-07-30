#!/usr/bin/env python3
"""
Steam Dashboard - Real-time sales monitoring for Steam games
https://github.com/LimeBlossom/steam-dashboard

Zero external dependencies (stdlib only).
Settings stored in SQLite. Web-based setup wizard on first run.
Supports multiple games.
"""

import json
import time
import threading
import sqlite3
import os
import sys
import csv
import io
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs, quote
from datetime import datetime, timedelta
from html.parser import HTMLParser

VERSION = "1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, 'steam_dashboard.db')
FINANCIAL_BASE = "https://partner.steam-api.com"
STUDIO_APP_ID = '__studio__'
FOLLOWER_FETCH_INTERVAL = 1800  # seconds; followers move a few times a week
FOLLOWER_RETRY_INTERVAL = 300  # seconds; back off a failing page without pinning the poll loop

# ========== DATABASE ==========

def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS player_history (
        app_id TEXT, timestamp TEXT, player_count INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS review_history (
        app_id TEXT, timestamp TEXT, total_positive INTEGER, total_negative INTEGER, total_reviews INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sales_snapshots (
        app_id TEXT, timestamp TEXT, total_units INTEGER, total_returns INTEGER,
        total_net_usd REAL, PRIMARY KEY (app_id, timestamp)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS wishlist_history (
        app_id TEXT, timestamp TEXT, total_adds INTEGER, total_deletes INTEGER,
        total_purchases INTEGER, net_wishlists INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sales_by_country_daily (
        app_id TEXT, date TEXT, country_code TEXT,
        units INTEGER, returns INTEGER, gross_usd REAL, net_usd REAL,
        PRIMARY KEY (app_id, date, country_code)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS wishlists_by_country_daily (
        app_id TEXT, date TEXT, country_code TEXT,
        adds INTEGER, deletes INTEGER, purchases INTEGER,
        fetch_attempts INTEGER DEFAULT 0,
        PRIMARY KEY (app_id, date, country_code)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS follower_history (
        app_id TEXT, date TEXT, follower_count INTEGER,
        PRIMARY KEY (app_id, date)
    )''')
    conn.commit()
    conn.close()


# --- Settings helpers ---

def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]
    return default


def set_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))
    conn.commit()
    conn.close()


def has_settings():
    return get_setting('steam_api_key') is not None


def get_all_settings():
    return {
        'steam_api_key': get_setting('steam_api_key', ''),
        'steam_financial_key': get_setting('steam_financial_key', ''),
        'games': get_setting('games', []),
        'studio': get_setting('studio', {'name': '', 'url': ''}),
        'telegram': get_setting('telegram', {'enabled': False, 'bot_token': '', 'chat_ids': []}),
        'dashboard': get_setting('dashboard', {'port': 8081, 'poll_interval': 300, 'language': 'en', 'theme': 'dark', 'accent': 'steam'}),
    }


def save_all_settings(data):
    set_setting('steam_api_key', data.get('steam_api_key', ''))
    set_setting('steam_financial_key', data.get('steam_financial_key', ''))
    set_setting('games', data.get('games', []))
    set_setting('studio', data.get('studio', {'name': '', 'url': ''}))
    set_setting('telegram', data.get('telegram', {'enabled': False, 'bot_token': '', 'chat_ids': []}))
    set_setting('dashboard', data.get('dashboard', {'port': 8081, 'poll_interval': 300, 'language': 'en', 'theme': 'dark', 'accent': 'steam'}))


# --- Per-game data helpers ---

def save_player_count(app_id, count):
    conn = get_conn()
    conn.execute("INSERT INTO player_history VALUES (?, ?, ?)", (str(app_id), datetime.now().isoformat(), count))
    conn.commit()
    conn.close()


def save_review_data(app_id, pos, neg, total):
    conn = get_conn()
    conn.execute("INSERT INTO review_history VALUES (?, ?, ?, ?, ?)", (str(app_id), datetime.now().isoformat(), pos, neg, total))
    conn.commit()
    conn.close()


def get_player_history(app_id, limit=144):
    conn = get_conn()
    rows = conn.execute("SELECT timestamp, player_count FROM player_history WHERE app_id=? ORDER BY timestamp DESC LIMIT ?", (str(app_id), limit)).fetchall()
    conn.close()
    return list(reversed(rows))


def get_all_daily_sales(app_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, units, returns, gross_usd, net_usd FROM sales_by_country_daily "
        "WHERE app_id=? AND country_code='__all__' ORDER BY date",
        (str(app_id),)
    ).fetchall()
    conn.close()
    return rows


def save_sales_snapshot(app_id, total_units, total_returns, total_net):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO sales_snapshots VALUES (?, ?, ?, ?, ?)",
                 (str(app_id), datetime.now().isoformat(), total_units, total_returns, total_net))
    conn.commit()
    conn.close()


def get_sales_snapshots(app_id):
    conn = get_conn()
    rows = conn.execute("SELECT timestamp, total_units, total_returns, total_net_usd FROM sales_snapshots WHERE app_id=? ORDER BY timestamp", (str(app_id),)).fetchall()
    conn.close()
    if not rows:
        return []
    result = []
    last_ts = None
    for row in rows:
        ts = datetime.fromisoformat(row[0])
        if last_ts is None or (ts - last_ts).total_seconds() >= 12 * 3600:
            result.append(row)
            last_ts = ts
    if rows[-1] not in result:
        result.append(rows[-1])
    return result


def save_wishlist_snapshot(app_id, adds, deletes, purchases, net):
    conn = get_conn()
    conn.execute("INSERT INTO wishlist_history VALUES (?, ?, ?, ?, ?, ?)",
                 (str(app_id), datetime.now().isoformat(), adds, deletes, purchases, net))
    conn.commit()
    conn.close()


def get_wishlist_history(app_id):
    conn = get_conn()
    rows = conn.execute("SELECT timestamp, net_wishlists FROM wishlist_history WHERE app_id=? ORDER BY timestamp DESC LIMIT 144", (str(app_id),)).fetchall()
    conn.close()
    return list(reversed(rows))


def get_sales_totals(app_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(units),0), COALESCE(SUM(returns),0), "
        "COALESCE(SUM(gross_usd),0), COALESCE(SUM(net_usd),0) "
        "FROM sales_by_country_daily WHERE app_id=? AND country_code='__all__'",
        (str(app_id),)
    ).fetchone()
    conn.close()
    return row


def get_all_games_sales_totals():
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(units),0), COALESCE(SUM(returns),0), "
        "COALESCE(SUM(gross_usd),0), COALESCE(SUM(net_usd),0) "
        "FROM sales_by_country_daily WHERE country_code='__all__'"
    ).fetchone()
    conn.close()
    return row



def load_sales_by_country(app_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT country_code, SUM(units), SUM(returns), SUM(net_usd) "
        "FROM sales_by_country_daily WHERE app_id=? AND country_code != '__all__' "
        "GROUP BY country_code ORDER BY SUM(units) DESC",
        (str(app_id),)
    ).fetchall()
    conn.close()
    return {r[0]: {"units": r[1], "returns": r[2], "net": r[3]} for r in rows}


def load_wishlists_by_country(app_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT country_code, SUM(adds), SUM(deletes), SUM(purchases) "
        "FROM wishlists_by_country_daily WHERE app_id=? AND country_code != '__all__' "
        "GROUP BY country_code ORDER BY SUM(adds) DESC",
        (str(app_id),)
    ).fetchall()
    conn.close()
    return {r[0]: {"adds": r[1], "deletes": r[2], "purchases": r[3]} for r in rows}


def load_wishlist_totals(app_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(adds),0), COALESCE(SUM(deletes),0), "
        "COALESCE(SUM(purchases),0) "
        "FROM wishlists_by_country_daily WHERE app_id=? AND country_code='__all__'",
        (str(app_id),)
    ).fetchone()
    conn.close()
    if row:
        total = {"adds": row[0], "deletes": row[1], "purchases": row[2], "gifts": 0}
        total["net"] = total["adds"] - total["deletes"] - total["purchases"]
        return total
    return {"adds": 0, "deletes": 0, "purchases": 0, "gifts": 0, "net": 0}


def get_daily_wishlists(app_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, adds, deletes, purchases FROM wishlists_by_country_daily "
        "WHERE app_id=? AND country_code='__all__' ORDER BY date",
        (str(app_id),)
    ).fetchall()
    conn.close()
    return rows


def save_follower_count(app_id, count):
    """Record today's follower count, overwriting any earlier reading for today."""
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO follower_history VALUES (?, ?, ?)",
                 (str(app_id), datetime.now().strftime("%Y-%m-%d"), count))
    conn.commit()
    conn.close()


def get_follower_history(app_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, follower_count FROM follower_history WHERE app_id=? ORDER BY date",
        (str(app_id),)
    ).fetchall()
    conn.close()
    return rows


def get_latest_follower_count(app_id):
    """Most recent follower reading, or None when no reading has ever succeeded.

    None must not be conflated with a genuine 0: a page that never resolves
    (unreachable members page, parse failure) has no row at all, and the UI
    needs to render that differently from a real zero-follower count.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT follower_count FROM follower_history WHERE app_id=? ORDER BY date DESC LIMIT 1",
        (str(app_id),)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def parse_follower_csv(text):
    """Parse a follower history CSV into [(date, count)], newest last.

    Accepts the SteamDB chart export (`"DateTime","Followers"`, where DateTime
    carries a time component) and a plain `date,count` file for hand-entered
    points. Returns (rows, rejected) so the caller can report bad lines instead
    of silently dropping them: a row we cannot read is worth telling the user
    about, since the whole point of importing is to fill gaps accurately.
    """
    rows, rejected = [], []
    # A leading BOM stops csv recognising the opening quote, so the first header
    # arrives as '﻿"DateTime"' and lookup fails. The CLI reads with
    # utf-8-sig, but strip it here too so the function is correct for any caller.
    text = text.lstrip('﻿')
    reader = csv.DictReader(io.StringIO(text))
    fields = [f.strip().lower() for f in (reader.fieldnames or [])]
    date_key = next((f for f in ('datetime', 'date') if f in fields), None)
    count_key = next((f for f in ('followers', 'count') if f in fields), None)
    if not date_key or not count_key:
        raise ValueError(
            f"need a date column (DateTime or date) and a count column "
            f"(Followers or count); found {reader.fieldnames}")

    seen = set()
    for n, raw in enumerate(reader, start=2):
        lowered = {(k or '').strip().lower(): v for k, v in raw.items()}
        d, c = (lowered.get(date_key) or '').strip(), (lowered.get(count_key) or '').strip()
        try:
            day = datetime.strptime(d.split(' ')[0], "%Y-%m-%d").date().isoformat()
            count = int(c)
            if count < 0:
                raise ValueError("negative count")
        except (ValueError, AttributeError) as e:
            rejected.append((n, f"{d!r},{c!r}", str(e)))
            continue
        if day in seen:
            rejected.append((n, f"{d!r},{c!r}", "duplicate date"))
            continue
        seen.add(day)
        rows.append((day, count))

    rows.sort()
    return rows, rejected


def import_follower_history(app_id, rows):
    """Insert historical rows, never overwriting an existing one.

    Uses INSERT OR IGNORE rather than OR REPLACE so a scraped reading always
    wins over an imported one. Imported points come from a third party and are
    less trustworthy than a value this dashboard read itself.

    Returns (inserted, skipped).
    """
    conn = get_conn()
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO follower_history VALUES (?, ?, ?)",
        [(str(app_id), day, count) for day, count in rows]
    )
    conn.commit()
    inserted = conn.total_changes - before
    conn.close()
    return inserted, len(rows) - inserted


def app_id_from_csv_name(path):
    """'steamdb_chart_2587260.csv' -> '2587260', else None.

    SteamDB names its export after the app, so the id need not be retyped.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    digits = stem.rsplit('_', 1)[-1]
    return digits if digits.isdigit() else None


def record_follower_count(app_id, count):
    """Persist a follower reading. Returns True when a row was written.

    A None count means the fetch failed, and nothing is written. Recording a
    failure as 0 would look like every follower unfollowing at once, and the
    damage would be permanent because past days cannot be refetched. Note that
    a genuine 0 IS written; only None is treated as absence of data.
    """
    if count is None:
        return False
    save_follower_count(app_id, count)
    return True


class RateLimiter:
    def __init__(self, max_per_second=20):
        self._lock = threading.Lock()
        self._min_interval = 1.0 / max_per_second
        self._last_time = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_time
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_time = time.monotonic()

    def slow_down(self, new_max=10):
        with self._lock:
            self._min_interval = 1.0 / new_max
            print(f"  [RATE LIMIT] Slowed to {new_max} req/s")


_rate_limiter = RateLimiter(max_per_second=20)


# ========== HTTP FETCH WITH BACKOFF ==========

_api_fail_counts = {}


def fetch_json(url, label="api"):
    global _api_fail_counts
    _rate_limiter.wait()
    try:
        req = Request(url, headers={"User-Agent": "SteamDashboard/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        _api_fail_counts[label] = 0
        return data
    except HTTPError as e:
        if e.code == 429:
            print(f"  [THROTTLED] {label}: HTTP 429 - rate limited by Steam")
            return "throttled"
        count = _api_fail_counts.get(label, 0) + 1
        _api_fail_counts[label] = count
        wait = min(2 ** count, 60)
        print(f"  [ERROR] {label}: {e} (backoff {wait}s)")
        time.sleep(wait)
        return None
    except Exception as e:
        count = _api_fail_counts.get(label, 0) + 1
        _api_fail_counts[label] = count
        wait = min(2 ** count, 60)
        print(f"  [ERROR] {label}: {e} (backoff {wait}s)")
        time.sleep(wait)
        return None


def fetch_html(url, label="html"):
    # Uses a browser User-Agent unlike fetch_json — required to bypass Steam's bot detection on community pages
    global _api_fail_counts
    _rate_limiter.wait()
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SteamDashboard/1.0)"})
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
        _api_fail_counts[label] = 0
        return html
    except HTTPError as e:
        if e.code == 429:
            print(f"  [THROTTLED] {label}: HTTP 429 - rate limited by Steam")
            return None
        count = _api_fail_counts.get(label, 0) + 1
        _api_fail_counts[label] = count
        wait = min(2 ** count, 60)
        print(f"  [ERROR] {label}: {e} (backoff {wait}s)")
        time.sleep(wait)
        return None
    except Exception as e:
        count = _api_fail_counts.get(label, 0) + 1
        _api_fail_counts[label] = count
        wait = min(2 ** count, 60)
        print(f"  [ERROR] {label}: {e} (backoff {wait}s)")
        time.sleep(wait)
        return None


# ========== STEAM API ==========

def get_current_players(api_key, app_id):
    data = fetch_json(
        f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={app_id}&key={api_key}",
        f"players_{app_id}"
    )
    if data and "response" in data:
        return data["response"].get("player_count", 0)
    return 0


def get_app_details(app_id):
    data = fetch_json(f"https://store.steampowered.com/api/appdetails?appids={app_id}", f"details_{app_id}")
    if data and str(app_id) in data and data[str(app_id)].get("success"):
        return data[str(app_id)]["data"]
    return None


def get_game_info_from_api(app_id):
    """Fetch name, launch date, and coming_soon flag from Steam store API."""
    details = get_app_details(app_id)
    if not details:
        return f"App {app_id}", None, False
    name = details.get("name", f"App {app_id}")
    launch_date = None
    rd = details.get("release_date", {})
    coming_soon = bool(rd.get("coming_soon"))
    if not coming_soon and rd.get("date"):
        try:
            launch_date = datetime.strptime(rd["date"], "%b %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            try:
                launch_date = datetime.strptime(rd["date"], "%d %b, %Y").strftime("%Y-%m-%d")
            except ValueError:
                pass
    return name, launch_date, coming_soon


def get_reviews(app_id):
    data = fetch_json(
        f"https://store.steampowered.com/appreviews/{app_id}?json=1&language=all&purchase_type=all&num_per_page=0",
        f"reviews_{app_id}"
    )
    if data and data.get("success") == 1:
        return data.get("query_summary", {})
    return {}


def get_recent_reviews(app_id):
    data = fetch_json(
        f"https://store.steampowered.com/appreviews/{app_id}?json=1&language=all&purchase_type=all&num_per_page=5&filter=recent",
        f"recent_reviews_{app_id}"
    )
    if data and data.get("success") == 1:
        return data.get("reviews", [])
    return []


class _DiscussionListParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.topics = []
        self._t = None
        self._cap = None
        self._div_depth = 0
        self._cap_div_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = set(a.get('class', '').split())
        if tag == 'div':
            self._div_depth += 1
        if tag == 'div' and 'forum_topic' in cls and 'data-gidforumtopic' in a:
            self._t = {'id': a['data-gidforumtopic'], 'title': '', 'author': '',
                       'reply_count': 0, 'last_post_time': 0, 'url': ''}
            self.topics.append(self._t)
        if not self._t:
            return
        if tag == 'a' and 'forum_topic_overlay' in cls:
            self._t['url'] = a.get('href', '')
        elif tag == 'div' and self._cap is None:
            if 'forum_topic_name' in cls:
                self._cap = 'title'
                self._cap_div_depth = self._div_depth
            elif 'forum_topic_op' in cls:
                self._cap = 'author'
                self._cap_div_depth = self._div_depth
            elif 'forum_topic_reply_count' in cls:
                self._cap = 'reply_count'
                self._cap_div_depth = self._div_depth
            elif 'forum_topic_lastpost' in cls and 'data-timestamp' in a:
                try:
                    self._t['last_post_time'] = int(a['data-timestamp'])
                except ValueError:
                    pass

    def handle_endtag(self, tag):
        if tag == 'div':
            if self._cap and self._div_depth == self._cap_div_depth:
                self._cap = None
            self._div_depth -= 1

    def handle_data(self, data):
        data = data.strip()
        if data and self._cap and self._t:
            if self._cap == 'reply_count':
                try:
                    self._t['reply_count'] = int(data.replace(',', ''))
                except ValueError:
                    pass
            elif not self._t[self._cap]:
                self._t[self._cap] = data


class _DiscussionDetailParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.op_text = ''
        self.op_time = 0
        self.latest_reply = None
        self._in_op = False
        self._op_div_depth = 0
        self._in_op_content = False
        self._content_div_depth = 0
        self._cap_reply_text = False
        self._reply_text_div_depth = 0
        self._in_bdi = False
        self._last_bdi = ''
        self._cur_reply_time = 0
        self._cur_reply_text = ''
        self._div_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = set(a.get('class', '').split())
        if tag == 'div':
            self._div_depth += 1
        if tag == 'div' and 'forum_op' in cls:
            self._in_op = True
            self._op_div_depth = self._div_depth
        if self._in_op:
            if tag == 'div' and 'content' in cls and not self._in_op_content:
                self._in_op_content = True
                self._content_div_depth = self._div_depth
            if tag == 'div' and 'commentthread_comment_timestamp' in cls and 'data-timestamp' in a:
                try:
                    self.op_time = int(a['data-timestamp'])
                except ValueError:
                    pass
        if not self._in_op:
            if tag == 'div' and 'commentthread_comment_timestamp' in cls and 'data-timestamp' in a:
                try:
                    self._cur_reply_time = int(a['data-timestamp'])
                except ValueError:
                    pass
            if tag == 'div' and 'commentthread_comment_text' in cls:
                self._cap_reply_text = True
                self._reply_text_div_depth = self._div_depth
                self._cur_reply_text = ''
        if tag == 'bdi':
            self._in_bdi = True

    def handle_endtag(self, tag):
        if tag == 'div':
            if self._in_op_content and self._div_depth == self._content_div_depth:
                self._in_op_content = False
            if self._in_op and self._div_depth == self._op_div_depth:
                self._in_op = False
                self._in_op_content = False
            if self._cap_reply_text and self._div_depth == self._reply_text_div_depth:
                self._cap_reply_text = False
                if self._cur_reply_text:
                    self.latest_reply = {
                        'author': self._last_bdi,
                        'time': self._cur_reply_time,
                        'text': self._cur_reply_text,
                    }
            self._div_depth -= 1
        if tag == 'bdi':
            self._in_bdi = False

    def handle_data(self, data):
        data = data.strip()
        if not data:
            return
        if self._in_op_content and not self.op_text:
            self.op_text = data[:300]
        if self._cap_reply_text and not self._cur_reply_text:
            self._cur_reply_text = data[:300]
        if self._in_bdi:
            self._last_bdi = data


def get_community_discussions(app_id, count=5):
    app_id = str(app_id)
    list_html = fetch_html(
        f"https://steamcommunity.com/app/{app_id}/discussions/",
        f"discussions_list_{app_id}"
    )
    if list_html is None:
        return None
    lp = _DiscussionListParser()
    lp.feed(list_html)
    topics = lp.topics[:count]
    result = []
    for t in topics:
        raw_url = t['url']
        if not raw_url:
            url = f"https://steamcommunity.com/app/{app_id}/discussions/0/{t['id']}/"
        elif not raw_url.startswith('http'):
            url = f"https://steamcommunity.com{raw_url}"
        else:
            url = raw_url
        detail_html = fetch_html(url, f"discussions_detail_{t['id']}")
        if detail_html is None:
            continue
        dp = _DiscussionDetailParser()
        dp.feed(detail_html)
        result.append({
            "id": t['id'],
            "title": t['title'],
            "url": url,
            "author": t['author'],
            "posted_at": dp.op_time or t['last_post_time'],
            "reply_count": t['reply_count'],
            "opening_snippet": dp.op_text,
            "latest_reply": {
                "author": dp.latest_reply['author'],
                "posted_at": dp.latest_reply['time'],
                "snippet": dp.latest_reply['text'],
            } if dp.latest_reply else None,
        })
    return result


# --- Followers ---
#
# Valve exposes no Web API for follower counts, so both the per-game and the
# studio numbers are scraped from public pages. Following a game is joining its
# community hub group, so the hub member count IS the follower count.

def _parse_member_count(text):
    """'1 - 31 of 44 Members' -> 44.

    Falls back to a single integer token when ' of ' is absent, but returns
    None if the text contains zero or multiple integer tokens. Ambiguous
    multi-number strings mean the page was not in the expected format, and
    returning None is safer than guessing, since a wrong value can never be
    corrected.
    """
    if ' of ' in text:
        text = text.split(' of ')[-1]
    tokens = text.replace(',', '').split()
    integers = [token for token in tokens if token.isdigit()]
    if len(integers) == 1:
        return int(integers[0])
    return None


class _GroupMemberCountParser(HTMLParser):
    """Pulls the follower count out of a community hub members page.

    Target markup, which appears twice per page:
        <div class="group_paging">
          <div class="pageLinks"> </div>
          1 - 31 of 44 Members </div>
    """

    def __init__(self):
        super().__init__()
        self._depth = 0
        self._text = []
        self.count = None

    def handle_starttag(self, tag, attrs):
        if tag != 'div':
            return
        if self._depth > 0:
            self._depth += 1
            return
        cls = dict(attrs).get('class') or ''
        if 'group_paging' in cls.split():
            self._depth = 1

    def handle_endtag(self, tag):
        if tag == 'div' and self._depth > 0:
            self._depth -= 1
            if self._depth == 0 and self.count is None:
                self.count = _parse_member_count(''.join(self._text))
                self._text = []

    def handle_data(self, data):
        if self._depth > 0 and self.count is None:
            self._text.append(data)


def get_game_followers(app_id):
    """Current follower count for a game, or None if it could not be read.

    Returns None rather than 0 on failure. A transient error recorded as a mass
    unfollow would be permanent, since past days cannot be refetched.
    """
    html = fetch_html(f"https://steamcommunity.com/games/{app_id}/members?l=english",
                      f"followers_{app_id}")
    if not html:
        return None
    parser = _GroupMemberCountParser()
    try:
        parser.feed(html)
    except Exception as e:
        print(f"  [ERROR] followers_{app_id}: parse failed ({e})")
        return None
    return parser.count


class _CuratorFollowerParser(HTMLParser):
    """Pulls the follower count out of a curator-backed store page.

    /developer/<slug>, /publisher/<slug> and /curator/<id> all render:
        <div class="num_followers" id="CuratorNumFollowers_44681599">20</div>

    Keyed on the class, not the id, because the id embeds a clan ID. The exact
    token match also avoids the sibling div.num_followers_text label.
    """

    def __init__(self):
        super().__init__()
        self._in_count = False
        self._text = []
        self.count = None

    def handle_starttag(self, tag, attrs):
        if tag == 'div' and self.count is None:
            cls = dict(attrs).get('class') or ''
            if 'num_followers' in cls.split():
                self._in_count = True

    def handle_endtag(self, tag):
        if tag == 'div' and self._in_count:
            self._in_count = False
            digits = ''.join(self._text).replace(',', '').strip()
            if digits.isdigit():
                self.count = int(digits)
            self._text = []

    def handle_data(self, data):
        if self._in_count:
            self._text.append(data)


def get_studio_followers(studio_url):
    """Current studio follower count, or None if unset or unreadable.

    Studio followers are independent of game followers, not a sum of them.
    """
    if not studio_url:
        return None
    html = fetch_html(studio_url, "followers_studio")
    if not html:
        return None
    parser = _CuratorFollowerParser()
    try:
        parser.feed(html)
    except Exception as e:
        print(f"  [ERROR] followers_studio: parse failed ({e})")
        return None
    return parser.count


# ========== FINANCIAL API ==========

def fetch_sales_for_date(financial_key, app_id, date_str):
    app_id = str(app_id)
    by_country = {}
    total_units = 0
    total_returns = 0
    total_gross = 0.0
    total_net = 0.0
    hwm = 0

    while True:
        url = (f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetDetailedSales/v001/"
               f"?key={financial_key}&date={date_str}&highwatermark_id={hwm}")
        data = fetch_json(url, f"sales_{app_id}")
        if not data or "response" not in data:
            return None
        resp = data["response"]
        for item in resp.get("results", []):
            if str(item.get("primary_appid", item.get("appid", ""))) == app_id:
                cc = item.get("country_code", "??")
                units = item.get("gross_units_sold", 0)
                returns = item.get("gross_units_returned", 0)
                gross = float(item.get("gross_sales_usd", 0))
                net = float(item.get("net_sales_usd", 0))
                if cc not in by_country:
                    by_country[cc] = {"units": 0, "returns": 0, "gross": 0.0, "net": 0.0}
                by_country[cc]["units"] += units
                by_country[cc]["returns"] += returns
                by_country[cc]["gross"] += gross
                by_country[cc]["net"] += net
                total_units += units
                total_returns += returns
                total_gross += gross
                total_net += net
        max_id = resp.get("max_id", 0)
        if max_id == hwm or max_id == 0:
            break
        hwm = max_id

    return {
        "totals": (total_units, total_returns, total_gross, total_net),
        "by_country": by_country
    }


def fetch_wishlist_for_date(financial_key, app_id, date_str):
    url = f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetAppWishlistReporting/v001/?key={financial_key}&appid={app_id}&date={date_str}"
    data = fetch_json(url, f"wishlist_{app_id}")
    if not data or "response" not in data:
        return None
    resp = data["response"]

    s = resp.get("wishlist_summary", resp.get("summary", {}))
    totals = {
        "adds": s.get("wishlist_adds", 0),
        "deletes": s.get("wishlist_deletes", 0),
        "purchases": s.get("wishlist_purchases", 0),
        "gifts": s.get("wishlist_gifts", 0)
    }

    by_country = {}
    for c in resp.get("country_summary", []):
        cc = c.get("country_code", "??")
        sa = c.get("summary_actions", {})
        by_country[cc] = {
            "adds": sa.get("wishlist_adds", 0),
            "deletes": sa.get("wishlist_deletes", 0),
            "purchases": sa.get("wishlist_purchases", 0)
        }

    return {"totals": totals, "by_country": by_country}


_earliest_wishlist_cache = {}

def find_earliest_wishlist_date(financial_key, app_id, launch_date):
    """Find the earliest date with wishlist data using app_min_date from the API."""
    if app_id in _earliest_wishlist_cache:
        return _earliest_wishlist_cache[app_id]

    # Query app_min_date using launch_date (reliably returns the field)
    data = fetch_json(
        f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetAppWishlistReporting/v001/"
        f"?key={financial_key}&appid={app_id}&date={launch_date}",
        f"wishlist_min_date_{app_id}"
    )
    if data and "response" in data:
        min_date = data["response"].get("app_min_date")
        if min_date:
            result = datetime.strptime(min_date, "%Y-%m-%d").date()
            print(f"  [{app_id}] Wishlist data available from {result}")
            _earliest_wishlist_cache[app_id] = result
            return result

    # Fallback: use launch_date
    result = datetime.strptime(launch_date, "%Y-%m-%d").date()
    _earliest_wishlist_cache[app_id] = result
    return result


def refresh_all_sales(financial_key, app_id, launch_date, on_progress=None, collector=None, game_state=None, unreleased=False):
    if unreleased:
        return
    app_id = str(app_id)
    launch = datetime.strptime(launch_date, "%Y-%m-%d").date()
    today = datetime.now().date()

    conn = get_conn()
    existing = set(r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM sales_by_country_daily WHERE app_id=?", (app_id,)
    ).fetchall())
    conn.close()

    last_collected = max(existing) if existing else None
    always_refresh = {today.strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")}
    if last_collected:
        always_refresh.add(last_collected)

    # Build list of dates to fetch
    dates_to_fetch = []
    current = today
    while current >= launch:
        ds = current.strftime("%Y-%m-%d")
        if ds in existing and ds not in always_refresh:
            current -= timedelta(days=1)
            continue
        dates_to_fetch.append(ds)
        current -= timedelta(days=1)

    if not dates_to_fetch:
        return

    skipped = 0
    throttled = False
    writes = 0

    def _write_result(ds, result):
        nonlocal skipped, writes
        if result is None:
            skipped += 1
            return
        totals = result["totals"]
        c = get_conn()
        c.execute(
            "INSERT OR REPLACE INTO sales_by_country_daily VALUES (?, ?, '__all__', ?, ?, ?, ?)",
            (app_id, ds, totals[0], totals[1], totals[2], totals[3])
        )
        for cc, d in result["by_country"].items():
            c.execute(
                "INSERT OR REPLACE INTO sales_by_country_daily VALUES (?, ?, ?, ?, ?, ?, ?)",
                (app_id, ds, cc, d["units"], d["returns"], d["gross"], d["net"])
            )
        c.commit()
        c.close()
        writes += 1
        if game_state and writes % 20 == 0:
            game_state.cached_sales_by_country = load_sales_by_country(app_id)
        if totals[0] > 0 or totals[1] > 0:
            print(f"  [{app_id}] [{ds}] +{totals[0]} sold, -{totals[1]} returned, ${totals[3]:.2f} net")

    if len(dates_to_fetch) > 5:
        # Parallel backfill
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {}
            for ds in dates_to_fetch:
                future = executor.submit(fetch_sales_for_date, financial_key, app_id, ds)
                futures[future] = ds

            for future in as_completed(futures):
                ds = futures[future]
                if on_progress:
                    on_progress(ds)
                result = future.result()
                if result == "throttled":
                    throttled = True
                    if collector:
                        collector.throttled = True
                    _rate_limiter.slow_down(10)
                    time.sleep(30)
                    skipped += 1
                    continue
                _write_result(ds, result)
    else:
        # Sequential for small batches
        for ds in dates_to_fetch:
            if on_progress:
                on_progress(ds)
            result = fetch_sales_for_date(financial_key, app_id, ds)
            if result == "throttled":
                throttled = True
                if collector:
                    collector.throttled = True
                _rate_limiter.slow_down(10)
                time.sleep(30)
                skipped += 1
                continue
            _write_result(ds, result)

    if skipped:
        print(f"  [{app_id}] WARNING: {skipped} day(s) skipped due to API errors")


def refresh_all_wishlists(financial_key, app_id, launch_date, on_progress=None, collector=None, game_state=None):
    app_id = str(app_id)
    today = datetime.now().date()
    earliest = find_earliest_wishlist_date(financial_key, app_id, launch_date)

    conn = get_conn()
    existing = set(r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM wishlists_by_country_daily WHERE app_id=? AND country_code='__all__' AND ((adds > 0 OR deletes > 0 OR purchases > 0) OR fetch_attempts >= 10)", (app_id,)
    ).fetchall())
    conn.close()

    last_collected = max(existing) if existing else None
    always_refresh = {today.strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")}
    if last_collected:
        always_refresh.add(last_collected)

    # Build list of dates to fetch
    dates_to_fetch = []
    current = today
    while current >= earliest:
        ds = current.strftime("%Y-%m-%d")
        if ds in existing and ds not in always_refresh:
            current -= timedelta(days=1)
            continue
        dates_to_fetch.append(ds)
        current -= timedelta(days=1)

    if not dates_to_fetch:
        return

    skipped = 0
    throttled = False
    writes = 0

    def _write_result(ds, result):
        nonlocal skipped, writes
        if result is None:
            skipped += 1
            return
        totals = result["totals"]
        c = get_conn()
        # Get current attempt count for this date
        row = c.execute(
            "SELECT fetch_attempts FROM wishlists_by_country_daily WHERE app_id=? AND date=? AND country_code='__all__'",
            (app_id, ds)
        ).fetchone()
        attempts = (row[0] if row else 0) + 1
        c.execute(
            "INSERT OR REPLACE INTO wishlists_by_country_daily VALUES (?, ?, '__all__', ?, ?, ?, ?)",
            (app_id, ds, totals["adds"], totals["deletes"], totals["purchases"], attempts)
        )
        for cc, d in result["by_country"].items():
            c.execute(
                "INSERT OR REPLACE INTO wishlists_by_country_daily VALUES (?, ?, ?, ?, ?, ?, 0)",
                (app_id, ds, cc, d["adds"], d["deletes"], d["purchases"])
            )
        c.commit()
        c.close()
        writes += 1
        if game_state and writes % 20 == 0:
            game_state.cached_wishlist_by_country = load_wishlists_by_country(app_id)
            game_state.cached_wishlist = load_wishlist_totals(app_id)

    if len(dates_to_fetch) > 5:
        # Parallel backfill
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {}
            for ds in dates_to_fetch:
                future = executor.submit(fetch_wishlist_for_date, financial_key, app_id, ds)
                futures[future] = ds

            for future in as_completed(futures):
                ds = futures[future]
                if on_progress:
                    on_progress(ds)
                result = future.result()
                if result == "throttled":
                    throttled = True
                    if collector:
                        collector.throttled = True
                    _rate_limiter.slow_down(10)
                    time.sleep(30)
                    skipped += 1
                    continue
                _write_result(ds, result)
    else:
        # Sequential for small batches
        for ds in dates_to_fetch:
            if on_progress:
                on_progress(ds)
            result = fetch_wishlist_for_date(financial_key, app_id, ds)
            if result == "throttled":
                throttled = True
                if collector:
                    collector.throttled = True
                _rate_limiter.slow_down(10)
                time.sleep(30)
                skipped += 1
                continue
            _write_result(ds, result)

    if skipped:
        print(f"  [{app_id}] WARNING: {skipped} wishlist day(s) skipped due to API errors")


# ========== TELEGRAM ==========

def send_telegram(tg_config, message):
    if not tg_config.get('enabled') or not tg_config.get('bot_token') or not tg_config.get('chat_ids'):
        return
    try:
        encoded = quote(message)
        for chat_id in tg_config['chat_ids']:
            url = f"https://api.telegram.org/bot{tg_config['bot_token']}/sendMessage?chat_id={chat_id}&text={encoded}&parse_mode=HTML"
            fetch_json(url, "telegram")
        print(f"  [TG] Sent to {len(tg_config['chat_ids'])} recipients")
    except Exception as e:
        print(f"  [TG ERROR] {e}")


def send_startup_report(settings, game):
    app_id = game['app_id']
    game_name = game.get('name', app_id)
    tg = settings.get('telegram', {})
    unreleased = bool(game.get('unreleased', False))

    if unreleased:
        wl = load_wishlist_totals(app_id)
        msg = (
            f"\U0001f377 <b>{game_name} Dashboard Online</b>\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\n"
            f"\U0001f4ca <b>Pre-Launch</b>\n"
            f"  Wishlists: <b>{wl.get('net', 0)}</b>\n"
            f"  Adds: {wl.get('adds', 0)} / Deletes: {wl.get('deletes', 0)}\n"
            f"\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f514 Monitoring started"
        )
        send_telegram(tg, msg)
        return

    totals = get_sales_totals(app_id)
    units, returns, gross, net = totals
    players = get_current_players(settings['steam_api_key'], app_id)
    reviews = get_reviews(app_id)
    total_reviews = reviews.get("total_reviews", 0)
    total_positive = reviews.get("total_positive", 0)
    rate = round(total_positive / max(total_reviews, 1) * 100)
    launch_dt = datetime.strptime(game['launch_date'], "%Y-%m-%d")
    delta = datetime.now() - launch_dt
    days_since = delta.days
    hours_since = int(delta.total_seconds() // 3600)

    daily = get_all_daily_sales(app_id)
    daily_lines = ""
    for row in daily:
        d, u, r, g, n = row
        bar_len = min(u, 30)
        bar = "\u2588" * bar_len + "\u2591" * max(0, 30 - bar_len)
        daily_lines += f"\n  {d[5:]}  {bar} {u} ${n:.0f}"

    msg = (
        f"\U0001f377 <b>{game_name} Dashboard Online</b>\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\n"
        f"\U0001f4ca <b>D+{days_since} ({hours_since}h)</b>\n"
        f"  Sales: <b>{units}</b> (refunds {returns})\n"
        f"  Revenue: ${gross:.0f} -> net ${net:.0f}\n"
        f"  Reviews: {total_reviews} ({rate}% positive)\n"
        f"  Players: {players}\n"
        f"\n"
        f"\U0001f4c8 <b>Daily Sales</b>"
        f"{daily_lines}\n"
        f"\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f514 Monitoring started"
    )
    send_telegram(tg, msg)


# ========== DATA COLLECTOR ==========

class GameState:
    def __init__(self, app_id):
        self.app_id = str(app_id)
        self.last_player_count = 0
        self.last_review_count = 0
        self.last_total_units = 0
        self.last_wishlist_net = 0
        self.peak_players = 0
        self.cached_players = 0
        self.cached_reviews = {}
        self.cached_recent_reviews = []
        self.cached_app_details = None
        self.cached_wishlist = load_wishlist_totals(app_id)
        self.cached_sales_by_country = load_sales_by_country(app_id)
        self.cached_wishlist_by_country = load_wishlists_by_country(app_id)
        self.cached_discussions = []
        self.discussions_last_fetched = 0.0
        self.cached_followers = get_latest_follower_count(app_id)
        self.followers_next_fetch = 0.0


class DataCollector:
    def __init__(self):
        self.game_states = {}
        self.collection_count = 0
        self.is_first_collection = True
        self._lock = threading.Lock()
        self.status = ""
        self.throttled = False
        self.cached_studio_followers = get_latest_follower_count(STUDIO_APP_ID)
        self.studio_followers_next_fetch = 0.0

    def get_state(self, app_id):
        app_id = str(app_id)
        if app_id not in self.game_states:
            self.game_states[app_id] = GameState(app_id)
        return self.game_states[app_id]

    def collect(self):
        if not has_settings():
            return

        settings = get_all_settings()
        api_key = settings['steam_api_key']
        financial_key = settings['steam_financial_key']
        games = settings.get('games', [])
        tg = settings.get('telegram', {})

        if not games:
            return

        now = datetime.now().strftime('%H:%M:%S')
        print(f"[{now}] Collecting for {len(games)} game(s)...")

        self.collection_count += 1
        self.throttled = False

        for game in games:
            app_id = str(game['app_id'])
            launch_date = game.get('launch_date', '2025-01-01')
            game_name = game.get('name', app_id)
            unreleased = bool(game.get('unreleased', False))
            gs = self.get_state(app_id)

            # Players + Reviews
            players = 0 if unreleased else get_current_players(api_key, app_id)
            reviews = get_reviews(app_id)
            recent_reviews = get_recent_reviews(app_id)
            save_player_count(app_id, players)

            total_reviews = reviews.get("total_reviews", 0)
            total_positive = reviews.get("total_positive", 0)
            total_negative = reviews.get("total_negative", 0)
            save_review_data(app_id, total_positive, total_negative, total_reviews)

            # Cache for instant game switching
            gs.cached_players = players
            gs.cached_reviews = reviews
            gs.cached_recent_reviews = recent_reviews
            if gs.cached_app_details is None:
                gs.cached_app_details = get_app_details(app_id)

            if players > gs.peak_players:
                gs.peak_players = players

            # Sales + Wishlists (unified fetch, no separate cadence needed)
            def _set_status(label):
                def _inner(ds):
                    self.status = f"{game_name}: {label} {ds}"
                return _inner

            refresh_all_sales(financial_key, app_id, launch_date, on_progress=_set_status("Fetching sales"), collector=self, game_state=gs, unreleased=unreleased)

            if unreleased:
                total_units = 0
                net_revenue = 0.0
            else:
                totals = get_sales_totals(app_id)
                total_units = totals[0]
                net_revenue = totals[3]
                save_sales_snapshot(app_id, totals[0], totals[1], totals[3])
                gs.cached_sales_by_country = load_sales_by_country(app_id)

            refresh_all_wishlists(financial_key, app_id, launch_date, on_progress=_set_status("Fetching wishlists"), collector=self, game_state=gs)

            gs.cached_wishlist_by_country = load_wishlists_by_country(app_id)
            gs.cached_wishlist = load_wishlist_totals(app_id)
            wl_net = gs.cached_wishlist.get("net", 0)
            save_wishlist_snapshot(app_id, gs.cached_wishlist["adds"],
                                   gs.cached_wishlist["deletes"],
                                   gs.cached_wishlist["purchases"], wl_net)

            if time.time() - gs.discussions_last_fetched > 1800:
                self.status = f"{game_name}: Fetching discussions"
                discussions = get_community_discussions(app_id)
                self.status = ""
                print(f"  [{game_name}] Discussions: {len(discussions) if discussions is not None else 'None (fetch failed)'}")
                if discussions is not None:
                    gs.cached_discussions = discussions
                    gs.discussions_last_fetched = time.time()

            if time.time() >= gs.followers_next_fetch:
                self.status = f"{game_name}: Fetching followers"
                followers = get_game_followers(app_id)
                self.status = ""
                if record_follower_count(app_id, followers):
                    gs.cached_followers = followers
                    gs.followers_next_fetch = time.time() + FOLLOWER_FETCH_INTERVAL
                    print(f"  [{game_name}] Followers: {followers}")
                else:
                    gs.followers_next_fetch = time.time() + FOLLOWER_RETRY_INTERVAL
                    print(f"  [{game_name}] Followers: fetch failed, keeping {gs.cached_followers}")

            # Telegram alerts (skip on first collection)
            if self.is_first_collection:
                gs.last_wishlist_net = wl_net
                gs.last_player_count = players
                gs.last_review_count = total_reviews
                gs.last_total_units = total_units
                print(f"  [{game_name}] Baseline: units={total_units}, wl={wl_net}, reviews={total_reviews}, players={players}")
                continue

            prefix = f"[{game_name}] " if len(games) > 1 else ""

            # Wishlist change
            if gs.last_wishlist_net > 0 and abs(wl_net - gs.last_wishlist_net) >= 5:
                diff = wl_net - gs.last_wishlist_net
                direction = "UP" if diff > 0 else "DOWN"
                send_telegram(tg,
                    f"\u2b50 <b>{prefix}Wishlist {direction}!</b>\n"
                    f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"  Change: {'+' if diff > 0 else ''}{diff}\n"
                    f"  Total adds: {gs.cached_wishlist.get('adds', 0)}\n"
                    f"  Conversions: {gs.cached_wishlist.get('purchases', 0)}\n"
                    f"  Net: ~{wl_net}"
                )
            gs.last_wishlist_net = wl_net

            # Player spike
            if gs.last_player_count > 0 and players > gs.last_player_count * 1.5 and players >= 5:
                send_telegram(tg,
                    f"\U0001f680 <b>{prefix}Player spike!</b>\n{gs.last_player_count} -> {players}")

            # New review
            if gs.last_review_count > 0 and total_reviews > gs.last_review_count:
                n = total_reviews - gs.last_review_count
                send_telegram(tg,
                    f"\U0001f4dd <b>{prefix}New review{'s' if n > 1 else ''} ({n})!</b>\n"
                    f"Total {total_reviews} (+{total_positive} -{total_negative})")

            # New sale
            if not unreleased and gs.last_total_units > 0 and total_units > gs.last_total_units:
                new_sales = total_units - gs.last_total_units
                country_lines = ""
                if gs.cached_sales_by_country:
                    sorted_countries = sorted(gs.cached_sales_by_country.items(),
                                              key=lambda x: x[1].get("units", 0), reverse=True)
                    top3 = sorted_countries[:3]
                    if top3:
                        lines = [f"  {cc}: {d['units']} units" for cc, d in top3]
                        country_lines = "\n\nTop countries:\n" + "\n".join(lines)
                send_telegram(tg,
                    f"\U0001f4b0 <b>{prefix}New sale{'s' if new_sales > 1 else ''} +{new_sales}!</b>\n"
                    f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"  Total: {total_units}\n"
                    f"  Net revenue: ${net_revenue:.0f}\n"
                    f"  Players: {players}"
                    f"{country_lines}"
                )

            gs.last_player_count = players
            gs.last_review_count = total_reviews
            gs.last_total_units = total_units

            print(f"  [{game_name}] Players: {players} | Reviews: {total_reviews} | Sales: {total_units} | Peak: {gs.peak_players}")

        studio_cfg = settings.get('studio', {})
        studio_url = studio_cfg.get('url', '')
        if studio_url and time.time() >= self.studio_followers_next_fetch:
            self.status = "Fetching studio followers"
            studio_followers = get_studio_followers(studio_url)
            self.status = ""
            if record_follower_count(STUDIO_APP_ID, studio_followers):
                self.cached_studio_followers = studio_followers
                self.studio_followers_next_fetch = time.time() + FOLLOWER_FETCH_INTERVAL
                print(f"  [studio] Followers: {studio_followers}")
            else:
                self.studio_followers_next_fetch = time.time() + FOLLOWER_RETRY_INTERVAL
                print(f"  [studio] Followers: fetch failed, keeping {self.cached_studio_followers}")

        self.is_first_collection = False
        self.status = ""

    def loop(self):
        while True:
            try:
                settings = get_all_settings()
                interval = settings.get('dashboard', {}).get('poll_interval', 300)
            except Exception:
                interval = 300
            try:
                self.collect()
            except Exception as e:
                print(f"[COLLECTOR ERROR] {e}")
            time.sleep(interval)


# ========== SETUP WIZARD HTML ==========

SETUP_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+CiAgPHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTIiIGZpbGw9IiMxNzFhMjEiLz4KICA8cmVjdCB4PSIxMCIgeT0iMjIiIHdpZHRoPSI0NCIgaGVpZ2h0PSIzMiIgcng9IjMiIGZpbGw9IiMxYjI4MzgiIG9wYWNpdHk9IjAuNiIvPgogIDxyZWN0IHg9IjE0IiB5PSI0MCIgd2lkdGg9IjYiIGhlaWdodD0iMTIiIHJ4PSIxIiBmaWxsPSIjMmE0NzVlIi8+CiAgPHJlY3QgeD0iMjIiIHk9IjM0IiB3aWR0aD0iNiIgaGVpZ2h0PSIxOCIgcng9IjEiIGZpbGw9IiMzZDZjOGUiLz4KICA8cmVjdCB4PSIzMCIgeT0iMjgiIHdpZHRoPSI2IiBoZWlnaHQ9IjI0IiByeD0iMSIgZmlsbD0iIzY2YzBmNCIvPgogIDxyZWN0IHg9IjM4IiB5PSIzMiIgd2lkdGg9IjYiIGhlaWdodD0iMjAiIHJ4PSIxIiBmaWxsPSIjNjZjMGY0Ii8+CiAgPHJlY3QgeD0iNDYiIHk9IjI0IiB3aWR0aD0iNiIgaGVpZ2h0PSIyOCIgcng9IjEiIGZpbGw9IiM2NmMwZjQiLz4KICA8cG9seWxpbmUgcG9pbnRzPSIxNywzOCAyNSwzMiAzMywyNiA0MSwzMCA0OSwyMiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjYTRkMDA3IiBzdHJva2Utd2lkdGg9IjIuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+CiAgPGNpcmNsZSBjeD0iMTciIGN5PSIzOCIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+CiAgPGNpcmNsZSBjeD0iMzMiIGN5PSIyNiIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+CiAgPGNpcmNsZSBjeD0iNDkiIGN5PSIyMiIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+Cjwvc3ZnPg==">
<title>Steam Dashboard - Setup</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --steam-dark: #171a21;
  --steam-navy: #1b2838;
  --steam-blue-dark: #2a475e;
  --steam-blue-med: #3d6c8e;
  --steam-blue-light: #66c0f4;
  --steam-green: #5c7e10;
  --steam-green-bright: #a4d007;
  --steam-text: #c7d5e0;
  --steam-text-dim: #8f98a0;
  --steam-text-dark: #556772;
  --font-body: 'Noto Sans', -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: var(--font-body);
  background: var(--steam-dark);
  color: var(--steam-text);
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
}
.wizard {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 680px;
}
.wizard-header {
  text-align: center;
  margin-bottom: 32px;
  padding-bottom: 20px;
  border-bottom: 1px solid #2a475e;
}
.wizard-header h1 {
  font-family: var(--font-body);
  font-size: 28px;
  font-weight: 700;
  margin-bottom: 8px;
  letter-spacing: -0.01em;
  color: #ffffff;
}
.wizard-header p {
  font-size: 14px;
  color: var(--steam-text-dim);
  line-height: 1.6;
}
/* Steps indicator — Steam tab bar style */
.steps-bar {
  display: flex;
  justify-content: center;
  gap: 4px;
  margin-bottom: 24px;
  background: rgba(0,0,0,0.2);
  border-radius: 4px;
  padding: 3px;
}
.step-dot {
  flex: 1;
  height: 32px;
  border-radius: 2px;
  background: transparent;
  transition: all 0.3s;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  color: var(--steam-text-dark);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.step-dot.active {
  background: var(--steam-blue-dark);
  color: var(--steam-blue-light);
  box-shadow: 0 0 8px rgba(102,192,244,0.15);
}
.step-dot.done {
  background: rgba(92,126,16,0.2);
  color: var(--steam-green-bright);
}
/* Step panels */
.step-panel {
  display: none;
  animation: fadeIn 0.3s ease;
}
.step-panel.active {
  display: block;
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
.card {
  background: #16202d;
  border: 1px solid #2a475e;
  border-radius: 4px;
  padding: 24px;
  margin-bottom: 16px;
}
.card h2 {
  font-family: var(--font-body);
  font-size: 20px;
  font-weight: 600;
  margin-bottom: 6px;
  color: #ffffff;
}
.card .hint {
  font-size: 13px;
  color: var(--steam-text-dim);
  margin-bottom: 20px;
  line-height: 1.6;
}
.card .hint a {
  color: var(--steam-blue-light);
  text-decoration: none;
}
.card .hint a:hover {
  text-decoration: underline;
}
.card .divider {
  border: none;
  border-top: 1px solid #2a475e;
  margin: 20px 0;
}
label {
  display: block;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--steam-text-dim);
  margin-bottom: 6px;
  margin-top: 16px;
}
label:first-of-type { margin-top: 0; }
input[type="text"], input[type="password"], input[type="number"], input[type="date"] {
  width: 100%;
  padding: 10px 14px;
  background: #32404e;
  border: 1px solid #556772;
  border-radius: 4px;
  color: var(--steam-text);
  font-family: var(--font-mono);
  font-size: 14px;
  transition: all 0.2s;
  outline: none;
}
input:focus {
  border-color: var(--steam-blue-light);
  box-shadow: 0 0 8px rgba(102,192,244,0.3);
}
input::placeholder {
  color: var(--steam-text-dark);
  opacity: 0.8;
}
/* Key status indicators */
.key-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-family: var(--font-mono);
  margin-left: 8px;
}
.key-status.ok { color: var(--steam-green-bright); }
.key-status.fail { color: #c45a5a; }
.key-status.pending { color: var(--steam-text-dark); }
/* Toggle */
.toggle-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.toggle {
  width: 44px;
  height: 24px;
  background: #32404e;
  border-radius: 12px;
  position: relative;
  cursor: pointer;
  transition: background 0.2s;
  flex-shrink: 0;
}
.toggle.on {
  background: var(--steam-green);
}
.toggle::after {
  content: '';
  position: absolute;
  top: 3px;
  left: 3px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: white;
  transition: transform 0.2s;
}
.toggle.on::after {
  transform: translateX(20px);
}
.toggle-label {
  font-size: 14px;
  color: var(--steam-text-dim);
}
.tg-fields {
  display: none;
}
.tg-fields.visible {
  display: block;
}
/* Game list */
.game-item {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  margin-bottom: 12px;
  padding: 12px;
  background: rgba(0,0,0,0.25);
  border-radius: 4px;
  border: 1px solid rgba(42,71,94,0.4);
}
.game-item .field { flex: 1; }
.game-item .field label { margin-top: 0; }
.game-item .field .field-hint {
  font-size: 11px;
  color: var(--steam-text-dark);
  margin-top: 4px;
}
.game-item .remove-btn {
  background: rgba(196,90,90,0.15);
  border: 1px solid rgba(196,90,90,0.3);
  color: #c45a5a;
  padding: 8px 12px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
  margin-bottom: 0;
  height: 38px;
}
.game-item .game-status {
  font-size: 12px;
  font-family: var(--font-mono);
  margin-bottom: 0;
  height: 38px;
  display: flex;
  align-items: center;
  min-width: 20px;
}
.game-item .game-status.ok { color: var(--steam-green-bright); }
.game-item .game-status.fail { color: #c45a5a; }
.add-game-btn {
  background: transparent;
  border: 1px dashed #2a475e;
  color: var(--steam-text-dark);
  padding: 10px;
  width: 100%;
  border-radius: 4px;
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 13px;
  transition: all 0.2s;
}
.add-game-btn:hover {
  border-color: var(--steam-blue-light);
  color: var(--steam-text-dim);
}
/* Accent picker */
/* Test button — Steam green */
.test-btn {
  background: linear-gradient(to right, #75b022, #588a1b);
  border: none;
  color: #d2efa9;
  padding: 10px 20px;
  border-radius: 4px;
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 14px;
  font-weight: 600;
  margin-top: 16px;
  transition: all 0.2s;
}
.test-btn:hover {
  background: linear-gradient(to right, #8ecb2a, #6aa020);
  color: #ffffff;
}
.test-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.test-result {
  margin-top: 10px;
  font-size: 13px;
  font-family: var(--font-mono);
  padding: 10px 14px;
  border-radius: 4px;
  display: none;
  line-height: 1.6;
}
.test-result.success {
  display: block;
  background: rgba(92,126,16,0.15);
  border: 1px solid rgba(164,208,7,0.3);
  color: var(--steam-green-bright);
}
.test-result.error {
  display: block;
  background: rgba(196,90,90,0.1);
  border: 1px solid rgba(196,90,90,0.3);
  color: #c45a5a;
}
.test-result.partial {
  display: block;
  background: rgba(201,168,76,0.1);
  border: 1px solid rgba(201,168,76,0.3);
  color: #c9a84c;
}
/* Navigation buttons */
.nav-buttons {
  display: flex;
  justify-content: space-between;
  margin-top: 20px;
}
.nav-btn {
  padding: 12px 28px;
  border-radius: 4px;
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 15px;
  font-weight: 600;
  transition: all 0.2s;
  border: none;
}
.nav-btn.prev {
  background: transparent;
  border: 1px solid #2a475e;
  color: var(--steam-text-dim);
}
.nav-btn.prev:hover {
  border-color: var(--steam-blue-med);
  color: var(--steam-text);
}
.nav-btn.next {
  background: linear-gradient(to right, rgba(102,192,244,0.25), rgba(102,192,244,0.15));
  border: 1px solid rgba(102,192,244,0.4);
  color: var(--steam-blue-light);
}
.nav-btn.next:hover {
  background: linear-gradient(to right, rgba(102,192,244,0.35), rgba(102,192,244,0.25));
  color: #ffffff;
}
.nav-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
  transform: none !important;
}
.nav-btn.start {
  background: linear-gradient(to right, #75b022, #588a1b);
  border: none;
  color: #d2efa9;
  font-size: 16px;
  padding: 14px 36px;
}
.nav-btn.start:hover {
  background: linear-gradient(to right, #8ecb2a, #6aa020);
  color: #ffffff;
}
</style>
</head>
<body>
<div class="wizard">
  <div class="wizard-header">
    <h1>Steam Dashboard Setup</h1>
    <p>Real-time sales monitoring for your Steam games. Let's get you set up in a few quick steps.</p>
  </div>

  <div class="steps-bar">
    <div class="step-dot active" data-step="0">INTRO</div>
    <div class="step-dot" data-step="1">CONNECT</div>
    <div class="step-dot" data-step="2">ALERTS</div>
    <div class="step-dot" data-step="3">PREFS</div>
    <div class="step-dot" data-step="4">GO</div>
  </div>

  <!-- Step 0: Welcome -->
  <div class="step-panel active" data-step="0">
    <div class="card">
      <h2>Welcome</h2>
      <div class="hint">
        This dashboard tracks your Steam game's sales, revenue, reviews,
        concurrent players, and wishlists in real-time. It can also send
        you Telegram alerts when something happens.
        <br><br>
        You'll need:
        <br>&bull; A <a href="https://steamcommunity.com/dev/apikey" target="_blank">Steam Web API Key</a>
        <br>&bull; A <a href="https://partner.steampowered.com/" target="_blank">Steamworks Financial API Key</a> (from Partner site)
        <br>&bull; Your game's App ID
      </div>
    </div>
  </div>

  <!-- Step 1: Steam Connection (API Keys + Games + Test) -->
  <div class="step-panel" data-step="1">
    <div class="card">
      <h2>Steam Connection</h2>
      <div class="hint">
        Enter your API keys and add games to monitor. Test the connection before proceeding.
      </div>

      <label>Steam Web API Key <span class="key-status pending" id="apiKeyStatus"></span></label>
      <input type="password" id="steamApiKey" placeholder="E719B9C8C920A1EB..." />
      <div class="hint" style="margin-bottom:0;margin-top:6px;font-size:11px;">
        <a href="https://steamcommunity.com/dev/apikey" target="_blank">steamcommunity.com/dev/apikey</a>
      </div>

      <label style="margin-top:18px;">Steam Financial API Key <span class="key-status pending" id="finKeyStatus"></span></label>
      <input type="password" id="steamFinancialKey" placeholder="064E0AB9C952..." />
      <div class="hint" style="margin-bottom:0;margin-top:6px;font-size:11px;">
        Steamworks Partner &rarr; Users &amp; Permissions &rarr; Manage Groups &rarr; [group] &rarr; Web API Key
      </div>

      <label style="margin-top:18px;">Studio Name (optional)</label>
      <input type="text" id="studioName" placeholder="Your Studio Name" />

      <label style="margin-top:18px;">Studio Page URL (optional) <span class="key-status pending" id="studioStatus"></span></label>
      <input type="text" id="studioUrl" placeholder="https://store.steampowered.com/developer/YourStudio" />
      <div class="hint" style="margin-bottom:0;margin-top:6px;font-size:11px;">
        Your developer, publisher, or curator page. Leave blank to hide studio followers.
      </div>

      <hr class="divider">

      <h2 style="margin-top:0;">Your Games</h2>
      <div class="hint">Add one or more games to monitor. The game name will be fetched automatically.</div>
      <div id="gamesList"></div>
      <button class="add-game-btn" onclick="addGameRow()">+ Add Another Game</button>

      <button class="test-btn" id="testBtn" onclick="testConnection()">Test Connection</button>
      <div class="test-result" id="testResult"></div>
    </div>
  </div>

  <!-- Step 2: Telegram -->
  <div class="step-panel" data-step="2">
    <div class="card">
      <h2>Telegram Alerts</h2>
      <div class="hint">Get instant notifications for new sales, reviews, and player spikes. This is optional.</div>
      <div class="toggle-row">
        <div class="toggle" id="tgToggle" onclick="toggleTelegram()"></div>
        <span class="toggle-label">Enable Telegram alerts</span>
      </div>
      <div class="tg-fields" id="tgFields">
        <label>Bot Token</label>
        <input type="password" id="tgBotToken" placeholder="123456:ABC-DEF..." />
        <label>Chat IDs (comma-separated)</label>
        <input type="text" id="tgChatIds" placeholder="7271353545, 8264620489" />
      </div>
    </div>
  </div>

  <!-- Step 3: Preferences -->
  <div class="step-panel" data-step="3">
    <div class="card">
      <h2>Preferences</h2>
      <div class="hint">Customize the look and feel of your dashboard.</div>

      <label style="margin-top:20px;">Port</label>
      <input type="number" id="portInput" value="{{PORT}}" min="1024" max="65535" />
    </div>
  </div>

  <!-- Step 4: Confirm -->
  <div class="step-panel" data-step="4">
    <div class="card" style="text-align:center;">
      <h2>Ready to Go</h2>
      <div class="hint" style="margin-bottom:8px;">
        Your dashboard will start collecting data immediately after setup.
        The first data collection may take a few minutes depending on how many days since launch.
      </div>
      <div id="setupSummary" style="text-align:left;font-family:var(--font-mono);font-size:13px;color:var(--steam-text-dim);margin:20px 0;padding:16px;background:rgba(0,0,0,0.3);border-radius:4px;border:1px solid #2a475e;"></div>
    </div>
  </div>

  <div class="nav-buttons">
    <button class="nav-btn prev" id="prevBtn" onclick="prevStep()" style="visibility:hidden;">Back</button>
    <button class="nav-btn next" id="nextBtn" onclick="nextStep()">Next</button>
  </div>
</div>

<script>
(function() {
  var currentStep = 0;
  var totalSteps = 5;
  var selectedAccent = 'steam'; // kept for settings payload compatibility
  var tgEnabled = false;
  var connectionTested = false;

  // Pre-fill if editing settings
  var existingSettings = {{EXISTING_SETTINGS_JSON}};
  if (existingSettings && existingSettings.steam_api_key) {
    connectionTested = true;
    document.getElementById('steamApiKey').value = existingSettings.steam_api_key || '';
    document.getElementById('steamFinancialKey').value = existingSettings.steam_financial_key || '';
    var studioCfg = existingSettings.studio || {};
    document.getElementById('studioName').value = studioCfg.name || '';
    document.getElementById('studioUrl').value = studioCfg.url || '';
    var tg = existingSettings.telegram || {};
    if (tg.enabled) {
      tgEnabled = true;
      document.getElementById('tgToggle').classList.add('on');
      document.getElementById('tgFields').classList.add('visible');
      document.getElementById('tgBotToken').value = tg.bot_token || '';
      document.getElementById('tgChatIds').value = (tg.chat_ids || []).join(', ');
    }
    var dash = existingSettings.dashboard || {};
    if (dash.port) document.getElementById('portInput').value = dash.port;
  }

  // Initialize games list
  var games = (existingSettings && existingSettings.games && existingSettings.games.length > 0)
    ? existingSettings.games
    : [{ app_id: '', name: '' }];

  function renderGames() {
    var container = document.getElementById('gamesList');
    container.innerHTML = '';
    games.forEach(function(g, i) {
      var div = document.createElement('div');
      div.className = 'game-item';
      div.innerHTML =
        '<div class="field"><label>App ID</label><input type="text" value="' + (g.app_id || '') + '" onchange="updateGame(' + i + ',\\'app_id\\',this.value)" placeholder="4451370" /></div>' +
        '<div class="game-status" id="gameStatus' + i + '"></div>' +
        (games.length > 1 ? '<button class="remove-btn" onclick="removeGame(' + i + ')">X</button>' : '');
      container.appendChild(div);
    });
  }

  window.addGameRow = function() {
    games.push({ app_id: '', name: '' });
    connectionTested = false;
    renderGames();
  };

  window.removeGame = function(i) {
    games.splice(i, 1);
    connectionTested = false;
    renderGames();
  };

  window.updateGame = function(i, field, value) {
    games[i][field] = value;
    connectionTested = false;
  };

  renderGames();

  window.toggleTelegram = function() {
    tgEnabled = !tgEnabled;
    var el = document.getElementById('tgToggle');
    var fields = document.getElementById('tgFields');
    if (tgEnabled) {
      el.classList.add('on');
      fields.classList.add('visible');
    } else {
      el.classList.remove('on');
      fields.classList.remove('visible');
    }
  };

  window.testConnection = function() {
    var apiKey = document.getElementById('steamApiKey').value.trim();
    var financialKey = document.getElementById('steamFinancialKey').value.trim();
    var validGames = games.filter(function(g) { return g.app_id; });
    var resultEl = document.getElementById('testResult');
    var testBtn = document.getElementById('testBtn');

    if (!apiKey || !validGames.length) {
      resultEl.className = 'test-result error';
      resultEl.textContent = 'Please fill in the API key and at least one App ID first.';
      return;
    }

    testBtn.disabled = true;
    resultEl.className = 'test-result';
    resultEl.style.display = 'block';
    resultEl.style.background = 'rgba(102,192,244,0.1)';
    resultEl.style.borderColor = 'rgba(102,192,244,0.3)';
    resultEl.style.color = '#66c0f4';
    resultEl.textContent = 'Testing...';

    // Reset statuses
    document.getElementById('apiKeyStatus').className = 'key-status pending';
    document.getElementById('apiKeyStatus').textContent = '';
    document.getElementById('finKeyStatus').className = 'key-status pending';
    document.getElementById('finKeyStatus').textContent = '';
    for (var k = 0; k < games.length; k++) {
      var gs = document.getElementById('gameStatus' + k);
      if (gs) { gs.textContent = ''; gs.className = 'game-status'; }
    }

    var appIds = validGames.map(function(g) { return g.app_id; }).join(',');
    var url = '/api/test?api_key=' + encodeURIComponent(apiKey) + '&app_ids=' + encodeURIComponent(appIds);
    if (financialKey) url += '&financial_key=' + encodeURIComponent(financialKey);
    var studioUrlVal = document.getElementById('studioUrl').value.trim();
    if (studioUrlVal) url += '&studio_url=' + encodeURIComponent(studioUrlVal);

    fetch(url)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        testBtn.disabled = false;
        var lines = [];

        // API key status
        var apiSt = document.getElementById('apiKeyStatus');
        if (data.api_key_valid) {
          apiSt.className = 'key-status ok';
          apiSt.textContent = '\\u2713';
          lines.push('\\u2713 Web API key verified');
        } else {
          apiSt.className = 'key-status fail';
          apiSt.textContent = '\\u2717';
        }

        // Financial key status
        var finSt = document.getElementById('finKeyStatus');
        if (financialKey) {
          if (data.financial_key_valid) {
            finSt.className = 'key-status ok';
            finSt.textContent = '\\u2713';
            lines.push('\\u2713 Financial API key verified');
          } else {
            finSt.className = 'key-status fail';
            finSt.textContent = '\\u2717';
            lines.push('\\u2717 Financial API key error (sales data excluded)');
          }
        }

        // Studio status (optional, never blocks setup)
        var studioSt = document.getElementById('studioStatus');
        if (studioUrlVal && data.studio) {
          if (data.studio.success) {
            studioSt.className = 'key-status ok';
            studioSt.textContent = '\\u2713';
            lines.push('\\u2713 Studio page verified (' + data.studio.followers + ' followers)');
          } else {
            studioSt.className = 'key-status fail';
            studioSt.textContent = '\\u2717';
            lines.push('\\u2717 Studio page: ' + (data.studio.error || 'Error'));
          }
        } else {
          studioSt.className = 'key-status pending';
          studioSt.textContent = '';
        }

        // Per-game results
        var gameResults = data.games || [];
        var allOk = data.api_key_valid;
        for (var j = 0; j < gameResults.length; j++) {
          var gr = gameResults[j];
          var gsEl = document.getElementById('gameStatus' + j);
          if (gr.success) {
            if (gsEl) { gsEl.className = 'game-status ok'; gsEl.textContent = '\\u2713'; }
            var preTag = gr.coming_soon ? ' [Pre-Launch]' : '';
            lines.push('\\u2713 ' + gr.app_id + (gr.name ? ' (' + gr.name + ')' : '') + preTag);
            if (gr.name && games[j]) games[j].name = gr.name;
          } else {
            allOk = false;
            if (gsEl) { gsEl.className = 'game-status fail'; gsEl.textContent = '\\u2717'; }
            lines.push('\\u2717 ' + gr.app_id + ': ' + (gr.error || 'Error'));
          }
        }

        if (allOk) {
          resultEl.className = 'test-result success';
          connectionTested = true;
        } else if (data.api_key_valid) {
          resultEl.className = 'test-result partial';
          connectionTested = true;
        } else {
          resultEl.className = 'test-result error';
          connectionTested = false;
        }
        resultEl.innerHTML = lines.join('<br>');
      })
      .catch(function(e) {
        testBtn.disabled = false;
        resultEl.className = 'test-result error';
        resultEl.textContent = 'Network error: ' + e.message;
        connectionTested = false;
      });
  };

  function updateStepDots() {
    document.querySelectorAll('.step-dot').forEach(function(dot, i) {
      dot.classList.toggle('active', i === currentStep);
      dot.classList.toggle('done', i < currentStep);
    });
  }

  function showStep(step) {
    document.querySelectorAll('.step-panel').forEach(function(panel) {
      panel.classList.toggle('active', parseInt(panel.getAttribute('data-step')) === step);
    });
    document.getElementById('prevBtn').style.visibility = step === 0 ? 'hidden' : 'visible';
    var nextBtn = document.getElementById('nextBtn');
    if (step === totalSteps - 1) {
      nextBtn.textContent = 'Start Monitoring';
      nextBtn.className = 'nav-btn start';
    } else {
      nextBtn.textContent = 'Next';
      nextBtn.className = 'nav-btn next';
    }
    document.getElementById('prevBtn').textContent = 'Back';
    updateStepDots();

    // Build summary on last step
    if (step === totalSteps - 1) {
      var lines = [];
      lines.push('Games: ' + games.filter(function(g){return g.app_id;}).map(function(g){return g.app_id + (g.name ? ' (' + g.name + ')' : '');}).join(', '));
      lines.push('Telegram: ' + (tgEnabled ? 'ON' : 'OFF'));
      lines.push('Port: ' + document.getElementById('portInput').value);
      document.getElementById('setupSummary').innerHTML = lines.join('<br>');
    }
  }

  window.nextStep = function() {
    // Validate step 1: must pass connection test
    if (currentStep === 1 && !connectionTested) {
      var resultEl = document.getElementById('testResult');
      resultEl.className = 'test-result error';
      resultEl.textContent = 'Connection test must pass before proceeding.';
      return;
    }

    if (currentStep === totalSteps - 1) {
      submitSetup();
      return;
    }
    currentStep++;
    showStep(currentStep);
  };

  window.prevStep = function() {
    if (currentStep > 0) {
      currentStep--;
      showStep(currentStep);
    }
  };

  // Clicking step dots
  document.querySelectorAll('.step-dot').forEach(function(dot) {
    dot.addEventListener('click', function() {
      var step = parseInt(this.getAttribute('data-step'));
      if (step <= currentStep + 1) {
        if (currentStep === 1 && step > 1 && !connectionTested) return;
        currentStep = step;
        showStep(currentStep);
      }
    });
  });

  function submitSetup() {
    var validGames = games.filter(function(g) { return g.app_id; });
    if (!validGames.length) {
      alert('Please add at least one game.');
      return;
    }

    var chatIdsStr = document.getElementById('tgChatIds').value.trim();
    var chatIds = chatIdsStr ? chatIdsStr.split(',').map(function(s) { return s.trim(); }).filter(Boolean) : [];

    var payload = {
      steam_api_key: document.getElementById('steamApiKey').value.trim(),
      steam_financial_key: document.getElementById('steamFinancialKey').value.trim(),
      games: validGames,
      studio: {
        name: document.getElementById('studioName').value.trim(),
        url: document.getElementById('studioUrl').value.trim()
      },
      telegram: {
        enabled: tgEnabled,
        bot_token: document.getElementById('tgBotToken').value.trim(),
        chat_ids: chatIds
      },
      dashboard: {
        port: parseInt(document.getElementById('portInput').value) || 8081,
        poll_interval: 300,
        language: 'en',
        theme: 'dark',
        accent: selectedAccent
      }
    };

    var nextBtn = document.getElementById('nextBtn');
    nextBtn.disabled = true;
    nextBtn.textContent = 'Saving...';

    var endpoint = existingSettings && existingSettings.steam_api_key ? '/api/settings' : '/api/setup';

    fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function(r) { return r.json(); }).then(function(data) {
      if (data.success) {
        window.location.href = '/';
      } else {
        nextBtn.disabled = false;
        nextBtn.textContent = 'Start Monitoring';
        alert('Error: ' + (data.error || 'Unknown'));
      }
    }).catch(function(e) {
      nextBtn.disabled = false;
      nextBtn.textContent = 'Start Monitoring';
      alert('Network error: ' + e.message);
    });
  }

})();
</script>
</body>
</html>'''


# ========== DASHBOARD HTML ==========

DASHBOARD_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+CiAgPHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTIiIGZpbGw9IiMxNzFhMjEiLz4KICA8cmVjdCB4PSIxMCIgeT0iMjIiIHdpZHRoPSI0NCIgaGVpZ2h0PSIzMiIgcng9IjMiIGZpbGw9IiMxYjI4MzgiIG9wYWNpdHk9IjAuNiIvPgogIDxyZWN0IHg9IjE0IiB5PSI0MCIgd2lkdGg9IjYiIGhlaWdodD0iMTIiIHJ4PSIxIiBmaWxsPSIjMmE0NzVlIi8+CiAgPHJlY3QgeD0iMjIiIHk9IjM0IiB3aWR0aD0iNiIgaGVpZ2h0PSIxOCIgcng9IjEiIGZpbGw9IiMzZDZjOGUiLz4KICA8cmVjdCB4PSIzMCIgeT0iMjgiIHdpZHRoPSI2IiBoZWlnaHQ9IjI0IiByeD0iMSIgZmlsbD0iIzY2YzBmNCIvPgogIDxyZWN0IHg9IjM4IiB5PSIzMiIgd2lkdGg9IjYiIGhlaWdodD0iMjAiIHJ4PSIxIiBmaWxsPSIjNjZjMGY0Ii8+CiAgPHJlY3QgeD0iNDYiIHk9IjI0IiB3aWR0aD0iNiIgaGVpZ2h0PSIyOCIgcng9IjEiIGZpbGw9IiM2NmMwZjQiLz4KICA8cG9seWxpbmUgcG9pbnRzPSIxNywzOCAyNSwzMiAzMywyNiA0MSwzMCA0OSwyMiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjYTRkMDA3IiBzdHJva2Utd2lkdGg9IjIuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+CiAgPGNpcmNsZSBjeD0iMTciIGN5PSIzOCIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+CiAgPGNpcmNsZSBjeD0iMzMiIGN5PSIyNiIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+CiAgPGNpcmNsZSBjeD0iNDkiIGN5PSIyMiIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+Cjwvc3ZnPg==">
<title>Steam Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --font-body: 'Noto Sans', -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
  --radius-sm: 4px;
  --radius-md: 4px;
  --ease-out: cubic-bezier(0.25, 0.46, 0.45, 0.94);
}

/* ---- ACCENT COLORS ---- */
:root {
  --accent: #66c0f4; --accent-dim: #2a475e;
  --accent-glow: rgba(102,192,244,0.2); --accent-fill: rgba(102,192,244,0.08);
}

/* ---- DARK THEME (Steam native) ---- */
:root[data-theme="dark"] {
  --bg-black: #171a21; --bg-deep: #1b2838; --bg-mid: #16202d;
  --bg-surface: #1b2838; --bg-elevated: #2a475e;
  --border-color: #2a475e; --border-light: #3d6c8e;
  --text-primary: #c7d5e0; --text-secondary: #8f98a0;
  --text-tertiary: #556772; --text-accent: #c7d5e0;
  --gold: #66c0f4; --gold-bright: #ffffff; --gold-dim: #2a475e;
  --gold-fill: rgba(102,192,244,0.08);
  --green: #5c7e10; --green-bright: #a4d007; --green-dim: #3d5a0a;
  --green-fill: rgba(92,126,16,0.08);
  --red: #c45a5a; --purple: #66c0f4; --purple-fill: rgba(102,192,244,0.08);
  --chart-grid: rgba(42,71,94,0.4); --chart-tick: #556772; --chart-legend: #8f98a0;
  --tooltip-bg: rgba(22,32,45,0.97); --tooltip-border: rgba(102,192,244,0.2);
  --status-bg: rgba(23,26,33,0.95);
  --header-bg: linear-gradient(165deg, #171a21 0%, #1b2838 100%);
  --header-glow: rgba(102,192,244,0.06);
  --shimmer-a: #16202d; --shimmer-b: #2a475e;
  --review-hover: rgba(42,71,94,0.15);
}

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: var(--font-body);
  background: var(--bg-black);
  color: var(--text-primary);
  min-height: 100vh;
  overflow-x: hidden;
}

.header {
  position: relative;
  background: var(--header-bg);
  padding: 24px 32px;
  display: flex; align-items: center; gap: 24px;
  border-bottom: 1px solid var(--border-color);
  overflow: hidden;
}
.header::before {
  content: '';
  position: absolute; top: -50%; right: -10%;
  width: 400px; height: 400px;
  background: radial-gradient(circle, var(--header-glow) 0%, transparent 70%);
  pointer-events: none;
}
.header-img {
  width: 184px; border-radius: var(--radius-sm);
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  flex-shrink: 0;
}
.header-info { flex: 1; min-width: 0; }
.header-info h1 {
  font-family: var(--font-body); font-size: 28px; font-weight: 700;
  color: #ffffff; letter-spacing: -0.01em; margin-bottom: 4px;
}
.header-info .subtitle { font-size: 13px; color: var(--text-tertiary); margin-bottom: 10px; }
.header-info .price-badge {
  display: inline-flex; align-items: center; gap: 6px;
  background: linear-gradient(135deg, rgba(164,208,7,0.15), rgba(92,126,16,0.1));
  border: 1px solid rgba(164,208,7,0.3);
  color: var(--green-bright); padding: 5px 14px; border-radius: var(--radius-sm);
  font-family: var(--font-mono); font-size: 13px; font-weight: 500;
}
.header-info .price-badge.prelaunch {
  background: linear-gradient(135deg, rgba(102,192,244,0.15), rgba(40,90,140,0.1));
  border-color: rgba(102,192,244,0.4);
  color: var(--accent);
  letter-spacing: 0.06em; text-transform: uppercase;
}
body.unreleased .sales-only { display: none !important; }
body.unreleased .metrics-grid { grid-template-columns: repeat(auto-fit, minmax(220px, 320px)); }
body.unreleased .country-grid { grid-template-columns: 1fr; }
.header-controls {
  margin-left: auto; text-align: right; flex-shrink: 0;
  display: flex; flex-direction: column; align-items: flex-end; gap: 6px;
}
.live-indicator {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 11px; font-weight: 600; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--green-bright);
}
.live-dot {
  width: 7px; height: 7px; background: var(--green-bright);
  border-radius: 50%; box-shadow: 0 0 8px rgba(164,208,7,0.5);
  animation: livePulse 2.5s ease-in-out infinite;
}
@keyframes livePulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 8px rgba(164,208,7,0.5); }
  50% { opacity: 0.4; box-shadow: 0 0 4px rgba(164,208,7,0.2); }
}
.update-time { font-size: 12px; color: var(--text-tertiary); font-family: var(--font-mono); }
.poll-info { font-size: 11px; color: var(--text-tertiary); opacity: 0.6; }
.header-buttons {
  display: flex; gap: 6px; align-items: center; margin-top: 4px;
}
.settings-btn {
  background: transparent; border: 1px solid var(--border-color);
  color: var(--text-tertiary); padding: 3px 8px; border-radius: 2px;
  cursor: pointer; font-size: 13px; transition: all 0.2s;
  text-decoration: none; display: inline-flex; align-items: center;
}
.settings-btn:hover { border-color: var(--border-light); color: var(--text-secondary); }
.game-selector {
  display: none; margin-top: 4px;
}
.game-selector.visible { display: flex; gap: 6px; flex-wrap: wrap; }
.game-tab {
  padding: 4px 12px; border-radius: 2px; font-size: 12px;
  font-family: var(--font-mono); cursor: pointer;
  border: 1px solid var(--border-color); background: transparent;
  color: var(--text-tertiary); transition: all 0.2s;
}
.game-tab.active {
  background: var(--bg-elevated); color: #ffffff;
  border-color: var(--border-light);
}

.dashboard { max-width: 1400px; margin: 0 auto; padding: 24px 24px 48px; }

.metrics-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px; margin-bottom: 12px;
}
.metric-card {
  position: relative;
  background: var(--bg-mid);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 18px 20px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
  overflow: hidden;
}
.metric-card:hover { border-color: var(--border-light); transform: translateY(-1px); }
.metric-card::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}
.metric-label {
  font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-tertiary); margin-bottom: 8px;
}
.metric-value {
  font-family: var(--font-mono); font-size: 30px; font-weight: 700;
  color: var(--text-primary); line-height: 1.1; letter-spacing: -0.02em;
}
.metric-value.gold { color: var(--accent); }
.metric-value.green { color: var(--green-bright); }
.metric-sub {
  font-size: 12px; color: var(--text-tertiary); margin-top: 6px;
  font-family: var(--font-mono); font-weight: 400;
}

.charts-grid { display: grid; grid-template-columns: 1fr; gap: 12px; margin-bottom: 12px; }
.charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.chart-span-full { grid-column: 1 / -1; }
.chart-card {
  position: relative;
  background: var(--bg-mid);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 20px 22px; overflow: hidden;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}
.chart-card:hover { border-color: var(--border-light); transform: translateY(-1px); }
.chart-card::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}
.chart-card h3 {
  font-family: var(--font-body); font-size: 16px; font-weight: 600;
  color: #ffffff; margin-bottom: 16px;
}
.chart-card canvas { width: 100% !important; }

.section-header {
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 12px; margin-top: 8px;
}
.section-header h2 {
  font-family: var(--font-body); font-size: 18px; font-weight: 600;
  color: #ffffff;
}
.section-header::after {
  content: ''; flex: 1; height: 1px;
  background: linear-gradient(90deg, var(--border-color), transparent);
}

.country-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.country-card {
  position: relative;
  background: var(--bg-mid);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 20px 22px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}
.country-card:hover { border-color: var(--border-light); transform: translateY(-1px); }
.country-card::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}
.country-card > div { overflow-x: auto; }
.country-card h3 {
  font-family: var(--font-body); font-size: 16px; font-weight: 600;
  color: #ffffff; margin-bottom: 14px;
}
.country-table { width: 100%; border-collapse: collapse; }
.country-table tr {
  border-bottom: 1px solid rgba(42,71,94,0.4); transition: background 0.2s;
}
.country-table tr:hover { background: var(--review-hover); }
.country-table td { padding: 7px 0; font-size: 13px; }
.country-table .cc { font-weight: 600; color: var(--text-secondary); width: 100px; }
.country-table .bar-cell { font-family: var(--font-mono); font-size: 11px; color: var(--accent); letter-spacing: -0.05em; }
.country-table .val { text-align: right; font-family: var(--font-mono); font-weight: 500; color: var(--text-primary); width: 60px; }

.reviews-grid { display: grid; grid-template-columns: 1fr; gap: 10px; }
.review-card {
  background: var(--bg-mid);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 18px 22px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}
.review-card:hover { border-color: var(--border-light); transform: translateY(-1px); }
.review-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.review-thumb {
  font-size: 18px; width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 4px; flex-shrink: 0;
}
.review-thumb.up { background: rgba(92,126,16,0.2); }
.review-thumb.down { background: rgba(196,90,90,0.2); }
.review-game {
  font-size: 11px; color: var(--accent); font-weight: 600;
  background: var(--accent-fill); padding: 1px 6px; border-radius: 2px;
  margin-right: 4px;
}
.review-author { font-weight: 600; font-size: 13px; color: var(--text-secondary); }
.review-playtime { margin-left: auto; font-size: 12px; font-family: var(--font-mono); color: var(--text-tertiary); }
.review-text {
  font-size: 13.5px; line-height: 1.7; color: var(--text-secondary);
  max-height: 80px; overflow: hidden;
  -webkit-mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
  mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
}

.discussions-grid { display: grid; grid-template-columns: 1fr; gap: 10px; }
.discussion-card {
  background: var(--bg-mid);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 18px 22px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}
.discussion-card:hover { border-color: var(--border-light); transform: translateY(-1px); }
.discussion-header {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 10px; margin-bottom: 6px;
}
.discussion-title {
  color: var(--text-secondary); text-decoration: none;
  font-weight: 600; font-size: 13.5px; line-height: 1.4;
}
.discussion-title:hover { color: var(--accent); text-decoration: underline; }
.discussion-replies {
  font-size: 11px; font-family: var(--font-mono); color: var(--text-tertiary);
  white-space: nowrap; flex-shrink: 0; padding-top: 2px;
}
.discussion-meta { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.discussion-reply {
  background: var(--bg-deep);
  border: 1px solid var(--border-color); border-radius: var(--radius-sm);
  padding: 10px 14px; margin-top: 12px;
}
.discussion-reply-header { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.discussion-reply-label {
  font-size: 10px; font-family: var(--font-mono); color: var(--text-tertiary);
  text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px;
}

.warning-banner {
  display: none;
  background: rgba(232,167,53,0.15);
  border-bottom: 1px solid rgba(232,167,53,0.3);
  color: #e8a735;
  font-size: 12px;
  padding: 6px 16px;
  text-align: center;
  font-family: var(--font-mono);
}
.warning-banner.visible { display: block; }

.status-bar {
  position: fixed; bottom: 0; left: 0; right: 0;
  background: var(--status-bg);
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  padding: 8px 24px;
  display: flex; align-items: center; gap: 20px;
  font-size: 11px; font-family: var(--font-mono); color: var(--text-tertiary);
  border-top: 1px solid var(--border-color); z-index: 100;
}
.status-bar .dot {
  display: inline-block; width: 6px; height: 6px;
  border-radius: 50%; margin-right: 4px; vertical-align: middle;
}
.status-bar .dot.on { background: var(--green-bright); box-shadow: 0 0 4px rgba(164,208,7,0.4); }
.status-bar .dot.off { background: var(--red); }

@keyframes shimmer {
  0% { background-position: -200px 0; }
  100% { background-position: 200px 0; }
}
.metric-value.loading {
  background: linear-gradient(90deg, var(--shimmer-a) 0%, var(--shimmer-b) 40%, var(--shimmer-a) 80%);
  background-size: 400px 100%; animation: shimmer 1.8s ease-in-out infinite;
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}

.metric-card, .chart-card, .country-card, .review-card, .discussion-card { animation: fadeUp 0.5s var(--ease-out) both; }
@keyframes fadeUp {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}
.metrics-grid .metric-card:nth-child(1) { animation-delay: 0.05s; }
.metrics-grid .metric-card:nth-child(2) { animation-delay: 0.1s; }
.metrics-grid .metric-card:nth-child(3) { animation-delay: 0.15s; }
.metrics-grid .metric-card:nth-child(4) { animation-delay: 0.2s; }
.metrics-grid .metric-card:nth-child(5) { animation-delay: 0.225s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(1) { animation-delay: 0.25s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(2) { animation-delay: 0.3s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(3) { animation-delay: 0.35s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(4) { animation-delay: 0.4s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(5) { animation-delay: 0.45s; }

@media (max-width: 1024px) {
  .metrics-grid { grid-template-columns: repeat(2, 1fr); }
  .charts-row { grid-template-columns: 1fr; }
  .country-grid { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  .header { padding: 20px 20px; gap: 16px; }
  .header-img { width: 140px; }
  .header-info h1 { font-size: 24px; }
  .dashboard { padding: 20px 16px 72px; }
  .chart-card canvas { min-height: 160px; }
  .country-table .cc { width: 70px; font-size: 12px; }
}
@media (max-width: 640px) {
  .header { flex-direction: column; align-items: flex-start; padding: 16px; gap: 14px; }
  .header-img { width: 100%; max-width: none; height: auto; max-height: 160px; object-fit: cover; border-radius: var(--radius-sm); }
  .header-info h1 { font-size: 22px; }
  .header-controls { margin-left: 0; display: flex; align-items: flex-start; gap: 8px; width: 100%; }
  .poll-info { display: none; }
  .dashboard { padding: 14px 10px 72px; }
  .metrics-grid { grid-template-columns: 1fr 1fr; gap: 10px; }
  .metric-card { padding: 14px 16px; }
  .metric-value { font-size: 24px; }
  .metric-label { font-size: 10px; }
  .metric-sub { font-size: 11px; }
  .chart-card { padding: 16px 14px; }
  .chart-card canvas { min-height: 150px; }
  .section-header { padding: 0 4px; }
  .section-header h2 { font-size: 16px; }
  .review-card { padding: 14px 16px; }
  .status-bar { padding: 6px 12px; gap: 10px; font-size: 10px; }
  .status-bar span:nth-child(1) { display: none; }
}
@media (max-width: 380px) {
  .metrics-grid { grid-template-columns: 1fr; }
  .header-img { max-height: 120px; }
}
</style>
</head>
<body>
<div class="header">
  <img id="headerImg" class="header-img" src="" alt="" />
  <div class="header-info">
    <h1 id="gameName">Loading...</h1>
    <div class="subtitle" id="gameDev"></div>
    <div class="price-badge" id="gamePrice"></div>
  </div>
  <div class="header-controls">
    <div class="live-indicator"><span class="live-dot"></span>LIVE</div>
    <div class="update-time" id="lastUpdate">--</div>
    <div class="poll-info">5min poll &middot; 30s refresh</div>
    <div class="header-buttons">
      <a class="settings-btn" href="/settings" title="Settings">\u2699</a>
    </div>
    <div class="game-selector" id="gameSelector"></div>
  </div>
</div>

<div class="warning-banner" id="warningBanner"></div>
<div class="dashboard">
  <div class="metrics-grid sales-only">
    <div class="metric-card">
      <div class="metric-label">Total Sales</div>
      <div class="metric-value gold loading" id="totalSales">--</div>
      <div class="metric-sub" id="salesSub"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Net Revenue</div>
      <div class="metric-value green loading" id="netRevenue">--</div>
      <div class="metric-sub" id="revenueSub"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Players Online</div>
      <div class="metric-value loading" id="currentPlayers">--</div>
      <div class="metric-sub" id="playerChange"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Peak Players</div>
      <div class="metric-value loading" id="peakPlayers">--</div>
      <div class="metric-sub">Session high</div>
    </div>
  </div>
  <div class="metrics-grid">
    <div class="metric-card sales-only">
      <div class="metric-label">Reviews</div>
      <div class="metric-value loading" id="totalReviews">--</div>
      <div class="metric-sub" id="reviewRatio"></div>
    </div>
    <div class="metric-card sales-only">
      <div class="metric-label">Positive Rate</div>
      <div class="metric-value green loading" id="positiveRate">--</div>
      <div class="metric-sub" id="reviewScore"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Wishlists</div>
      <div class="metric-value loading" id="wishlistNet">--</div>
      <div class="metric-sub" id="wishlistSub"></div>
    </div>
    <div class="metric-card" id="followersCard">
      <div class="metric-label" id="followersLabel">Followers</div>
      <div class="metric-value loading" id="followerCount">--</div>
      <div class="metric-sub" id="followerSub"></div>
    </div>
    <div class="metric-card sales-only">
      <div class="metric-label">Refund Rate</div>
      <div class="metric-value loading" id="refundRate">--</div>
      <div class="metric-sub">returns / sales</div>
    </div>
  </div>
  <div class="section-header sales-only"><h2>Sales Performance</h2></div>
  <div id="cumChartsRow" class="charts-grid sales-only">
    <div class="chart-card">
      <h3 id="cumSalesTitle">Cumulative Sales &amp; Revenue</h3>
      <canvas id="salesTimelineChart" height="180"></canvas>
    </div>
    <div class="chart-card" id="cumRevenueCard" style="display:none;">
      <h3>Cumulative Revenue</h3>
      <canvas id="revenueTimelineChart" height="180"></canvas>
    </div>
  </div>
  <div class="charts-row sales-only">
    <div class="chart-card">
      <h3>Daily Sales &amp; Revenue</h3>
      <canvas id="salesChart" height="220"></canvas>
    </div>
    <div class="chart-card">
      <h3>Player Activity</h3>
      <canvas id="playerChart" height="220"></canvas>
    </div>
  </div>
  <div id="wishlistChartsRow" class="charts-row">
    <div class="chart-card">
      <h3>Wishlist Activity</h3>
      <canvas id="wishlistChart" height="180"></canvas>
    </div>
    <div class="chart-card" id="wishlistStackedCard" style="display:none;">
      <h3>Cumulative Wishlists by Game</h3>
      <canvas id="wishlistStackedChart" height="180"></canvas>
    </div>
    <div class="chart-card" id="followerChartCard">
      <h3>Follower Growth</h3>
      <canvas id="followerChart" height="180"></canvas>
    </div>
  </div>
  <div class="section-header"><h2>Geographic Breakdown</h2></div>
  <div class="country-grid">
    <div class="country-card sales-only">
      <h3>Sales by Country</h3>
      <div id="salesByCountry"></div>
    </div>
    <div class="country-card">
      <h3>Wishlists by Country</h3>
      <div id="wishlistByCountry"></div>
    </div>
  </div>
  <div class="section-header sales-only"><h2>Recent Reviews</h2></div>
  <div class="reviews-grid sales-only" id="recentReviews"></div>
  <div class="section-header" id="discussionsSectionHeader"><h2>Recent Discussions</h2></div>
  <div class="discussions-grid" id="recentDiscussions"></div>
</div>

<div class="status-bar">
  <span>App ID: <span id="statusAppId">{{DEFAULT_APP_ID}}</span></span>
  <span>Poll: {{POLL_INTERVAL}}s</span>
  <span id="collectorStatus" style="color:var(--accent);"></span>
  <span>Telegram: <span class="dot" id="tgDot"></span> <span id="tgStatus"></span></span>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
(function() {
  var rootEl = document.documentElement;
  rootEl.setAttribute('data-theme', '{{THEME}}');

  var playerChart, salesChart, salesTimelineChart, revenueTimelineChart, wishlistChart, wishlistStackedChart, followerChart;
  var currentAppId = localStorage.getItem('selectedGame') || '{{DEFAULT_APP_ID}}';
  var allGames = {{GAMES_JSON}};
  var isPortfolioMode = (currentAppId === '__all__');

  // Show game selector if multiple games
  if (allGames.length > 1) {
    var sel = document.getElementById('gameSelector');
    sel.classList.add('visible');

    // Add "All Games" tab first
    var allBtn = document.createElement('button');
    allBtn.className = 'game-tab' + (currentAppId === '__all__' ? ' active' : '');
    allBtn.textContent = 'All Games';
    allBtn.setAttribute('data-appid', '__all__');
    allBtn.onclick = function() { switchGame('__all__'); };
    sel.appendChild(allBtn);

    allGames.forEach(function(g) {
      var btn = document.createElement('button');
      btn.className = 'game-tab' + (g.app_id === currentAppId ? ' active' : '');
      btn.textContent = g.name || g.app_id;
      btn.setAttribute('data-appid', g.app_id);
      btn.onclick = function() { switchGame(g.app_id); };
      sel.appendChild(btn);
    });
  }

  function switchGame(appId) {
    currentAppId = appId;
    localStorage.setItem('selectedGame', appId);
    isPortfolioMode = (appId === '__all__');
    document.getElementById('statusAppId').textContent = isPortfolioMode ? 'All Games' : appId;
    document.querySelectorAll('.game-tab').forEach(function(btn) {
      btn.classList.toggle('active', btn.getAttribute('data-appid') === appId);
    });
    document.querySelectorAll('.metric-value').forEach(function(el) { el.classList.add('loading'); });
    rebuildCharts();
    fetchData();
  }

  function getChartColors() {
    var cs = getComputedStyle(rootEl);
    return {
      gold: cs.getPropertyValue('--accent').trim() || '#66c0f4',
      goldFill: cs.getPropertyValue('--accent-fill').trim() || 'rgba(102,192,244,0.08)',
      green: '#e8a735',
      greenFill: 'rgba(232,167,53,0.08)',
      red: '#c45a5a',
      purple: '#66c0f4',
      purpleFill: 'rgba(102,192,244,0.08)',
      grid: cs.getPropertyValue('--chart-grid').trim() || 'rgba(42,71,94,0.4)',
      tick: cs.getPropertyValue('--chart-tick').trim() || '#556772',
      legend: cs.getPropertyValue('--chart-legend').trim() || '#8f98a0',
      tooltipBg: cs.getPropertyValue('--tooltip-bg').trim() || 'rgba(22,32,45,0.97)',
      tooltipBorder: cs.getPropertyValue('--tooltip-border').trim() || 'rgba(102,192,244,0.2)'
    };
  }

  // Every calendar day between two dates, inclusive.
  //
  // Chart.js category axes space labels evenly, so listing only the dates that
  // have readings would draw a multi-week gap as a single step and make the
  // line look near-vertical. Follower history has exactly that shape: an
  // imported series ends weeks before local collection begins. Emitting every
  // day keeps the spacing proportional, so a gap reads as the long slow span it
  // actually was. Uses UTC arithmetic so a DST boundary cannot skip a day.
  function dailyRange(first, last) {
    var out = [];
    var d = new Date(first + 'T00:00:00Z');
    var end = new Date(last + 'T00:00:00Z');
    if (isNaN(d) || isNaN(end)) return out;
    while (d <= end) {
      out.push(d.toISOString().slice(0, 10));
      d.setUTCDate(d.getUTCDate() + 1);
    }
    return out;
  }

  var gameColors = [
    { border: '#66c0f4', fill: 'rgba(102,192,244,0.3)' },
    { border: '#d667a3', fill: 'rgba(214,103,163,0.3)' },
    { border: '#c45a5a', fill: 'rgba(196,90,90,0.3)' },
    { border: '#c9a84c', fill: 'rgba(201,168,76,0.3)' },
    { border: '#7a5aaa', fill: 'rgba(122,90,170,0.3)' },
    { border: '#5ac4c4', fill: 'rgba(90,196,196,0.3)' },
    { border: '#e07850', fill: 'rgba(224,120,80,0.3)' },
    { border: '#50b050', fill: 'rgba(80,176,80,0.3)' }
  ];

  function rebuildCharts() {
    if (salesTimelineChart) salesTimelineChart.destroy();
    if (revenueTimelineChart) revenueTimelineChart.destroy();
    if (salesChart) salesChart.destroy();
    if (playerChart) playerChart.destroy();
    if (wishlistChart) wishlistChart.destroy();
    if (wishlistStackedChart) wishlistStackedChart.destroy();
    if (followerChart) followerChart.destroy();
    initCharts();
  }

  function initCharts() {
    var cc = getChartColors();
    var isMobile = window.innerWidth <= 768;
    var pr = 0;
    var phr = isMobile ? 2 : 3;
    var baseScaleX = {
      ticks: { color: cc.tick, maxTicksLimit: 12, font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: cc.grid, lineWidth: 0.5 }, border: { display: false }
    };
    var baseScaleY = {
      ticks: { color: cc.tick, font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: cc.grid, lineWidth: 0.5 }, border: { display: false }, beginAtZero: true
    };
    var baseTooltip = {
      backgroundColor: cc.tooltipBg, borderColor: cc.tooltipBorder, borderWidth: 1,
      titleFont: { family: "'Noto Sans'", weight: '600' },
      bodyFont: { family: "'JetBrains Mono'", size: 12 },
      padding: 12, cornerRadius: 4, displayColors: true, boxPadding: 4
    };
    var baseOpts = {
      responsive: true,
      animation: { duration: 500, easing: 'easeOutQuart' },
      interaction: { mode: 'index', intersect: false }
    };
    var legendCfg = { display: true, labels: { color: cc.legend, usePointStyle: true, pointStyle: 'circle', padding: 16, font: { family: "'Noto Sans'", size: 12 } } };

    if (isPortfolioMode) {
      // Stacked cumulative sales
      salesTimelineChart = new Chart(document.getElementById('salesTimelineChart'), {
        type: 'line',
        data: { labels: [], datasets: [] },
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: Object.assign({}, baseScaleX, { ticks: Object.assign({}, baseScaleX.ticks, { maxTicksLimit: 20 }) }),
            y: Object.assign({}, baseScaleY, { stacked: true })
          }
        })
      });

      // Stacked cumulative revenue
      revenueTimelineChart = new Chart(document.getElementById('revenueTimelineChart'), {
        type: 'line',
        data: { labels: [], datasets: [] },
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: Object.assign({}, baseScaleX, { ticks: Object.assign({}, baseScaleX.ticks, { maxTicksLimit: 20 }) }),
            y: Object.assign({}, baseScaleY, { stacked: true })
          }
        })
      });

      // Stacked daily sales (bars) + combined revenue (line)
      salesChart = new Chart(document.getElementById('salesChart'), {
        type: 'bar',
        data: { labels: [], datasets: [] },
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: Object.assign({}, baseScaleX, { stacked: true }),
            y: Object.assign({}, baseScaleY, { stacked: true, position: 'left', title: { display: !isMobile, text: 'Units', color: cc.tick, font: { family: "'Noto Sans'", size: 11 } } }),
            y1: Object.assign({}, baseScaleY, { position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: 'Revenue ($)', color: cc.tick, font: { family: "'Noto Sans'", size: 11 } } })
          }
        })
      });

      // Stacked player activity
      playerChart = new Chart(document.getElementById('playerChart'), {
        type: 'line',
        data: { labels: [], datasets: [] },
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: baseScaleX,
            y: Object.assign({}, baseScaleY, { stacked: true })
          }
        })
      });
    } else {
      salesTimelineChart = new Chart(document.getElementById('salesTimelineChart'), {
        type: 'line',
        data: { labels: [], datasets: [
          { label: 'Cumulative Sales', data: [], borderColor: cc.gold, backgroundColor: cc.goldFill, fill: true, tension: 0.35, pointRadius: pr, pointHoverRadius: phr, pointBackgroundColor: cc.gold, pointBorderColor: 'transparent', borderWidth: 2.5, yAxisID: 'y' },
          { label: 'Net Revenue ($)', data: [], borderColor: cc.green, backgroundColor: 'transparent', borderDash: [6, 4], tension: 0.35, pointRadius: pr, pointHoverRadius: phr, pointBackgroundColor: cc.green, pointBorderColor: 'transparent', borderWidth: 2, yAxisID: 'y1' }
        ]},
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: Object.assign({}, baseScaleX, { ticks: Object.assign({}, baseScaleX.ticks, { maxTicksLimit: 20 }) }),
            y: Object.assign({}, baseScaleY, { position: 'left', title: { display: !isMobile, text: 'Sales', color: cc.tick, font: { family: "'Noto Sans'", size: 11 } } }),
            y1: Object.assign({}, baseScaleY, { position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: 'Revenue ($)', color: cc.tick, font: { family: "'Noto Sans'", size: 11 } } })
          }
        })
      });
      revenueTimelineChart = null;

      salesChart = new Chart(document.getElementById('salesChart'), {
        type: 'bar',
        data: { labels: [], datasets: [
          { label: 'Sales', data: [], backgroundColor: cc.gold, borderRadius: 2, yAxisID: 'y', order: 2, barPercentage: 0.7 },
          { label: 'Refunds', data: [], backgroundColor: cc.red, borderRadius: 2, yAxisID: 'y', order: 3, barPercentage: 0.7 },
          { label: 'Net Revenue ($)', data: [], type: 'line', borderColor: cc.green, backgroundColor: 'transparent', borderWidth: 2, pointRadius: Math.max(1, pr - 1), pointHoverRadius: Math.max(2, phr - 1), pointBackgroundColor: cc.green, pointBorderColor: 'transparent', tension: 0.35, yAxisID: 'y1', order: 1 }
        ]},
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: baseScaleX,
            y: Object.assign({}, baseScaleY, { position: 'left', title: { display: !isMobile, text: 'Units', color: cc.tick, font: { family: "'Noto Sans'", size: 11 } } }),
            y1: Object.assign({}, baseScaleY, { position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: 'Revenue ($)', color: cc.tick, font: { family: "'Noto Sans'", size: 11 } } })
          }
        })
      });

      playerChart = new Chart(document.getElementById('playerChart'), {
        type: 'line',
        data: { labels: [], datasets: [{
          label: 'Players', data: [],
          borderColor: cc.purple, backgroundColor: cc.purpleFill,
          fill: true, tension: 0.35, pointRadius: 0, pointHoverRadius: isMobile ? 2 : 4,
          pointBackgroundColor: cc.purple, pointBorderColor: 'transparent', borderWidth: 2
        }]},
        options: Object.assign({}, baseOpts, {
          plugins: { legend: { display: false }, tooltip: baseTooltip },
          scales: { x: baseScaleX, y: baseScaleY }
        })
      });
    }

    // Wishlist chart (cumulative + daily adds)
    wishlistChart = new Chart(document.getElementById('wishlistChart'), {
      type: 'bar',
      data: { labels: [], datasets: [
        { label: 'Adds', data: [], backgroundColor: cc.gold, borderRadius: 2, yAxisID: 'y', order: 2, barPercentage: 0.7 },
        { label: 'Deletes', data: [], backgroundColor: cc.red, borderRadius: 2, yAxisID: 'y', order: 3, barPercentage: 0.7 },
        { label: 'Cumulative', data: [], type: 'line', borderColor: cc.green, backgroundColor: 'transparent', borderWidth: 2, pointRadius: 0, pointHoverRadius: isMobile ? 2 : 4, pointBackgroundColor: cc.green, pointBorderColor: 'transparent', tension: 0.35, yAxisID: 'y1', order: 1 }
      ]},
      options: Object.assign({}, baseOpts, {
        plugins: { legend: legendCfg, tooltip: baseTooltip },
        scales: {
          x: baseScaleX,
          y: Object.assign({}, baseScaleY, { position: 'left' }),
          y1: Object.assign({}, baseScaleY, { position: 'right', grid: { drawOnChartArea: false } })
        }
      })
    });

    if (isPortfolioMode) {
      // Stacked cumulative wishlists by game
      wishlistStackedChart = new Chart(document.getElementById('wishlistStackedChart'), {
        type: 'line',
        data: { labels: [], datasets: [] },
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: Object.assign({}, baseScaleX, { ticks: Object.assign({}, baseScaleX.ticks, { maxTicksLimit: 20 }) }),
            y: Object.assign({}, baseScaleY, { stacked: true })
          }
        })
      });
    } else {
      wishlistStackedChart = null;
    }

    // Follower growth. Single shared y-axis: a second axis for the studio
    // series would obscure that these measure different things.
    followerChart = new Chart(document.getElementById('followerChart'), {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: Object.assign({}, baseOpts, {
        plugins: { legend: isPortfolioMode ? legendCfg : { display: false }, tooltip: baseTooltip },
        scales: {
          x: Object.assign({}, baseScaleX, { ticks: Object.assign({}, baseScaleX.ticks, { maxTicksLimit: 20 }) }),
          y: Object.assign({}, baseScaleY, { beginAtZero: false })
        }
      })
    });
  }

  function updatePortfolioCharts(data) {
    var perGame = data.per_game || {};
    var gameIds = Object.keys(perGame);
    var isMobile = window.innerWidth <= 768;

    // Collect all unique dates across all games
    var allDates = {};
    gameIds.forEach(function(id) {
      (perGame[id].daily_sales || []).forEach(function(r) {
        allDates[r[0]] = true;
      });
    });
    var sortedDates = Object.keys(allDates).sort();
    var labels = sortedDates;

    // Cumulative sales & revenue (stacked area)
    var cumSalesDatasets = [];
    var cumRevDatasets = [];
    gameIds.forEach(function(id, idx) {
      var color = gameColors[idx % gameColors.length];
      var dailyByDate = {};
      (perGame[id].daily_sales || []).forEach(function(r) { dailyByDate[r[0]] = r; });

      var cumUnits = 0, cumNet = 0;
      var unitsArr = [], netArr = [];
      sortedDates.forEach(function(date) {
        var row = dailyByDate[date];
        if (row) { cumUnits += row[1]; cumNet += row[4]; }
        unitsArr.push(cumUnits);
        netArr.push(Math.round(cumNet * 100) / 100);
      });

      cumSalesDatasets.push({
        label: perGame[id].name,
        data: unitsArr, borderColor: color.border, backgroundColor: color.fill,
        fill: true, tension: 0.35, pointRadius: 0,
        pointHoverRadius: isMobile ? 2 : 4, pointBackgroundColor: color.border,
        pointBorderColor: 'transparent', borderWidth: 2
      });
      cumRevDatasets.push({
        label: perGame[id].name,
        data: netArr, borderColor: color.border, backgroundColor: color.fill,
        fill: true, tension: 0.35, pointRadius: 0,
        pointHoverRadius: isMobile ? 2 : 4, pointBackgroundColor: color.border,
        pointBorderColor: 'transparent', borderWidth: 2
      });
    });

    salesTimelineChart.data.labels = labels;
    salesTimelineChart.data.datasets = cumSalesDatasets;
    salesTimelineChart.update('none');

    revenueTimelineChart.data.labels = labels;
    revenueTimelineChart.data.datasets = cumRevDatasets;
    revenueTimelineChart.update('none');

    // Daily sales (stacked bar) + combined revenue (line)
    var dailyBarDatasets = [];
    var combinedRevArr = sortedDates.map(function() { return 0; });

    gameIds.forEach(function(id, idx) {
      var color = gameColors[idx % gameColors.length];
      var dailyByDate = {};
      (perGame[id].daily_sales || []).forEach(function(r) { dailyByDate[r[0]] = r; });

      var unitsArr = [];
      sortedDates.forEach(function(date, di) {
        var row = dailyByDate[date];
        unitsArr.push(row ? row[1] : 0);
        if (row) combinedRevArr[di] += row[4];
      });

      dailyBarDatasets.push({
        label: perGame[id].name,
        data: unitsArr, backgroundColor: color.fill, borderColor: color.border,
        borderWidth: 1, borderRadius: 2, yAxisID: 'y', stack: 'sales'
      });
    });

    dailyBarDatasets.push({
      label: 'Net Revenue ($)',
      data: combinedRevArr.map(function(v) { return Math.round(v * 100) / 100; }),
      type: 'line', borderColor: getChartColors().green, backgroundColor: 'transparent',
      borderWidth: 2, pointRadius: 0, pointHoverRadius: isMobile ? 2 : 4,
      pointBackgroundColor: getChartColors().green, pointBorderColor: 'transparent',
      tension: 0.35, yAxisID: 'y1', order: 0
    });

    salesChart.data.labels = labels;
    salesChart.data.datasets = dailyBarDatasets;
    salesChart.update('none');

    // Player activity (stacked area)
    var allTimestamps = {};
    gameIds.forEach(function(id) {
      (perGame[id].player_history || []).forEach(function(r) { allTimestamps[r[0]] = true; });
    });
    var sortedTimestamps = Object.keys(allTimestamps).sort();
    var playerLabels = sortedTimestamps.map(function(ts) {
      var d = new Date(ts);
      return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
    });

    var playerDatasets = [];
    gameIds.forEach(function(id, idx) {
      var color = gameColors[idx % gameColors.length];
      var histByTs = {};
      (perGame[id].player_history || []).forEach(function(r) { histByTs[r[0]] = r[1]; });

      var playerArr = sortedTimestamps.map(function(ts) { return histByTs[ts] || 0; });

      playerDatasets.push({
        label: perGame[id].name,
        data: playerArr, borderColor: color.border, backgroundColor: color.fill,
        fill: true, tension: 0.35, pointRadius: 0,
        pointHoverRadius: isMobile ? 2 : 4, pointBackgroundColor: color.border,
        pointBorderColor: 'transparent', borderWidth: 2
      });
    });

    playerChart.data.labels = playerLabels;
    playerChart.data.datasets = playerDatasets;
    playerChart.update('none');

    // Cumulative wishlists by game (stacked area)
    if (wishlistStackedChart) {
      var allWlDates = {};
      gameIds.forEach(function(id) {
        (perGame[id].daily_wishlists || []).forEach(function(r) { allWlDates[r[0]] = true; });
      });
      var sortedWlDates = Object.keys(allWlDates).sort();

      var wlDatasets = [];
      gameIds.forEach(function(id, idx) {
        var color = gameColors[idx % gameColors.length];
        var byDate = {};
        (perGame[id].daily_wishlists || []).forEach(function(r) { byDate[r[0]] = r; });

        var cumNet = 0;
        var netArr = [];
        sortedWlDates.forEach(function(date) {
          var row = byDate[date];
          if (row) cumNet += row[1] - row[2] - row[3];
          netArr.push(cumNet);
        });

        wlDatasets.push({
          label: perGame[id].name,
          data: netArr, borderColor: color.border, backgroundColor: color.fill,
          fill: true, tension: 0.35, pointRadius: 0,
          pointHoverRadius: isMobile ? 2 : 4, pointBackgroundColor: color.border,
          pointBorderColor: 'transparent', borderWidth: 2
        });
      });

      wishlistStackedChart.data.labels = sortedWlDates;
      wishlistStackedChart.data.datasets = wlDatasets;
      wishlistStackedChart.update('none');
    }

    // Follower growth: one line per game plus the studio. The studio line is
    // dashed, heavier and legend-first so it cannot read as another game --
    // studio followers are independent of game followers, not a sum of them.
    if (followerChart) {
      var fcc = getChartColors();
      var studioFh = data.studio_follower_history || [];
      var allFDates = {};
      gameIds.forEach(function(id) {
        (perGame[id].follower_history || []).forEach(function(r) { allFDates[r[0]] = true; });
      });
      studioFh.forEach(function(r) { allFDates[r[0]] = true; });
      var presentFDates = Object.keys(allFDates).sort();
      var sortedFDates = presentFDates.length
        ? dailyRange(presentFDates[0], presentFDates[presentFDates.length - 1])
        : [];

      var fDatasets = [];

      if (data.studio_configured) {
        var studioByDate = {};
        studioFh.forEach(function(r) { studioByDate[r[0]] = r[1]; });
        fDatasets.push({
          label: data.studio_name || 'Studio',
          data: sortedFDates.map(function(d) {
            return studioByDate[d] === undefined ? null : studioByDate[d];
          }),
          borderColor: fcc.gold, backgroundColor: 'transparent',
          borderDash: [6, 3], borderWidth: 3, tension: 0.35,
          pointRadius: 0, pointHoverRadius: isMobile ? 2 : 4,
          pointBackgroundColor: fcc.gold, pointBorderColor: 'transparent',
          spanGaps: true
        });
      }

      gameIds.forEach(function(id, idx) {
        var color = gameColors[idx % gameColors.length];
        var byDate = {};
        (perGame[id].follower_history || []).forEach(function(r) { byDate[r[0]] = r[1]; });
        fDatasets.push({
          label: perGame[id].name,
          data: sortedFDates.map(function(d) {
            return byDate[d] === undefined ? null : byDate[d];
          }),
          borderColor: color.border, backgroundColor: 'transparent',
          borderWidth: 2, tension: 0.35,
          pointRadius: 0, pointHoverRadius: isMobile ? 2 : 4,
          pointBackgroundColor: color.border, pointBorderColor: 'transparent',
          spanGaps: true
        });
      });

      followerChart.data.labels = sortedFDates;
      followerChart.data.datasets = fDatasets;
      followerChart.update('none');
    }
  }

  function fetchData() {
    var url = isPortfolioMode
      ? '/api/data-all'
      : '/api/data?app_id=' + encodeURIComponent(currentAppId);
    fetch(url).then(function(resp) { return resp.json(); }).then(function(data) {
      // Toggle pre-launch (unreleased) layout
      var unreleased = !isPortfolioMode && !!data.unreleased;
      document.body.classList.toggle('unreleased', unreleased);

      // On All Games the wishlist row already holds two charts, so a third
      // across is too cramped for a multi-line follower series. Give it the
      // full width of its own line there. On a single game the stacked
      // wishlist card is hidden, so it pairs with Wishlist Activity instead.
      document.getElementById('followerChartCard')
              .classList.toggle('chart-span-full', isPortfolioMode);

      var priceEl = document.getElementById('gamePrice');

      // Header
      if (isPortfolioMode) {
        document.getElementById('gameName').textContent = 'All Games';
        document.getElementById('gameDev').textContent = allGames.length + ' games';
        document.getElementById('headerImg').src = '';
        document.getElementById('headerImg').style.display = 'none';
        priceEl.textContent = '';
        priceEl.classList.remove('prelaunch');
        priceEl.style.display = 'none';
        document.getElementById('cumRevenueCard').style.display = '';
        document.getElementById('cumChartsRow').className = 'charts-row sales-only';
        document.getElementById('cumSalesTitle').innerHTML = 'Cumulative Sales';
        document.getElementById('wishlistStackedCard').style.display = '';
      } else {
        priceEl.style.display = '';
        document.getElementById('cumRevenueCard').style.display = 'none';
        document.getElementById('cumChartsRow').className = 'charts-grid sales-only';
        document.getElementById('cumSalesTitle').innerHTML = 'Cumulative Sales &amp; Revenue';
        document.getElementById('wishlistStackedCard').style.display = 'none';
        var d = data.app_details || {};
        var fallback = (allGames.find(function(g) { return g.app_id === currentAppId; }) || {});
        document.getElementById('gameName').textContent = d.name || fallback.name || currentAppId;
        document.getElementById('gameDev').textContent = !((d.developers||[]).length||(d.publishers||[]).length) ? '' : (d.developers || []).join(', ') + ' \u00B7 ' + (d.publishers || []).join(', ');
        var headerImg = document.getElementById('headerImg');
        if (d.header_image) {
          headerImg.src = d.header_image;
          headerImg.style.display = '';
        } else {
          headerImg.removeAttribute('src');
          headerImg.style.display = 'none';
        }
        if (unreleased) {
          priceEl.textContent = 'Pre-Launch';
          priceEl.classList.add('prelaunch');
        } else {
          priceEl.classList.remove('prelaunch');
          priceEl.textContent = (d.price_overview && d.price_overview.final_formatted) || '';
        }
      }

      // Followers. Studio followers are a separate metric shown only on All
      // Games, never a sum of the per-game counts.
      var fCard = document.getElementById('followersCard');
      if (isPortfolioMode) {
        if (data.studio_configured) {
          fCard.style.display = '';
          document.getElementById('followersLabel').textContent = 'Studio Followers';
          document.getElementById('followerCount').textContent = (data.studio_followers === null || data.studio_followers === undefined) ? '--' : data.studio_followers.toLocaleString();
          document.getElementById('followerSub').textContent = data.studio_name || '';
        } else {
          fCard.style.display = 'none';
        }
      } else {
        fCard.style.display = '';
        document.getElementById('followersLabel').textContent = 'Followers';
        document.getElementById('followerCount').textContent = (data.followers === null || data.followers === undefined) ? '--' : data.followers.toLocaleString();
        document.getElementById('followerSub').textContent = '';
      }
      document.querySelectorAll('.metric-value.loading').forEach(function(el) { el.classList.remove('loading'); });

      var s = isPortfolioMode ? (data.totals || {}) : (data.sales_totals || {});
      document.getElementById('totalSales').textContent = (s.units || 0).toLocaleString();
      document.getElementById('salesSub').textContent = 'refunds ' + (s.returns || 0) + ' \u00B7 gross $' + (s.gross || 0).toFixed(0);
      document.getElementById('netRevenue').textContent = '$' + (s.net || 0).toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 0});
      document.getElementById('revenueSub').textContent = 'before fees $' + (s.gross || 0).toFixed(0);
      document.getElementById('refundRate').textContent = (s.units > 0 ? ((s.returns / s.units) * 100).toFixed(1) : '0') + '%';

      var players = data.current_players || 0;
      document.getElementById('currentPlayers').textContent = players.toLocaleString();
      document.getElementById('peakPlayers').textContent = (data.peak_players || 0).toLocaleString();

      var hist = data.player_history || [];
      if (!isPortfolioMode) {
        if (hist.length > 1) {
          var prev = hist[hist.length - 2][1];
          var diff = players - prev;
          var el = document.getElementById('playerChange');
          el.textContent = diff > 0 ? '\u25B2 +' + diff : diff < 0 ? '\u25BC ' + diff : '\u2014 no change';
          el.style.color = diff > 0 ? 'var(--green-bright)' : diff < 0 ? 'var(--red)' : 'var(--text-tertiary)';
        }
      } else {
        document.getElementById('playerChange').textContent = allGames.length + ' games';
        document.getElementById('playerChange').style.color = 'var(--text-tertiary)';
      }

      // Charts
      if (isPortfolioMode) {
        updatePortfolioCharts(data);
      } else {
        var dailyForCum = data.daily_sales || [];
        var cumUnits = 0, cumNet = 0;
        var cumLabels = [], cumUnitsData = [], cumNetData = [];
        dailyForCum.forEach(function(r) {
          cumUnits += r[1];
          cumNet += r[4];
          cumLabels.push(r[0]);
          cumUnitsData.push(cumUnits);
          cumNetData.push(Math.round(cumNet * 100) / 100);
        });
        salesTimelineChart.data.labels = cumLabels;
        salesTimelineChart.data.datasets[0].data = cumUnitsData;
        salesTimelineChart.data.datasets[1].data = cumNetData;
        salesTimelineChart.update('none');

        var dailyRaw = data.daily_sales || [];
        var daily = dailyRaw;
        salesChart.data.labels = daily.map(function(r) { return r[0]; });
        salesChart.data.datasets[0].data = daily.map(function(r) { return r[1]; });
        salesChart.data.datasets[1].data = daily.map(function(r) { return -r[2]; });
        salesChart.data.datasets[2].data = daily.map(function(r) { return r[4]; });
        salesChart.update('none');

        playerChart.data.labels = hist.map(function(r) { var d = new Date(r[0]); return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0'); });
        playerChart.data.datasets[0].data = hist.map(function(r) { return r[1]; });
        playerChart.update('none');
      }

      // Wishlist chart (daily adds/deletes + cumulative line)
      var dw = data.daily_wishlists || [];
      var cumWl = 0;
      wishlistChart.data.labels = dw.map(function(r) { return r[0]; });
      wishlistChart.data.datasets[0].data = dw.map(function(r) { return r[1]; });
      wishlistChart.data.datasets[1].data = dw.map(function(r) { return -r[2]; });
      wishlistChart.data.datasets[2].data = dw.map(function(r) { cumWl += r[1] - r[2] - r[3]; return cumWl; });
      wishlistChart.update('none');

      if (followerChart && !isPortfolioMode) {
        var fcc = getChartColors();
        var isMobile = window.innerWidth <= 768;
        var fh = data.follower_history || [];
        var fByDate = {};
        fh.forEach(function(r) { fByDate[r[0]] = r[1]; });
        var fLabels = fh.length ? dailyRange(fh[0][0], fh[fh.length - 1][0]) : [];
        followerChart.data.labels = fLabels;
        followerChart.data.datasets = [{
          label: 'Followers',
          data: fLabels.map(function(d) {
            return fByDate[d] === undefined ? null : fByDate[d];
          }),
          spanGaps: true,
          borderColor: fcc.gold, backgroundColor: 'transparent',
          borderWidth: 2, tension: 0.35,
          pointRadius: 0, pointHoverRadius: isMobile ? 2 : 4,
          pointBackgroundColor: fcc.gold, pointBorderColor: 'transparent'
        }];
        followerChart.update('none');
      }

      var rev = data.reviews || {};
      var total = rev.total_reviews || 0, pos = rev.total_positive || 0, neg = rev.total_negative || 0;
      document.getElementById('totalReviews').textContent = total;
      document.getElementById('reviewRatio').innerHTML = String.fromCodePoint(0x1F44D) + ' ' + pos + ' / ' + String.fromCodePoint(0x1F44E) + ' ' + neg;
      document.getElementById('positiveRate').textContent = total > 0 ? Math.round(pos/total*100) + '%' : '--';
      document.getElementById('reviewScore').textContent = rev.review_score_desc || '';

      var wl = data.wishlist || {};
      document.getElementById('wishlistNet').textContent = '~' + (wl.net || 0).toLocaleString();
      document.getElementById('wishlistSub').textContent = '+' + (wl.adds||0) + ' / -' + (wl.deletes||0) + ' / conv. ' + (wl.purchases||0);

      var esc = function(str) { var d = document.createElement('div'); d.textContent = String(str); return d.innerHTML; };
      var renderCountryTable = function(obj, valFn) {
        var entries = Object.entries(obj).slice(0, 15);
        if (!entries.length) return '<div style="color:var(--text-tertiary);font-style:italic;padding:12px 0;">' + 'Collecting data...' + '</div>';
        var maxVal = Math.max(1, valFn(entries[0][1]));
        return '<table class="country-table">' + entries.map(function(entry) {
          var cc = esc(entry[0]); var d = entry[1]; var val = valFn(d);
          var pct = Math.round(val / maxVal * 100);
          return '<tr><td class="cc">' + cc + '</td><td class="bar-cell"><div style="background:linear-gradient(90deg, var(--accent), var(--accent-dim));width:' + pct + '%;height:7px;border-radius:2px;min-width:6px;box-shadow:0 0 6px var(--accent-glow);"></div></td><td class="val">' + val + '</td></tr>';
        }).join('') + '</table>';
      };
      document.getElementById('salesByCountry').innerHTML = renderCountryTable(data.sales_by_country || {}, function(d) { return d.units || 0; });
      document.getElementById('wishlistByCountry').innerHTML = renderCountryTable(data.wishlist_by_country || {}, function(d) { return d.adds || 0; });

      var recent = data.recent_reviews || [];
      document.getElementById('recentReviews').innerHTML = recent.map(function(r) {
        var isUp = r.voted_up;
        var thumb = isUp ? String.fromCodePoint(0x1F44D) : String.fromCodePoint(0x1F44E);
        var thumbClass = isUp ? 'up' : 'down';
        var playtime = Math.round((r.author && r.author.playtime_forever || 0) / 60 * 10) / 10;
        var reviewDate = '';
        if (r.timestamp_created) {
          var rd = new Date(r.timestamp_created * 1000);
          reviewDate = rd.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' }) + ' \u2014 ';
        }
        var text = esc((r.review || '').substring(0, 300)).split(String.fromCharCode(10)).join(' ');
        var gameTag = (isPortfolioMode && r.game_name) ? '<span class="review-game">' + esc(r.game_name) + '</span>' : '';
        return '<div class="review-card"><div class="review-header"><span class="review-thumb ' + thumbClass + '">' + thumb + '</span>' + gameTag + '<span class="review-author">' + esc(r.author && r.author.personaname || 'Anonymous') + '</span><span class="review-playtime">' + reviewDate + playtime + 'h' + '</span></div><div class="review-text">' + text + '</div></div>';
      }).join('');

      var discussions = data.discussions || [];
      var discHeader = document.getElementById('discussionsSectionHeader');
      var discGrid = document.getElementById('recentDiscussions');
      if (discGrid) {
        discGrid.innerHTML = discussions.length ? discussions.map(function(d) {
          var postedDate = d.posted_at
            ? new Date(d.posted_at * 1000).toLocaleDateString(undefined, {year:'numeric',month:'long',day:'numeric'})
            : '';
          var replyLabel = d.reply_count === 1 ? '1 reply' : (d.reply_count + ' replies');
          var gameTag = (isPortfolioMode && d.game_name)
            ? '<span class="review-game">' + esc(d.game_name) + '</span>' : '';
          var snippet = esc((d.opening_snippet || '').substring(0, 300)).split(String.fromCharCode(10)).join(' ');
          var replyHtml = '';
          if (d.latest_reply) {
            var rDate = d.latest_reply.posted_at
              ? new Date(d.latest_reply.posted_at * 1000).toLocaleDateString(undefined, {year:'numeric',month:'long',day:'numeric'})
              : '';
            var rSnippet = esc((d.latest_reply.snippet || '').substring(0, 300)).split(String.fromCharCode(10)).join(' ');
            replyHtml = '<div class="discussion-reply">'
              + '<div class="discussion-reply-label">Latest reply</div>'
              + '<div class="discussion-reply-header">'
              + '<span class="review-author">' + esc(d.latest_reply.author) + '</span>'
              + '<span class="review-playtime">' + rDate + '</span>'
              + '</div>'
              + '<div class="review-text">' + rSnippet + '</div>'
              + '</div>';
          }
          return '<div class="discussion-card" data-id="' + esc(d.id) + '">'
            + '<div class="discussion-header">'
            + '<span>' + gameTag + '<a href="' + esc(d.url) + '" target="_blank" class="discussion-title">' + esc(d.title) + '</a></span>'
            + '<span class="discussion-replies">' + replyLabel + '</span>'
            + '</div>'
            + '<div class="discussion-meta">'
            + '<span class="review-author">' + esc(d.author) + '</span>'
            + '<span class="review-playtime">' + postedDate + '</span>'
            + '</div>'
            + '<div class="review-text">' + snippet + '</div>'
            + replyHtml
            + '</div>';
        }).join('')
          : '<div style="color:var(--text-tertiary);font-style:italic;padding:12px 0;">No discussions yet.</div>';
      }

      document.getElementById('tgDot').className = 'dot ' + (data.telegram_active ? 'on' : 'off');
      document.getElementById('tgStatus').textContent = data.telegram_active ? 'ON' : 'OFF';
      document.getElementById('collectorStatus').textContent = data.collector_status || '';
      document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
      var banner = document.getElementById('warningBanner');
      if (data.warnings && data.warnings.length > 0) {
        banner.textContent = data.warnings[0];
        banner.classList.add('visible');
      } else {
        banner.classList.remove('visible');
      }
      fetchFailCount = 0;
      if (!hasInitialResize) { hasInitialResize = true; rebuildCharts(); fetchData(); }
    }).catch(function(e) { console.error('Fetch error:', e); fetchFailCount++; });
  }

  initCharts();

  var hasInitialResize = false;
  var fetchFailCount = 0;
  function fetchWithBackoff() {
    fetchData();
    var delay = Math.min(30000 * Math.pow(1.5, fetchFailCount), 300000);
    setTimeout(fetchWithBackoff, delay);
  }
  fetchWithBackoff();
})();
</script>
</body>
</html>'''


# ========== HTTP SERVER ==========

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length > 0:
            return self.rfile.read(length)
        return b''

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _html_response(self, html):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path in ('/', '/dashboard'):
            if not has_settings():
                html = SETUP_HTML_TEMPLATE.replace('{{EXISTING_SETTINGS_JSON}}', 'null')
                html = html.replace('{{PORT}}', '8081')
                self._html_response(html)
            else:
                self._html_response(self.server.dashboard_html)

        elif parsed.path == '/settings':
            settings = get_all_settings()
            html = SETUP_HTML_TEMPLATE.replace(
                '{{EXISTING_SETTINGS_JSON}}',
                json.dumps(settings, ensure_ascii=False)
            )
            dash = settings.get('dashboard', {})
            html = html.replace('{{PORT}}', str(dash.get('port', 8081)))
            self._html_response(html)

        elif parsed.path == '/api/test':
            api_key = params.get('api_key', [''])[0]
            financial_key = params.get('financial_key', [''])[0]
            app_ids_raw = params.get('app_ids', [''])[0]
            app_ids = [a.strip() for a in app_ids_raw.split(',') if a.strip()] if app_ids_raw else []
            studio_url = params.get('studio_url', [''])[0]

            if not api_key:
                self._json_response({'success': False, 'error': 'Missing api_key'})
                return
            if not app_ids:
                self._json_response({'success': False, 'error': 'Missing app_ids'})
                return

            results = []
            api_key_valid = False
            financial_key_valid = False

            # Validate each game. Released games go through the player-count API
            # (which also confirms the API key); unreleased games fall back to the
            # public store API since player-count returns errors for them.
            for app_id in app_ids:
                try:
                    player_data = fetch_json(
                        f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?key={api_key}&appid={app_id}",
                        "test_api"
                    )
                    player_ok = bool(player_data and "response" in player_data)
                    if player_ok:
                        api_key_valid = True

                    # Always check the store API to detect coming_soon and to validate unreleased games
                    details = fetch_json(
                        f"https://store.steampowered.com/api/appdetails?appids={app_id}",
                        "test_details"
                    )
                    name = ""
                    coming_soon = False
                    store_ok = False
                    if details and str(app_id) in details and details[str(app_id)].get("success"):
                        d = details[str(app_id)]["data"]
                        name = d.get("name", "")
                        coming_soon = bool(d.get("release_date", {}).get("coming_soon"))
                        store_ok = True

                    if player_ok or (store_ok and coming_soon):
                        results.append({"app_id": app_id, "name": name, "coming_soon": coming_soon, "success": True})
                    elif store_ok:
                        # Released per the store but player-count failed — likely an API key problem
                        results.append({"app_id": app_id, "name": name, "coming_soon": coming_soon, "success": False,
                                        "error": "API key invalid or app not accessible"})
                    else:
                        results.append({"app_id": app_id, "name": "", "coming_soon": False, "success": False,
                                        "error": "App not found on Steam"})
                except Exception as e:
                    results.append({"app_id": app_id, "name": "", "coming_soon": False, "success": False, "error": str(e)})

            # Test financial key if provided
            if financial_key:
                try:
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    fin_data = fetch_json(
                        f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetDetailedSales/v001/?key={financial_key}&date={today_str}&highwatermark_id=0",
                        "test_financial"
                    )
                    if fin_data and "response" in fin_data:
                        financial_key_valid = True
                except Exception:
                    pass

            # Studio follower page is optional, so it never gates overall success
            studio_result = None
            if studio_url:
                studio_count = get_studio_followers(studio_url)
                if studio_count is None:
                    studio_result = {'success': False,
                                     'error': 'Could not read a follower count from that page'}
                else:
                    studio_result = {'success': True, 'followers': studio_count}

            all_games_ok = all(r["success"] for r in results)
            self._json_response({
                'success': all_games_ok and api_key_valid,
                'api_key_valid': api_key_valid,
                'financial_key_valid': financial_key_valid,
                'studio': studio_result,
                'games': results
            })

        elif parsed.path == '/api/data':
            if not has_settings():
                self._json_response({'error': 'Not configured'}, 503)
                return

            settings = get_all_settings()
            games = settings.get('games', [])
            req_app_id = params.get('app_id', [''])[0]

            if not req_app_id and games:
                req_app_id = str(games[0]['app_id'])

            collector = self.server.collector
            gs = collector.get_state(req_app_id)

            # Lazily populate app_details on first view so a freshly-added game has a header
            # before the next collector cycle runs.
            if gs.cached_app_details is None:
                gs.cached_app_details = get_app_details(req_app_id)

            p_history = get_player_history(req_app_id)
            daily = get_all_daily_sales(req_app_id)
            timeline = get_sales_snapshots(req_app_id)
            totals = get_sales_totals(req_app_id)
            wl_history = get_wishlist_history(req_app_id)

            tg = settings.get('telegram', {})
            game_cfg = next((g for g in games if str(g.get('app_id')) == req_app_id), {})

            payload = {
                "unreleased": bool(game_cfg.get('unreleased', False)),
                "current_players": gs.cached_players,
                "peak_players": gs.peak_players,
                "reviews": gs.cached_reviews,
                "recent_reviews": gs.cached_recent_reviews,
                "app_details": gs.cached_app_details,
                "player_history": p_history,
                "daily_sales": daily,
                "sales_timeline": timeline,
                "sales_totals": {
                    "units": totals[0], "returns": totals[1],
                    "gross": totals[2], "net": totals[3]
                },
                "wishlist": gs.cached_wishlist,
                "wishlist_history": wl_history,
                "daily_wishlists": get_daily_wishlists(req_app_id),
                "sales_by_country": gs.cached_sales_by_country,
                "wishlist_by_country": gs.cached_wishlist_by_country,
                "telegram_active": bool(tg.get('enabled') and tg.get('bot_token') and tg.get('chat_ids')),
                "collector_status": collector.status,
                "warnings": ["Steam API rate limit detected. Backfill slowed."] if collector.throttled else [],
                "discussions": gs.cached_discussions,
                "followers": gs.cached_followers,
                "follower_history": get_follower_history(req_app_id),
                "timestamp": datetime.now().isoformat()
            }
            self._json_response(payload)

        elif parsed.path == '/api/data-all':
            if not has_settings():
                self._json_response({'error': 'Not configured'}, 503)
                return

            settings = get_all_settings()
            games = settings.get('games', [])
            collector = self.server.collector

            # Aggregated sales totals
            totals_row = get_all_games_sales_totals()
            totals = {
                "units": totals_row[0], "returns": totals_row[1],
                "gross": totals_row[2], "net": totals_row[3]
            }

            # Aggregate reviews, wishlists, players, per-game chart data
            agg_reviews = {"total_positive": 0, "total_negative": 0, "total_reviews": 0}
            agg_wishlist = {"net": 0, "adds": 0, "deletes": 0, "purchases": 0}
            total_players = 0
            total_peak = 0
            per_game = {}
            all_recent_reviews = []
            all_discussions = []
            merged_sales_by_country = {}
            merged_wl_by_country = {}

            for game in games:
                app_id = str(game['app_id'])
                game_name = game.get('name', app_id)
                gs = collector.get_state(app_id)

                # Players
                total_players += gs.cached_players
                total_peak += gs.peak_players

                # Reviews
                rev = gs.cached_reviews
                agg_reviews["total_positive"] += rev.get("total_positive", 0)
                agg_reviews["total_negative"] += rev.get("total_negative", 0)
                agg_reviews["total_reviews"] += rev.get("total_reviews", 0)

                # Wishlists
                wl = gs.cached_wishlist
                agg_wishlist["net"] += wl.get("net", 0)
                agg_wishlist["adds"] += wl.get("adds", 0)
                agg_wishlist["deletes"] += wl.get("deletes", 0)
                agg_wishlist["purchases"] += wl.get("purchases", 0)

                # Per-game chart data
                per_game[app_id] = {
                    "name": game_name,
                    "daily_sales": get_all_daily_sales(app_id),
                    "player_history": get_player_history(app_id),
                    "wishlist_history": get_wishlist_history(app_id),
                    "daily_wishlists": get_daily_wishlists(app_id),
                    "follower_history": get_follower_history(app_id)
                }

                # Recent reviews with game name
                recent = list(gs.cached_recent_reviews)
                for r in recent:
                    r["game_name"] = game_name
                all_recent_reviews.extend(recent)

                # Discussions with game name tagged
                for d in gs.cached_discussions:
                    d = dict(d)  # copy to avoid mutating cached state
                    d["game_name"] = game_name
                    all_discussions.append(d)

                # Merge sales by country
                for country, data in gs.cached_sales_by_country.items():
                    if country not in merged_sales_by_country:
                        merged_sales_by_country[country] = {}
                    for k, v in data.items():
                        merged_sales_by_country[country][k] = merged_sales_by_country[country].get(k, 0) + v

                # Merge wishlists by country
                for country, data in gs.cached_wishlist_by_country.items():
                    if country not in merged_wl_by_country:
                        merged_wl_by_country[country] = {}
                    for k, v in data.items():
                        merged_wl_by_country[country][k] = merged_wl_by_country[country].get(k, 0) + v

            # Aggregate daily wishlists across all games
            agg_daily_wl = {}
            for gid, gdata in per_game.items():
                for row in gdata.get("daily_wishlists", []):
                    d = row[0]
                    if d not in agg_daily_wl:
                        agg_daily_wl[d] = [d, 0, 0, 0]
                    agg_daily_wl[d][1] += row[1]
                    agg_daily_wl[d][2] += row[2]
                    agg_daily_wl[d][3] += row[3]
            agg_daily_wishlists = [agg_daily_wl[d] for d in sorted(agg_daily_wl.keys())]

            # Sort recent reviews by timestamp, limit to 20
            all_recent_reviews.sort(key=lambda r: r.get("timestamp_created", 0), reverse=True)
            all_recent_reviews = all_recent_reviews[:20]

            # Sort discussions by posted_at, limit to 20
            all_discussions.sort(key=lambda d: d.get("posted_at", 0), reverse=True)
            all_discussions = all_discussions[:20]

            # Sort country dicts by primary value descending
            merged_sales_by_country = dict(sorted(merged_sales_by_country.items(), key=lambda x: x[1].get("units", 0), reverse=True))
            merged_wl_by_country = dict(sorted(merged_wl_by_country.items(), key=lambda x: x[1].get("adds", 0), reverse=True))

            tg = settings.get('telegram', {})

            payload = {
                "totals": totals,
                "reviews": agg_reviews,
                "wishlist": agg_wishlist,
                "current_players": total_players,
                "peak_players": total_peak,
                "per_game": per_game,
                "daily_wishlists": agg_daily_wishlists,
                "sales_by_country": merged_sales_by_country,
                "wishlist_by_country": merged_wl_by_country,
                "recent_reviews": all_recent_reviews,
                "discussions": all_discussions,
                "studio_followers": collector.cached_studio_followers,
                "studio_name": settings.get('studio', {}).get('name', ''),
                "studio_configured": bool(settings.get('studio', {}).get('url', '')),
                "studio_follower_history": get_follower_history(STUDIO_APP_ID),
                "telegram_active": bool(tg.get('enabled') and tg.get('bot_token') and tg.get('chat_ids')),
                "collector_status": collector.status,
                "warnings": ["Steam API rate limit detected. Backfill slowed."] if collector.throttled else [],
                "timestamp": datetime.now().isoformat()
            }
            self._json_response(payload)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path in ('/api/setup', '/api/settings'):
            body = self._read_body()
            try:
                data = json.loads(body.decode('utf-8'))
            except Exception:
                self._json_response({'success': False, 'error': 'Invalid JSON'}, 400)
                return

            # Auto-fetch game names, launch dates, and pre-launch status.
            # Re-detect unreleased games every save so they flip to released on launch day.
            games = data.get('games', [])
            today_str = datetime.now().strftime("%Y-%m-%d")
            for g in games:
                if not g.get('app_id'):
                    continue
                missing_basics = not g.get('name') or not g.get('launch_date') or 'unreleased' not in g
                recheck_prelaunch = bool(g.get('unreleased'))
                if not missing_basics and not recheck_prelaunch:
                    continue
                name, launch_date, coming_soon = get_game_info_from_api(g['app_id'])
                if not g.get('name'):
                    g['name'] = name
                g['unreleased'] = coming_soon
                if coming_soon:
                    # Use today as placeholder; find_earliest_wishlist_date resolves real horizon via app_min_date
                    if not g.get('launch_date'):
                        g['launch_date'] = today_str
                else:
                    if launch_date:
                        # Released: prefer the real release date so sales backfill starts from the right day
                        g['launch_date'] = launch_date
                    elif not g.get('launch_date'):
                        g['launch_date'] = today_str

            data['games'] = games
            save_all_settings(data)

            # Rebuild dashboard HTML
            self.server.dashboard_html = build_dashboard_html()

            # Signal collector to re-read settings on next cycle
            print("[SETTINGS] Updated and saved.")

            self._json_response({'success': True})
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


# ========== HTML BUILDERS ==========

def build_dashboard_html():
    settings = get_all_settings()
    dash = settings.get('dashboard', {})
    games = settings.get('games', [])
    default_app_id = str(games[0]['app_id']) if games else ''

    html = DASHBOARD_HTML_TEMPLATE
    html = html.replace('{{THEME}}', dash.get('theme', 'dark'))
    html = html.replace('{{POLL_INTERVAL}}', str(dash.get('poll_interval', 300)))
    html = html.replace('{{DEFAULT_APP_ID}}', default_app_id)
    html = html.replace('{{GAMES_JSON}}', json.dumps(games, ensure_ascii=False))
    return html


# ========== MAIN ==========

def main():
    init_db()

    port = 8081
    configured = has_settings()

    if configured:
        settings = get_all_settings()
        dash = settings.get('dashboard', {})
        port = dash.get('port', 8081)
        games = settings.get('games', [])
        tg = settings.get('telegram', {})
        tg_on = bool(tg.get('enabled') and tg.get('bot_token') and tg.get('chat_ids'))
        tg_count = len(tg.get('chat_ids', [])) if tg_on else 0

        # Fetch first game name for banner
        game_name = games[0].get('name', games[0]['app_id']) if games else 'No games'
        game_count = len(games)

        print("=" * 50)
        print(f"  Steam Dashboard v{VERSION}")
        print("=" * 50)
        print(f"  Game:       {game_name}" + (f" (+{game_count - 1} more)" if game_count > 1 else ""))
        print(f"  App ID:     {games[0]['app_id']}" if games else "  App ID:     N/A")
        print(f"  Dashboard:  http://localhost:{port}")
        print(f"  Polling:    {dash.get('poll_interval', 300) // 60}min")
        print(f"  Telegram:   {'ON (' + str(tg_count) + ' recipients)' if tg_on else 'OFF'}")
        print(f"  Theme:      {dash.get('theme', 'dark')}")
        print("=" * 50)
    else:
        print("=" * 50)
        print(f"  Steam Dashboard v{VERSION}")
        print("=" * 50)
        print(f"  No config found. Starting setup wizard...")
        print(f"  Open http://localhost:{port} to configure.")
        print("=" * 50)

    # Create collector
    collector = DataCollector()

    # Build HTML
    dashboard_html = build_dashboard_html() if configured else ''

    # Start HTTP server
    server = ReusableHTTPServer(('0.0.0.0', port), DashboardHandler)
    server.collector = collector
    server.dashboard_html = dashboard_html

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"\n[READY] Dashboard at http://localhost:{port}")

    if configured:
        settings = get_all_settings()
        tg = settings.get('telegram', {})
        tg_on = bool(tg.get('enabled') and tg.get('bot_token') and tg.get('chat_ids'))

        # Send startup telegram for each game
        if tg_on:
            for game in settings.get('games', []):
                print(f"[INIT] Sending startup report for {game.get('name', game['app_id'])}...")
                send_startup_report(settings, game)

        # Start collector
        collector_thread = threading.Thread(target=collector.loop, daemon=True)
        collector_thread.start()
    else:
        # Wait for setup, then start collector
        def wait_for_setup():
            while not has_settings():
                time.sleep(2)
            print("\n[SETUP] Configuration saved! Starting data collection...")
            server.dashboard_html = build_dashboard_html()
            settings = get_all_settings()
            tg = settings.get('telegram', {})
            tg_on = bool(tg.get('enabled') and tg.get('bot_token') and tg.get('chat_ids'))
            if tg_on:
                for game in settings.get('games', []):
                    send_startup_report(settings, game)
            collector.loop()

        setup_waiter = threading.Thread(target=wait_for_setup, daemon=True)
        setup_waiter.start()

    # Keep main thread alive
    try:
        server_thread.join()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


def cli_import_followers(paths, app_id_override=None, dry_run=False):
    """Import historical follower rows from CSV files. Returns an exit code.

    Steam exposes no follower API and no history, so a series can only start the
    day collection starts. This lets a trusted external export (a SteamDB chart
    CSV, or hand-entered points) fill in the past. Scraped rows always win.
    """
    init_db()
    total_in = total_skip = 0
    failed = False

    for path in paths:
        app_id = app_id_override or app_id_from_csv_name(path)
        if not app_id:
            print(f"  [SKIP] {path}: cannot tell which app this is. "
                  f"Name it steamdb_chart_<appid>.csv or pass --app-id")
            failed = True
            continue
        try:
            with open(path, encoding='utf-8-sig') as f:
                rows, rejected = parse_follower_csv(f.read())
        except (OSError, ValueError) as e:
            print(f"  [SKIP] {path}: {e}")
            failed = True
            continue

        label = f"{app_id}"
        if not rows:
            print(f"  [SKIP] {path}: no usable rows")
            failed = True
            continue

        if dry_run:
            print(f"  [DRY] {label}: would import {len(rows)} rows "
                  f"{rows[0][0]} .. {rows[-1][0]} (values {rows[0][1]} -> {rows[-1][1]})")
            inserted = skipped = 0
        else:
            inserted, skipped = import_follower_history(app_id, rows)
            print(f"  [OK]  {label}: {inserted} inserted, {skipped} already present, "
                  f"{rows[0][0]} .. {rows[-1][0]} (values {rows[0][1]} -> {rows[-1][1]})")
        total_in += inserted
        total_skip += skipped

        for n, raw, why in rejected[:5]:
            print(f"        line {n} rejected ({why}): {raw}")
        if len(rejected) > 5:
            print(f"        ... and {len(rejected) - 5} more rejected rows")

    verb = "would import" if dry_run else "imported"
    print(f"\n{verb} {total_in} rows, {total_skip} left alone (already recorded)")
    return 1 if failed else 0


def cli_anchor_zero(app_id, day, dry_run=False):
    """Record a known-true zero: followers were 0 before a store page existed."""
    init_db()
    try:
        day = datetime.strptime(day, "%Y-%m-%d").date().isoformat()
    except ValueError:
        print(f"  bad date {day!r}, expected YYYY-MM-DD")
        return 1
    if dry_run:
        print(f"  [DRY] would anchor {app_id} at 0 on {day}")
        return 0
    inserted, skipped = import_follower_history(app_id, [(day, 0)])
    print(f"  anchored {app_id} at 0 on {day}"
          if inserted else f"  {app_id} already has a row for {day}, left alone")
    return 0


def _cli(argv):
    """Handle the maintenance subcommands. Returns None to fall through to main()."""
    dry = '--dry-run' in argv
    argv = [a for a in argv if a != '--dry-run']

    if '--import-followers' in argv:
        i = argv.index('--import-followers')
        rest = argv[i + 1:]
        override = None
        if '--app-id' in rest:
            j = rest.index('--app-id')
            override = rest[j + 1] if j + 1 < len(rest) else None
            rest = rest[:j] + rest[j + 2:]
        if not rest:
            print("usage: dashboard.py --import-followers <csv> [<csv>...] "
                  "[--app-id <id>] [--dry-run]")
            return 1
        return cli_import_followers(rest, override, dry)

    if '--anchor-zero' in argv:
        i = argv.index('--anchor-zero')
        rest = argv[i + 1:]
        if len(rest) < 2:
            print("usage: dashboard.py --anchor-zero <app_id> <YYYY-MM-DD> [--dry-run]")
            return 1
        return cli_anchor_zero(rest[0], rest[1], dry)

    return None


if __name__ == '__main__':
    code = _cli(sys.argv[1:])
    if code is None:
        main()
    else:
        sys.exit(code)
