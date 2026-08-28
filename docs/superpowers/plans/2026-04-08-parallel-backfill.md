# Parallel Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Speed up initial backfill by fetching multiple dates in parallel, with rate limiting and a UI warning if Steam throttles us.

**Architecture:** Add a rate limiter and thread pool to the existing backfill functions. `fetch_json` detects HTTP 429. Warning banner in dashboard UI driven by a `warnings` field in the API response.

**Tech Stack:** Python stdlib (`concurrent.futures`, `threading`, `urllib.error`)

---

### Task 1: Rate limiter and HTTP 429 detection

**Files:**
- Modify: `dashboard.py` (imports, `fetch_json`, new `RateLimiter` class)

- [ ] **Step 1: Add imports**

Add `from urllib.error import HTTPError` to the existing urllib imports (line 18), and add `from concurrent.futures import ThreadPoolExecutor, as_completed` to the imports section.

Change line 18 from:
```python
from urllib.request import urlopen, Request
```
To:
```python
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed
```

- [ ] **Step 2: Add RateLimiter class**

Add this class right before the `fetch_json` function (before the `# ========== HTTP FETCH WITH BACKOFF ==========` section):

```python
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
```

- [ ] **Step 3: Update `fetch_json` to use rate limiter and detect HTTP 429**

Replace the existing `fetch_json` function with:

```python
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
```

- [ ] **Step 4: Add `throttled` flag to DataCollector**

In `DataCollector.__init__`, add after `self.status = ""`:

```python
        self.throttled = False
```

- [ ] **Step 5: Verify syntax**

Run: `python -c "import py_compile; py_compile.compile('C:/Programming/steam-dashboard/dashboard.py', doraise=True); print('OK')"`
Expected: OK

---

### Task 2: Parallelize `refresh_all_sales`

**Files:**
- Modify: `dashboard.py` (function: `refresh_all_sales`)

- [ ] **Step 1: Rewrite `refresh_all_sales` with parallel backfill**

Replace the existing `refresh_all_sales` function with:

```python
def refresh_all_sales(financial_key, app_id, launch_date, on_progress=None, collector=None):
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

    def _write_result(ds, result):
        nonlocal skipped
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
```

- [ ] **Step 2: Update the caller in `DataCollector.collect` to pass `collector=self`**

Find the line that calls `refresh_all_sales` in the collector (should be around line 680). Change:

```python
            refresh_all_sales(financial_key, app_id, launch_date, on_progress=_set_status("Fetching sales"))
```

To:

```python
            refresh_all_sales(financial_key, app_id, launch_date, on_progress=_set_status("Fetching sales"), collector=self)
```

- [ ] **Step 3: Verify syntax**

Run: `python -c "import py_compile; py_compile.compile('C:/Programming/steam-dashboard/dashboard.py', doraise=True); print('OK')"`
Expected: OK

---

### Task 3: Parallelize `refresh_all_wishlists`

**Files:**
- Modify: `dashboard.py` (function: `refresh_all_wishlists`)

- [ ] **Step 1: Rewrite `refresh_all_wishlists` with parallel backfill**

Replace the existing `refresh_all_wishlists` function with:

```python
def refresh_all_wishlists(financial_key, app_id, launch_date, on_progress=None, collector=None):
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

    def _write_result(ds, result):
        nonlocal skipped
        if result is None:
            skipped += 1
            return
        totals = result["totals"]
        c = get_conn()
        c.execute(
            "INSERT OR REPLACE INTO wishlists_by_country_daily VALUES (?, ?, '__all__', ?, ?, ?)",
            (app_id, ds, totals["adds"], totals["deletes"], totals["purchases"])
        )
        for cc, d in result["by_country"].items():
            c.execute(
                "INSERT OR REPLACE INTO wishlists_by_country_daily VALUES (?, ?, ?, ?, ?, ?)",
                (app_id, ds, cc, d["adds"], d["deletes"], d["purchases"])
            )
        c.commit()
        c.close()

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
```

- [ ] **Step 2: Update the caller in `DataCollector.collect` to pass `collector=self`**

Find the line that calls `refresh_all_wishlists` in the collector. Change:

```python
            refresh_all_wishlists(financial_key, app_id, launch_date, on_progress=_set_status("Fetching wishlists"))
```

To:

```python
            refresh_all_wishlists(financial_key, app_id, launch_date, on_progress=_set_status("Fetching wishlists"), collector=self)
```

- [ ] **Step 3: Reset throttled flag at start of collection cycle**

In `DataCollector.collect`, right after the line `self.collection_count += 1` (around line 652), add:

```python
        self.throttled = False
```

- [ ] **Step 4: Verify syntax**

Run: `python -c "import py_compile; py_compile.compile('C:/Programming/steam-dashboard/dashboard.py', doraise=True); print('OK')"`
Expected: OK

---

### Task 4: Warning banner in dashboard UI

**Files:**
- Modify: `dashboard.py` (dashboard HTML template, CSS, JS, API response)

- [ ] **Step 1: Add warning banner CSS**

Find the `.status-bar` CSS (around the bottom of the dashboard CSS section). Add before it:

```css
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
```

- [ ] **Step 2: Add warning banner HTML**

Find the line `<div class="dashboard">` in the dashboard HTML template. Add the warning banner div right before it:

```html
<div class="warning-banner" id="warningBanner"></div>
```

- [ ] **Step 3: Add `warnings` to API responses**

In the `/api/data` handler, find where the `payload` dict is built. Add to the payload:

```python
                "warnings": ["Steam API rate limit detected. Backfill slowed."] if collector.throttled else [],
```

In the `/api/data-all` handler, find where the response payload is built. Add the same field:

```python
                "warnings": ["Steam API rate limit detected. Backfill slowed."] if collector.throttled else [],
```

- [ ] **Step 4: Add JS to show/hide the banner**

In the `fetchData` function, find the end of the `.then(function(data) {` handler where data is processed (around the `lastUpdate` line). Add:

```javascript
      var banner = document.getElementById('warningBanner');
      if (data.warnings && data.warnings.length > 0) {
        banner.textContent = data.warnings[0];
        banner.classList.add('visible');
      } else {
        banner.classList.remove('visible');
      }
```

- [ ] **Step 5: Verify syntax**

Run: `python -c "import py_compile; py_compile.compile('C:/Programming/steam-dashboard/dashboard.py', doraise=True); print('OK')"`
Expected: OK

---

### Task 5: Test and verify

**Files:**
- None (verification only)

- [ ] **Step 1: Delete the database to force a full backfill**

Run: `python -c "import sqlite3; conn = sqlite3.connect('C:/Programming/steam-dashboard/steam_dashboard.db'); [conn.execute(f'DELETE FROM {t}') for t in ['sales_by_country_daily', 'wishlists_by_country_daily', 'player_history', 'review_history', 'sales_snapshots', 'wishlist_history', 'sales_by_country', 'wishlists_by_country', 'wishlist_totals', 'fetch_progress'] if t != 'settings']; conn.commit(); conn.close(); print('Data cleared, settings preserved')"`

- [ ] **Step 2: Start the dashboard**

Run: `python C:/Programming/steam-dashboard/dashboard.py` (in background)
Then: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8081/`
Expected: 200

- [ ] **Step 3: Verify parallel backfill is running**

Watch the console output. You should see multiple dates being processed faster than before (several per second instead of one per second). The status bar should show progress.

- [ ] **Step 4: Verify the warning banner is hidden during normal operation**

Check http://localhost:8081/ in the browser. The warning banner should not be visible (no throttling expected at 20 req/s with 4 workers).

- [ ] **Step 5: Verify game switching is still fast**

After the first collection cycle completes, switch between games. Should be instant (cached data, no API calls).
