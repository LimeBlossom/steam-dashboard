# Follower Tracking Design

**Date:** 2026-07-29
**Status:** Approved

## Goal

Track Steam follower counts over time for each configured game, and separately for the studio. Per-game followers appear on that game's tab. Studio followers appear only on the All Games tab.

Following a game and following a studio are independent actions. Studio followers are **not** a rollup of game followers and must never be presented as one. Observed values at design time: studio 20, games 44 / 44 / 37 / 4.

## Data sources

There is no Steamworks API for follower counts. Valve's own documentation points at the community members page, and the partner portal only offers manual CSV export. Both numbers are available from public pages, verified against the four configured games on 2026-07-29 (no authentication required, including for the unreleased title).

### Per-game

`https://steamcommunity.com/games/<app_id>/members`

Followers are the members of the game's community hub group, so the member count is the follower count.

```html
<div class="group_paging">
  <div class="pageLinks"> </div>
  1 - 31 of 44 Members </div>
```

Parse the character data inside `div.group_paging`. Take the substring after `" of "`, then its first whitespace-delimited token, then strip commas and parse as an integer. When `" of "` is absent (a single-page group may render only `"4 Members"`), fall back to the first integer token in the text. The element appears twice on the page; take the first successful parse and ignore the rest.

### Studio

The configured studio URL. `/developer/<slug>`, `/publisher/<slug>`, and `/curator/<id>` are all curator-backed and share this markup, so one parser handles every form:

```html
<div class="num_followers" id="CuratorNumFollowers_44681599">20</div>
```

Key on `class="num_followers"`, not on the id, since the id embeds a clan ID. Strip commas and parse the character data as an integer. Take the first occurrence.

### Failure handling

Both fetches go through the existing `fetch_html`, which returns `None` on failure. A failed fetch or an unparseable page yields `None` from the getter, and the caller writes nothing. Writing a zero on failure would look like every follower unfollowing at once, and would corrupt the history permanently since there is no way to refetch a past day.

## Constraint: no history, no backfill

These sources expose one current number. Unlike sales and wishlists, there is nothing to backfill: the series begins the day collection starts and grows forward only. The charts will show a single point on day one and stay near-flat for the first week or two. This is inherent to the data source and cannot be engineered around.

## Schema

One table. The studio is stored under a sentinel `app_id` rather than in a second table, mirroring the `country_code='__all__'` sentinel this codebase already uses, which keeps the All Games query a single SELECT.

```sql
CREATE TABLE IF NOT EXISTS follower_history (
    app_id TEXT,            -- real app_id, or '__studio__'
    date TEXT,              -- 'YYYY-MM-DD'
    follower_count INTEGER,
    PRIMARY KEY (app_id, date)
)
```

Added to `init_db()`. `CREATE TABLE IF NOT EXISTS` makes this a no-op on existing databases; no migration or data loss. The settings table is untouched.

One row per game per day, roughly 1.5k rows per game per year. Each poll overwrites the current day's row via `INSERT OR REPLACE`, so the row holds the most recent reading for that date.

### Module constants

```python
STUDIO_APP_ID = '__studio__'
FOLLOWER_FETCH_INTERVAL = 1800   # seconds
FOLLOWER_RETRY_INTERVAL = 300    # seconds; back off a failing page
```

### Data helpers

Placed with the other per-game helpers:

- `save_follower_count(app_id, count)` — `INSERT OR REPLACE` using today's date.
- `record_follower_count(app_id, count)` — writes nothing and returns `False` when `count` is `None`; writes a genuine `0`. This is the single guard that a failed fetch never persists a value.
- `get_follower_history(app_id)` — `[(date, count)]` ordered by date ascending.
- `get_latest_follower_count(app_id)` — most recent stored count, or **`None`** if no reading has ever succeeded. `None` and `0` are different facts and the UI must render them differently.

### Scrapers

Placed with the existing HTML scrapers, as `HTMLParser` subclasses matching the `_DiscussionListParser` idiom. No new module imports are needed; parsing uses string methods only.

- `class _GroupMemberCountParser(HTMLParser)`
- `get_game_followers(app_id) -> int | None`
- `class _CuratorFollowerParser(HTMLParser)`
- `get_studio_followers(studio_url) -> int | None`

## Collection

Followers move a few times a week, so scraping them on every 300-second poll is wasted traffic. They use the same throttle the discussions fetch already uses: at most once per `FOLLOWER_FETCH_INTERVAL`.

New state on `GameState`:

- `cached_followers` — seeded from `get_latest_follower_count(app_id)` in `__init__`, the way `cached_wishlist` already loads from the database, so a restart shows the real number instead of `--` for up to half an hour. May be `None` when no reading has ever succeeded.
- `followers_next_fetch` — float absolute timestamp, `0.0` initially, meaning due immediately. A success schedules `now + FOLLOWER_FETCH_INTERVAL`; a failure schedules `now + FOLLOWER_RETRY_INTERVAL`.

A failure must still advance this timestamp. `fetch_html` sleeps up to 60 seconds in-thread on failure, so leaving the timestamp unstamped would retry a permanently failing page on every 300-second poll and block sales and wishlist collection behind that sleep.

New state on `DataCollector`, seeded in `__init__` from `get_latest_follower_count(STUDIO_APP_ID)`:

- `cached_studio_followers`
- `studio_followers_next_fetch`

In `collect()`, inside the per-game loop, after the discussions block: if `time.time() >= gs.followers_next_fetch`, set `self.status` to `f"{game_name}: Fetching followers"`, call `get_game_followers(app_id)`, and pass the result to `record_follower_count`. On a write, update `cached_followers` and schedule the full interval. On `None`, leave the cached value alone and schedule the shorter retry.

The studio is fetched **once per cycle, outside the per-game loop**, only when `studio.url` is non-empty, under the same throttle, writing to `STUDIO_APP_ID`.

## Alerts

**No Telegram alerting. Removed by decision after implementation.**

Alerts were originally specified to fire on any change in either direction, guarded only by `is_first_collection`. The whole-branch review found that guard unsound: `is_first_collection` is scoped to the process lifetime, but configuration is not. Saving settings does not restart the process, so configuring a studio URL or adding a game to a running install left the baseline at zero with the flag already cleared, and the next cycle would report a fabricated `0 → 20 (+20)`. The same happened when the first fetch failed and recorded no baseline.

The user did not want follower alerting, so it was removed rather than guarded. That dissolved the defect and deleted the `last_follower_count` / `last_studio_followers` bookkeeping which existed only to serve it.

The wishlist, player-spike, new-review and new-sale alerts are unaffected.

If follower alerting is ever wanted, the baseline must be `None` when no reading exists, and an alert must be skipped against a `None` baseline. A process-lifetime flag is not sufficient on its own.

## Settings

New `studio` setting, defaulting to `{'name': '', 'url': ''}`, threaded through `get_all_settings()` and `save_all_settings()` alongside the existing keys.

Two optional fields, Studio name and Studio page URL, added to both the setup wizard and the settings form near the API key fields.

**A blank URL hides the studio card and disables the studio fetch entirely.** The feature is opt-in, so nothing changes for an install that never configures it. This matters given the upstream SteamDash direction.

`/api/test` gains studio validation: when a studio URL is supplied, fetch it and report the parsed follower count, or an error when the page cannot be read or parsed. A setup wizard that silently fails is the failure mode the opt-in rule guards against, and reporting the live count closes the loop at configuration time.

## API payloads

`/api/data` (per-game) adds:

- `followers` — `gs.cached_followers`
- `follower_history` — `get_follower_history(app_id)`

No studio keys are present on this endpoint.

`/api/data-all` adds:

- `studio_followers` — `collector.cached_studio_followers`
- `studio_name` — from settings
- `studio_follower_history` — `get_follower_history(STUDIO_APP_ID)`
- `follower_history` inside each `per_game[app_id]` entry

## UI

### Metric card

A fifth card is added to the second metrics grid, **inserted directly after Wishlists** rather than appended at the end, so the two demand-side metrics sit side by side:

```
Reviews | Positive Rate | Wishlists | Followers | Refund Rate
```

`.metrics-grid` changes from `grid-template-columns: repeat(4, 1fr)` to `repeat(auto-fit, minmax(200px, 1fr))` so five cards flow evenly. The existing `nth-child` animation-delay rules cover children 1 through 4 only; a fifth rule is added to each of the two grids to keep the stagger consistent. Because those rules are positional, inserting mid-row shifts Refund Rate's delay by one step, which is the desired left-to-right cascade and needs no special handling.

The card is **not** marked `sales-only`. Reviews, Positive Rate, and Refund Rate all are, so on an unreleased game the row collapses to exactly Wishlists and Followers, adjacent — the pre-launch pairing that matters most, and verified to work since the unreleased title returns a real count.

Element ids: `followersCard`, `followersLabel`, `followerCount`, `followerSub`.

The card is reused across views with a swapped label, the way the existing code already repurposes `playerChange` in portfolio mode:

| View | Label | Value | Sub-line |
|---|---|---|---|
| Per-game | `Followers` | game followers | none |
| All Games | `Studio Followers` | studio followers | studio name |

The per-game card shows the count and nothing else. No delta: the existing `playerChange` delta is poll-over-poll, and a second delta on a different time window would introduce a competing convention on a card whose whole content is one scalar. Trend lives in the chart directly below.

No summed figure appears on the All Games card. A consequence, accepted deliberately: on the All Games tab per-game follower counts are visible only in the chart, not as a number.

There are **three distinct states**, and the card must render each differently:

| State | Renders |
|---|---|
| Studio URL not configured (All Games only) | card hidden |
| Configured or tracked, but no reading has ever succeeded | `--` |
| A genuine reading of zero | `0` |

The middle case is why `get_latest_follower_count` returns `None` rather than `0`. A `|| 0` fallback in the render path collapses the second and third states, so a game whose members page is unreachable would display `Followers 0` indefinitely, indistinguishable from a real zero. Both render sites must test explicitly for `null` and `undefined`.

### Chart

A `Follower Growth` chart card holding canvas `followerChart`, added as a third child of `wishlistChartsRow`. No DOM nodes are moved between views; layout is driven by the row's class, extending the conditional class swap the code already performs on this row.

- **Per-game:** the row is `charts-row` (2 columns). The stacked wishlist card is `display:none` and so is removed from grid flow, leaving Wishlist Activity paired with Follower Growth.
- **All Games:** the row is a new `charts-grid-3` class, `repeat(auto-fit, minmax(320px, 1fr))`, so three visible cards sit across on wide screens and reflow to 2+1 or a single column on narrower ones. Without this, three cards in a 2-column row would orphan the third.

Chart configuration: line chart, x axis is date, `tension: 0.35`, `pointRadius: 0`, matching the existing charts. The y axis uses `beginAtZero: false`, because growth from 44 to 47 is the signal and a zero-anchored axis would flatten it.

Per-game view draws one dataset. All Games draws one dataset per game using the existing `gameColors` palette, plus a studio dataset.

**A single shared y-axis. No second axis for the studio series.**

The studio dataset must not read as another game: `borderDash: [6, 3]`, `borderWidth: 3`, the accent color rather than a palette entry, and pinned first in the dataset array so it leads the legend. Labelled with the configured studio name, falling back to `Studio` when the name is blank.

## Testing

Parser tests against saved HTML fixtures, following `tests/test_discussions.py`:

- Game page, normal count (`1 - 31 of 44 Members` → 44).
- Game page, comma-separated count (`1 - 31 of 1,234 Members` → 1234).
- Game page, single page with no `" of "` (`4 Members` → 4).
- Game page, unparseable or empty HTML → `None`.
- Studio page, `num_followers` present → the integer.
- Studio page, comma-separated count.
- Studio page, markup absent → `None`.
- `save_follower_count` called twice on the same day leaves exactly one row holding the later value.
- A `None` fetch result writes no row.

## Out of scope

- **Followers by country.** Not exposed by either source.
- **Historical backfill.** Impossible; see the constraint section.
- **Follower to wishlist or follower to sale conversion analysis.** Needs a per-user join that neither source provides.
- **A combined total-game-followers card.** Not requested.
