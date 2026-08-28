# API Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate duplicate Steam API calls by fetching sales and wishlist data once per date per endpoint, storing everything in two tables with `__all__` sentinel rows for totals.

**Architecture:** Replace 6 fetch/walk functions with 4 (two fetch, two walk). Remove `daily_sales` and `wishlist_totals_daily` tables. All readers switch to `sales_by_country_daily` and `wishlists_by_country_daily` with `country_code='__all__'` for totals. Nuke the DB instead of migrating.

**Tech Stack:** Python stdlib, SQLite

---

### Task 1: Consolidate sales fetch and walk

**Files:**
- Modify: `dashboard.py` (functions: `fetch_sales_for_date`, `fetch_sales_by_country`, `refresh_all_sales`, `refresh_recent_sales`, schema in `init_db`)

- [ ] **Step 1: Update `sales_by_country_daily` schema to include `gross_usd`**

In `init_db()`, change the `sales_by_country_daily` CREATE TABLE from:

```python
    c.execute('''CREATE TABLE IF NOT EXISTS sales_by_country_daily (
        app_id TEXT, date TEXT, country_code TEXT,
        units INTEGER, returns INTEGER, net_usd REAL,
        PRIMARY KEY (app_id, date, country_code)
    )''')
```

To:

```python
    c.execute('''CREATE TABLE IF NOT EXISTS sales_by_country_daily (
        app_id TEXT, date TEXT, country_code TEXT,
        units INTEGER, returns INTEGER, gross_usd REAL, net_usd REAL,
        PRIMARY KEY (app_id, date, country_code)
    )''')
```

- [ ] **Step 2: Remove `daily_sales` table from `init_db()`**

Delete the `daily_sales` CREATE TABLE statement and the `upsert_daily_sales` function entirely.

- [ ] **Step 3: Rewrite `fetch_sales_for_date` to return totals + country breakdown**

Replace the existing `fetch_sales_for_date` function with:

```python
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
```

- [ ] **Step 4: Delete `fetch_sales_by_country` and `refresh_recent_sales`**

Remove both functions entirely.

- [ ] **Step 5: Rewrite `refresh_all_sales` to write both country rows and `__all__` row**

Replace `refresh_all_sales` with:

```python
def refresh_all_sales(financial_key, app_id, launch_date, on_progress=None):
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

    skipped = 0
    current = today
    while current >= launch:
        ds = current.strftime("%Y-%m-%d")
        if ds in existing and ds not in always_refresh:
            current -= timedelta(days=1)
            continue
        if on_progress:
            on_progress(ds)
        result = fetch_sales_for_date(financial_key, app_id, ds)
        if result is None:
            skipped += 1
            current -= timedelta(days=1)
            continue
        totals = result["totals"]
        conn = get_conn()
        # Write __all__ totals row
        conn.execute(
            "INSERT OR REPLACE INTO sales_by_country_daily VALUES (?, ?, '__all__', ?, ?, ?, ?)",
            (app_id, ds, totals[0], totals[1], totals[2], totals[3])
        )
        # Write per-country rows
        for cc, d in result["by_country"].items():
            conn.execute(
                "INSERT OR REPLACE INTO sales_by_country_daily VALUES (?, ?, ?, ?, ?, ?, ?)",
                (app_id, ds, cc, d["units"], d["returns"], d["gross"], d["net"])
            )
        conn.commit()
        conn.close()
        if totals[0] > 0 or totals[1] > 0:
            print(f"  [{app_id}] [{ds}] +{totals[0]} sold, -{totals[1]} returned, ${totals[3]:.2f} net")
        current -= timedelta(days=1)
    if skipped:
        print(f"  [{app_id}] WARNING: {skipped} day(s) skipped due to API errors")
```

- [ ] **Step 6: Verify syntax**

Run: `python -c "import py_compile; py_compile.compile('C:/Programming/steam-dashboard/dashboard.py', doraise=True); print('OK')"`
Expected: OK

---

### Task 2: Consolidate wishlist fetch and walk

**Files:**
- Modify: `dashboard.py` (functions: `fetch_wishlist_for_date`, `fetch_wishlist_by_country`, `fetch_wishlist_totals`, schema in `init_db`)

- [ ] **Step 1: Remove `wishlist_totals_daily` table from `init_db()`**

Delete the `wishlist_totals_daily` CREATE TABLE statement.

- [ ] **Step 2: Rewrite `fetch_wishlist_for_date` to return totals + country breakdown**

Replace the existing `fetch_wishlist_for_date` function with:

```python
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
```

- [ ] **Step 3: Delete `fetch_wishlist_by_country` and `fetch_wishlist_totals`**

Remove both functions entirely.

- [ ] **Step 4: Write `refresh_all_wishlists`**

Add this new function:

```python
def refresh_all_wishlists(financial_key, app_id, launch_date, on_progress=None):
    app_id = str(app_id)
    today = datetime.now().date()
    earliest = find_earliest_wishlist_date(financial_key, app_id, launch_date)

    conn = get_conn()
    existing = set(r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM wishlists_by_country_daily WHERE app_id=?", (app_id,)
    ).fetchall())
    conn.close()

    last_collected = max(existing) if existing else None
    always_refresh = {today.strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")}
    if last_collected:
        always_refresh.add(last_collected)

    skipped = 0
    current = today
    while current >= earliest:
        ds = current.strftime("%Y-%m-%d")
        if ds in existing and ds not in always_refresh:
            current -= timedelta(days=1)
            continue
        if on_progress:
            on_progress(ds)
        result = fetch_wishlist_for_date(financial_key, app_id, ds)
        if result is None:
            skipped += 1
            current -= timedelta(days=1)
            continue
        totals = result["totals"]
        conn = get_conn()
        # Write __all__ totals row
        conn.execute(
            "INSERT OR REPLACE INTO wishlists_by_country_daily VALUES (?, ?, '__all__', ?, ?, ?)",
            (app_id, ds, totals["adds"], totals["deletes"], totals["purchases"])
        )
        # Write per-country rows
        for cc, d in result["by_country"].items():
            conn.execute(
                "INSERT OR REPLACE INTO wishlists_by_country_daily VALUES (?, ?, ?, ?, ?, ?)",
                (app_id, ds, cc, d["adds"], d["deletes"], d["purchases"])
            )
        conn.commit()
        conn.close()
        current -= timedelta(days=1)
    if skipped:
        print(f"  [{app_id}] WARNING: {skipped} wishlist day(s) skipped due to API errors")
```

- [ ] **Step 5: Verify syntax**

Run: `python -c "import py_compile; py_compile.compile('C:/Programming/steam-dashboard/dashboard.py', doraise=True); print('OK')"`
Expected: OK

---

### Task 3: Update reader functions

**Files:**
- Modify: `dashboard.py` (functions: `get_all_daily_sales`, `get_sales_totals`, `get_all_games_sales_totals`, `load_sales_by_country`, `load_wishlist_totals`, `load_wishlists_by_country`, `get_daily_wishlists`)

- [ ] **Step 1: Rewrite `get_all_daily_sales`**

Change from reading `daily_sales` to reading `__all__` rows from `sales_by_country_daily`:

```python
def get_all_daily_sales(app_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, units, returns, gross_usd, net_usd FROM sales_by_country_daily "
        "WHERE app_id=? AND country_code='__all__' ORDER BY date",
        (str(app_id),)
    ).fetchall()
    conn.close()
    return rows
```

- [ ] **Step 2: Rewrite `get_sales_totals`**

```python
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
```

- [ ] **Step 3: Rewrite `get_all_games_sales_totals`**

```python
def get_all_games_sales_totals():
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(units),0), COALESCE(SUM(returns),0), "
        "COALESCE(SUM(gross_usd),0), COALESCE(SUM(net_usd),0) "
        "FROM sales_by_country_daily WHERE country_code='__all__'"
    ).fetchone()
    conn.close()
    return row
```

- [ ] **Step 4: Update `load_sales_by_country` to exclude `__all__`**

```python
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
```

- [ ] **Step 5: Rewrite `load_wishlist_totals` to read from `__all__` rows**

```python
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
```

- [ ] **Step 6: Update `load_wishlists_by_country` to exclude `__all__`**

```python
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
```

- [ ] **Step 7: Rewrite `get_daily_wishlists` to read from `__all__` rows**

```python
def get_daily_wishlists(app_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, adds, deletes, purchases FROM wishlists_by_country_daily "
        "WHERE app_id=? AND country_code='__all__' ORDER BY date",
        (str(app_id),)
    ).fetchall()
    conn.close()
    return rows
```

- [ ] **Step 8: Remove `upsert_daily_sales` if not already removed in Task 1**

Delete the function. Grep for any remaining callers and remove them.

- [ ] **Step 9: Verify syntax**

Run: `python -c "import py_compile; py_compile.compile('C:/Programming/steam-dashboard/dashboard.py', doraise=True); print('OK')"`
Expected: OK

---

### Task 4: Update the collector

**Files:**
- Modify: `dashboard.py` (function: `DataCollector.collect`)

- [ ] **Step 1: Replace the sales + hourly cadence block in `collect()`**

Find the section from `# Sales` through the hourly cadence block (approximately lines 740-773). Replace it with:

```python
            # Sales + Wishlists (unified fetch, no separate cadence needed)
            def _set_status(label):
                def _inner(ds):
                    self.status = f"{game_name}: {label} {ds}"
                return _inner

            refresh_all_sales(financial_key, app_id, launch_date, on_progress=_set_status("Fetching sales"))

            totals = get_sales_totals(app_id)
            total_units = totals[0]
            net_revenue = totals[3]
            save_sales_snapshot(app_id, totals[0], totals[1], totals[3])

            refresh_all_wishlists(financial_key, app_id, launch_date, on_progress=_set_status("Fetching wishlists"))

            gs.cached_sales_by_country = load_sales_by_country(app_id)
            gs.cached_wishlist_by_country = load_wishlists_by_country(app_id)
            gs.cached_wishlist = load_wishlist_totals(app_id)
            wl_net = gs.cached_wishlist.get("net", 0)
            save_wishlist_snapshot(app_id, gs.cached_wishlist["adds"],
                                   gs.cached_wishlist["deletes"],
                                   gs.cached_wishlist["purchases"], wl_net)
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "import py_compile; py_compile.compile('C:/Programming/steam-dashboard/dashboard.py', doraise=True); print('OK')"`
Expected: OK

---

### Task 5: Clean up dead code and nuke DB

**Files:**
- Modify: `dashboard.py`
- Delete: `steam_dashboard.db`

- [ ] **Step 1: Remove any remaining references to deleted tables/functions**

Grep for `daily_sales`, `wishlist_totals_daily`, `upsert_daily_sales`, `refresh_recent_sales`, `fetch_sales_by_country`, `fetch_wishlist_by_country`, `fetch_wishlist_totals` and remove any remaining references.

Also remove `get_last_fetched_date` if no longer called (was used by `fetch_sales_by_country`).

- [ ] **Step 2: Delete the database**

Run: `rm C:/Programming/steam-dashboard/steam_dashboard.db`

- [ ] **Step 3: Final syntax check**

Run: `python -c "import py_compile; py_compile.compile('C:/Programming/steam-dashboard/dashboard.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 4: Start the dashboard and verify it runs**

Run: `python C:/Programming/steam-dashboard/dashboard.py` (in background)
Then: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8081/`
Expected: 200

- [ ] **Step 5: Verify game switching is fast and data populates**

Check http://localhost:8081/ in the browser. Confirm:
- Dashboard loads
- Data starts populating as backfill runs
- Switching games is instant (no API calls on switch)
