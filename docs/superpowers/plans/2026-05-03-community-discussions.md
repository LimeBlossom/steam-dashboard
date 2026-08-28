# Community Discussions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Recent Discussions" section to the dashboard that shows Steam Community Hub forum threads with opening snippets and latest replies, polled every 30 minutes.

**Architecture:** A new `get_community_discussions(app_id, count=5)` function fetches thread metadata via Steam's undocumented `store.steampowered.com/forums/` endpoints, then fetches per-thread post details for snippets. `GameState` caches the result with a `discussions_last_fetched` timestamp; the `DataCollector.collect()` loop refreshes it every 30 minutes. Both `/api/data` and `/api/data-all` include the discussion data. The UI mirrors the existing reviews section.

**Tech Stack:** Python 3 stdlib (urllib, json), SQLite (no new tables), vanilla JS (no new libraries), CSS custom properties (already defined).

---

## File Map

| File | Change |
|------|--------|
| `dashboard.py:376` | Add `get_community_discussions` after `get_recent_reviews` |
| `dashboard.py:764-778` | Add 2 fields to `GameState.__init__` |
| `dashboard.py:929` | Add 30-min gate + discussions fetch inside `collect()` per-game loop |
| `dashboard.py:3003` | Add `"discussions"` key to `/api/data` payload |
| `dashboard.py:3073-3076` | Add discussions aggregation to `/api/data-all` loop |
| `dashboard.py:3124` | Add `"discussions"` key to `/api/data-all` payload |
| `dashboard.py:1994` | Add `.discussions-grid`, `.discussion-card`, `.discussion-reply` CSS |
| `dashboard.py:2223` | Add discussions section header + grid div in HTML |
| `dashboard.py:2803` | Add discussions JS renderer |
| `tests/test_discussions.py` | New file — unit tests for `get_community_discussions` |

---

## Task 1: Probe the Steam forums API

The two endpoints are undocumented. Before writing the implementation, confirm the actual JSON field names by inspecting live responses. Replace `YOUR_APP_ID` with any app ID from your configured games.

**Files:**
- No files changed — this is an exploratory step only.

- [ ] **Step 1: Probe GetTopicList**

Run in PowerShell or a Python terminal (replace `YOUR_APP_ID`):
```python
python -c "
import urllib.request, json
url = 'https://store.steampowered.com/forums/GetTopicList/?appid=YOUR_APP_ID&forum_type=0&start=0&count=3'
data = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'SteamDashboard/1.0'}), timeout=10).read())
print(json.dumps(data, indent=2)[:4000])
"
```

Expected: a JSON object with a `response` key containing a `topics` list. Note the field names for: topic ID, title, subforum ID, reply count, post timestamp, author display name (if present).

- [ ] **Step 2: Probe GetTopicDetails**

Take a `gidtopic` value from the list above and replace `TOPIC_ID`:
```python
python -c "
import urllib.request, json
url = 'https://store.steampowered.com/forums/GetTopicDetails/?topicid=TOPIC_ID&start=0&count=10'
data = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'SteamDashboard/1.0'}), timeout=10).read())
print(json.dumps(data, indent=2)[:4000])
"
```

Expected: a JSON object with a `response` key containing a `posts` list. Note the field names for: post body, author display name, post timestamp.

- [ ] **Step 3: Record confirmed field names**

Add a comment block at the top of the `get_community_discussions` function (added in Task 2) documenting the confirmed field names, e.g.:
```python
# GetTopicList topic fields: gidtopic, subject, gidforumid, numreplies, posttime
# GetTopicDetails post fields: body, personaname, posttime
```

If actual field names differ from those used in Task 2, update Task 2's implementation to match before running tests.

---

## Task 2: Implement `get_community_discussions` with tests

**Files:**
- Create: `tests/test_discussions.py`
- Modify: `dashboard.py` — insert after line 375 (end of `get_recent_reviews`)

- [ ] **Step 1: Create the test file**

Create `tests/test_discussions.py`:

```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import patch, call
import dashboard

_TOPIC_LIST = {
    "response": {
        "topics": [
            {
                "gidtopic": "111",
                "gidforumid": "0",
                "subject": "Bug report",
                "numreplies": 2,
                "posttime": 1000,
            }
        ]
    }
}

_DETAIL_WITH_REPLIES = {
    "response": {
        "posts": [
            {"body": "Opening post", "personaname": "PlayerA", "posttime": 1000},
            {"body": "First reply",  "personaname": "PlayerB", "posttime": 1100},
            {"body": "Latest reply", "personaname": "DevC",    "posttime": 1200},
        ]
    }
}

_DETAIL_NO_REPLIES = {
    "response": {
        "posts": [
            {"body": "Opening post", "personaname": "PlayerA", "posttime": 1000},
        ]
    }
}


class TestGetCommunityDiscussions(unittest.TestCase):

    def _responses(self, *items):
        it = iter(items)
        return lambda *a, **kw: next(it)

    def test_returns_none_when_topic_list_fails(self):
        with patch.object(dashboard, 'fetch_json', return_value=None):
            result = dashboard.get_community_discussions("12345")
        self.assertIsNone(result)

    def test_returns_none_when_response_missing(self):
        with patch.object(dashboard, 'fetch_json', return_value={}):
            result = dashboard.get_community_discussions("12345")
        self.assertIsNone(result)

    def test_thread_fields_populated(self):
        with patch.object(dashboard, 'fetch_json',
                          side_effect=self._responses(_TOPIC_LIST, _DETAIL_WITH_REPLIES)):
            result = dashboard.get_community_discussions("12345")
        self.assertEqual(len(result), 1)
        t = result[0]
        self.assertEqual(t["id"], "111")
        self.assertEqual(t["title"], "Bug report")
        self.assertEqual(t["reply_count"], 2)
        self.assertEqual(t["posted_at"], 1000)
        self.assertEqual(t["author"], "PlayerA")
        self.assertEqual(t["opening_snippet"], "Opening post")

    def test_latest_reply_is_last_post(self):
        with patch.object(dashboard, 'fetch_json',
                          side_effect=self._responses(_TOPIC_LIST, _DETAIL_WITH_REPLIES)):
            result = dashboard.get_community_discussions("12345")
        lr = result[0]["latest_reply"]
        self.assertIsNotNone(lr)
        self.assertEqual(lr["snippet"], "Latest reply")
        self.assertEqual(lr["author"], "DevC")
        self.assertEqual(lr["posted_at"], 1200)

    def test_latest_reply_none_when_no_replies(self):
        with patch.object(dashboard, 'fetch_json',
                          side_effect=self._responses(_TOPIC_LIST, _DETAIL_NO_REPLIES)):
            result = dashboard.get_community_discussions("12345")
        self.assertIsNone(result[0]["latest_reply"])

    def test_skips_thread_when_detail_fails(self):
        with patch.object(dashboard, 'fetch_json',
                          side_effect=self._responses(_TOPIC_LIST, None)):
            result = dashboard.get_community_discussions("12345")
        self.assertEqual(result, [])

    def test_url_uses_subforum_id_from_api(self):
        list_data = {"response": {"topics": [
            {"gidtopic": "999", "gidforumid": "42", "subject": "Q",
             "numreplies": 0, "posttime": 0}
        ]}}
        detail_data = {"response": {"posts": [
            {"body": "x", "personaname": "U", "posttime": 0}
        ]}}
        with patch.object(dashboard, 'fetch_json',
                          side_effect=self._responses(list_data, detail_data)):
            result = dashboard.get_community_discussions("12345")
        self.assertIn("/discussions/42/999/", result[0]["url"])

    def test_opening_snippet_truncated_to_300_chars(self):
        long_body = "A" * 500
        list_data = {"response": {"topics": [
            {"gidtopic": "1", "gidforumid": "0", "subject": "T",
             "numreplies": 0, "posttime": 0}
        ]}}
        detail_data = {"response": {"posts": [
            {"body": long_body, "personaname": "U", "posttime": 0}
        ]}}
        with patch.object(dashboard, 'fetch_json',
                          side_effect=self._responses(list_data, detail_data)):
            result = dashboard.get_community_discussions("12345")
        self.assertEqual(len(result[0]["opening_snippet"]), 300)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to confirm they all fail**

```
python -m pytest tests/test_discussions.py -v
```

Expected: `AttributeError: module 'dashboard' has no attribute 'get_community_discussions'` (or similar import-time error for each test).

- [ ] **Step 3: Implement `get_community_discussions` in `dashboard.py`**

Insert after line 375 (the blank line following `get_recent_reviews`), before the `# ========== FINANCIAL API ==========` comment:

```python
def get_community_discussions(app_id, count=5):
    # GetTopicList topic fields: gidtopic, subject, gidforumid, numreplies, posttime
    # GetTopicDetails post fields: body, personaname, posttime
    # (field names confirmed against live API — update if Steam changes the schema)
    app_id = str(app_id)
    list_data = fetch_json(
        f"https://store.steampowered.com/forums/GetTopicList/"
        f"?appid={app_id}&forum_type=0&start=0&count={count}",
        f"discussions_list_{app_id}"
    )
    if not list_data or "response" not in list_data:
        return None
    topics = list_data["response"].get("topics", [])
    result = []
    for topic in topics:
        topic_id = topic.get("gidtopic", "")
        subforum_id = topic.get("gidforumid", "0")
        detail_data = fetch_json(
            f"https://store.steampowered.com/forums/GetTopicDetails/"
            f"?topicid={topic_id}&start=0&count=50",
            f"discussions_detail_{topic_id}"
        )
        if not detail_data or "response" not in detail_data:
            continue
        posts = detail_data["response"].get("posts", [])
        if not posts:
            continue
        opening = posts[0]
        latest = posts[-1] if len(posts) > 1 else None
        result.append({
            "id": topic_id,
            "title": topic.get("subject", ""),
            "url": f"https://steamcommunity.com/app/{app_id}/discussions/{subforum_id}/{topic_id}/",
            "author": opening.get("personaname", "Unknown"),
            "posted_at": topic.get("posttime", 0),
            "reply_count": topic.get("numreplies", 0),
            "opening_snippet": (opening.get("body", "") or "")[:300],
            "latest_reply": {
                "author": latest.get("personaname", "Unknown"),
                "posted_at": latest.get("posttime", 0),
                "snippet": (latest.get("body", "") or "")[:300],
            } if latest else None,
        })
    return result
```

**Note:** If the probe in Task 1 found different field names than `gidtopic`, `gidforumid`, `subject`, `numreplies`, `posttime`, `body`, `personaname` — update the field names here before running the tests.

- [ ] **Step 4: Run tests to confirm they all pass**

```
python -m pytest tests/test_discussions.py -v
```

Expected: 8 tests PASSED.

- [ ] **Step 5: Commit**

```
git add tests/test_discussions.py dashboard.py
git commit -m "feat: add get_community_discussions for Steam forum threads"
```

---

## Task 3: Wire GameState, Collector, and `/api/data`

**Files:**
- Modify: `dashboard.py:764-779` (GameState init)
- Modify: `dashboard.py:929` (end of per-game loop in collect)
- Modify: `dashboard.py:3003` (/api/data payload)

- [ ] **Step 1: Add fields to `GameState.__init__`**

In `dashboard.py`, find the `GameState.__init__` method (around line 764). It currently ends with:
```python
        self.cached_wishlist_by_country = load_wishlists_by_country(app_id)
```

Add two lines immediately after:
```python
        self.cached_discussions = []
        self.discussions_last_fetched = 0.0
```

- [ ] **Step 2: Add the 30-minute poll gate in `collect()`**

In `dashboard.py`, find the line (around line 929) inside the per-game `for game in games:` loop:
```python
            gs.last_total_units = total_units
```

Add the following block immediately after it (before the `print(f"  [{game_name}]...")` line):
```python
            if time.time() - gs.discussions_last_fetched > 1800:
                discussions = get_community_discussions(app_id)
                if discussions is not None:
                    gs.cached_discussions = discussions
                    gs.discussions_last_fetched = time.time()
```

- [ ] **Step 3: Add `"discussions"` to the `/api/data` payload**

In `dashboard.py`, find the `/api/data` payload dict (around line 2989). It currently ends with:
```python
                "timestamp": datetime.now().isoformat()
```

Add `"discussions"` before `"timestamp"`:
```python
                "discussions": gs.cached_discussions,
                "timestamp": datetime.now().isoformat()
```

- [ ] **Step 4: Verify the server starts without errors**

```
python dashboard.py
```

Expected: Server starts normally, no AttributeError or NameError. Press Ctrl+C to stop.

- [ ] **Step 5: Commit**

```
git add dashboard.py
git commit -m "feat: wire discussions into GameState, collector 30min poll, and /api/data"
```

---

## Task 4: Wire `/api/data-all` (portfolio view)

**Files:**
- Modify: `dashboard.py:3073-3076` (per-game loop in `/api/data-all`)
- Modify: `dashboard.py:3114` (`/api/data-all` payload dict)

- [ ] **Step 1: Aggregate discussions in the `/api/data-all` per-game loop**

In `dashboard.py`, find the `/api/data-all` handler. Locate the two lines that initialize aggregation lists **before** the `for game in games:` loop (around line 3037):
```python
            all_recent_reviews = []
            merged_sales_by_country = {}
```

Add `all_discussions = []` on the line immediately after `all_recent_reviews = []` (still before the loop):
```python
            all_recent_reviews = []
            all_discussions = []
            merged_sales_by_country = {}
```

Then, inside the `for game in games:` loop, immediately after the `all_recent_reviews.extend(recent)` block (around line 3076), add:
```python
                # Discussions with game name tagged
                for d in gs.cached_discussions:
                    d = dict(d)  # copy to avoid mutating cached state
                    d["game_name"] = game_name
                    all_discussions.append(d)
```

- [ ] **Step 2: Sort and cap discussions after the per-game loop**

Find the block that sorts recent reviews (around line 3104):
```python
            # Sort recent reviews by timestamp, limit to 20
            all_recent_reviews.sort(key=lambda r: r.get("timestamp_created", 0), reverse=True)
            all_recent_reviews = all_recent_reviews[:20]
```

Add immediately after it:
```python
            all_discussions.sort(key=lambda d: d.get("posted_at", 0), reverse=True)
            all_discussions = all_discussions[:20]
```

- [ ] **Step 3: Add `"discussions"` to the `/api/data-all` payload**

Find the `payload` dict in the `/api/data-all` handler (around line 3114). It currently contains `"recent_reviews": all_recent_reviews`. Add:
```python
                "discussions": all_discussions,
```

alongside `"recent_reviews"`.

- [ ] **Step 4: Verify no errors on the `/api/data-all` endpoint**

With the server running and at least one game configured, open:
```
http://localhost:8081/api/data-all
```

Expected: JSON response that includes a `"discussions"` key (value may be an empty list if no discussions have been fetched yet, which is expected on first run).

- [ ] **Step 5: Commit**

```
git add dashboard.py
git commit -m "feat: aggregate discussions in /api/data-all portfolio view"
```

---

## Task 5: Add CSS

**Files:**
- Modify: `dashboard.py:1994` (CSS section in `DASHBOARD_HTML_TEMPLATE`)

- [ ] **Step 1: Add discussion CSS classes**

In `dashboard.py`, find the line (around line 1994):
```css
.reviews-grid { display: grid; grid-template-columns: 1fr; gap: 10px; }
```

Add the following CSS block immediately after the closing `}` of `.review-text` (around line 2023):

```css
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
```

Also find the animation line (around line 2062):
```css
.metric-card, .chart-card, .country-card, .review-card { animation: fadeUp 0.5s var(--ease-out) both; }
```

Add `.discussion-card` to that selector:
```css
.metric-card, .chart-card, .country-card, .review-card, .discussion-card { animation: fadeUp 0.5s var(--ease-out) both; }
```

- [ ] **Step 2: Verify the server starts without template errors**

```
python dashboard.py
```

Expected: Server starts normally.

- [ ] **Step 3: Commit**

```
git add dashboard.py
git commit -m "feat: add discussion card CSS classes"
```

---

## Task 6: Add HTML section and JS renderer

**Files:**
- Modify: `dashboard.py:2223` (HTML template — after `recentReviews` div)
- Modify: `dashboard.py:2803` (JS — after `recentReviews` renderer)

- [ ] **Step 1: Add the discussions section to the HTML template**

In `dashboard.py`, find (around line 2222):
```html
  <div class="section-header sales-only"><h2>Recent Reviews</h2></div>
  <div class="reviews-grid sales-only" id="recentReviews"></div>
</div>
```

Replace with:
```html
  <div class="section-header sales-only"><h2>Recent Reviews</h2></div>
  <div class="reviews-grid sales-only" id="recentReviews"></div>
  <div class="section-header" id="discussionsSectionHeader" style="display:none;"><h2>Recent Discussions</h2></div>
  <div class="discussions-grid" id="recentDiscussions"></div>
</div>
```

- [ ] **Step 2: Add the JS renderer**

In `dashboard.py`, find the end of the `recentReviews` renderer (around line 2803):
```javascript
      }).join('');

      document.getElementById('tgDot')
```

Insert after the `}).join('');` line (before `document.getElementById('tgDot')`):

```javascript
      var discussions = data.discussions || [];
      var discHeader = document.getElementById('discussionsSectionHeader');
      var discGrid = document.getElementById('recentDiscussions');
      if (discHeader) discHeader.style.display = discussions.length ? '' : 'none';
      if (discGrid) {
        discGrid.innerHTML = discussions.map(function(d) {
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
          return '<div class="discussion-card">'
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
        }).join('');
      }
```

- [ ] **Step 3: Start the dashboard and verify visually**

```
python dashboard.py
```

Open `http://localhost:8081` in a browser. The "Recent Discussions" section should be hidden until the collector runs its first discussions fetch (at most 30 minutes after startup). To verify the renderer works without waiting, temporarily change `> 1800` to `> 0` in the collector's discussion gate, restart, wait one poll cycle, then revert.

- [ ] **Step 4: Verify portfolio view**

Switch to "All Games" view if you have multiple games configured. Confirm the discussions section appears and game name tags are shown on each thread card.

- [ ] **Step 5: Revert the temp change (if applied in Step 3)**

If you changed `> 1800` to `> 0`, revert it:
```python
            if time.time() - gs.discussions_last_fetched > 1800:
```

- [ ] **Step 6: Commit**

```
git add dashboard.py
git commit -m "feat: add Recent Discussions section to dashboard UI"
```
