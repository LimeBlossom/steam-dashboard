# Parallel Backfill with Rate Limiting and Warning Banner

## Problem

The sales and wishlist backfill fetches dates sequentially. For a fresh DB with 3 games spanning months of history, this means hundreds of sequential HTTP calls at ~1/second. A full backfill can take 10+ minutes.

## Design

### Parallel date fetching

`refresh_all_sales` and `refresh_all_wishlists` currently walk dates in a loop, fetching one at a time. When there are more than 5 dates to fetch, they should instead submit date fetches to a `ThreadPoolExecutor(max_workers=4)` with a shared rate limiter.

**Threshold:** If 5 or fewer dates need fetching, use sequential fetching (normal polling path). If more than 5, use the thread pool.

**Flow (parallel mode):**
1. Build the list of dates that need fetching (same skip logic as today)
2. Submit each date to the thread pool via `executor.submit(fetch_sales_for_date, ...)`
3. As futures complete, write results to DB on the main thread
4. Fire `on_progress` callback per completion
5. If a fetch returns `None`, count it as skipped and continue
6. If a fetch returns `"throttled"`, trigger throttle handling

**DB writes stay on the main thread.** Only the HTTP fetches run in parallel. This avoids SQLite threading issues.

### Rate limiter

A shared rate limiter caps requests across all threads at 20 requests/second (default). Implemented as a simple token bucket or time-based gate using `threading.Lock`.

The rate limiter is shared between sales and wishlist backfills. Since they run sequentially per game (sales first, then wishlists), the limiter just ensures the thread pool doesn't burst too fast.

### HTTP 429 detection

`fetch_json` currently catches all exceptions generically. It needs to distinguish HTTP 429 (rate limited) from other errors.

On HTTP 429:
- `fetch_json` returns the string `"throttled"` instead of `None`
- The caller detects this and sets `collector.throttled = True`
- All workers pause for 30 seconds
- Rate limit reduces to 10 requests/second for the remainder of the backfill
- `collector.throttled` resets to `False` at the start of the next collection cycle

On other HTTP errors:
- Behavior unchanged (return `None`, exponential backoff in `fetch_json`)

### Warning banner

A hidden `<div>` at the top of the dashboard page, styled as a warning bar (e.g., amber background, warning text).

**Backend:**
- `DataCollector` gets a `throttled` boolean flag (default `False`)
- `/api/data` and `/api/data-all` responses include a `"warnings"` list
- If `collector.throttled` is `True`, warnings includes: `"Steam API rate limit detected. Backfill slowed."`
- If warnings is empty, the field is an empty list

**Frontend:**
- A hidden `<div id="warningBanner">` at the top of the dashboard HTML
- On each data fetch, JS checks `data.warnings`
- If non-empty: show the banner with the first warning message
- If empty: hide the banner
- No dismiss button needed. Banner clears automatically when the condition resolves.

### What doesn't change

- Sequential fetching for the normal polling cycle (2 dates per endpoint)
- Per-game processing order (game 1 finishes before game 2 starts)
- The `always_refresh` logic (today, yesterday, last collected date)
- Telegram alerts
- The collector's existing threading model (collector runs on its own timer thread, this adds a pool within each collection cycle)
