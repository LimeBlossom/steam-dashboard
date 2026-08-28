# Community Discussions Feature Design

**Date:** 2026-05-03  
**Status:** Approved

## Problem

Steam Community Hub forum threads are slow-moving, so they go unchecked for weeks. Important bug reports or support requests can be missed. The dashboard should surface recent threads and their latest replies without requiring the developer to visit the community hub manually.

## Goal

Add a "Recent Discussions" section to the dashboard that shows recent Steam Community Hub forum threads for each tracked game, including the opening post snippet and the most recent reply.

## Scope

- Display only, no Telegram notifications
- Per-game view and All Games portfolio view
- No new settings required

---

## Data Fetching

### API Endpoints

Two undocumented Steam endpoints (no API key required, publicly accessible):

**Topic list:**
```
GET https://store.steampowered.com/forums/GetTopicList/
  ?appid={app_id}&forum_type=0&start=0&count=10
```

**Topic details (posts):**
```
GET https://store.steampowered.com/forums/GetTopicDetails/
  ?topicid={topic_id}&start=0&count=50
```

> **Implementation note:** Exact parameter names and response field names must be confirmed against live API responses during implementation. `fetch_json` handles failures gracefully.

### New Function: `get_community_discussions(app_id, count=5)`

1. Call `GetTopicList` to retrieve recent thread metadata.
2. For each thread, call `GetTopicDetails` to retrieve posts.
3. Extract opening post (index 0) and latest reply (last index, if different from index 0).
4. Return a list of dicts; return `None` on API failure (preserves cached data).

**Thread dict shape:**
```python
{
    "id": str,
    "title": str,
    "url": f"https://steamcommunity.com/app/{app_id}/discussions/{subforum_id}/{id}/",
    # subforum_id comes from the API response, not hardcoded (varies by subforum: General, Bug Reports, etc.)
    "author": str,
    "posted_at": int,       # Unix timestamp
    "reply_count": int,
    "opening_snippet": str, # first 300 chars of opening post
    "latest_reply": {       # None if reply_count == 0
        "author": str,
        "posted_at": int,
        "snippet": str      # first 300 chars
    }
}
```

---

## Collector Integration

### GameState additions

```python
self.cached_discussions = []
self.discussions_last_fetched = 0.0
```

### Polling cadence

Inside the collector's `collect()` loop, after per-game data fetches:

```python
if time.time() - gs.discussions_last_fetched > 1800:  # 30 minutes
    discussions = get_community_discussions(app_id)
    if discussions is not None:
        gs.cached_discussions = discussions
        gs.discussions_last_fetched = time.time()
```

A `None` return on failure leaves `cached_discussions` unchanged. Stale data is preferred over empty.

### `/api/data` payload addition

```python
"discussions": gs.cached_discussions
```

### `/api/data-all` aggregation

Mirrors the `all_recent_reviews` pattern:
- Inject `game_name` field into each thread dict from each game.
- Merge all games' discussions into a single list.
- Sort by `posted_at` descending.
- Cap at 20 entries.

---

## UI

### Layout

A "Recent Discussions" section renders below the "Recent Reviews" section. Uses the same card grid layout.

The section is hidden entirely when the discussions array is empty.

### Thread card (`.discussion-card`)

Mirrors `.review-card` structurally.

**Header row:**
- Thread title as a `<a target="_blank">` link to Steam community thread
- Reply count badge on the right (e.g., "3 replies")

**Sub-header:**
- Author name (`.review-author` style)
- Posted date (`.review-playtime` style)

**Opening snippet:**
- First 300 chars, faded bottom mask (same `-webkit-mask-image` as `.review-text`)

**Latest reply sub-card (`.discussion-reply`):**
- Background: `var(--bg-low)` (slightly inset appearance)
- Shows: reply author, reply date, reply snippet
- Hidden if `reply_count === 0`

**Portfolio mode:**
- Game name tag in header (same `.review-game` style as review cards)

### CSS

New classes `.discussion-card` and `.discussion-reply`. No new layout primitives. Reuses existing CSS variables throughout.
