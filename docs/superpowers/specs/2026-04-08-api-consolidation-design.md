# API Consolidation: Eliminate Duplicate Sales & Wishlist Fetches

## Problem

The dashboard fetches sales and wishlist data from Steam's Partner API twice per date per endpoint:

- **Sales:** `fetch_sales_for_date` and `fetch_sales_by_country` both call `GetDetailedSales` for the same date. One sums totals, the other groups by country.
- **Wishlists:** `fetch_wishlist_for_date` and `fetch_wishlist_by_country` both call `GetAppWishlistReporting` for the same date. One reads `wishlist_summary`, the other reads `country_summary`.

This doubles the API traffic and slows down backfills.

## Design

### Single fetch, single table

**Sales:** One function calls `GetDetailedSales` once per date. It groups line items by `country_code` and also writes a `__all__` row with the summed totals. Everything goes into `sales_by_country_daily`.

**Wishlists:** One function calls `GetAppWishlistReporting` once per date. It reads both `country_summary` and `wishlist_summary` from the single response. Country rows plus an `__all__` totals row go into `wishlists_by_country_daily`.

### Tables

**`sales_by_country_daily`** (add `gross_usd` column):
- `app_id TEXT, date TEXT, country_code TEXT, units INTEGER, returns INTEGER, gross_usd REAL, net_usd REAL`
- `PRIMARY KEY (app_id, date, country_code)`
- `country_code='__all__'` stores the daily totals
- `gross_usd` is new. The old schema lacked it but the dashboard needs it for "before fees" display.

**`wishlists_by_country_daily`** (unchanged schema):
- `app_id TEXT, date TEXT, country_code TEXT, adds INTEGER, deletes INTEGER, purchases INTEGER`
- `PRIMARY KEY (app_id, date, country_code)`
- `country_code='__all__'` stores the daily totals

### Tables to drop

- `daily_sales` (replaced by `__all__` rows in `sales_by_country_daily`)
- `wishlist_totals_daily` (replaced by `__all__` rows in `wishlists_by_country_daily`)

### Functions to remove

- `fetch_sales_for_date` (merged into new unified sales fetch)
- `fetch_sales_by_country` (merged into new unified sales fetch)
- `fetch_wishlist_for_date` (merged into new unified wishlist fetch)
- `fetch_wishlist_by_country` (merged into new unified wishlist fetch)
- `fetch_wishlist_totals` (merged into new unified wishlist walk)
- `refresh_recent_sales` (dead code)

### New functions

- `fetch_sales_for_date(financial_key, app_id, date_str)`: Single `GetDetailedSales` call. Returns `{"totals": (units, returns, gross, net), "by_country": {"US": {...}, ...}}` or `None` on failure.
- `fetch_wishlist_for_date(financial_key, app_id, date_str)`: Single `GetAppWishlistReporting` call. Returns `{"totals": {"adds", "deletes", "purchases", "gifts"}, "by_country": {"US": {...}, ...}}` or `None` on failure.
- `refresh_all_sales(financial_key, app_id, launch_date, on_progress)`: Walks dates backwards, calls `fetch_sales_for_date`, writes both country rows and `__all__` row to `sales_by_country_daily`.
- `refresh_all_wishlists(financial_key, app_id, launch_date, on_progress)`: Walks dates backwards from earliest wishlist date, calls `fetch_wishlist_for_date`, writes both country rows and `__all__` row to `wishlists_by_country_daily`.

### Reader changes

All queries currently reading from `daily_sales` switch to:
```sql
SELECT date, units, returns, net FROM sales_by_country_daily
WHERE app_id=? AND country_code='__all__' ORDER BY date
```

All queries reading from `wishlist_totals_daily` switch to:
```sql
SELECT date, adds, deletes, purchases FROM wishlists_by_country_daily
WHERE app_id=? AND country_code='__all__' ORDER BY date
```

Country queries remain the same but exclude `__all__`:
```sql
WHERE app_id=? AND country_code != '__all__'
```

### Collector changes

- `refresh_all_sales` replaces the current sales call (already runs every cycle)
- `refresh_all_wishlists` replaces both `fetch_wishlist_totals` and `fetch_wishlist_by_country` (currently on separate cadences, now unified to one pass every cycle)
- Country scans no longer need the hourly cadence since they happen automatically

### Migration

Nuke the DB. No migration logic needed. The backfill rebuilds everything from scratch.

### Result

- API calls per date: 2 instead of 4 (one per endpoint instead of two)
- Simpler code: fewer functions, fewer tables, no hourly cadence logic
- Faster backfills: half the network round trips
