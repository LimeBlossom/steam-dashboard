#!/usr/bin/env python3
"""
Steam Dashboard - Real-time sales monitoring for Steam games
https://github.com/xxx/steam-dashboard

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
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.parse import urlparse, parse_qs, quote
from datetime import datetime, timedelta

VERSION = "1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, 'steam_dashboard.db')
FINANCIAL_BASE = "https://partner.steam-api.com"

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
    c.execute('''CREATE TABLE IF NOT EXISTS daily_sales (
        app_id TEXT, date TEXT, units_sold INTEGER, units_returned INTEGER,
        gross_revenue_usd REAL, net_revenue_usd REAL,
        PRIMARY KEY (app_id, date)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sales_snapshots (
        app_id TEXT, timestamp TEXT, total_units INTEGER, total_returns INTEGER,
        total_net_usd REAL, PRIMARY KEY (app_id, timestamp)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS wishlist_history (
        app_id TEXT, timestamp TEXT, total_adds INTEGER, total_deletes INTEGER,
        total_purchases INTEGER, net_wishlists INTEGER
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
        'telegram': get_setting('telegram', {'enabled': False, 'bot_token': '', 'chat_ids': []}),
        'dashboard': get_setting('dashboard', {'port': 8081, 'poll_interval': 300, 'language': 'en', 'theme': 'dark', 'accent': 'wine'}),
    }


def save_all_settings(data):
    set_setting('steam_api_key', data.get('steam_api_key', ''))
    set_setting('steam_financial_key', data.get('steam_financial_key', ''))
    set_setting('games', data.get('games', []))
    set_setting('telegram', data.get('telegram', {'enabled': False, 'bot_token': '', 'chat_ids': []}))
    set_setting('dashboard', data.get('dashboard', {'port': 8081, 'poll_interval': 300, 'language': 'en', 'theme': 'dark', 'accent': 'wine'}))


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


def upsert_daily_sales(app_id, date_str, units, returns, gross, net):
    conn = get_conn()
    conn.execute("""INSERT INTO daily_sales VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(app_id, date) DO UPDATE SET
        units_sold=excluded.units_sold, units_returned=excluded.units_returned,
        gross_revenue_usd=excluded.gross_revenue_usd, net_revenue_usd=excluded.net_revenue_usd
    """, (str(app_id), date_str, units, returns, gross, net))
    conn.commit()
    conn.close()


def get_player_history(app_id, limit=144):
    conn = get_conn()
    rows = conn.execute("SELECT timestamp, player_count FROM player_history WHERE app_id=? ORDER BY timestamp DESC LIMIT ?", (str(app_id), limit)).fetchall()
    conn.close()
    return list(reversed(rows))


def get_all_daily_sales(app_id):
    conn = get_conn()
    rows = conn.execute("SELECT date, units_sold, units_returned, gross_revenue_usd, net_revenue_usd FROM daily_sales WHERE app_id=? ORDER BY date", (str(app_id),)).fetchall()
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
    row = conn.execute("SELECT COALESCE(SUM(units_sold),0), COALESCE(SUM(units_returned),0), COALESCE(SUM(gross_revenue_usd),0), COALESCE(SUM(net_revenue_usd),0) FROM daily_sales WHERE app_id=?", (str(app_id),)).fetchone()
    conn.close()
    return row


# ========== HTTP FETCH WITH BACKOFF ==========

_api_fail_counts = {}


def fetch_json(url, label="api"):
    global _api_fail_counts
    try:
        req = Request(url, headers={"User-Agent": "SteamDashboard/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        _api_fail_counts[label] = 0
        return data
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


def get_game_name_from_api(app_id):
    details = get_app_details(app_id)
    if details:
        return details.get("name", f"App {app_id}")
    return f"App {app_id}"


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


# ========== FINANCIAL API ==========

def fetch_sales_for_date(financial_key, app_id, date_str):
    app_id = str(app_id)
    units = 0
    returns = 0
    gross = 0.0
    net = 0.0
    hwm = 0

    while True:
        url = (f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetDetailedSales/v001/"
               f"?key={financial_key}&date={date_str}&highwatermark_id={hwm}")
        data = fetch_json(url, f"sales_{app_id}")
        if not data or "response" not in data:
            break
        resp = data["response"]
        for item in resp.get("results", []):
            if str(item.get("primary_appid", item.get("appid", ""))) == app_id:
                units += item.get("gross_units_sold", 0)
                returns += item.get("gross_units_returned", 0)
                gross += float(item.get("gross_sales_usd", 0))
                net += float(item.get("net_sales_usd", 0))
        max_id = resp.get("max_id", 0)
        if max_id == hwm or max_id == 0:
            break
        hwm = max_id

    return units, returns, gross, net


def fetch_sales_by_country(financial_key, app_id, launch_date):
    app_id = str(app_id)
    launch = datetime.strptime(launch_date, "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch
    countries = {}

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        hwm = 0
        while True:
            url = (f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetDetailedSales/v001/"
                   f"?key={financial_key}&date={ds}&highwatermark_id={hwm}")
            data = fetch_json(url, f"country_sales_{app_id}")
            if not data or "response" not in data:
                break
            resp = data["response"]
            for item in resp.get("results", []):
                if str(item.get("primary_appid", item.get("appid", ""))) == app_id:
                    cc = item.get("country_code", "??")
                    sold = item.get("gross_units_sold", 0)
                    ret = item.get("gross_units_returned", 0)
                    n = float(item.get("net_sales_usd", 0))
                    if cc not in countries:
                        countries[cc] = {"units": 0, "returns": 0, "net": 0.0}
                    countries[cc]["units"] += sold
                    countries[cc]["returns"] += ret
                    countries[cc]["net"] += n
            max_id = resp.get("max_id", 0)
            if max_id == hwm or max_id == 0:
                break
            hwm = max_id
        current += timedelta(days=1)

    return dict(sorted(countries.items(), key=lambda x: x[1]["units"], reverse=True))


def fetch_wishlist_for_date(financial_key, app_id, date_str):
    url = f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetAppWishlistReporting/v001/?key={financial_key}&appid={app_id}&date={date_str}"
    data = fetch_json(url, f"wishlist_day_{app_id}")
    if data and "response" in data:
        s = data["response"].get("wishlist_summary", data["response"].get("summary", {}))
        return {"adds": s.get("wishlist_adds", 0), "deletes": s.get("wishlist_deletes", 0),
                "purchases": s.get("wishlist_purchases", 0), "gifts": s.get("wishlist_gifts", 0)}
    return {"adds": 0, "deletes": 0, "purchases": 0, "gifts": 0}


def fetch_wishlist_totals(financial_key, app_id, launch_date):
    launch = datetime.strptime(launch_date, "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch
    total = {"adds": 0, "deletes": 0, "purchases": 0, "gifts": 0}

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        day = fetch_wishlist_for_date(financial_key, app_id, ds)
        total["adds"] += day["adds"]
        total["deletes"] += day["deletes"]
        total["purchases"] += day["purchases"]
        total["gifts"] += day["gifts"]
        current += timedelta(days=1)

    total["net"] = total["adds"] - total["deletes"] - total["purchases"] - total["gifts"]
    return total


def fetch_wishlist_by_country(financial_key, app_id, launch_date):
    launch = datetime.strptime(launch_date, "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch
    countries = {}

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        url = f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetAppWishlistReporting/v001/?key={financial_key}&appid={app_id}&date={ds}"
        data = fetch_json(url, f"wishlist_country_{app_id}")
        if data and "response" in data:
            for c in data["response"].get("country_summary", []):
                cc = c.get("country_code", "??")
                s = c.get("summary_actions", {})
                if cc not in countries:
                    countries[cc] = {"adds": 0, "deletes": 0, "purchases": 0}
                countries[cc]["adds"] += s.get("wishlist_adds", 0)
                countries[cc]["deletes"] += s.get("wishlist_deletes", 0)
                countries[cc]["purchases"] += s.get("wishlist_purchases", 0)
        current += timedelta(days=1)

    return dict(sorted(countries.items(), key=lambda x: x[1]["adds"], reverse=True))


def refresh_all_sales(financial_key, app_id, launch_date):
    launch = datetime.strptime(launch_date, "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        units, returns, gross, net = fetch_sales_for_date(financial_key, app_id, ds)
        upsert_daily_sales(app_id, ds, units, returns, gross, net)
        if units > 0 or returns > 0:
            print(f"  [{app_id}] [{ds}] +{units} sold, -{returns} returned, ${net:.2f} net")
        current += timedelta(days=1)


def refresh_recent_sales(financial_key, app_id):
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    for d in [yesterday, today]:
        ds = d.strftime("%Y-%m-%d")
        units, returns, gross, net = fetch_sales_for_date(financial_key, app_id, ds)
        upsert_daily_sales(app_id, ds, units, returns, gross, net)
        if units > 0 or returns > 0:
            print(f"  [{app_id}] [{ds}] +{units} sold, -{returns} returned, ${net:.2f} net")


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
    financial_key = settings['steam_financial_key']

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
        self.cached_wishlist = {}
        self.cached_sales_by_country = {}
        self.cached_wishlist_by_country = {}


class DataCollector:
    def __init__(self):
        self.game_states = {}
        self.collection_count = 0
        self.is_first_collection = True
        self._lock = threading.Lock()

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

        for game in games:
            app_id = str(game['app_id'])
            launch_date = game.get('launch_date', '2025-01-01')
            game_name = game.get('name', app_id)
            gs = self.get_state(app_id)

            # Players + Reviews
            players = get_current_players(api_key, app_id)
            reviews = get_reviews(app_id)
            save_player_count(app_id, players)

            total_reviews = reviews.get("total_reviews", 0)
            total_positive = reviews.get("total_positive", 0)
            total_negative = reviews.get("total_negative", 0)
            save_review_data(app_id, total_positive, total_negative, total_reviews)

            if players > gs.peak_players:
                gs.peak_players = players

            # Sales
            if self.is_first_collection:
                existing = get_sales_totals(app_id)
                if existing[0] > 0:
                    print(f"  [{game_name}] Existing data, refreshing recent only...")
                    refresh_recent_sales(financial_key, app_id)
                else:
                    print(f"  [{game_name}] No data, full refresh...")
                    refresh_all_sales(financial_key, app_id, launch_date)
            else:
                refresh_recent_sales(financial_key, app_id)

            totals = get_sales_totals(app_id)
            total_units = totals[0]
            net_revenue = totals[3]
            save_sales_snapshot(app_id, totals[0], totals[1], totals[3])

            # Hourly cadence for expensive scans
            if self.collection_count % 12 == 0 or self.is_first_collection:
                try:
                    gs.cached_sales_by_country = fetch_sales_by_country(financial_key, app_id, launch_date)
                    gs.cached_wishlist_by_country = fetch_wishlist_by_country(financial_key, app_id, launch_date)
                    print(f"  [{game_name}] Countries: {len(gs.cached_sales_by_country)} sales, {len(gs.cached_wishlist_by_country)} wishlist")
                except Exception as e:
                    print(f"  [{game_name}] [COUNTRY ERROR] {e}")

                try:
                    gs.cached_wishlist = fetch_wishlist_totals(financial_key, app_id, launch_date)
                    wl_net = gs.cached_wishlist.get("net", 0)
                    save_wishlist_snapshot(app_id, gs.cached_wishlist["adds"],
                                           gs.cached_wishlist["deletes"],
                                           gs.cached_wishlist["purchases"], wl_net)
                except Exception as e:
                    wl_net = gs.last_wishlist_net
                    print(f"  [{game_name}] [WISHLIST ERROR] {e}")
            else:
                wl_net = gs.last_wishlist_net

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
            if gs.last_total_units > 0 and total_units > gs.last_total_units:
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

        self.is_first_collection = False

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
<title>Steam Dashboard - Setup</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Crimson+Pro:wght@400;500;600;700&family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #080509;
  --bg-card: #1a0f14;
  --bg-input: #0d0a0e;
  --border: #3a2030;
  --border-focus: #a84a56;
  --text: #e8ddd0;
  --text-dim: #9a8878;
  --text-muted: #6a5a4e;
  --accent: #a84a56;
  --accent-dim: #722f37;
  --gold: #c9a84c;
  --green: #5a9a5e;
  --red: #c45a5a;
  --font-display: 'Crimson Pro', Georgia, serif;
  --font-body: 'DM Sans', -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: var(--font-body);
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
}
body::after {
  content: '';
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  opacity: 0.02;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
}
.wizard {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 640px;
}
.wizard-header {
  text-align: center;
  margin-bottom: 40px;
}
.wizard-header h1 {
  font-family: var(--font-display);
  font-size: 36px;
  font-weight: 600;
  margin-bottom: 8px;
  letter-spacing: -0.02em;
}
.wizard-header p {
  font-size: 15px;
  color: var(--text-dim);
  line-height: 1.6;
}
/* Steps indicator */
.steps-bar {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin-bottom: 32px;
}
.step-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--border);
  transition: all 0.3s;
  cursor: pointer;
}
.step-dot.active {
  background: var(--accent);
  box-shadow: 0 0 8px rgba(168,74,86,0.4);
}
.step-dot.done {
  background: var(--green);
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
  background: linear-gradient(170deg, var(--bg-card), #241520);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 28px;
  margin-bottom: 20px;
}
.card::after {
  content: '';
  display: block;
  height: 0;
}
.card h2 {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 500;
  margin-bottom: 6px;
  color: var(--text);
}
.card .hint {
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 20px;
  line-height: 1.5;
}
.card .hint a {
  color: var(--gold);
  text-decoration: none;
}
.card .hint a:hover {
  text-decoration: underline;
}
label {
  display: block;
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  margin-bottom: 6px;
  margin-top: 16px;
}
label:first-of-type { margin-top: 0; }
input[type="text"], input[type="password"], input[type="number"], input[type="date"] {
  width: 100%;
  padding: 10px 14px;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 14px;
  transition: border-color 0.2s;
  outline: none;
}
input:focus {
  border-color: var(--border-focus);
}
input::placeholder {
  color: var(--text-muted);
  opacity: 0.6;
}
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
  background: var(--border);
  border-radius: 12px;
  position: relative;
  cursor: pointer;
  transition: background 0.2s;
  flex-shrink: 0;
}
.toggle.on {
  background: var(--green);
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
  color: var(--text-dim);
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
  background: rgba(0,0,0,0.2);
  border-radius: 8px;
  border: 1px solid rgba(58,32,48,0.3);
}
.game-item .field { flex: 1; }
.game-item .field label { margin-top: 0; }
.game-item .remove-btn {
  background: rgba(196,90,90,0.15);
  border: 1px solid rgba(196,90,90,0.3);
  color: var(--red);
  padding: 8px 12px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  margin-bottom: 0;
  height: 38px;
}
.add-game-btn {
  background: transparent;
  border: 1px dashed var(--border);
  color: var(--text-muted);
  padding: 10px;
  width: 100%;
  border-radius: 8px;
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 13px;
  transition: all 0.2s;
}
.add-game-btn:hover {
  border-color: var(--accent);
  color: var(--text-dim);
}
/* Accent picker */
.accent-grid {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.accent-swatch {
  width: 40px;
  height: 40px;
  border-radius: 8px;
  cursor: pointer;
  border: 2px solid transparent;
  transition: all 0.2s;
  position: relative;
}
.accent-swatch.selected {
  border-color: var(--text);
  box-shadow: 0 0 12px rgba(255,255,255,0.15);
}
.accent-swatch.selected::after {
  content: '\u2713';
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  font-weight: bold;
  font-size: 16px;
  text-shadow: 0 1px 3px rgba(0,0,0,0.5);
}
/* Theme selector */
.theme-grid {
  display: flex;
  gap: 12px;
}
.theme-option {
  flex: 1;
  padding: 16px;
  border-radius: 8px;
  border: 2px solid var(--border);
  cursor: pointer;
  text-align: center;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.2s;
}
.theme-option.selected {
  border-color: var(--accent);
}
.theme-option.dark-opt {
  background: #1a0f14;
  color: #e8ddd0;
}
.theme-option.light-opt {
  background: #f5f2ef;
  color: #2a2420;
}
/* Language selector */
.lang-grid {
  display: flex;
  gap: 12px;
}
.lang-option {
  flex: 1;
  padding: 14px;
  border-radius: 8px;
  border: 2px solid var(--border);
  cursor: pointer;
  text-align: center;
  font-size: 15px;
  font-weight: 500;
  transition: all 0.2s;
  color: var(--text-dim);
}
.lang-option.selected {
  border-color: var(--accent);
  color: var(--text);
}
/* Test button */
.test-btn {
  background: linear-gradient(135deg, var(--accent), var(--accent-dim));
  border: none;
  color: white;
  padding: 10px 20px;
  border-radius: 8px;
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 14px;
  font-weight: 600;
  margin-top: 16px;
  transition: all 0.2s;
}
.test-btn:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(168,74,86,0.3);
}
.test-result {
  margin-top: 10px;
  font-size: 13px;
  font-family: var(--font-mono);
  padding: 8px 12px;
  border-radius: 6px;
  display: none;
}
.test-result.success {
  display: block;
  background: rgba(90,154,94,0.1);
  border: 1px solid rgba(90,154,94,0.3);
  color: var(--green);
}
.test-result.error {
  display: block;
  background: rgba(196,90,90,0.1);
  border: 1px solid rgba(196,90,90,0.3);
  color: var(--red);
}
/* Navigation buttons */
.nav-buttons {
  display: flex;
  justify-content: space-between;
  margin-top: 24px;
}
.nav-btn {
  padding: 12px 28px;
  border-radius: 8px;
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 15px;
  font-weight: 600;
  transition: all 0.2s;
  border: none;
}
.nav-btn.prev {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-dim);
}
.nav-btn.prev:hover {
  border-color: var(--text-muted);
  color: var(--text);
}
.nav-btn.next {
  background: linear-gradient(135deg, var(--accent), var(--accent-dim));
  color: white;
}
.nav-btn.next:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 16px rgba(168,74,86,0.3);
}
.nav-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
  transform: none !important;
}
.nav-btn.start {
  background: linear-gradient(135deg, var(--green), #3a6a3e);
  color: white;
  font-size: 16px;
  padding: 14px 36px;
}
.nav-btn.start:hover {
  box-shadow: 0 4px 16px rgba(90,154,94,0.3);
}
</style>
</head>
<body>
<div class="wizard">
  <div class="wizard-header">
    <h1>Steam Dashboard</h1>
    <p>Real-time sales monitoring for your Steam games.<br>Let's get you set up in a few quick steps.</p>
  </div>

  <div class="steps-bar">
    <div class="step-dot active" data-step="0"></div>
    <div class="step-dot" data-step="1"></div>
    <div class="step-dot" data-step="2"></div>
    <div class="step-dot" data-step="3"></div>
    <div class="step-dot" data-step="4"></div>
    <div class="step-dot" data-step="5"></div>
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

  <!-- Step 1: API Keys -->
  <div class="step-panel" data-step="1">
    <div class="card">
      <h2>Steam API Keys</h2>
      <div class="hint">
        Your Steam Web API key is used for public data (player counts, reviews).
        Get one at <a href="https://steamcommunity.com/dev/apikey" target="_blank">steamcommunity.com/dev/apikey</a>.
      </div>
      <label>Steam Web API Key</label>
      <input type="password" id="steamApiKey" placeholder="E719B9C8C920A1EB..." />

      <div class="hint" style="margin-top:20px;">
        The Financial API key is for sales and wishlist data from the Steamworks Partner site.
      </div>
      <label>Steam Financial API Key</label>
      <input type="password" id="steamFinancialKey" placeholder="064E0AB9C952..." />
    </div>
  </div>

  <!-- Step 2: Games -->
  <div class="step-panel" data-step="2">
    <div class="card">
      <h2>Your Games</h2>
      <div class="hint">Add one or more games to monitor. The game name will be fetched automatically from Steam.</div>
      <div id="gamesList"></div>
      <button class="add-game-btn" onclick="addGameRow()">+ Add Another Game</button>
      <button class="test-btn" onclick="testConnection()" style="margin-top:16px;">Test Connection</button>
      <div class="test-result" id="testResult"></div>
    </div>
  </div>

  <!-- Step 3: Telegram -->
  <div class="step-panel" data-step="3">
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

  <!-- Step 4: Preferences -->
  <div class="step-panel" data-step="4">
    <div class="card">
      <h2>Preferences</h2>
      <div class="hint">Customize the look and feel of your dashboard.</div>

      <label>Language</label>
      <div class="lang-grid">
        <div class="lang-option selected" data-lang="en" onclick="selectLang('en')">English</div>
        <div class="lang-option" data-lang="ko" onclick="selectLang('ko')">Korean</div>
      </div>

      <label style="margin-top:20px;">Theme</label>
      <div class="theme-grid">
        <div class="theme-option dark-opt selected" data-theme="dark" onclick="selectTheme('dark')">Dark</div>
        <div class="theme-option light-opt" data-theme="light" onclick="selectTheme('light')">Light</div>
      </div>

      <label style="margin-top:20px;">Accent Color</label>
      <div class="accent-grid">
        <div class="accent-swatch selected" data-accent="wine" onclick="selectAccent('wine')" style="background: linear-gradient(135deg, #a84a56, #722f37);"></div>
        <div class="accent-swatch" data-accent="ocean" onclick="selectAccent('ocean')" style="background: linear-gradient(135deg, #4a8aaa, #2f5a72);"></div>
        <div class="accent-swatch" data-accent="forest" onclick="selectAccent('forest')" style="background: linear-gradient(135deg, #5a9a5e, #2f6a37);"></div>
        <div class="accent-swatch" data-accent="amber" onclick="selectAccent('amber')" style="background: linear-gradient(135deg, #c9a84c, #8a7434);"></div>
        <div class="accent-swatch" data-accent="slate" onclick="selectAccent('slate')" style="background: linear-gradient(135deg, #7a8a9a, #4a5a6a);"></div>
      </div>

      <label style="margin-top:20px;">Port</label>
      <input type="number" id="portInput" value="{{PORT}}" min="1024" max="65535" />
    </div>
  </div>

  <!-- Step 5: Confirm -->
  <div class="step-panel" data-step="5">
    <div class="card" style="text-align:center;">
      <h2>Ready to Go</h2>
      <div class="hint" style="margin-bottom:8px;">
        Your dashboard will start collecting data immediately after setup.<br>
        The first data collection may take a few minutes depending on how many days since launch.
      </div>
      <div id="setupSummary" style="text-align:left;font-family:var(--font-mono);font-size:13px;color:var(--text-dim);margin:20px 0;padding:16px;background:rgba(0,0,0,0.3);border-radius:8px;"></div>
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
  var totalSteps = 6;
  var selectedLang = 'en';
  var selectedTheme = 'dark';
  var selectedAccent = 'wine';
  var tgEnabled = false;

  // Pre-fill if editing settings
  var existingSettings = {{EXISTING_SETTINGS_JSON}};
  if (existingSettings && existingSettings.steam_api_key) {
    document.getElementById('steamApiKey').value = existingSettings.steam_api_key || '';
    document.getElementById('steamFinancialKey').value = existingSettings.steam_financial_key || '';
    var tg = existingSettings.telegram || {};
    if (tg.enabled) {
      tgEnabled = true;
      document.getElementById('tgToggle').classList.add('on');
      document.getElementById('tgFields').classList.add('visible');
      document.getElementById('tgBotToken').value = tg.bot_token || '';
      document.getElementById('tgChatIds').value = (tg.chat_ids || []).join(', ');
    }
    var dash = existingSettings.dashboard || {};
    selectedLang = dash.language || 'en';
    selectedTheme = dash.theme || 'dark';
    selectedAccent = dash.accent || 'wine';
    if (dash.port) document.getElementById('portInput').value = dash.port;
    selectLang(selectedLang);
    selectTheme(selectedTheme);
    selectAccent(selectedAccent);
  }

  // Initialize games list
  var games = (existingSettings && existingSettings.games && existingSettings.games.length > 0)
    ? existingSettings.games
    : [{ app_id: '', name: '', launch_date: '' }];

  function renderGames() {
    var container = document.getElementById('gamesList');
    container.innerHTML = '';
    games.forEach(function(g, i) {
      var div = document.createElement('div');
      div.className = 'game-item';
      div.innerHTML =
        '<div class="field"><label>App ID</label><input type="text" value="' + (g.app_id || '') + '" onchange="updateGame(' + i + ',\\'app_id\\',this.value)" placeholder="4451370" /></div>' +
        '<div class="field"><label>Launch Date</label><input type="date" value="' + (g.launch_date || '') + '" onchange="updateGame(' + i + ',\\'launch_date\\',this.value)" /></div>' +
        (games.length > 1 ? '<button class="remove-btn" onclick="removeGame(' + i + ')">X</button>' : '');
      container.appendChild(div);
    });
  }

  window.addGameRow = function() {
    games.push({ app_id: '', name: '', launch_date: '' });
    renderGames();
  };

  window.removeGame = function(i) {
    games.splice(i, 1);
    renderGames();
  };

  window.updateGame = function(i, field, value) {
    games[i][field] = value;
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

  window.selectLang = function(lang) {
    selectedLang = lang;
    document.querySelectorAll('.lang-option').forEach(function(el) {
      el.classList.toggle('selected', el.getAttribute('data-lang') === lang);
    });
  };

  window.selectTheme = function(theme) {
    selectedTheme = theme;
    document.querySelectorAll('.theme-option').forEach(function(el) {
      el.classList.toggle('selected', el.getAttribute('data-theme') === theme);
    });
  };

  window.selectAccent = function(accent) {
    selectedAccent = accent;
    document.querySelectorAll('.accent-swatch').forEach(function(el) {
      el.classList.toggle('selected', el.getAttribute('data-accent') === accent);
    });
  };

  window.testConnection = function() {
    var apiKey = document.getElementById('steamApiKey').value.trim();
    var resultEl = document.getElementById('testResult');
    if (!apiKey || !games[0] || !games[0].app_id) {
      resultEl.className = 'test-result error';
      resultEl.textContent = 'Please fill in the API key and at least one App ID first.';
      return;
    }
    resultEl.className = 'test-result';
    resultEl.style.display = 'block';
    resultEl.style.background = 'rgba(201,168,76,0.1)';
    resultEl.style.borderColor = 'rgba(201,168,76,0.3)';
    resultEl.style.color = '#c9a84c';
    resultEl.textContent = 'Testing...';

    fetch('/api/test?api_key=' + encodeURIComponent(apiKey) + '&app_id=' + encodeURIComponent(games[0].app_id))
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.success) {
          resultEl.className = 'test-result success';
          resultEl.textContent = 'Connected! Game: ' + (data.name || games[0].app_id);
          if (data.name && games[0]) {
            games[0].name = data.name;
          }
        } else {
          resultEl.className = 'test-result error';
          resultEl.textContent = 'Failed: ' + (data.error || 'Unknown error');
        }
      })
      .catch(function(e) {
        resultEl.className = 'test-result error';
        resultEl.textContent = 'Network error: ' + e.message;
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
    updateStepDots();

    // Build summary on last step
    if (step === totalSteps - 1) {
      var lines = [];
      lines.push('Games: ' + games.filter(function(g){return g.app_id;}).map(function(g){return g.app_id + (g.name ? ' (' + g.name + ')' : '');}).join(', '));
      lines.push('Telegram: ' + (tgEnabled ? 'ON' : 'OFF'));
      lines.push('Theme: ' + selectedTheme + ' / ' + selectedAccent);
      lines.push('Language: ' + selectedLang);
      lines.push('Port: ' + document.getElementById('portInput').value);
      document.getElementById('setupSummary').innerHTML = lines.join('<br>');
    }
  }

  window.nextStep = function() {
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
      telegram: {
        enabled: tgEnabled,
        bot_token: document.getElementById('tgBotToken').value.trim(),
        chat_ids: chatIds
      },
      dashboard: {
        port: parseInt(document.getElementById('portInput').value) || 8081,
        poll_interval: 300,
        language: selectedLang,
        theme: selectedTheme,
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
<html lang="{{LANGUAGE}}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Steam Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Crimson+Pro:wght@400;500;600;700&family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --font-display: 'Crimson Pro', Georgia, serif;
  --font-body: 'DM Sans', -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;
  --ease-out: cubic-bezier(0.25, 0.46, 0.45, 0.94);
}

/* ---- ACCENT COLORS ---- */
:root[data-accent="wine"] {
  --accent: #a84a56; --accent-dim: #722f37;
  --accent-glow: rgba(168,74,86,0.2); --accent-fill: rgba(168,74,86,0.08);
}
:root[data-accent="ocean"] {
  --accent: #4a8aaa; --accent-dim: #2f5a72;
  --accent-glow: rgba(74,138,170,0.2); --accent-fill: rgba(74,138,170,0.08);
}
:root[data-accent="forest"] {
  --accent: #5a9a5e; --accent-dim: #2f6a37;
  --accent-glow: rgba(90,154,94,0.2); --accent-fill: rgba(90,154,94,0.08);
}
:root[data-accent="amber"] {
  --accent: #c9a84c; --accent-dim: #8a7434;
  --accent-glow: rgba(201,168,76,0.2); --accent-fill: rgba(201,168,76,0.08);
}
:root[data-accent="slate"] {
  --accent: #7a8a9a; --accent-dim: #4a5a6a;
  --accent-glow: rgba(122,138,154,0.2); --accent-fill: rgba(122,138,154,0.08);
}

/* ---- DARK THEME ---- */
:root[data-theme="dark"] {
  --bg-black: #080509; --bg-deep: #0d0a0e; --bg-mid: #1a0f14;
  --bg-surface: #241520; --bg-elevated: #2e1a28;
  --border-color: #3a2030; --border-light: #4a2a3c;
  --text-primary: #e8ddd0; --text-secondary: #9a8878;
  --text-tertiary: #6a5a4e; --text-accent: #d4c0a0;
  --gold: #c9a84c; --gold-bright: #dbb94e; --gold-dim: #8a7434;
  --gold-fill: rgba(201,168,76,0.08);
  --green: #5a9a5e; --green-bright: #6cc070; --green-dim: #3a6a3e;
  --green-fill: rgba(90,154,94,0.06);
  --red: #c45a5a; --purple: #8b5a8a; --purple-fill: rgba(139,90,138,0.08);
  --chart-grid: rgba(58,32,48,0.3); --chart-tick: #6a5a4e; --chart-legend: #9a8878;
  --tooltip-bg: rgba(13,10,14,0.95); --tooltip-border: rgba(201,168,76,0.2);
  --status-bg: rgba(8,5,9,0.9);
  --header-bg: linear-gradient(165deg, #0d0a0e 0%, #1a0a10 40%, #200e18 100%);
  --header-glow: rgba(114,47,55,0.12);
  --shimmer-a: #241520; --shimmer-b: #2e1a28;
  --review-hover: rgba(114,47,55,0.08);
}

/* ---- LIGHT THEME ---- */
:root[data-theme="light"] {
  --bg-black: #f5f2ef; --bg-deep: #f0ece8; --bg-mid: #faf8f6;
  --bg-surface: #ffffff; --bg-elevated: #ffffff;
  --border-color: #e0d8d0; --border-light: #d0c8c0;
  --text-primary: #2a2420; --text-secondary: #6a5a50;
  --text-tertiary: #9a8a80; --text-accent: #4a3a30;
  --gold: #b89830; --gold-bright: #c9a84c; --gold-dim: #8a7434;
  --gold-fill: rgba(184,152,48,0.1);
  --green: #3a8a3e; --green-bright: #4a9a50; --green-dim: #2a6a2e;
  --green-fill: rgba(58,138,62,0.08);
  --red: #c04040; --purple: #7a4a7a; --purple-fill: rgba(122,74,122,0.08);
  --chart-grid: rgba(0,0,0,0.06); --chart-tick: #9a8a80; --chart-legend: #6a5a50;
  --tooltip-bg: rgba(255,255,255,0.97); --tooltip-border: rgba(0,0,0,0.1);
  --status-bg: rgba(245,242,239,0.95);
  --header-bg: linear-gradient(165deg, #f0ece8 0%, #faf8f6 100%);
  --header-glow: rgba(114,47,55,0.06);
  --shimmer-a: #f0ece8; --shimmer-b: #e8e0d8;
  --review-hover: rgba(0,0,0,0.03);
}

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: var(--font-body);
  background: var(--bg-black);
  color: var(--text-primary);
  min-height: 100vh;
  overflow-x: hidden;
}
body::after {
  content: '';
  position: fixed; inset: 0;
  pointer-events: none; z-index: 0; opacity: 0.025;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
}
:root[data-theme="light"] body::after { opacity: 0.012; }

.header {
  position: relative;
  background: var(--header-bg);
  padding: 28px 32px;
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
  width: 180px; border-radius: var(--radius-md);
  box-shadow: 0 8px 32px rgba(0,0,0,0.3), 0 0 0 1px rgba(201,168,76,0.15);
  flex-shrink: 0;
}
.header-info { flex: 1; min-width: 0; }
.header-info h1 {
  font-family: var(--font-display); font-size: 32px; font-weight: 600;
  color: var(--text-primary); letter-spacing: -0.02em; margin-bottom: 4px;
}
.header-info .subtitle { font-size: 13px; color: var(--text-tertiary); margin-bottom: 10px; }
.header-info .price-badge {
  display: inline-flex; align-items: center; gap: 6px;
  background: linear-gradient(135deg, rgba(201,168,76,0.12), rgba(201,168,76,0.06));
  border: 1px solid rgba(201,168,76,0.2);
  color: var(--gold); padding: 5px 14px; border-radius: 6px;
  font-family: var(--font-mono); font-size: 13px; font-weight: 500;
}
.header-controls {
  margin-left: auto; text-align: right; flex-shrink: 0;
  display: flex; flex-direction: column; align-items: flex-end; gap: 6px;
}
.live-indicator {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 11px; font-weight: 600; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--green);
}
.live-dot {
  width: 7px; height: 7px; background: var(--green-bright);
  border-radius: 50%; box-shadow: 0 0 8px rgba(108,192,112,0.5);
  animation: livePulse 2.5s ease-in-out infinite;
}
@keyframes livePulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 8px rgba(108,192,112,0.5); }
  50% { opacity: 0.4; box-shadow: 0 0 4px rgba(108,192,112,0.2); }
}
.update-time { font-size: 12px; color: var(--text-tertiary); font-family: var(--font-mono); }
.poll-info { font-size: 11px; color: var(--text-tertiary); opacity: 0.6; }
.header-buttons {
  display: flex; gap: 6px; align-items: center; margin-top: 4px;
}
.lang-toggle {
  display: inline-flex; gap: 0; border-radius: 4px; overflow: hidden;
  border: 1px solid var(--border-color); font-size: 11px;
}
.lang-toggle button {
  background: transparent; border: none; color: var(--text-tertiary);
  padding: 3px 8px; cursor: pointer; font-family: var(--font-mono);
  font-size: 11px; transition: all 0.2s;
}
.lang-toggle button.active { background: var(--bg-elevated); color: var(--text-primary); }
.settings-btn {
  background: transparent; border: 1px solid var(--border-color);
  color: var(--text-tertiary); padding: 3px 8px; border-radius: 4px;
  cursor: pointer; font-size: 13px; transition: all 0.2s;
  text-decoration: none; display: inline-flex; align-items: center;
}
.settings-btn:hover { border-color: var(--border-light); color: var(--text-secondary); }
.game-selector {
  display: none; margin-top: 4px;
}
.game-selector.visible { display: flex; gap: 6px; flex-wrap: wrap; }
.game-tab {
  padding: 4px 12px; border-radius: 4px; font-size: 12px;
  font-family: var(--font-mono); cursor: pointer;
  border: 1px solid var(--border-color); background: transparent;
  color: var(--text-tertiary); transition: all 0.2s;
}
.game-tab.active {
  background: var(--bg-elevated); color: var(--text-primary);
  border-color: var(--border-light);
}

.dashboard { max-width: 1400px; margin: 0 auto; padding: 24px 24px 48px; }

.metrics-grid {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 14px; margin-bottom: 14px;
}
.metric-card {
  position: relative;
  background: linear-gradient(170deg, var(--bg-mid) 0%, var(--bg-surface) 100%);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 18px 20px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
  overflow: hidden;
}
.metric-card:hover { border-color: var(--border-light); transform: translateY(-2px); }
.metric-card::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}
.metric-label {
  font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-tertiary); margin-bottom: 8px;
}
.metric-value {
  font-family: var(--font-display); font-size: 32px; font-weight: 700;
  color: var(--text-primary); line-height: 1.1; letter-spacing: -0.02em;
}
.metric-value.gold { color: var(--gold); }
.metric-value.green { color: var(--green); }
.metric-sub {
  font-size: 12px; color: var(--text-tertiary); margin-top: 6px;
  font-family: var(--font-mono); font-weight: 400;
}

.charts-grid { display: grid; grid-template-columns: 1fr; gap: 14px; margin-bottom: 14px; }
.charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
.chart-card {
  position: relative;
  background: linear-gradient(170deg, var(--bg-mid) 0%, var(--bg-surface) 100%);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 20px 22px; overflow: hidden;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}
.chart-card:hover { border-color: var(--border-light); transform: translateY(-2px); }
.chart-card::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}
.chart-card h3 {
  font-family: var(--font-display); font-size: 18px; font-weight: 500;
  color: var(--text-accent); margin-bottom: 16px; letter-spacing: -0.01em;
}
.chart-card canvas { width: 100% !important; }

.section-header {
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 14px; margin-top: 8px;
}
.section-header h2 {
  font-family: var(--font-display); font-size: 22px; font-weight: 500;
  color: var(--text-accent); letter-spacing: -0.01em;
}
.section-header::after {
  content: ''; flex: 1; height: 1px;
  background: linear-gradient(90deg, var(--border-color), transparent);
}

.country-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
.country-card {
  position: relative;
  background: linear-gradient(170deg, var(--bg-mid) 0%, var(--bg-surface) 100%);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 20px 22px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}
.country-card:hover { border-color: var(--border-light); transform: translateY(-2px); }
.country-card::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}
.country-card > div { overflow-x: auto; }
.country-card h3 {
  font-family: var(--font-display); font-size: 18px; font-weight: 500;
  color: var(--text-accent); margin-bottom: 14px;
}
.country-table { width: 100%; border-collapse: collapse; }
.country-table tr {
  border-bottom: 1px solid rgba(58,32,48,0.4); transition: background 0.2s;
}
:root[data-theme="light"] .country-table tr { border-bottom: 1px solid rgba(0,0,0,0.06); }
.country-table tr:hover { background: var(--review-hover); }
.country-table td { padding: 7px 0; font-size: 13px; }
.country-table .cc { font-weight: 600; color: var(--text-secondary); width: 100px; }
.country-table .bar-cell { font-family: var(--font-mono); font-size: 11px; color: var(--accent); letter-spacing: -0.05em; }
.country-table .val { text-align: right; font-family: var(--font-mono); font-weight: 500; color: var(--text-primary); width: 60px; }

.reviews-grid { display: grid; grid-template-columns: 1fr; gap: 10px; }
.review-card {
  background: linear-gradient(170deg, var(--bg-mid) 0%, var(--bg-surface) 100%);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 18px 22px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}
.review-card:hover { border-color: var(--border-light); transform: translateY(-2px); }
.review-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.review-thumb {
  font-size: 18px; width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 6px; flex-shrink: 0;
}
.review-thumb.up { background: rgba(90,154,94,0.15); }
.review-thumb.down { background: rgba(196,90,90,0.15); }
.review-author { font-weight: 600; font-size: 13px; color: var(--text-secondary); }
.review-playtime { margin-left: auto; font-size: 12px; font-family: var(--font-mono); color: var(--text-tertiary); }
.review-text {
  font-size: 13.5px; line-height: 1.7; color: var(--text-secondary);
  max-height: 80px; overflow: hidden;
  -webkit-mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
  mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
}

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
.status-bar .dot.on { background: var(--green-bright); box-shadow: 0 0 4px rgba(108,192,112,0.4); }
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

.metric-card, .chart-card, .country-card, .review-card { animation: fadeUp 0.5s var(--ease-out) both; }
@keyframes fadeUp {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}
.metrics-grid .metric-card:nth-child(1) { animation-delay: 0.05s; }
.metrics-grid .metric-card:nth-child(2) { animation-delay: 0.1s; }
.metrics-grid .metric-card:nth-child(3) { animation-delay: 0.15s; }
.metrics-grid .metric-card:nth-child(4) { animation-delay: 0.2s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(1) { animation-delay: 0.25s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(2) { animation-delay: 0.3s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(3) { animation-delay: 0.35s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(4) { animation-delay: 0.4s; }

@media (max-width: 1024px) {
  .metrics-grid { grid-template-columns: repeat(2, 1fr); }
  .charts-row { grid-template-columns: 1fr; }
  .country-grid { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  .header { padding: 20px 20px; gap: 16px; }
  .header-img { width: 140px; }
  .header-info h1 { font-size: 26px; }
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
  .metric-value { font-size: 26px; }
  .metric-label { font-size: 10px; }
  .metric-sub { font-size: 11px; }
  .chart-card { padding: 16px 14px; }
  .chart-card canvas { min-height: 150px; }
  .section-header { padding: 0 4px; }
  .section-header h2 { font-size: 17px; }
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
    <div class="poll-info" data-i18n="pollInfo">5min poll</div>
    <div class="header-buttons">
      <div class="lang-toggle">
        <button id="langKo" onclick="setLang('ko')">KR</button>
        <button id="langEn" onclick="setLang('en')">EN</button>
      </div>
      <a class="settings-btn" href="/settings" title="Settings">\u2699</a>
    </div>
    <div class="game-selector" id="gameSelector"></div>
  </div>
</div>

<div class="dashboard">
  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-label" data-i18n="totalSales">Total Sales</div>
      <div class="metric-value gold loading" id="totalSales">--</div>
      <div class="metric-sub" id="salesSub"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="netRevenue">Net Revenue</div>
      <div class="metric-value green loading" id="netRevenue">--</div>
      <div class="metric-sub" id="revenueSub"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="playersOnline">Players Online</div>
      <div class="metric-value loading" id="currentPlayers">--</div>
      <div class="metric-sub" id="playerChange"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="peakPlayers">Peak Players</div>
      <div class="metric-value loading" id="peakPlayers">--</div>
      <div class="metric-sub" data-i18n="sessionHigh">Session high</div>
    </div>
  </div>
  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-label" data-i18n="reviews">Reviews</div>
      <div class="metric-value loading" id="totalReviews">--</div>
      <div class="metric-sub" id="reviewRatio"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="positiveRate">Positive Rate</div>
      <div class="metric-value green loading" id="positiveRate">--</div>
      <div class="metric-sub" id="reviewScore"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="wishlists">Wishlists</div>
      <div class="metric-value loading" id="wishlistNet">--</div>
      <div class="metric-sub" id="wishlistSub"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="refundRate">Refund Rate</div>
      <div class="metric-value loading" id="refundRate">--</div>
      <div class="metric-sub" data-i18n="refundSales">returns / sales</div>
    </div>
  </div>
  <div class="section-header"><h2 data-i18n="salesPerf">Sales Performance</h2></div>
  <div class="charts-grid">
    <div class="chart-card">
      <h3 data-i18n-html="cumSales">Cumulative Sales &amp; Revenue</h3>
      <canvas id="salesTimelineChart" height="180"></canvas>
    </div>
  </div>
  <div class="charts-row">
    <div class="chart-card">
      <h3 data-i18n-html="dailySales">Daily Sales &amp; Revenue</h3>
      <canvas id="salesChart" height="220"></canvas>
    </div>
    <div class="chart-card">
      <h3 data-i18n="playerActivity">Player Activity</h3>
      <canvas id="playerChart" height="220"></canvas>
    </div>
  </div>
  <div class="section-header"><h2 data-i18n="geoBreakdown">Geographic Breakdown</h2></div>
  <div class="country-grid">
    <div class="country-card">
      <h3 data-i18n="salesByCountryLabel">Sales by Country</h3>
      <div id="salesByCountry"></div>
    </div>
    <div class="country-card">
      <h3 data-i18n="wlByCountry">Wishlists by Country</h3>
      <div id="wishlistByCountry"></div>
    </div>
  </div>
  <div class="section-header"><h2 data-i18n="recentReviews">Recent Reviews</h2></div>
  <div class="reviews-grid" id="recentReviews"></div>
</div>

<div class="status-bar">
  <span>App ID: <span id="statusAppId">{{DEFAULT_APP_ID}}</span></span>
  <span>Poll: {{POLL_INTERVAL}}s</span>
  <span>Telegram: <span class="dot" id="tgDot"></span> <span id="tgStatus"></span></span>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
(function() {
  var rootEl = document.documentElement;
  rootEl.setAttribute('data-theme', '{{THEME}}');
  rootEl.setAttribute('data-accent', '{{ACCENT}}');

  var playerChart, salesChart, salesTimelineChart;
  var curLang = localStorage.getItem('dashLang') || '{{LANGUAGE}}';
  var currentAppId = '{{DEFAULT_APP_ID}}';
  var allGames = {{GAMES_JSON}};

  // Show game selector if multiple games
  if (allGames.length > 1) {
    var sel = document.getElementById('gameSelector');
    sel.classList.add('visible');
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
    document.getElementById('statusAppId').textContent = appId;
    document.querySelectorAll('.game-tab').forEach(function(btn) {
      btn.classList.toggle('active', btn.getAttribute('data-appid') === appId);
    });
    document.querySelectorAll('.metric-value').forEach(function(el) { el.classList.add('loading'); });
    fetchData();
  }

  var i18n = {
    ko: {
      totalSales: '\uCD1D \uD310\uB9E4', netRevenue: '\uC21C\uC218\uC775',
      playersOnline: '\uD604\uC7AC \uB3D9\uC811', peakPlayers: '\uD53C\uD06C \uB3D9\uC811',
      sessionHigh: '\uC138\uC158 \uCD5C\uACE0\uCE58', reviews: '\uB9AC\uBDF0',
      positiveRate: '\uAE0D\uC815\uB960', wishlists: '\uC704\uC2DC\uB9AC\uC2A4\uD2B8',
      refundRate: '\uD658\uBD88\uB960', refundSales: '\uD658\uBD88 / \uD310\uB9E4',
      salesPerf: '\uD310\uB9E4 \uD604\uD669',
      cumSales: '\uB204\uC801 \uD310\uB9E4 &amp; \uC218\uC775',
      dailySales: '\uC77C\uBCC4 \uD310\uB9E4 &amp; \uC218\uC775',
      playerActivity: '\uB3D9\uC811\uC790 \uCD94\uC774',
      geoBreakdown: '\uAD6D\uAC00\uBCC4 \uD604\uD669',
      salesByCountryLabel: '\uAD6D\uAC00\uBCC4 \uD310\uB9E4',
      wlByCountry: '\uAD6D\uAC00\uBCC4 \uC704\uC2DC\uB9AC\uC2A4\uD2B8',
      recentReviews: '\uCD5C\uADFC \uB9AC\uBDF0',
      pollInfo: '5\uBD84 \uD3F4\uB9C1 \u00B7 30\uCD08 \uAC31\uC2E0',
      collecting: '\uB370\uC774\uD130 \uC218\uC9D1 \uC911...',
      noChange: '\u2014 \uBCC0\uB3D9 \uC5C6\uC74C',
      refunds: '\uD658\uBD88', grossLabel: '\uCD1D\uB9E4\uCD9C',
      beforeFees: '\uC218\uC218\uB8CC \uC804', conversion: '\uAD6C\uB9E4\uC804\uD658',
      hours: '\uC2DC\uAC04', unitSuffix: '\uAC74',
      chartCumSales: '\uB204\uC801 \uD310\uB9E4 (\uAC74)',
      chartCumRev: '\uB204\uC801 \uC21C\uC218\uC775 ($)',
      chartSales: '\uD310\uB9E4 (\uAC74)', chartRefunds: '\uD658\uBD88',
      chartNetRev: '\uC21C\uC218\uC775 ($)', chartUnits: '\uAC74\uC218',
      chartRevenue: '\uC218\uC775 ($)', chartPlayers: '\uB3D9\uC811',
      chartSalesAxis: '\uD310\uB9E4 (\uAC74)', chartRevenueAxis: '\uC218\uC775 ($)'
    },
    en: {
      totalSales: 'Total Sales', netRevenue: 'Net Revenue',
      playersOnline: 'Players Online', peakPlayers: 'Peak Players',
      sessionHigh: 'Session high', reviews: 'Reviews',
      positiveRate: 'Positive Rate', wishlists: 'Wishlists',
      refundRate: 'Refund Rate', refundSales: 'returns / sales',
      salesPerf: 'Sales Performance',
      cumSales: 'Cumulative Sales &amp; Revenue',
      dailySales: 'Daily Sales &amp; Revenue',
      playerActivity: 'Player Activity',
      geoBreakdown: 'Geographic Breakdown',
      salesByCountryLabel: 'Sales by Country',
      wlByCountry: 'Wishlists by Country',
      recentReviews: 'Recent Reviews',
      pollInfo: '5min poll \u00B7 30s refresh',
      collecting: 'Collecting data...',
      noChange: '\u2014 no change',
      refunds: 'refunds', grossLabel: 'gross',
      beforeFees: 'before fees', conversion: 'conv.',
      hours: 'h', unitSuffix: '',
      chartCumSales: 'Cumulative Sales', chartCumRev: 'Net Revenue ($)',
      chartSales: 'Sales', chartRefunds: 'Refunds',
      chartNetRev: 'Net Revenue ($)', chartUnits: 'Units',
      chartRevenue: 'Revenue ($)', chartPlayers: 'Players',
      chartSalesAxis: 'Sales', chartRevenueAxis: 'Revenue ($)'
    }
  };

  function T(key) { return (i18n[curLang] || i18n.en)[key] || key; }

  function applyStaticLabels() {
    document.querySelectorAll('[data-i18n]').forEach(function(el) {
      el.textContent = T(el.getAttribute('data-i18n'));
    });
    document.querySelectorAll('[data-i18n-html]').forEach(function(el) {
      el.innerHTML = T(el.getAttribute('data-i18n-html'));
    });
  }

  function updateToggleButtons() {
    document.getElementById('langKo').className = curLang === 'ko' ? 'active' : '';
    document.getElementById('langEn').className = curLang === 'en' ? 'active' : '';
  }

  window.setLang = function(lang) {
    curLang = lang;
    localStorage.setItem('dashLang', lang);
    applyStaticLabels();
    updateToggleButtons();
    rebuildCharts();
    fetchData();
  };

  function getChartColors() {
    var cs = getComputedStyle(rootEl);
    return {
      gold: cs.getPropertyValue('--gold').trim() || '#c9a84c',
      goldFill: cs.getPropertyValue('--gold-fill').trim() || 'rgba(201,168,76,0.08)',
      green: cs.getPropertyValue('--green').trim() || '#5a9a5e',
      greenFill: cs.getPropertyValue('--green-fill').trim() || 'rgba(90,154,94,0.06)',
      red: cs.getPropertyValue('--red').trim() || '#c45a5a',
      purple: cs.getPropertyValue('--purple').trim() || '#8b5a8a',
      purpleFill: cs.getPropertyValue('--purple-fill').trim() || 'rgba(139,90,138,0.08)',
      grid: cs.getPropertyValue('--chart-grid').trim() || 'rgba(58,32,48,0.3)',
      tick: cs.getPropertyValue('--chart-tick').trim() || '#6a5a4e',
      legend: cs.getPropertyValue('--chart-legend').trim() || '#9a8878',
      tooltipBg: cs.getPropertyValue('--tooltip-bg').trim() || 'rgba(13,10,14,0.95)',
      tooltipBorder: cs.getPropertyValue('--tooltip-border').trim() || 'rgba(201,168,76,0.2)'
    };
  }

  function rebuildCharts() {
    if (salesTimelineChart) salesTimelineChart.destroy();
    if (salesChart) salesChart.destroy();
    if (playerChart) playerChart.destroy();
    initCharts();
  }

  function initCharts() {
    var cc = getChartColors();
    var isMobile = window.innerWidth <= 768;
    var pr = isMobile ? 2 : 4;
    var phr = isMobile ? 3 : 6;
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
      titleFont: { family: "'DM Sans'", weight: '600' },
      bodyFont: { family: "'JetBrains Mono'", size: 12 },
      padding: 12, cornerRadius: 8, displayColors: true, boxPadding: 4
    };
    var baseOpts = {
      responsive: true,
      animation: { duration: 500, easing: 'easeOutQuart' },
      interaction: { mode: 'index', intersect: false }
    };
    var legendCfg = { display: true, labels: { color: cc.legend, usePointStyle: true, pointStyle: 'circle', padding: 16, font: { family: "'DM Sans'", size: 12 } } };

    salesTimelineChart = new Chart(document.getElementById('salesTimelineChart'), {
      type: 'line',
      data: { labels: [], datasets: [
        { label: T('chartCumSales'), data: [], borderColor: cc.gold, backgroundColor: cc.goldFill, fill: true, tension: 0.35, pointRadius: pr, pointHoverRadius: phr, pointBackgroundColor: cc.gold, pointBorderColor: 'transparent', borderWidth: 2.5, yAxisID: 'y' },
        { label: T('chartCumRev'), data: [], borderColor: cc.green, backgroundColor: 'transparent', borderDash: [6, 4], tension: 0.35, pointRadius: Math.max(1, pr - 1), pointHoverRadius: Math.max(2, phr - 1), pointBackgroundColor: cc.green, pointBorderColor: 'transparent', borderWidth: 2, yAxisID: 'y1' }
      ]},
      options: Object.assign({}, baseOpts, {
        plugins: { legend: legendCfg, tooltip: baseTooltip },
        scales: {
          x: Object.assign({}, baseScaleX, { ticks: Object.assign({}, baseScaleX.ticks, { maxTicksLimit: 20 }) }),
          y: Object.assign({}, baseScaleY, { position: 'left', title: { display: !isMobile, text: T('chartSalesAxis'), color: cc.tick, font: { family: "'DM Sans'", size: 11 } } }),
          y1: Object.assign({}, baseScaleY, { position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: T('chartRevenueAxis'), color: cc.tick, font: { family: "'DM Sans'", size: 11 } } })
        }
      })
    });

    salesChart = new Chart(document.getElementById('salesChart'), {
      type: 'bar',
      data: { labels: [], datasets: [
        { label: T('chartSales'), data: [], backgroundColor: cc.gold, borderRadius: 4, yAxisID: 'y', order: 2, barPercentage: 0.7 },
        { label: T('chartRefunds'), data: [], backgroundColor: cc.red, borderRadius: 4, yAxisID: 'y', order: 3, barPercentage: 0.7 },
        { label: T('chartNetRev'), data: [], type: 'line', borderColor: cc.green, backgroundColor: 'transparent', borderWidth: 2, pointRadius: Math.max(1, pr - 1), pointHoverRadius: Math.max(2, phr - 1), pointBackgroundColor: cc.green, pointBorderColor: 'transparent', tension: 0.35, yAxisID: 'y1', order: 1 }
      ]},
      options: Object.assign({}, baseOpts, {
        plugins: { legend: legendCfg, tooltip: baseTooltip },
        scales: {
          x: baseScaleX,
          y: Object.assign({}, baseScaleY, { position: 'left', title: { display: !isMobile, text: T('chartUnits'), color: cc.tick, font: { family: "'DM Sans'", size: 11 } } }),
          y1: Object.assign({}, baseScaleY, { position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: T('chartRevenueAxis'), color: cc.tick, font: { family: "'DM Sans'", size: 11 } } })
        }
      })
    });

    playerChart = new Chart(document.getElementById('playerChart'), {
      type: 'line',
      data: { labels: [], datasets: [{
        label: T('chartPlayers'), data: [],
        borderColor: cc.purple, backgroundColor: cc.purpleFill,
        fill: true, tension: 0.35, pointRadius: isMobile ? 1 : 1.5, pointHoverRadius: isMobile ? 2 : 4,
        pointBackgroundColor: cc.purple, pointBorderColor: 'transparent', borderWidth: 2
      }]},
      options: Object.assign({}, baseOpts, {
        plugins: { legend: { display: false }, tooltip: baseTooltip },
        scales: { x: baseScaleX, y: baseScaleY }
      })
    });
  }

  function fetchData() {
    var url = '/api/data?app_id=' + encodeURIComponent(currentAppId);
    fetch(url).then(function(resp) { return resp.json(); }).then(function(data) {
      if (data.app_details) {
        var d = data.app_details;
        document.getElementById('gameName').textContent = d.name || '';
        document.getElementById('gameDev').textContent = (d.developers || []).join(', ') + ' \u00B7 ' + (d.publishers || []).join(', ');
        document.getElementById('headerImg').src = d.header_image || '';
        if (d.price_overview) document.getElementById('gamePrice').textContent = d.price_overview.final_formatted || '';
      }
      document.querySelectorAll('.metric-value.loading').forEach(function(el) { el.classList.remove('loading'); });

      var s = data.sales_totals || {};
      var suffix = T('unitSuffix');
      document.getElementById('totalSales').textContent = (s.units || 0).toLocaleString();
      document.getElementById('salesSub').textContent = T('refunds') + ' ' + (s.returns || 0) + suffix + ' \u00B7 ' + T('grossLabel') + ' $' + (s.gross || 0).toFixed(0);
      document.getElementById('netRevenue').textContent = '$' + (s.net || 0).toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 0});
      document.getElementById('revenueSub').textContent = T('beforeFees') + ' $' + (s.gross || 0).toFixed(0);
      document.getElementById('refundRate').textContent = (s.units > 0 ? ((s.returns / s.units) * 100).toFixed(1) : '0') + '%';

      var timeline = data.sales_timeline || [];
      salesTimelineChart.data.labels = timeline.map(function(r) { var d = new Date(r[0]); return (d.getMonth()+1) + '/' + d.getDate() + ' ' + (d.getHours() < 12 ? 'AM' : 'PM'); });
      salesTimelineChart.data.datasets[0].data = timeline.map(function(r) { return r[1]; });
      salesTimelineChart.data.datasets[1].data = timeline.map(function(r) { return r[3]; });
      salesTimelineChart.update('none');

      var daily = data.daily_sales || [];
      salesChart.data.labels = daily.map(function(r) { return r[0].substring(5); });
      salesChart.data.datasets[0].data = daily.map(function(r) { return r[1]; });
      salesChart.data.datasets[1].data = daily.map(function(r) { return -r[2]; });
      salesChart.data.datasets[2].data = daily.map(function(r) { return r[4]; });
      salesChart.update('none');

      var players = data.current_players || 0;
      document.getElementById('currentPlayers').textContent = players.toLocaleString();
      document.getElementById('peakPlayers').textContent = (data.peak_players || 0).toLocaleString();

      var hist = data.player_history || [];
      if (hist.length > 1) {
        var prev = hist[hist.length - 2][1];
        var diff = players - prev;
        var el = document.getElementById('playerChange');
        el.textContent = diff > 0 ? '\u25B2 +' + diff : diff < 0 ? '\u25BC ' + diff : T('noChange');
        el.style.color = diff > 0 ? 'var(--green)' : diff < 0 ? 'var(--red)' : 'var(--text-tertiary)';
      }

      playerChart.data.labels = hist.map(function(r) { var d = new Date(r[0]); return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0'); });
      playerChart.data.datasets[0].data = hist.map(function(r) { return r[1]; });
      playerChart.update('none');

      var rev = data.reviews || {};
      var total = rev.total_reviews || 0, pos = rev.total_positive || 0, neg = rev.total_negative || 0;
      document.getElementById('totalReviews').textContent = total;
      document.getElementById('reviewRatio').innerHTML = '\uD83D\uDC4D ' + pos + ' / \uD83D\uDC4E ' + neg;
      document.getElementById('positiveRate').textContent = total > 0 ? Math.round(pos/total*100) + '%' : '--';
      document.getElementById('reviewScore').textContent = rev.review_score_desc || '';

      var wl = data.wishlist || {};
      document.getElementById('wishlistNet').textContent = '~' + (wl.net || 0).toLocaleString();
      document.getElementById('wishlistSub').textContent = '+' + (wl.adds||0) + ' / -' + (wl.deletes||0) + ' / ' + T('conversion') + ' ' + (wl.purchases||0);

      var esc = function(str) { var d = document.createElement('div'); d.textContent = String(str); return d.innerHTML; };
      var renderCountryTable = function(obj, valFn) {
        var entries = Object.entries(obj).slice(0, 15);
        if (!entries.length) return '<div style="color:var(--text-tertiary);font-style:italic;padding:12px 0;">' + T('collecting') + '</div>';
        var maxVal = Math.max(1, valFn(entries[0][1]));
        return '<table class="country-table">' + entries.map(function(entry) {
          var cc = esc(entry[0]); var d = entry[1]; var val = valFn(d);
          var pct = Math.round(val / maxVal * 100);
          return '<tr><td class="cc">' + cc + '</td><td class="bar-cell"><div style="background:linear-gradient(90deg, var(--accent), var(--accent-dim));width:' + pct + '%;height:7px;border-radius:3px;min-width:6px;box-shadow:0 0 6px var(--accent-glow);"></div></td><td class="val">' + val + '</td></tr>';
        }).join('') + '</table>';
      };
      document.getElementById('salesByCountry').innerHTML = renderCountryTable(data.sales_by_country || {}, function(d) { return d.units || 0; });
      document.getElementById('wishlistByCountry').innerHTML = renderCountryTable(data.wishlist_by_country || {}, function(d) { return d.adds || 0; });

      var recent = data.recent_reviews || [];
      document.getElementById('recentReviews').innerHTML = recent.map(function(r) {
        var isUp = r.voted_up;
        var thumb = isUp ? '\uD83D\uDC4D' : '\uD83D\uDC4E';
        var thumbClass = isUp ? 'up' : 'down';
        var playtime = Math.round((r.author && r.author.playtime_forever || 0) / 60 * 10) / 10;
        var text = esc((r.review || '').substring(0, 300)).split(String.fromCharCode(10)).join(' ');
        return '<div class="review-card"><div class="review-header"><span class="review-thumb ' + thumbClass + '">' + thumb + '</span><span class="review-author">' + esc(r.author && r.author.personaname || 'Anonymous') + '</span><span class="review-playtime">' + playtime + T('hours') + '</span></div><div class="review-text">' + text + '</div></div>';
      }).join('');

      document.getElementById('tgDot').className = 'dot ' + (data.telegram_active ? 'on' : 'off');
      document.getElementById('tgStatus').textContent = data.telegram_active ? 'ON' : 'OFF';
      document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
      fetchFailCount = 0;
    }).catch(function(e) { console.error('Fetch error:', e); fetchFailCount++; });
  }

  applyStaticLabels();
  updateToggleButtons();
  initCharts();

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
        self.send_header('Access-Control-Allow-Origin', '*')
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
            app_id = params.get('app_id', [''])[0]
            if not api_key or not app_id:
                self._json_response({'success': False, 'error': 'Missing api_key or app_id'})
                return
            try:
                data = fetch_json(
                    f"https://store.steampowered.com/api/appdetails?appids={app_id}",
                    "test"
                )
                if data and str(app_id) in data and data[str(app_id)].get("success"):
                    name = data[str(app_id)]["data"].get("name", "")
                    self._json_response({'success': True, 'name': name})
                else:
                    self._json_response({'success': False, 'error': 'App not found or API error'})
            except Exception as e:
                self._json_response({'success': False, 'error': str(e)})

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

            players = get_current_players(settings['steam_api_key'], req_app_id)
            reviews = get_reviews(req_app_id)
            recent = get_recent_reviews(req_app_id)
            app_details = get_app_details(req_app_id)
            p_history = get_player_history(req_app_id)
            daily = get_all_daily_sales(req_app_id)
            timeline = get_sales_snapshots(req_app_id)
            totals = get_sales_totals(req_app_id)
            wl_history = get_wishlist_history(req_app_id)

            tg = settings.get('telegram', {})

            payload = {
                "current_players": players,
                "peak_players": gs.peak_players,
                "reviews": reviews,
                "recent_reviews": recent,
                "app_details": app_details,
                "player_history": p_history,
                "daily_sales": daily,
                "sales_timeline": timeline,
                "sales_totals": {
                    "units": totals[0], "returns": totals[1],
                    "gross": totals[2], "net": totals[3]
                },
                "wishlist": gs.cached_wishlist,
                "wishlist_history": wl_history,
                "sales_by_country": gs.cached_sales_by_country,
                "wishlist_by_country": gs.cached_wishlist_by_country,
                "telegram_active": bool(tg.get('enabled') and tg.get('bot_token') and tg.get('chat_ids')),
                "timestamp": datetime.now().isoformat()
            }
            self._json_response(payload)

        elif parsed.path == '/api/settings':
            settings = get_all_settings()
            self._json_response(settings)

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

            # Auto-fetch game names for any game missing a name
            games = data.get('games', [])
            for g in games:
                if g.get('app_id') and not g.get('name'):
                    g['name'] = get_game_name_from_api(g['app_id'])

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
        self.send_header('Access-Control-Allow-Origin', '*')
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
    html = html.replace('{{ACCENT}}', dash.get('accent', 'wine'))
    html = html.replace('{{LANGUAGE}}', dash.get('language', 'en'))
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
        print(f"  Theme:      {dash.get('theme', 'dark')} / {dash.get('accent', 'wine')}")
        print(f"  Language:   {dash.get('language', 'en')}")
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


if __name__ == '__main__':
    main()
