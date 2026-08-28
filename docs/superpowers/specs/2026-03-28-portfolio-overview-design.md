# Portfolio Overview — All Games Aggregated View

## Summary

Add an "All Games" tab to the Steam Dashboard that shows aggregated metrics, stacked charts, combined geographic breakdowns, and merged recent reviews across all configured games. The tab appears in the game selector when 2+ games are configured.

## Navigation

- An "All Games" tab appears as the **first item** in the game selector when 2+ games exist.
- Uses a special app_id value `"__all__"` to distinguish from real game tabs.
- Clicking it switches the dashboard to portfolio mode and calls `/api/data-all` instead of `/api/data`.

## API — `/api/data-all` Endpoint

New GET endpoint. No query parameters needed — it aggregates across all configured games.

### Response Shape

```json
{
  "totals": {
    "units": 500,
    "returns": 12,
    "gross": 5000.00,
    "net": 3500.00
  },
  "reviews": {
    "total_positive": 80,
    "total_negative": 5,
    "total_reviews": 85
  },
  "wishlist": {
    "net": 1200,
    "adds": 1500,
    "deletes": 200,
    "purchases": 100
  },
  "per_game": {
    "12345": {
      "name": "Game A",
      "daily_sales": [["2026-03-01", 10, 1, 50.00, 35.00], ...],
      "player_history": [["2026-03-01T12:00:00", 42], ...],
      "wishlist_history": [["2026-03-01T12:00:00", 120], ...]
    },
    "67890": {
      "name": "Game B",
      "daily_sales": [...],
      "player_history": [...],
      "wishlist_history": [...]
    }
  },
  "sales_by_country": { "US": { "units": 200 }, "DE": { "units": 80 }, ... },
  "wishlist_by_country": { "US": { "adds": 500 }, "CN": { "adds": 300 }, ... },
  "recent_reviews": [
    { "game_name": "Game A", "voted_up": true, "review": "...", "author": {...}, ... }
  ],
  "telegram_active": true,
  "timestamp": "2026-03-28T12:00:00"
}
```

### Backend Queries

- **Totals:** `SELECT SUM(units_sold), SUM(units_returned), SUM(gross_revenue_usd), SUM(net_revenue_usd) FROM daily_sales` (no app_id filter).
- **Players Online:** For each game, call `get_current_players(api_key, app_id)` and sum the results.
- **Peak Players:** Sum `collector.get_state(app_id).peak_players` across all games.
- **Reviews:** For each game, call `get_reviews(app_id)` and sum `total_positive`, `total_negative`, `total_reviews`.
- **Wishlists:** Sum cached wishlist data across all games from the collector state (`collector.get_state(app_id).cached_wishlist`).
- **Per-game chart data:** For each configured game, call existing `get_all_daily_sales(app_id)`, `get_player_history(app_id)`, `get_wishlist_history(app_id)`.
- **Geographic breakdowns:** Merge `cached_sales_by_country` and `cached_wishlist_by_country` dicts across all games, summing values per country.
- **Recent reviews:** Fetch `get_recent_reviews(app_id)` for each game, add `game_name` to each review object, merge and sort by timestamp, limit to ~20.

## Frontend — Header

When "All Games" is active:
- The game-specific header (image, name, price, developer) is replaced with a "Portfolio Overview" title.
- Subtitle shows the game count (e.g. "3 games").

## Frontend — Metric Cards

Same 8 metric cards as the per-game view, populated with aggregated values:

| Card | Source |
|------|--------|
| Total Sales | `totals.units` |
| Net Revenue | `totals.net` |
| Players Online | Sum of current players across all games |
| Peak Players | Sum of session peak players across all games |
| Reviews | `reviews.total_reviews` |
| Positive Rate | `reviews.total_positive / reviews.total_reviews` |
| Wishlists | `wishlist.net` |
| Refund Rate | `totals.returns / totals.units` |

## Frontend — Stacked Charts

Each game gets a consistent color across all charts. Chart.js stacked datasets are used.

### Cumulative Sales & Revenue
- Stacked area chart.
- Each game is a colored layer showing its contribution to cumulative units sold.
- Second dataset: cumulative net revenue, also stacked by game.

### Daily Sales & Revenue
- Stacked bar chart for units sold (each day's bar segmented by game).
- Combined line for net revenue (single line, not stacked — stacking revenue lines is visually noisy).

### Player Activity
- Stacked area chart.
- Each game is a layer showing its concurrent player count over time.

## Frontend — Geographic Breakdowns

- **Sales by Country:** Same country table format, values summed across all games.
- **Wishlists by Country:** Same country table format, values summed across all games.

## Frontend — Recent Reviews

- Combined list of recent reviews from all games, sorted by timestamp.
- Each review card includes the **game name** so the user knows which game it belongs to.
- Capped at ~20 reviews.

## Visibility Rules

- The "All Games" tab only appears when 2+ games are configured.
- With a single game, the dashboard behaves exactly as it does today — no changes.
