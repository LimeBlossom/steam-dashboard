#!/usr/bin/env python3
"""
Steam Dashboard - Real-time sales monitoring for Steam games
https://github.com/xxx/steam-dashboard

Zero external dependencies. Reads config from config.json.
"""

import json
import time
import threading
import sqlite3
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.parse import urlparse, quote
from datetime import datetime, timedelta

VERSION = "1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FINANCIAL_BASE = "https://partner.steam-api.com"

# ========== CONFIG ==========

def load_config():
    config_path = os.path.join(SCRIPT_DIR, 'config.json')
    if not os.path.exists(config_path):
        print("No config.json found. Creating from interactive setup...")
        config = interactive_setup()
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
        print(f"Config saved to {config_path}")
        return config
    with open(config_path) as f:
        return json.load(f)


def interactive_setup():
    print("\n" + "=" * 50)
    print("  Steam Dashboard - First Time Setup")
    print("=" * 50 + "\n")

    config = {}

    config['steam_api_key'] = input("Steam API Key: ").strip()
    config['steam_financial_key'] = input("Steam Financial API Key (partner): ").strip()
    config['app_id'] = input("App ID: ").strip()
    config['launch_date'] = input("Launch date (YYYY-MM-DD): ").strip()

    tg = input("Enable Telegram alerts? (y/n) [n]: ").strip().lower()
    if tg == 'y':
        config['telegram'] = {
            'enabled': True,
            'bot_token': input("  Telegram Bot Token: ").strip(),
            'chat_ids': [x.strip() for x in input("  Chat IDs (comma-separated): ").strip().split(',')]
        }
    else:
        config['telegram'] = {'enabled': False, 'bot_token': '', 'chat_ids': []}

    lang = input("Language (en/ko) [en]: ").strip() or 'en'
    theme = input("Theme (dark/light) [dark]: ").strip() or 'dark'
    accent = input("Accent color (wine/ocean/forest/amber/slate) [wine]: ").strip() or 'wine'
    port = input("Port [8081]: ").strip() or '8081'

    config['dashboard'] = {
        'port': int(port),
        'poll_interval': 300,
        'language': lang,
        'theme': theme,
        'accent': accent
    }

    print("\nSetup complete!\n")
    return config


# ========== DATABASE ==========

def get_db_path(config):
    return os.path.join(SCRIPT_DIR, 'steam_dashboard.db')


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS player_history (
        timestamp TEXT, player_count INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS review_history (
        timestamp TEXT, total_positive INTEGER, total_negative INTEGER, total_reviews INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_sales (
        date TEXT PRIMARY KEY, units_sold INTEGER, units_returned INTEGER,
        gross_revenue_usd REAL, net_revenue_usd REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sales_snapshots (
        timestamp TEXT PRIMARY KEY, total_units INTEGER, total_returns INTEGER,
        total_net_usd REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS wishlist_history (
        timestamp TEXT, total_adds INTEGER, total_deletes INTEGER,
        total_purchases INTEGER, net_wishlists INTEGER
    )''')
    conn.commit()
    conn.close()


def save_player_count(db_path, count):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO player_history VALUES (?, ?)", (datetime.now().isoformat(), count))
    conn.commit()
    conn.close()


def save_review_data(db_path, pos, neg, total):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO review_history VALUES (?, ?, ?, ?)", (datetime.now().isoformat(), pos, neg, total))
    conn.commit()
    conn.close()


def upsert_daily_sales(db_path, date_str, units, returns, gross, net):
    conn = sqlite3.connect(db_path)
    conn.execute("""INSERT INTO daily_sales VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
        units_sold=excluded.units_sold, units_returned=excluded.units_returned,
        gross_revenue_usd=excluded.gross_revenue_usd, net_revenue_usd=excluded.net_revenue_usd
    """, (date_str, units, returns, gross, net))
    conn.commit()
    conn.close()


def get_player_history(db_path, limit=144):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT timestamp, player_count FROM player_history ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return list(reversed(rows))


def get_review_history(db_path, limit=144):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT timestamp, total_positive, total_negative, total_reviews FROM review_history ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return list(reversed(rows))


def get_all_daily_sales(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT date, units_sold, units_returned, gross_revenue_usd, net_revenue_usd FROM daily_sales ORDER BY date").fetchall()
    conn.close()
    return rows


def save_sales_snapshot(db_path, total_units, total_returns, total_net):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR REPLACE INTO sales_snapshots VALUES (?, ?, ?, ?)",
                 (datetime.now().isoformat(), total_units, total_returns, total_net))
    conn.commit()
    conn.close()


def get_sales_snapshots(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT timestamp, total_units, total_returns, total_net_usd FROM sales_snapshots ORDER BY timestamp").fetchall()
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


def save_wishlist_snapshot(db_path, adds, deletes, purchases, net):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO wishlist_history VALUES (?, ?, ?, ?, ?)",
                 (datetime.now().isoformat(), adds, deletes, purchases, net))
    conn.commit()
    conn.close()


def get_wishlist_history(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT timestamp, net_wishlists FROM wishlist_history ORDER BY timestamp DESC LIMIT 144").fetchall()
    conn.close()
    return list(reversed(rows))


def get_latest_wishlist_net(db_path):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT net_wishlists FROM wishlist_history ORDER BY timestamp DESC LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else 0


def get_sales_totals(db_path):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT COALESCE(SUM(units_sold),0), COALESCE(SUM(units_returned),0), COALESCE(SUM(gross_revenue_usd),0), COALESCE(SUM(net_revenue_usd),0) FROM daily_sales").fetchone()
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

def get_current_players(config):
    app_id = config['app_id']
    api_key = config['steam_api_key']
    data = fetch_json(
        f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={app_id}&key={api_key}",
        "players"
    )
    if data and "response" in data:
        return data["response"].get("player_count", 0)
    return 0


def get_app_details(config):
    app_id = config['app_id']
    data = fetch_json(f"https://store.steampowered.com/api/appdetails?appids={app_id}", "app_details")
    if data and app_id in data and data[app_id].get("success"):
        return data[app_id]["data"]
    return None


def get_game_name(config):
    details = get_app_details(config)
    if details:
        return details.get("name", f"App {config['app_id']}")
    return f"App {config['app_id']}"


def get_reviews(config):
    app_id = config['app_id']
    data = fetch_json(
        f"https://store.steampowered.com/appreviews/{app_id}?json=1&language=all&purchase_type=all&num_per_page=0",
        "reviews"
    )
    if data and data.get("success") == 1:
        return data.get("query_summary", {})
    return {}


def get_recent_reviews(config):
    app_id = config['app_id']
    data = fetch_json(
        f"https://store.steampowered.com/appreviews/{app_id}?json=1&language=all&purchase_type=all&num_per_page=5&filter=recent",
        "recent_reviews"
    )
    if data and data.get("success") == 1:
        return data.get("reviews", [])
    return []


# ========== FINANCIAL API ==========

def fetch_sales_for_date(config, date_str):
    app_id = str(config['app_id'])
    key = config['steam_financial_key']
    units = 0
    returns = 0
    gross = 0.0
    net = 0.0
    hwm = 0

    while True:
        url = (f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetDetailedSales/v001/"
               f"?key={key}&date={date_str}&highwatermark_id={hwm}")
        data = fetch_json(url, "sales")
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


def fetch_sales_by_country(config):
    app_id = str(config['app_id'])
    key = config['steam_financial_key']
    launch = datetime.strptime(config['launch_date'], "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch
    countries = {}

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        hwm = 0
        while True:
            url = (f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetDetailedSales/v001/"
                   f"?key={key}&date={ds}&highwatermark_id={hwm}")
            data = fetch_json(url, "country_sales")
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


def fetch_wishlist_for_date(config, date_str):
    key = config['steam_financial_key']
    app_id = config['app_id']
    url = f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetAppWishlistReporting/v001/?key={key}&appid={app_id}&date={date_str}"
    data = fetch_json(url, "wishlist_day")
    if data and "response" in data:
        s = data["response"].get("wishlist_summary", data["response"].get("summary", {}))
        return {"adds": s.get("wishlist_adds", 0), "deletes": s.get("wishlist_deletes", 0),
                "purchases": s.get("wishlist_purchases", 0), "gifts": s.get("wishlist_gifts", 0)}
    return {"adds": 0, "deletes": 0, "purchases": 0, "gifts": 0}


def fetch_wishlist_totals(config):
    launch = datetime.strptime(config['launch_date'], "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch
    total = {"adds": 0, "deletes": 0, "purchases": 0, "gifts": 0}

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        day = fetch_wishlist_for_date(config, ds)
        total["adds"] += day["adds"]
        total["deletes"] += day["deletes"]
        total["purchases"] += day["purchases"]
        total["gifts"] += day["gifts"]
        current += timedelta(days=1)

    total["net"] = total["adds"] - total["deletes"] - total["purchases"] - total["gifts"]
    return total


def fetch_wishlist_by_country(config):
    key = config['steam_financial_key']
    app_id = config['app_id']
    launch = datetime.strptime(config['launch_date'], "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch
    countries = {}

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        url = f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetAppWishlistReporting/v001/?key={key}&appid={app_id}&date={ds}"
        data = fetch_json(url, "wishlist_country")
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


def refresh_all_sales(config, db_path):
    launch = datetime.strptime(config['launch_date'], "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        units, returns, gross, net = fetch_sales_for_date(config, ds)
        upsert_daily_sales(db_path, ds, units, returns, gross, net)
        if units > 0 or returns > 0:
            print(f"  [{ds}] +{units} sold, -{returns} returned, ${net:.2f} net")
        current += timedelta(days=1)


def refresh_recent_sales(config, db_path):
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    for d in [yesterday, today]:
        ds = d.strftime("%Y-%m-%d")
        units, returns, gross, net = fetch_sales_for_date(config, ds)
        upsert_daily_sales(db_path, ds, units, returns, gross, net)
        if units > 0 or returns > 0:
            print(f"  [{ds}] +{units} sold, -{returns} returned, ${net:.2f} net")


# ========== TELEGRAM ==========

def send_telegram(config, message):
    tg = config.get('telegram', {})
    if not tg.get('enabled') or not tg.get('bot_token') or not tg.get('chat_ids'):
        return
    try:
        encoded = quote(message)
        for chat_id in tg['chat_ids']:
            url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage?chat_id={chat_id}&text={encoded}&parse_mode=HTML"
            fetch_json(url, "telegram")
        print(f"  [TG] Sent to {len(tg['chat_ids'])} recipients")
    except Exception as e:
        print(f"  [TG ERROR] {e}")


def send_startup_report(config, db_path):
    totals = get_sales_totals(db_path)
    units, returns, gross, net = totals
    players = get_current_players(config)
    reviews = get_reviews(config)
    total_reviews = reviews.get("total_reviews", 0)
    total_positive = reviews.get("total_positive", 0)
    rate = round(total_positive / max(total_reviews, 1) * 100)
    launch_dt = datetime.strptime(config['launch_date'], "%Y-%m-%d")
    delta = datetime.now() - launch_dt
    days_since = delta.days
    hours_since = int(delta.total_seconds() // 3600)

    daily = get_all_daily_sales(db_path)
    daily_lines = ""
    for row in daily:
        d, u, r, g, n = row
        bar_len = min(u, 30)
        bar = "\u2588" * bar_len + "\u2591" * max(0, 30 - bar_len)
        daily_lines += f"\n  {d[5:]}  {bar} {u} ${n:.0f}"

    msg = (
        f"\U0001f377 <b>Steam Dashboard Online</b>\n"
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
    send_telegram(config, msg)


# ========== DATA COLLECTOR ==========

class DataCollector:
    def __init__(self, config, db_path):
        self.config = config
        self.db_path = db_path
        self.last_player_count = 0
        self.last_review_count = 0
        self.last_total_units = 0
        self.last_wishlist_net = 0
        self.peak_players = 0
        self.cached_wishlist = {}
        self.cached_sales_by_country = {}
        self.cached_wishlist_by_country = {}
        self.is_first_collection = True
        self.collection_count = 0

    def collect(self):
        now = datetime.now().strftime('%H:%M:%S')
        print(f"[{now}] Collecting...")

        players = get_current_players(self.config)
        reviews = get_reviews(self.config)
        save_player_count(self.db_path, players)

        total_reviews = reviews.get("total_reviews", 0)
        total_positive = reviews.get("total_positive", 0)
        total_negative = reviews.get("total_negative", 0)
        save_review_data(self.db_path, total_positive, total_negative, total_reviews)

        if players > self.peak_players:
            self.peak_players = players

        if self.is_first_collection:
            print(f"  [FIRST] Full sales refresh (baseline, no alerts)...")
            refresh_all_sales(self.config, self.db_path)
        else:
            print(f"  Refreshing sales (yesterday+today)...")
            refresh_recent_sales(self.config, self.db_path)

        totals = get_sales_totals(self.db_path)
        total_units = totals[0]
        net_revenue = totals[3]
        save_sales_snapshot(self.db_path, totals[0], totals[1], totals[3])

        # Hourly cadence for expensive country/wishlist full-scans
        self.collection_count += 1
        if self.collection_count % 12 == 0 or self.is_first_collection:
            try:
                self.cached_sales_by_country = fetch_sales_by_country(self.config)
                self.cached_wishlist_by_country = fetch_wishlist_by_country(self.config)
                print(f"  Countries: {len(self.cached_sales_by_country)} sales, {len(self.cached_wishlist_by_country)} wishlist")
            except Exception as e:
                print(f"  [COUNTRY ERROR] {e}")

            print(f"  Refreshing wishlist data...")
            try:
                self.cached_wishlist = fetch_wishlist_totals(self.config)
                wl_net = self.cached_wishlist.get("net", 0)
                save_wishlist_snapshot(self.db_path, self.cached_wishlist["adds"],
                                       self.cached_wishlist["deletes"],
                                       self.cached_wishlist["purchases"], wl_net)
            except Exception as e:
                wl_net = self.last_wishlist_net
                print(f"  [WISHLIST ERROR] {e}")
        else:
            remaining = 12 - (self.collection_count % 12)
            print(f"  Skipping country/wishlist scan (hourly, next in {remaining} polls)")
            wl_net = self.last_wishlist_net

        # Telegram alerts
        if self.is_first_collection:
            print(f"  [FIRST] Baseline set -- units:{total_units}, wl:{wl_net}, reviews:{total_reviews}, players:{players}")
            self.last_wishlist_net = wl_net
            self.last_player_count = players
            self.last_review_count = total_reviews
            self.last_total_units = total_units
            self.is_first_collection = False
            print(f"  Players: {players} | Reviews: {total_reviews} | Sales: {total_units} | Peak: {self.peak_players}")
            return

        # Wishlist change (5+)
        if self.last_wishlist_net > 0 and abs(wl_net - self.last_wishlist_net) >= 5:
            diff = wl_net - self.last_wishlist_net
            direction = "UP" if diff > 0 else "DOWN"
            send_telegram(self.config,
                f"\u2b50 <b>Wishlist {direction}!</b>\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"  Change: {'+' if diff > 0 else ''}{diff}\n"
                f"  Total adds: {self.cached_wishlist.get('adds', 0)}\n"
                f"  Conversions: {self.cached_wishlist.get('purchases', 0)}\n"
                f"  Net: ~{wl_net}"
            )
        self.last_wishlist_net = wl_net

        # Player spike
        if self.last_player_count > 0 and players > self.last_player_count * 1.5 and players >= 5:
            send_telegram(self.config,
                f"\U0001f680 <b>Player spike!</b>\n{self.last_player_count} -> {players}")

        # New review
        if self.last_review_count > 0 and total_reviews > self.last_review_count:
            n = total_reviews - self.last_review_count
            send_telegram(self.config,
                f"\U0001f4dd <b>New review{'s' if n > 1 else ''} ({n})!</b>\n"
                f"Total {total_reviews} (+{total_positive} -{total_negative})")

        # New sale
        if self.last_total_units > 0 and total_units > self.last_total_units:
            new_sales = total_units - self.last_total_units
            country_lines = ""
            if self.cached_sales_by_country:
                sorted_countries = sorted(self.cached_sales_by_country.items(),
                                          key=lambda x: x[1].get("units", 0), reverse=True)
                top3 = sorted_countries[:3]
                if top3:
                    lines = [f"  {cc}: {d['units']} units" for cc, d in top3]
                    country_lines = "\n\nTop countries (cumulative):\n" + "\n".join(lines)
            send_telegram(self.config,
                f"\U0001f4b0 <b>New sale{'s' if new_sales > 1 else ''} +{new_sales}!</b>\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"  Total: {total_units}\n"
                f"  Net revenue: ${net_revenue:.0f}\n"
                f"  Players: {players}"
                f"{country_lines}"
            )

        self.last_player_count = players
        self.last_review_count = total_reviews
        self.last_total_units = total_units

        print(f"  Players: {players} | Reviews: {total_reviews} | Sales: {total_units} | Peak: {self.peak_players}")

    def loop(self):
        interval = self.config.get('dashboard', {}).get('poll_interval', 300)
        while True:
            try:
                self.collect()
            except Exception as e:
                print(f"[COLLECTOR ERROR] {e}")
            time.sleep(interval)


# ========== HTML TEMPLATE ==========

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
  --bg-black: #080509;
  --bg-deep: #0d0a0e;
  --bg-mid: #1a0f14;
  --bg-surface: #241520;
  --bg-elevated: #2e1a28;
  --border-color: #3a2030;
  --border-light: #4a2a3c;
  --text-primary: #e8ddd0;
  --text-secondary: #9a8878;
  --text-tertiary: #6a5a4e;
  --text-accent: #d4c0a0;
  --gold: #c9a84c;
  --gold-bright: #dbb94e;
  --gold-dim: #8a7434;
  --gold-fill: rgba(201,168,76,0.08);
  --green: #5a9a5e;
  --green-bright: #6cc070;
  --green-dim: #3a6a3e;
  --green-fill: rgba(90,154,94,0.06);
  --red: #c45a5a;
  --purple: #8b5a8a;
  --purple-fill: rgba(139,90,138,0.08);
  --chart-grid: rgba(58,32,48,0.3);
  --chart-tick: #6a5a4e;
  --chart-legend: #9a8878;
  --tooltip-bg: rgba(13,10,14,0.95);
  --tooltip-border: rgba(201,168,76,0.2);
  --status-bg: rgba(8,5,9,0.9);
  --header-bg: linear-gradient(165deg, #0d0a0e 0%, #1a0a10 40%, #200e18 100%);
  --header-glow: rgba(114,47,55,0.12);
  --shimmer-a: #241520;
  --shimmer-b: #2e1a28;
  --review-hover: rgba(114,47,55,0.08);
  --bar-from: var(--accent);
  --bar-to: var(--accent-dim);
}

/* ---- LIGHT THEME ---- */
:root[data-theme="light"] {
  --bg-black: #f5f2ef;
  --bg-deep: #f0ece8;
  --bg-mid: #faf8f6;
  --bg-surface: #ffffff;
  --bg-elevated: #ffffff;
  --border-color: #e0d8d0;
  --border-light: #d0c8c0;
  --text-primary: #2a2420;
  --text-secondary: #6a5a50;
  --text-tertiary: #9a8a80;
  --text-accent: #4a3a30;
  --gold: #b89830;
  --gold-bright: #c9a84c;
  --gold-dim: #8a7434;
  --gold-fill: rgba(184,152,48,0.1);
  --green: #3a8a3e;
  --green-bright: #4a9a50;
  --green-dim: #2a6a2e;
  --green-fill: rgba(58,138,62,0.08);
  --red: #c04040;
  --purple: #7a4a7a;
  --purple-fill: rgba(122,74,122,0.08);
  --chart-grid: rgba(0,0,0,0.06);
  --chart-tick: #9a8a80;
  --chart-legend: #6a5a50;
  --tooltip-bg: rgba(255,255,255,0.97);
  --tooltip-border: rgba(0,0,0,0.1);
  --status-bg: rgba(245,242,239,0.95);
  --header-bg: linear-gradient(165deg, #f0ece8 0%, #faf8f6 100%);
  --header-glow: rgba(114,47,55,0.06);
  --shimmer-a: #f0ece8;
  --shimmer-b: #e8e0d8;
  --review-hover: rgba(0,0,0,0.03);
  --bar-from: var(--accent);
  --bar-to: var(--accent-dim);
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
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  opacity: 0.025;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
}

:root[data-theme="light"] body::after { opacity: 0.012; }

/* --- HEADER --- */
.header {
  position: relative;
  background: var(--header-bg);
  padding: 28px 32px;
  display: flex;
  align-items: center;
  gap: 24px;
  border-bottom: 1px solid var(--border-color);
  overflow: hidden;
}

.header::before {
  content: '';
  position: absolute;
  top: -50%;
  right: -10%;
  width: 400px;
  height: 400px;
  background: radial-gradient(circle, var(--header-glow) 0%, transparent 70%);
  pointer-events: none;
}

.header-img {
  width: 180px;
  border-radius: var(--radius-md);
  box-shadow: 0 8px 32px rgba(0,0,0,0.3), 0 0 0 1px rgba(201,168,76,0.15);
  flex-shrink: 0;
}

.header-info { flex: 1; min-width: 0; }

.header-info h1 {
  font-family: var(--font-display);
  font-size: 32px;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.02em;
  margin-bottom: 4px;
}

.header-info .subtitle {
  font-size: 13px;
  color: var(--text-tertiary);
  margin-bottom: 10px;
}

.header-info .price-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: linear-gradient(135deg, rgba(201,168,76,0.12), rgba(201,168,76,0.06));
  border: 1px solid rgba(201,168,76,0.2);
  color: var(--gold);
  padding: 5px 14px;
  border-radius: 6px;
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 500;
}

.live-badge {
  margin-left: auto;
  text-align: right;
  flex-shrink: 0;
}

.live-indicator {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--green);
  margin-bottom: 6px;
}

.live-dot {
  width: 7px;
  height: 7px;
  background: var(--green-bright);
  border-radius: 50%;
  box-shadow: 0 0 8px rgba(108,192,112,0.5);
  animation: livePulse 2.5s ease-in-out infinite;
}

@keyframes livePulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 8px rgba(108,192,112,0.5); }
  50% { opacity: 0.4; box-shadow: 0 0 4px rgba(108,192,112,0.2); }
}

.live-badge .update-time {
  font-size: 12px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}

.live-badge .poll-info {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 2px;
  opacity: 0.6;
}

.lang-toggle {
  display: inline-flex;
  gap: 0;
  border-radius: 4px;
  overflow: hidden;
  border: 1px solid var(--border-color);
  font-size: 11px;
  margin-top: 4px;
}

.lang-toggle button {
  background: transparent;
  border: none;
  color: var(--text-tertiary);
  padding: 3px 8px;
  cursor: pointer;
  font-family: var(--font-mono);
  font-size: 11px;
  transition: all 0.2s;
}

.lang-toggle button.active {
  background: var(--bg-elevated);
  color: var(--text-primary);
}

/* --- MAIN LAYOUT --- */
.dashboard {
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px 24px 48px;
}

/* --- METRIC CARDS --- */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-bottom: 14px;
}

.metric-card {
  position: relative;
  background: linear-gradient(170deg, var(--bg-mid) 0%, var(--bg-surface) 100%);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  padding: 18px 20px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
  overflow: hidden;
}

.metric-card:hover {
  border-color: var(--border-light);
  transform: translateY(-2px);
}

.metric-card::after {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}

.metric-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-tertiary);
  margin-bottom: 8px;
}

.metric-value {
  font-family: var(--font-display);
  font-size: 32px;
  font-weight: 700;
  color: var(--text-primary);
  line-height: 1.1;
  letter-spacing: -0.02em;
}

.metric-value.gold { color: var(--gold); }
.metric-value.green { color: var(--green); }

.metric-sub {
  font-size: 12px;
  color: var(--text-tertiary);
  margin-top: 6px;
  font-family: var(--font-mono);
  font-weight: 400;
}

/* --- CHART CARDS --- */
.charts-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
  margin-bottom: 14px;
}

.charts-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 14px;
}

.chart-card {
  position: relative;
  background: linear-gradient(170deg, var(--bg-mid) 0%, var(--bg-surface) 100%);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  padding: 20px 22px;
  overflow: hidden;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}

.chart-card:hover {
  border-color: var(--border-light);
  transform: translateY(-2px);
}

.chart-card::after {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}

.chart-card h3 {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 500;
  color: var(--text-accent);
  margin-bottom: 16px;
  letter-spacing: -0.01em;
}

.chart-card canvas {
  width: 100% !important;
}

/* --- SECTION TITLES --- */
.section-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
  margin-top: 8px;
}

.section-header h2 {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 500;
  color: var(--text-accent);
  letter-spacing: -0.01em;
}

.section-header::after {
  content: '';
  flex: 1;
  height: 1px;
  background: linear-gradient(90deg, var(--border-color), transparent);
}

/* --- COUNTRY TABLES --- */
.country-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 14px;
}

.country-card {
  position: relative;
  background: linear-gradient(170deg, var(--bg-mid) 0%, var(--bg-surface) 100%);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  padding: 20px 22px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}

.country-card:hover {
  border-color: var(--border-light);
  transform: translateY(-2px);
}

.country-card::after {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}

.country-card > div { overflow-x: auto; }

.country-card h3 {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 500;
  color: var(--text-accent);
  margin-bottom: 14px;
}

.country-table {
  width: 100%;
  border-collapse: collapse;
}

.country-table tr {
  border-bottom: 1px solid rgba(58,32,48,0.4);
  transition: background 0.2s;
}

:root[data-theme="light"] .country-table tr {
  border-bottom: 1px solid rgba(0,0,0,0.06);
}

.country-table tr:hover {
  background: var(--review-hover);
}

.country-table td {
  padding: 7px 0;
  font-size: 13px;
}

.country-table .cc {
  font-weight: 600;
  color: var(--text-secondary);
  width: 100px;
}

.country-table .bar-cell {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--accent);
  letter-spacing: -0.05em;
}

.country-table .val {
  text-align: right;
  font-family: var(--font-mono);
  font-weight: 500;
  color: var(--text-primary);
  width: 60px;
}

/* --- REVIEWS --- */
.reviews-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px;
}

.review-card {
  background: linear-gradient(170deg, var(--bg-mid) 0%, var(--bg-surface) 100%);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  padding: 18px 22px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}

.review-card:hover {
  border-color: var(--border-light);
  transform: translateY(-2px);
}

.review-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.review-thumb {
  font-size: 18px;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  flex-shrink: 0;
}

.review-thumb.up { background: rgba(90,154,94,0.15); }
.review-thumb.down { background: rgba(196,90,90,0.15); }

.review-author {
  font-weight: 600;
  font-size: 13px;
  color: var(--text-secondary);
}

.review-playtime {
  margin-left: auto;
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
}

.review-text {
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--text-secondary);
  max-height: 80px;
  overflow: hidden;
  -webkit-mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
  mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
}

/* --- STATUS BAR --- */
.status-bar {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  background: var(--status-bg);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  padding: 8px 24px;
  display: flex;
  align-items: center;
  gap: 20px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
  border-top: 1px solid var(--border-color);
  z-index: 100;
}

.status-bar .dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-right: 4px;
  vertical-align: middle;
}

.status-bar .dot.on { background: var(--green-bright); box-shadow: 0 0 4px rgba(108,192,112,0.4); }
.status-bar .dot.off { background: var(--red); }

/* --- LOADING SHIMMER --- */
@keyframes shimmer {
  0% { background-position: -200px 0; }
  100% { background-position: 200px 0; }
}

.metric-value.loading {
  background: linear-gradient(90deg, var(--shimmer-a) 0%, var(--shimmer-b) 40%, var(--shimmer-a) 80%);
  background-size: 400px 100%;
  animation: shimmer 1.8s ease-in-out infinite;
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

/* --- ANIMATIONS --- */
.metric-card, .chart-card, .country-card, .review-card {
  animation: fadeUp 0.5s var(--ease-out) both;
}

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

/* --- RESPONSIVE --- */
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
  .header {
    flex-direction: column;
    align-items: flex-start;
    padding: 16px;
    gap: 14px;
  }
  .header-img {
    width: 100%;
    max-width: none;
    height: auto;
    max-height: 160px;
    object-fit: cover;
    border-radius: var(--radius-sm);
  }
  .header-info h1 { font-size: 22px; }
  .live-badge {
    margin-left: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    width: 100%;
  }
  .live-badge .poll-info { display: none; }
  .dashboard { padding: 14px 10px 72px; }
  .metrics-grid {
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .metric-card { padding: 14px 16px; }
  .metric-value { font-size: 26px; }
  .metric-label { font-size: 10px; }
  .metric-sub { font-size: 11px; }
  .chart-card { padding: 16px 14px; }
  .chart-card canvas { min-height: 150px; }
  .section-header { padding: 0 4px; }
  .section-header h2 { font-size: 17px; }
  .review-card { padding: 14px 16px; }
  .status-bar {
    padding: 6px 12px;
    gap: 10px;
    font-size: 10px;
  }
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
  <div class="live-badge">
    <div class="live-indicator"><span class="live-dot"></span>LIVE</div>
    <div class="update-time" id="lastUpdate">--</div>
    <div class="poll-info" data-i18n="pollInfo">5min poll</div>
    <div class="lang-toggle" id="langToggle">
      <button id="langKo" onclick="setLang('ko')">KR</button>
      <button id="langEn" onclick="setLang('en')">EN</button>
    </div>
  </div>
</div>

<div class="dashboard">

  <!-- Row 1: Sales & Revenue -->
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

  <!-- Row 2: Reviews & Wishlist -->
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

  <!-- Charts -->
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

  <!-- Country Breakdown -->
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

  <!-- Reviews -->
  <div class="section-header"><h2 data-i18n="recentReviews">Recent Reviews</h2></div>

  <div class="reviews-grid" id="recentReviews"></div>

</div>

<div class="status-bar">
  <span>App ID: {{APP_ID}}</span>
  <span>Poll: {{POLL_INTERVAL}}s</span>
  <span>Telegram: <span class="dot" id="tgDot"></span> <span id="tgStatus"></span></span>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
(function() {
  // Apply theme and accent from config
  var rootEl = document.documentElement;
  rootEl.setAttribute('data-theme', '{{THEME}}');
  rootEl.setAttribute('data-accent', '{{ACCENT}}');

  var playerChart, salesChart, salesTimelineChart;
  var curLang = localStorage.getItem('dashLang') || '{{LANGUAGE}}';

  var i18n = {
    ko: {
      totalSales: '\uCD1D \uD310\uB9E4',
      netRevenue: '\uC21C\uC218\uC775',
      playersOnline: '\uD604\uC7AC \uB3D9\uC811',
      peakPlayers: '\uD53C\uD06C \uB3D9\uC811',
      sessionHigh: '\uC138\uC158 \uCD5C\uACE0\uCE58',
      reviews: '\uB9AC\uBDF0',
      positiveRate: '\uAE0D\uC815\uB960',
      wishlists: '\uC704\uC2DC\uB9AC\uC2A4\uD2B8',
      refundRate: '\uD658\uBD88\uB960',
      refundSales: '\uD658\uBD88 / \uD310\uB9E4',
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
      refunds: '\uD658\uBD88',
      grossLabel: '\uCD1D\uB9E4\uCD9C',
      beforeFees: '\uC218\uC218\uB8CC \uC804',
      conversion: '\uAD6C\uB9E4\uC804\uD658',
      hours: '\uC2DC\uAC04',
      unitSuffix: '\uAC74',
      chartCumSales: '\uB204\uC801 \uD310\uB9E4 (\uAC74)',
      chartCumRev: '\uB204\uC801 \uC21C\uC218\uC775 ($)',
      chartSales: '\uD310\uB9E4 (\uAC74)',
      chartRefunds: '\uD658\uBD88',
      chartNetRev: '\uC21C\uC218\uC775 ($)',
      chartUnits: '\uAC74\uC218',
      chartRevenue: '\uC218\uC775 ($)',
      chartPlayers: '\uB3D9\uC811',
      chartSalesAxis: '\uD310\uB9E4 (\uAC74)',
      chartRevenueAxis: '\uC218\uC775 ($)'
    },
    en: {
      totalSales: 'Total Sales',
      netRevenue: 'Net Revenue',
      playersOnline: 'Players Online',
      peakPlayers: 'Peak Players',
      sessionHigh: 'Session high',
      reviews: 'Reviews',
      positiveRate: 'Positive Rate',
      wishlists: 'Wishlists',
      refundRate: 'Refund Rate',
      refundSales: 'returns / sales',
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
      refunds: 'refunds',
      grossLabel: 'gross',
      beforeFees: 'before fees',
      conversion: 'conv.',
      hours: 'h',
      unitSuffix: '',
      chartCumSales: 'Cumulative Sales',
      chartCumRev: 'Net Revenue ($)',
      chartSales: 'Sales',
      chartRefunds: 'Refunds',
      chartNetRev: 'Net Revenue ($)',
      chartUnits: 'Units',
      chartRevenue: 'Revenue ($)',
      chartPlayers: 'Players',
      chartSalesAxis: 'Sales',
      chartRevenueAxis: 'Revenue ($)'
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
    var base = {
      responsive: true,
      animation: { duration: 500, easing: 'easeOutQuart' },
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          ticks: { color: cc.tick, maxTicksLimit: 12, font: { family: "'JetBrains Mono'", size: 10 } },
          grid: { color: cc.grid, lineWidth: 0.5 },
          border: { display: false }
        },
        y: {
          ticks: { color: cc.tick, font: { family: "'JetBrains Mono'", size: 10 } },
          grid: { color: cc.grid, lineWidth: 0.5 },
          border: { display: false },
          beginAtZero: true
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: cc.tooltipBg,
          borderColor: cc.tooltipBorder,
          borderWidth: 1,
          titleFont: { family: "'DM Sans'", weight: '600' },
          bodyFont: { family: "'JetBrains Mono'", size: 12 },
          padding: 12,
          cornerRadius: 8,
          displayColors: true,
          boxPadding: 4
        }
      }
    };

    salesTimelineChart = new Chart(document.getElementById('salesTimelineChart'), {
      type: 'line',
      data: {
        labels: [],
        datasets: [{
          label: T('chartCumSales'),
          data: [],
          borderColor: cc.gold,
          backgroundColor: cc.goldFill,
          fill: true, tension: 0.35, pointRadius: pr, pointHoverRadius: phr,
          pointBackgroundColor: cc.gold,
          pointBorderColor: 'transparent',
          borderWidth: 2.5, yAxisID: 'y'
        }, {
          label: T('chartCumRev'),
          data: [],
          borderColor: cc.green,
          backgroundColor: 'transparent',
          borderDash: [6, 4], tension: 0.35, pointRadius: Math.max(1, pr - 1), pointHoverRadius: Math.max(2, phr - 1),
          pointBackgroundColor: cc.green,
          pointBorderColor: 'transparent',
          borderWidth: 2, yAxisID: 'y1'
        }]
      },
      options: {
        responsive: base.responsive,
        animation: base.animation,
        interaction: base.interaction,
        plugins: {
          legend: { display: true, labels: { color: cc.legend, usePointStyle: true, pointStyle: 'circle', padding: 16, font: { family: "'DM Sans'", size: 12 } } },
          tooltip: base.plugins.tooltip
        },
        scales: {
          x: { ticks: { color: cc.tick, maxTicksLimit: 20, font: { family: "'JetBrains Mono'", size: 10 } }, grid: { color: cc.grid, lineWidth: 0.5 }, border: { display: false } },
          y: { ticks: { color: cc.tick, font: { family: "'JetBrains Mono'", size: 10 } }, grid: { color: cc.grid, lineWidth: 0.5 }, border: { display: false }, beginAtZero: true, position: 'left', title: { display: !isMobile, text: T('chartSalesAxis'), color: cc.tick, font: { family: "'DM Sans'", size: 11 } } },
          y1: { ticks: { color: cc.tick, font: { family: "'JetBrains Mono'", size: 10 } }, grid: { drawOnChartArea: false }, border: { display: false }, beginAtZero: true, position: 'right', title: { display: !isMobile, text: T('chartRevenueAxis'), color: cc.tick, font: { family: "'DM Sans'", size: 11 } } }
        }
      }
    });

    salesChart = new Chart(document.getElementById('salesChart'), {
      type: 'bar',
      data: {
        labels: [],
        datasets: [
          { label: T('chartSales'), data: [], backgroundColor: cc.gold, borderRadius: 4, yAxisID: 'y', order: 2, barPercentage: 0.7 },
          { label: T('chartRefunds'), data: [], backgroundColor: cc.red, borderRadius: 4, yAxisID: 'y', order: 3, barPercentage: 0.7 },
          { label: T('chartNetRev'), data: [], type: 'line', borderColor: cc.green, backgroundColor: 'transparent',
            borderWidth: 2, pointRadius: Math.max(1, pr - 1), pointHoverRadius: Math.max(2, phr - 1), pointBackgroundColor: cc.green, pointBorderColor: 'transparent', tension: 0.35, yAxisID: 'y1', order: 1 }
        ]
      },
      options: {
        responsive: base.responsive,
        animation: base.animation,
        interaction: base.interaction,
        plugins: {
          legend: { display: true, labels: { color: cc.legend, usePointStyle: true, pointStyle: 'circle', padding: 16, font: { family: "'DM Sans'", size: 12 } } },
          tooltip: base.plugins.tooltip
        },
        scales: {
          x: base.scales.x,
          y: { ticks: { color: cc.tick, font: { family: "'JetBrains Mono'", size: 10 } }, grid: { color: cc.grid, lineWidth: 0.5 }, border: { display: false }, beginAtZero: true, position: 'left', title: { display: !isMobile, text: T('chartUnits'), color: cc.tick, font: { family: "'DM Sans'", size: 11 } } },
          y1: { ticks: { color: cc.tick, font: { family: "'JetBrains Mono'", size: 10 } }, grid: { drawOnChartArea: false }, border: { display: false }, beginAtZero: true, position: 'right', title: { display: !isMobile, text: T('chartRevenueAxis'), color: cc.tick, font: { family: "'DM Sans'", size: 11 } } }
        }
      }
    });

    playerChart = new Chart(document.getElementById('playerChart'), {
      type: 'line',
      data: {
        labels: [],
        datasets: [{
          label: T('chartPlayers'),
          data: [],
          borderColor: cc.purple,
          backgroundColor: cc.purpleFill,
          fill: true, tension: 0.35, pointRadius: isMobile ? 1 : 1.5, pointHoverRadius: isMobile ? 2 : 4,
          pointBackgroundColor: cc.purple,
          pointBorderColor: 'transparent',
          borderWidth: 2
        }]
      },
      options: base
    });
  }

  function fetchData() {
    fetch('/api/data').then(function(resp) {
      return resp.json();
    }).then(function(data) {
      // Game info
      if (data.app_details) {
        var d = data.app_details;
        document.getElementById('gameName').textContent = d.name || '';
        document.getElementById('gameDev').textContent = (d.developers || []).join(', ') + ' \u00B7 ' + (d.publishers || []).join(', ');
        document.getElementById('headerImg').src = d.header_image || '';
        if (d.price_overview) document.getElementById('gamePrice').textContent = d.price_overview.final_formatted || '';
      }

      // Remove loading shimmer
      document.querySelectorAll('.metric-value.loading').forEach(function(el) { el.classList.remove('loading'); });

      // Sales totals
      var s = data.sales_totals || {};
      document.getElementById('totalSales').textContent = (s.units || 0).toLocaleString();
      var suffix = T('unitSuffix');
      document.getElementById('salesSub').textContent = T('refunds') + ' ' + (s.returns || 0) + (suffix ? suffix : '') + ' \u00B7 ' + T('grossLabel') + ' $' + (s.gross || 0).toFixed(0);
      document.getElementById('netRevenue').textContent = '$' + (s.net || 0).toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 0});
      document.getElementById('revenueSub').textContent = T('beforeFees') + ' $' + (s.gross || 0).toFixed(0);
      var refRate = s.units > 0 ? ((s.returns / s.units) * 100).toFixed(1) : '0';
      document.getElementById('refundRate').textContent = refRate + '%';

      // Sales timeline (12h)
      var timeline = data.sales_timeline || [];
      salesTimelineChart.data.labels = timeline.map(function(r) {
        var d = new Date(r[0]);
        return (d.getMonth()+1) + '/' + d.getDate() + ' ' + (d.getHours() < 12 ? 'AM' : 'PM');
      });
      salesTimelineChart.data.datasets[0].data = timeline.map(function(r) { return r[1]; });
      salesTimelineChart.data.datasets[1].data = timeline.map(function(r) { return r[3]; });
      salesTimelineChart.update('none');

      // Daily sales chart
      var daily = data.daily_sales || [];
      salesChart.data.labels = daily.map(function(r) { return r[0].substring(5); });
      salesChart.data.datasets[0].data = daily.map(function(r) { return r[1]; });
      salesChart.data.datasets[1].data = daily.map(function(r) { return -r[2]; });
      salesChart.data.datasets[2].data = daily.map(function(r) { return r[4]; });
      salesChart.update('none');

      // Players
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

      // Player chart
      playerChart.data.labels = hist.map(function(r) {
        var d = new Date(r[0]);
        return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0');
      });
      playerChart.data.datasets[0].data = hist.map(function(r) { return r[1]; });
      playerChart.update('none');

      // Reviews
      var rev = data.reviews || {};
      var total = rev.total_reviews || 0, pos = rev.total_positive || 0, neg = rev.total_negative || 0;
      document.getElementById('totalReviews').textContent = total;
      document.getElementById('reviewRatio').innerHTML = '\uD83D\uDC4D ' + pos + ' / \uD83D\uDC4E ' + neg;
      document.getElementById('positiveRate').textContent = total > 0 ? Math.round(pos/total*100) + '%' : '--';
      document.getElementById('reviewScore').textContent = rev.review_score_desc || '';

      // Wishlist
      var wl = data.wishlist || {};
      var wlNet = wl.net || 0;
      document.getElementById('wishlistNet').textContent = '~' + wlNet.toLocaleString();
      document.getElementById('wishlistSub').textContent = '+' + (wl.adds||0) + ' / -' + (wl.deletes||0) + ' / ' + T('conversion') + ' ' + (wl.purchases||0);

      // Country data
      var sc = data.sales_by_country || {};
      var wlc = data.wishlist_by_country || {};

      var esc = function(str) { var d = document.createElement('div'); d.textContent = String(str); return d.innerHTML; };

      var renderCountryTable = function(obj, valFn) {
        var entries = Object.entries(obj).slice(0, 15);
        if (!entries.length) return '<div style="color:var(--text-tertiary);font-style:italic;padding:12px 0;">' + T('collecting') + '</div>';
        var maxVal = Math.max(1, valFn(entries[0][1]));
        return '<table class="country-table">' +
          entries.map(function(entry) {
            var cc = esc(entry[0]);
            var d = entry[1];
            var val = valFn(d);
            var pct = Math.round(val / maxVal * 100);
            return '<tr>' +
              '<td class="cc">' + cc + '</td>' +
              '<td class="bar-cell"><div style="background:linear-gradient(90deg, var(--accent), var(--accent-dim));width:' + pct + '%;height:7px;border-radius:3px;min-width:6px;box-shadow:0 0 6px var(--accent-glow);"></div></td>' +
              '<td class="val">' + val + '</td></tr>';
          }).join('') + '</table>';
      };

      document.getElementById('salesByCountry').innerHTML = renderCountryTable(sc, function(d) { return d.units || 0; });
      document.getElementById('wishlistByCountry').innerHTML = renderCountryTable(wlc, function(d) { return d.adds || 0; });

      // Recent reviews
      var recent = data.recent_reviews || [];
      document.getElementById('recentReviews').innerHTML = recent.map(function(r) {
        var isUp = r.voted_up;
        var thumb = isUp ? '\uD83D\uDC4D' : '\uD83D\uDC4E';
        var thumbClass = isUp ? 'up' : 'down';
        var playtime = Math.round((r.author && r.author.playtime_forever || 0) / 60 * 10) / 10;
        var text = esc((r.review || '').substring(0, 300)).split(String.fromCharCode(10)).join(' ');
        return '<div class="review-card">' +
          '<div class="review-header">' +
          '<span class="review-thumb ' + thumbClass + '">' + thumb + '</span>' +
          '<span class="review-author">' + esc(r.author && r.author.personaname || 'Anonymous') + '</span>' +
          '<span class="review-playtime">' + playtime + T('hours') + '</span>' +
          '</div>' +
          '<div class="review-text">' + text + '</div>' +
          '</div>';
      }).join('');

      // Status
      document.getElementById('tgDot').className = 'dot ' + (data.telegram_active ? 'on' : 'off');
      document.getElementById('tgStatus').textContent = data.telegram_active ? 'ON' : 'OFF';
      document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();

      fetchFailCount = 0;
    }).catch(function(e) {
      console.error('Fetch error:', e);
      fetchFailCount++;
    });
  }

  // Initialize
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

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ('/', '/dashboard'):
            html = self.server.dashboard_html
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html.encode('utf-8'))

        elif parsed.path == '/api/data':
            config = self.server.config
            db_path = self.server.db_path
            collector = self.server.collector

            players = get_current_players(config)
            reviews = get_reviews(config)
            recent = get_recent_reviews(config)
            app_details = get_app_details(config)
            p_history = get_player_history(db_path)
            daily = get_all_daily_sales(db_path)
            timeline = get_sales_snapshots(db_path)
            totals = get_sales_totals(db_path)
            wl_history = get_wishlist_history(db_path)

            tg = config.get('telegram', {})

            payload = {
                "current_players": players,
                "peak_players": collector.peak_players,
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
                "wishlist": collector.cached_wishlist,
                "wishlist_history": wl_history,
                "sales_by_country": collector.cached_sales_by_country,
                "wishlist_by_country": collector.cached_wishlist_by_country,
                "telegram_active": bool(tg.get('enabled') and tg.get('bot_token') and tg.get('chat_ids')),
                "timestamp": datetime.now().isoformat()
            }

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

        else:
            self.send_response(404)
            self.end_headers()


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


# ========== MAIN ==========

def build_html(config):
    dash = config.get('dashboard', {})
    html = DASHBOARD_HTML_TEMPLATE
    html = html.replace('{{APP_ID}}', str(config['app_id']))
    html = html.replace('{{THEME}}', dash.get('theme', 'dark'))
    html = html.replace('{{ACCENT}}', dash.get('accent', 'wine'))
    html = html.replace('{{LANGUAGE}}', dash.get('language', 'en'))
    html = html.replace('{{POLL_INTERVAL}}', str(dash.get('poll_interval', 300)))
    return html


def main():
    config = load_config()
    dash = config.get('dashboard', {})
    db_path = get_db_path(config)
    port = dash.get('port', 8081)
    poll_interval = dash.get('poll_interval', 300)

    init_db(db_path)

    # Fetch game name
    game_name = get_game_name(config)

    tg = config.get('telegram', {})
    tg_on = bool(tg.get('enabled') and tg.get('bot_token') and tg.get('chat_ids'))
    tg_count = len(tg.get('chat_ids', [])) if tg_on else 0

    print("=" * 50)
    print(f"  Steam Dashboard v{VERSION}")
    print("=" * 50)
    print(f"  Game:       {game_name}")
    print(f"  App ID:     {config['app_id']}")
    print(f"  Dashboard:  http://localhost:{port}")
    print(f"  Polling:    {poll_interval // 60}min")
    print(f"  Telegram:   {'ON (' + str(tg_count) + ' recipients)' if tg_on else 'OFF'}")
    print(f"  Theme:      {dash.get('theme', 'dark')} / {dash.get('accent', 'wine')}")
    print(f"  Language:   {dash.get('language', 'en')}")
    print("=" * 50)

    # Build HTML
    dashboard_html = build_html(config)

    # Create collector
    collector = DataCollector(config, db_path)

    # Start HTTP server
    server = ReusableHTTPServer(('0.0.0.0', port), DashboardHandler)
    server.config = config
    server.db_path = db_path
    server.collector = collector
    server.dashboard_html = dashboard_html

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"\n[READY] Dashboard at http://localhost:{port}")

    # Initial data load
    existing_totals = get_sales_totals(db_path)
    if existing_totals[0] > 0:
        print(f"\n[INIT] Existing data found: {existing_totals[0]} units, ${existing_totals[3]:.2f} net")
        print("[INIT] Refreshing latest data only...")
        refresh_recent_sales(config, db_path)
    else:
        print("\n[INIT] No existing data. Fetching all sales since launch...")
        refresh_all_sales(config, db_path)

    totals = get_sales_totals(db_path)
    collector.last_total_units = totals[0]
    save_sales_snapshot(db_path, totals[0], totals[1], totals[3])

    # Country data initial load
    print("[INIT] Fetching country data...")
    try:
        collector.cached_sales_by_country = fetch_sales_by_country(config)
        collector.cached_wishlist_by_country = fetch_wishlist_by_country(config)
        print(f"[INIT] Countries: {len(collector.cached_sales_by_country)} sales, {len(collector.cached_wishlist_by_country)} wishlist")
    except Exception as e:
        print(f"[INIT] Country fetch error: {e}")

    # Wishlist initial load
    print("[INIT] Fetching wishlist data...")
    try:
        collector.cached_wishlist = fetch_wishlist_totals(config)
        collector.last_wishlist_net = collector.cached_wishlist.get("net", 0)
        save_wishlist_snapshot(db_path, collector.cached_wishlist["adds"],
                               collector.cached_wishlist["deletes"],
                               collector.cached_wishlist["purchases"],
                               collector.last_wishlist_net)
    except Exception as e:
        print(f"[INIT] Wishlist fetch error: {e}")

    print(f"[INIT] Sales: {totals[0]} units | Revenue: ${totals[3]:.2f} | Wishlists: ~{collector.last_wishlist_net}")

    # Startup telegram report
    if tg_on:
        print("[INIT] Sending startup report to Telegram...")
        send_startup_report(config, db_path)

    # Mark first collection done (baseline is set via INIT)
    collector.is_first_collection = False
    collector.last_player_count = get_current_players(config)
    reviews = get_reviews(config)
    collector.last_review_count = reviews.get("total_reviews", 0)

    # Background collector
    collector_thread = threading.Thread(target=collector.loop, daemon=True)
    collector_thread.start()

    # Keep main thread alive
    try:
        server_thread.join()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
