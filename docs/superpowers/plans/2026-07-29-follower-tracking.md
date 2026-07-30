# Follower Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track Steam follower counts daily for each configured game and for the studio, showing per-game counts on each game tab and studio counts on the All Games tab.

> **Post-implementation amendments.** This plan was executed and then amended to match
> what shipped. Read these before treating any task below as authoritative:
>
> - **Task 6 (Telegram alerts) was REMOVED after implementation** and is no longer part of
>   the feature. Its guard relied on `is_first_collection`, which is process-lifetime while
>   configuration is not, so configuring a studio URL or adding a game to a running install
>   produced a fabricated `0 → N` alert. The user did not want follower alerting, so it was
>   deleted rather than guarded. Skip Task 6 entirely. See the spec's Alerts section.
> - **Task 1:** `get_latest_follower_count` returns `None`, not `0`, when no reading has
>   ever succeeded. `None` and `0` are different facts and the UI renders them differently.
> - **Task 5:** the throttle field is `followers_next_fetch` (an absolute due-time), not
>   `followers_last_fetched`. A failure schedules `FOLLOWER_RETRY_INTERVAL` (300s) rather
>   than leaving the timestamp unstamped, which would otherwise retry a permanently failing
>   page on every poll and block sales collection behind `fetch_html`'s 60s sleep.
> - **Task 8:** the render path tests explicitly for `null`/`undefined` and shows `--`.
>   A `|| 0` fallback is the bug it was written to avoid.
> - **Task 2, 3, 9:** amended inline where the plan's original code was wrong.

**Architecture:** No Steamworks API exposes follower counts, so both numbers are scraped from public HTML with `HTMLParser` subclasses following the existing `_DiscussionListParser` pattern. Counts are stored one row per day in a single `follower_history` table, with the studio under a sentinel `app_id` mirroring the existing `country_code='__all__'` convention. The collector fetches on a 30 minute throttle reusing the discussions throttle pattern.

**Tech Stack:** Python 3 standard library only (`sqlite3`, `html.parser`, `http.server`), Chart.js 4.4.1 loaded from CDN, `unittest` for tests.

## Global Constraints

- **Zero external dependencies.** Standard library only. Do not add `re`, `requests`, `bs4`, or any pip package. Parsing uses `HTMLParser` plus string methods.
- **Single file.** All Python, CSS, HTML, and JS lives in `dashboard.py`. Do not split it.
- **Never write 0 on a fetch failure.** A failed or unparseable fetch returns `None` and the caller writes nothing. Past days cannot be refetched, so a transient error recorded as 0 corrupts the history permanently.
- **Studio followers are not a rollup of game followers.** Never display a summed game total as, or adjacent to, a studio figure. Observed: studio 20, games 44 / 44 / 37 / 4.
- **Studio metric appears only on the All Games tab.** `/api/data` must not carry studio keys.
- **Single shared y-axis on the follower chart.** No second axis for the studio series.
- **Blank studio URL disables the feature.** No fetch, card hidden. The feature is opt-in so nothing changes for an install that never configures it.
- **`CREATE TABLE IF NOT EXISTS` only.** No migrations, no `DROP`, and never touch the `settings` table.
- **Commit messages must not include a `Co-Authored-By` trailer.**
- The HTML/CSS/JS lives inside Python string literals, so backslash escapes are doubled: write `'\\u2713'` in the source to emit `✓` in the browser.

---

### Task 1: Follower storage

**Files:**
- Modify: `dashboard.py:28` (add `STUDIO_APP_ID` constant after `FINANCIAL_BASE`)
- Modify: `dashboard.py:66-67` (add table to `init_db`)
- Modify: `dashboard.py:261` (add helpers after `get_daily_wishlists`)
- Test: `tests/test_followers.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `STUDIO_APP_ID` (str, value `'__studio__'`)
  - `save_follower_count(app_id, count) -> None`
  - `record_follower_count(app_id, count) -> bool` (writes nothing and returns False when `count` is `None`)
  - `get_follower_history(app_id) -> list[tuple[str, int]]` ordered by date ascending
  - `get_latest_follower_count(app_id) -> int` (0 when no rows)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_followers.py`:

```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import tempfile
import shutil
import sqlite3
from unittest.mock import patch
import dashboard


class TestFollowerStorage(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patcher = patch.object(dashboard, 'DB_PATH', os.path.join(self._tmp, 'test.db'))
        self._patcher.start()
        dashboard.init_db()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _insert(self, app_id, date, count):
        conn = sqlite3.connect(dashboard.DB_PATH)
        conn.execute("INSERT OR REPLACE INTO follower_history VALUES (?, ?, ?)",
                     (app_id, date, count))
        conn.commit()
        conn.close()

    def test_latest_is_zero_when_empty(self):
        self.assertEqual(dashboard.get_latest_follower_count("12345"), 0)

    def test_history_is_empty_when_no_rows(self):
        self.assertEqual(dashboard.get_follower_history("12345"), [])

    def test_save_then_read_latest(self):
        dashboard.save_follower_count("12345", 44)
        self.assertEqual(dashboard.get_latest_follower_count("12345"), 44)

    def test_same_day_save_overwrites(self):
        dashboard.save_follower_count("12345", 44)
        dashboard.save_follower_count("12345", 45)
        self.assertEqual(len(dashboard.get_follower_history("12345")), 1)
        self.assertEqual(dashboard.get_latest_follower_count("12345"), 45)

    def test_history_ordered_by_date_ascending(self):
        self._insert("12345", "2026-07-03", 40)
        self._insert("12345", "2026-07-01", 38)
        self._insert("12345", "2026-07-02", 39)
        self.assertEqual(dashboard.get_follower_history("12345"),
                         [("2026-07-01", 38), ("2026-07-02", 39), ("2026-07-03", 40)])

    def test_latest_uses_newest_date_not_insert_order(self):
        self._insert("12345", "2026-07-03", 40)
        self._insert("12345", "2026-07-01", 38)
        self.assertEqual(dashboard.get_latest_follower_count("12345"), 40)

    def test_studio_stored_separately_from_games(self):
        dashboard.save_follower_count("12345", 44)
        dashboard.save_follower_count(dashboard.STUDIO_APP_ID, 20)
        self.assertEqual(dashboard.get_latest_follower_count("12345"), 44)
        self.assertEqual(dashboard.get_latest_follower_count(dashboard.STUDIO_APP_ID), 20)

    def test_history_scoped_to_requested_app(self):
        dashboard.save_follower_count("12345", 44)
        dashboard.save_follower_count("67890", 37)
        self.assertEqual(len(dashboard.get_follower_history("12345")), 1)
        self.assertEqual(dashboard.get_follower_history("12345")[0][1], 44)

    def test_record_writes_and_reports_true(self):
        self.assertTrue(dashboard.record_follower_count("12345", 44))
        self.assertEqual(dashboard.get_latest_follower_count("12345"), 44)

    def test_record_of_none_writes_no_row(self):
        self.assertFalse(dashboard.record_follower_count("12345", None))
        self.assertEqual(dashboard.get_follower_history("12345"), [])

    def test_record_of_none_does_not_overwrite_a_good_reading(self):
        dashboard.record_follower_count("12345", 44)
        dashboard.record_follower_count("12345", None)
        self.assertEqual(dashboard.get_latest_follower_count("12345"), 44)

    def test_record_of_zero_is_written(self):
        self.assertTrue(dashboard.record_follower_count("12345", 0))
        self.assertEqual(len(dashboard.get_follower_history("12345")), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_followers -v`
Expected: every test FAILS. The first errors will be `AttributeError: module 'dashboard' has no attribute 'STUDIO_APP_ID'` and `sqlite3.OperationalError: no such table: follower_history`.

- [ ] **Step 3: Add the constant**

In `dashboard.py`, immediately after the `FINANCIAL_BASE` line (line 28):

```python
STUDIO_APP_ID = '__studio__'
FOLLOWER_FETCH_INTERVAL = 1800  # seconds; followers move a few times a week
```

`FOLLOWER_FETCH_INTERVAL` is unused until Task 5. It is added here so both module constants live together.

- [ ] **Step 4: Add the table**

In `init_db()`, after the `wishlists_by_country_daily` block and before `conn.commit()` (currently line 67):

```python
    c.execute('''CREATE TABLE IF NOT EXISTS follower_history (
        app_id TEXT, date TEXT, follower_count INTEGER,
        PRIMARY KEY (app_id, date)
    )''')
```

- [ ] **Step 5: Add the helpers**

In `dashboard.py`, after `get_daily_wishlists` ends (currently line 261) and before `class RateLimiter`:

```python
def save_follower_count(app_id, count):
    """Record today's follower count, overwriting any earlier reading for today."""
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO follower_history VALUES (?, ?, ?)",
                 (str(app_id), datetime.now().strftime("%Y-%m-%d"), count))
    conn.commit()
    conn.close()


def get_follower_history(app_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, follower_count FROM follower_history WHERE app_id=? ORDER BY date",
        (str(app_id),)
    ).fetchall()
    conn.close()
    return rows


def get_latest_follower_count(app_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT follower_count FROM follower_history WHERE app_id=? ORDER BY date DESC LIMIT 1",
        (str(app_id),)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def record_follower_count(app_id, count):
    """Persist a follower reading. Returns True when a row was written.

    A None count means the fetch failed, and nothing is written. Recording a
    failure as 0 would look like every follower unfollowing at once, and the
    damage would be permanent because past days cannot be refetched. Note that
    a genuine 0 IS written; only None is treated as absence of data.
    """
    if count is None:
        return False
    save_follower_count(app_id, count)
    return True
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m unittest tests.test_followers -v`
Expected: 12 passed.

- [ ] **Step 7: Confirm the existing suite still passes**

Run: `python -m unittest discover -s tests -v`
Expected: all tests pass, including the 9 in `test_discussions.py`.

- [ ] **Step 8: Commit**

```bash
git add dashboard.py tests/test_followers.py
git commit -m "feat: add follower_history table and storage helpers

One row per app per day, studio under a '__studio__' sentinel app_id
mirroring the existing '__all__' country convention."
```

---

### Task 2: Game follower scraper

Followers are the members of a game's community hub group, so the hub members page count is the follower count. Verified public with no login on 2026-07-29, including for an unreleased app.

**Files:**
- Modify: `dashboard.py:583` (add after `get_community_discussions`, before the `# ========== FINANCIAL API ==========` banner)
- Test: `tests/test_followers.py`

**Interfaces:**
- Consumes: `fetch_html(url, label)` from `dashboard.py:320`, which already sends a browser User-Agent to get past Steam's bot detection and returns `None` on any failure.
- Produces:
  - `_parse_member_count(text) -> int | None`
  - `class _GroupMemberCountParser(HTMLParser)` with attribute `count` (`int | None`)
  - `get_game_followers(app_id) -> int | None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_followers.py`, before the `if __name__` block:

```python
_MEMBERS_HTML = '''<html><body>
<div class="group_paging">
  <div class="pageLinks"> </div>
  1 - 31 of 44 Members </div>
<div id="memberList"></div>
<div class="group_paging">
  <div class="pageLinks"> </div>
  1 - 31 of 44 Members </div>
</body></html>'''

_MEMBERS_HTML_COMMAS = '''<html><body>
<div class="group_paging">
  <div class="pageLinks"> </div>
  1 - 31 of 1,234 Members </div>
</body></html>'''

_MEMBERS_HTML_SINGLE_PAGE = '''<html><body>
<div class="group_paging">
  <div class="pageLinks"> </div>
  4 Members </div>
</body></html>'''

_MEMBERS_HTML_GERMAN = '''<html><body>
<div class="group_paging">
  <div class="pageLinks"> </div>
  1 - 31 von 44 Mitglieder </div>
</body></html>'''


class TestGetGameFollowers(unittest.TestCase):

    def test_parses_paged_count(self):
        with patch.object(dashboard, 'fetch_html', return_value=_MEMBERS_HTML):
            self.assertEqual(dashboard.get_game_followers("2587260"), 44)

    def test_parses_comma_separated_count(self):
        with patch.object(dashboard, 'fetch_html', return_value=_MEMBERS_HTML_COMMAS):
            self.assertEqual(dashboard.get_game_followers("2587260"), 1234)

    def test_parses_single_page_count_without_of(self):
        with patch.object(dashboard, 'fetch_html', return_value=_MEMBERS_HTML_SINGLE_PAGE):
            self.assertEqual(dashboard.get_game_followers("4627290"), 4)

    def test_returns_none_when_fetch_fails(self):
        with patch.object(dashboard, 'fetch_html', return_value=None):
            self.assertIsNone(dashboard.get_game_followers("2587260"))

    def test_returns_none_when_markup_absent(self):
        with patch.object(dashboard, 'fetch_html', return_value='<html><body></body></html>'):
            self.assertIsNone(dashboard.get_game_followers("2587260"))

    def test_zero_members_is_zero_not_none(self):
        html = '<html><body><div class="group_paging">0 Members </div></body></html>'
        with patch.object(dashboard, 'fetch_html', return_value=html):
            self.assertEqual(dashboard.get_game_followers("2587260"), 0)

    def test_requests_the_members_page(self):
        with patch.object(dashboard, 'fetch_html', return_value=_MEMBERS_HTML) as m:
            dashboard.get_game_followers("2587260")
        url = m.call_args[0][0]
        self.assertEqual(url, "https://steamcommunity.com/games/2587260/members?l=english")

    def test_returns_none_for_localised_page(self):
        with patch.object(dashboard, 'fetch_html', return_value=_MEMBERS_HTML_GERMAN):
            self.assertIsNone(dashboard.get_game_followers("2587260"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_followers.TestGetGameFollowers -v`
Expected: 8 FAIL with `AttributeError: module 'dashboard' has no attribute 'get_game_followers'`.

- [ ] **Step 3: Write the implementation**

In `dashboard.py`, after `get_community_discussions` ends (currently line 583) and before the `# ========== FINANCIAL API ==========` banner:

```python
# --- Followers ---
#
# Valve exposes no Web API for follower counts, so both the per-game and the
# studio numbers are scraped from public pages. Following a game is joining its
# community hub group, so the hub member count IS the follower count.

def _parse_member_count(text):
    """'1 - 31 of 44 Members' -> 44.

    Falls back to a single integer token when ' of ' is absent, but returns
    None if the text contains zero or multiple integer tokens. Ambiguous
    multi-number strings mean the page was not in the expected format, and
    returning None is safer than guessing, since a wrong value can never be
    corrected.
    """
    if ' of ' in text:
        text = text.split(' of ')[-1]
    tokens = text.replace(',', '').split()
    integers = [token for token in tokens if token.isdigit()]
    if len(integers) == 1:
        return int(integers[0])
    return None


class _GroupMemberCountParser(HTMLParser):
    """Pulls the follower count out of a community hub members page.

    Target markup, which appears twice per page:
        <div class="group_paging">
          <div class="pageLinks"> </div>
          1 - 31 of 44 Members </div>
    """

    def __init__(self):
        super().__init__()
        self._depth = 0
        self._text = []
        self.count = None

    def handle_starttag(self, tag, attrs):
        if tag != 'div':
            return
        if self._depth > 0:
            self._depth += 1
            return
        cls = dict(attrs).get('class') or ''
        if 'group_paging' in cls.split():
            self._depth = 1

    def handle_endtag(self, tag):
        if tag == 'div' and self._depth > 0:
            self._depth -= 1
            if self._depth == 0 and self.count is None:
                self.count = _parse_member_count(''.join(self._text))
                self._text = []

    def handle_data(self, data):
        if self._depth > 0 and self.count is None:
            self._text.append(data)


def get_game_followers(app_id):
    """Current follower count for a game, or None if it could not be read.

    Returns None rather than 0 on failure. A transient error recorded as a mass
    unfollow would be permanent, since past days cannot be refetched.
    """
    html = fetch_html(f"https://steamcommunity.com/games/{app_id}/members?l=english",
                      f"followers_{app_id}")
    if not html:
        return None
    parser = _GroupMemberCountParser()
    try:
        parser.feed(html)
    except Exception as e:
        print(f"  [ERROR] followers_{app_id}: parse failed ({e})")
        return None
    return parser.count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_followers -v`
Expected: 20 passed.

**Why the locale is pinned and the fallback is strict:** `fetch_html` sends no
`Accept-Language` header. A German page renders `1 - 31 von 44 Mitglieder`, which
has no `" of "` separator, so a fallback that took the *first* integer token would
silently return `1`. A wrong count is worse than no count, because past days
cannot be refetched. `?l=english` forces the expected separator, and requiring
exactly one integer token means anything unexpected becomes `None` and is retried
next cycle rather than guessed at.

- [ ] **Step 5: Verify against the live page**

Run:
```bash
python -c "import dashboard; print(dashboard.get_game_followers('2587260'))"
```
Expected: a small integer, around 44. A `None` result means the page markup changed and the parser needs revisiting before continuing.

- [ ] **Step 6: Commit**

```bash
git add dashboard.py tests/test_followers.py
git commit -m "feat: scrape per-game follower count from community hub

No Steamworks API exposes followers. Hub group members are the game's
followers, so the members page count is the number. Returns None on
failure so a transient error is never recorded as a mass unfollow."
```

---

### Task 3: Studio follower scraper

`/developer/<slug>`, `/publisher/<slug>`, and `/curator/<id>` are all curator-backed and share the same follower markup, so one parser covers every URL form.

**Files:**
- Modify: `dashboard.py` (append after `get_game_followers` from Task 2)
- Test: `tests/test_followers.py`

**Interfaces:**
- Consumes: `fetch_html(url, label)`.
- Produces:
  - `class _CuratorFollowerParser(HTMLParser)` with attribute `count` (`int | None`)
  - `get_studio_followers(studio_url) -> int | None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_followers.py`, before the `if __name__` block:

```python
_STUDIO_HTML = '''<html><body>
<div class="follow_controls">
  <div class="follow_btn_stats">
    <div class="num_followers" id="CuratorNumFollowers_44681599">20</div>
    <div class="num_followers_text">Followers</div>
  </div>
</div>
</body></html>'''

_STUDIO_HTML_COMMAS = '''<html><body>
<div class="num_followers" id="CuratorNumFollowers_1">12,345</div>
<div class="num_followers_text">Followers</div>
</body></html>'''


class TestGetStudioFollowers(unittest.TestCase):

    def test_parses_follower_count(self):
        with patch.object(dashboard, 'fetch_html', return_value=_STUDIO_HTML):
            self.assertEqual(
                dashboard.get_studio_followers("https://store.steampowered.com/developer/LimeBlossom"), 20)

    def test_parses_comma_separated_count(self):
        with patch.object(dashboard, 'fetch_html', return_value=_STUDIO_HTML_COMMAS):
            self.assertEqual(dashboard.get_studio_followers("https://x/"), 12345)

    def test_ignores_num_followers_text_label(self):
        html = '<html><body><div class="num_followers_text">Followers</div></body></html>'
        with patch.object(dashboard, 'fetch_html', return_value=html):
            self.assertIsNone(dashboard.get_studio_followers("https://x/"))

    def test_returns_none_when_fetch_fails(self):
        with patch.object(dashboard, 'fetch_html', return_value=None):
            self.assertIsNone(dashboard.get_studio_followers("https://x/"))

    def test_returns_none_when_markup_absent(self):
        with patch.object(dashboard, 'fetch_html', return_value='<html><body></body></html>'):
            self.assertIsNone(dashboard.get_studio_followers("https://x/"))

    def test_blank_url_does_not_fetch(self):
        with patch.object(dashboard, 'fetch_html') as m:
            self.assertIsNone(dashboard.get_studio_followers(""))
        m.assert_not_called()

    def test_fetches_the_url_given(self):
        target = "https://store.steampowered.com/curator/44681599"
        with patch.object(dashboard, 'fetch_html', return_value=_STUDIO_HTML) as m:
            dashboard.get_studio_followers(target)
        self.assertEqual(m.call_args[0][0], target)

    def test_dot_separated_count_is_none_not_wrong(self):
        html = '<html><body><div class="num_followers">12.345</div></body></html>'
        with patch.object(dashboard, 'fetch_html', return_value=html):
            self.assertIsNone(dashboard.get_studio_followers("https://x/"))
```

**No locale parameter on the studio URL, deliberately.** Verified live: the
`num_followers` value is locale-invariant (`?l=english` and `?l=german` both render
`>20`); only the sibling `num_followers_text` label changes, and the parser never
reads it. The studio URL is user-supplied, so appending a parameter would cause more
harm than the risk it avoids. The residual risk is a >999 count on a localised page
rendering `12.345`; the strict `isdigit()` check yields `None` there, which is correct.
`test_dot_separated_count_is_none_not_wrong` pins that so nobody later "fixes" the
parser by also stripping dots, turning 12.345 into 12345.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_followers.TestGetStudioFollowers -v`
Expected: 8 FAIL with `AttributeError: module 'dashboard' has no attribute 'get_studio_followers'`.

- [ ] **Step 3: Write the implementation**

Append in `dashboard.py` immediately after `get_game_followers`:

```python
class _CuratorFollowerParser(HTMLParser):
    """Pulls the follower count out of a curator-backed store page.

    /developer/<slug>, /publisher/<slug> and /curator/<id> all render:
        <div class="num_followers" id="CuratorNumFollowers_44681599">20</div>

    Keyed on the class, not the id, because the id embeds a clan ID. The exact
    token match also avoids the sibling div.num_followers_text label.
    """

    def __init__(self):
        super().__init__()
        self._in_count = False
        self._text = []
        self.count = None

    def handle_starttag(self, tag, attrs):
        if tag == 'div' and self.count is None:
            cls = dict(attrs).get('class') or ''
            if 'num_followers' in cls.split():
                self._in_count = True

    def handle_endtag(self, tag):
        if tag == 'div' and self._in_count:
            self._in_count = False
            digits = ''.join(self._text).replace(',', '').strip()
            if digits.isdigit():
                self.count = int(digits)
            self._text = []

    def handle_data(self, data):
        if self._in_count:
            self._text.append(data)


def get_studio_followers(studio_url):
    """Current studio follower count, or None if unset or unreadable.

    Studio followers are independent of game followers, not a sum of them.
    """
    if not studio_url:
        return None
    html = fetch_html(studio_url, "followers_studio")
    if not html:
        return None
    parser = _CuratorFollowerParser()
    try:
        parser.feed(html)
    except Exception as e:
        print(f"  [ERROR] followers_studio: parse failed ({e})")
        return None
    return parser.count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_followers -v`
Expected: 28 passed (37 across `tests/`).

- [ ] **Step 5: Verify against the live page**

Run:
```bash
python -c "import dashboard; print(dashboard.get_studio_followers('https://store.steampowered.com/developer/LimeBlossom'))"
```
Expected: a small integer, around 20.

- [ ] **Step 6: Commit**

```bash
git add dashboard.py tests/test_followers.py
git commit -m "feat: scrape studio follower count from curator page

Developer, publisher and curator pages share the num_followers markup,
so one parser covers all three URL forms."
```

---

### Task 4: Studio setting and setup wizard field

**Files:**
- Modify: `dashboard.py:96-103` (`get_all_settings`)
- Modify: `dashboard.py:106-111` (`save_all_settings`)
- Modify: `dashboard.py:1600-1604` (wizard form, after the financial key hint, before `<hr class="divider">`)
- Modify: `dashboard.py:1677-1678` (prefill block)
- Modify: `dashboard.py:1774-1775` (test URL construction)
- Modify: `dashboard.py:1804` (test result rendering, after the financial key block)
- Modify: `dashboard.py:1929-1933` (settings payload)
- Modify: `dashboard.py:3179-3254` (`/api/test` handler)
- Test: `tests/test_followers.py`

**Interfaces:**
- Consumes: `get_studio_followers(studio_url)` from Task 3.
- Produces:
  - `get_all_settings()` gains key `studio` -> `{'name': str, 'url': str}`
  - `/api/test` accepts query param `studio_url` and returns key `studio` -> `None` when not supplied, else `{'success': True, 'followers': int}` or `{'success': False, 'error': str}`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_followers.py`, before the `if __name__` block:

```python
class TestStudioSetting(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patcher = patch.object(dashboard, 'DB_PATH', os.path.join(self._tmp, 'test.db'))
        self._patcher.start()
        dashboard.init_db()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_defaults_to_blank_studio(self):
        self.assertEqual(dashboard.get_all_settings()['studio'], {'name': '', 'url': ''})

    def test_round_trips_studio(self):
        dashboard.save_all_settings({
            'steam_api_key': 'k',
            'studio': {'name': 'Lime Blossom Studio',
                       'url': 'https://store.steampowered.com/developer/LimeBlossom'},
        })
        studio = dashboard.get_all_settings()['studio']
        self.assertEqual(studio['name'], 'Lime Blossom Studio')
        self.assertEqual(studio['url'], 'https://store.steampowered.com/developer/LimeBlossom')

    def test_missing_studio_key_saves_blank(self):
        dashboard.save_all_settings({'steam_api_key': 'k'})
        self.assertEqual(dashboard.get_all_settings()['studio'], {'name': '', 'url': ''})

    def test_saving_settings_preserves_existing_games(self):
        dashboard.save_all_settings({'steam_api_key': 'k', 'games': [{'app_id': '1'}]})
        dashboard.save_all_settings({'steam_api_key': 'k', 'games': [{'app_id': '1'}],
                                     'studio': {'name': 'S', 'url': 'https://x/'}})
        self.assertEqual(dashboard.get_all_settings()['games'], [{'app_id': '1'}])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_followers.TestStudioSetting -v`
Expected: 3 of 4 FAIL with `KeyError: 'studio'`. (`test_saving_settings_preserves_existing_games` passes already; it is a regression guard.)

- [ ] **Step 3: Add the setting to the read and write paths**

In `get_all_settings()`, after the `'games'` line:

```python
        'studio': get_setting('studio', {'name': '', 'url': ''}),
```

In `save_all_settings()`, after the `set_setting('games', ...)` line:

```python
    set_setting('studio', data.get('studio', {'name': '', 'url': ''}))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_followers.TestStudioSetting -v`
Expected: 4 passed.

- [ ] **Step 5: Add the wizard form fields**

In the Step 1 wizard card, after the financial key hint `</div>` and before `<hr class="divider">` (currently line 1604):

```html
      <label style="margin-top:18px;">Studio Name (optional)</label>
      <input type="text" id="studioName" placeholder="Lime Blossom Studio" />

      <label style="margin-top:18px;">Studio Page URL (optional) <span class="key-status pending" id="studioStatus"></span></label>
      <input type="text" id="studioUrl" placeholder="https://store.steampowered.com/developer/LimeBlossom" />
      <div class="hint" style="margin-bottom:0;margin-top:6px;font-size:11px;">
        Your developer, publisher, or curator page. Leave blank to hide studio followers.
      </div>
```

- [ ] **Step 6: Prefill the fields when editing settings**

In the prefill block, after the `steamFinancialKey` line (currently line 1678):

```javascript
    var studioCfg = existingSettings.studio || {};
    document.getElementById('studioName').value = studioCfg.name || '';
    document.getElementById('studioUrl').value = studioCfg.url || '';
```

- [ ] **Step 7: Send the fields when saving**

In the `var payload = {` object, immediately after the `games: validGames,` line (currently line 1928) and before `telegram: {`:

```javascript
      studio: {
        name: document.getElementById('studioName').value.trim(),
        url: document.getElementById('studioUrl').value.trim()
      },
```

- [ ] **Step 8: Validate the studio URL in /api/test**

In the `/api/test` handler, after the `app_ids` parsing line (currently line 3183):

```python
            studio_url = params.get('studio_url', [''])[0]
```

After the financial key test block and before the `all_games_ok = ...` line (currently line 3248):

```python
            # Studio follower page is optional, so it never gates overall success
            studio_result = None
            if studio_url:
                studio_count = get_studio_followers(studio_url)
                if studio_count is None:
                    studio_result = {'success': False,
                                     'error': 'Could not read a follower count from that page'}
                else:
                    studio_result = {'success': True, 'followers': studio_count}
```

Add `'studio': studio_result,` to the `self._json_response({...})` dict, after `'financial_key_valid': financial_key_valid,`.

- [ ] **Step 9: Send and render the studio result in testConnection**

After the `if (financialKey) url += ...` line (currently line 1775):

```javascript
    var studioUrlVal = document.getElementById('studioUrl').value.trim();
    if (studioUrlVal) url += '&studio_url=' + encodeURIComponent(studioUrlVal);
```

After the financial key status block closes and before the `// Per-game results` comment (currently line 1804):

```javascript
        // Studio status (optional, never blocks setup)
        var studioSt = document.getElementById('studioStatus');
        if (studioUrlVal && data.studio) {
          if (data.studio.success) {
            studioSt.className = 'key-status ok';
            studioSt.textContent = '\\u2713';
            lines.push('\\u2713 Studio page verified (' + data.studio.followers + ' followers)');
          } else {
            studioSt.className = 'key-status fail';
            studioSt.textContent = '\\u2717';
            lines.push('\\u2717 Studio page: ' + (data.studio.error || 'Error'));
          }
        } else {
          studioSt.className = 'key-status pending';
          studioSt.textContent = '';
        }
```

Note the doubled backslashes: this JS lives inside a Python string.

- [ ] **Step 10: Verify the wizard end to end**

Run: `python dashboard.py`, open `http://localhost:8081/settings`, paste `https://store.steampowered.com/developer/LimeBlossom` into Studio Page URL, and click Test Connection.
Expected: a green check beside the Studio Page URL label and a line reading `✓ Studio page verified (20 followers)`. Save, reload the settings page, and confirm both studio fields are prefilled.

Then clear the Studio Page URL, save, and reload.
Expected: the field stays blank and no studio line appears in the test output.

- [ ] **Step 11: Run the full suite**

Run: `python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 12: Commit**

```bash
git add dashboard.py tests/test_followers.py
git commit -m "feat: add optional studio name and page URL settings

Blank URL disables studio follower tracking entirely, so the feature is
opt-in. Test Connection reports the live follower count, because a
wizard field that fails silently is the failure mode worth guarding."
```

---

### Task 5: Collect follower counts

**Files:**
- Modify: `dashboard.py:971-987` (`GameState.__init__`)
- Modify: `dashboard.py:991-997` (`DataCollector.__init__`)
- Modify: `dashboard.py:1086` (per-game fetch, after the discussions block)
- Modify: `dashboard.py:1151` (studio fetch, after the per-game loop, before `self.is_first_collection = False`)
- Test: `tests/test_followers.py`

**Interfaces:**
- Consumes: `STUDIO_APP_ID`, `FOLLOWER_FETCH_INTERVAL`, `record_follower_count`, `get_latest_follower_count` (Task 1); `get_game_followers` (Task 2); `get_studio_followers` (Task 3); the `studio` setting (Task 4).
- Produces:
  - `GameState.cached_followers` (int), `GameState.last_follower_count` (int), `GameState.followers_last_fetched` (float)
  - `DataCollector.cached_studio_followers` (int), `DataCollector.last_studio_followers` (int), `DataCollector.studio_followers_last_fetched` (float)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_followers.py`, before the `if __name__` block:

```python
class TestFollowerWarmStart(unittest.TestCase):
    """A restart must show the stored count, not '--', until the next fetch."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patcher = patch.object(dashboard, 'DB_PATH', os.path.join(self._tmp, 'test.db'))
        self._patcher.start()
        dashboard.init_db()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_game_state_loads_stored_count(self):
        dashboard.save_follower_count("12345", 44)
        gs = dashboard.GameState("12345")
        self.assertEqual(gs.cached_followers, 44)
        self.assertEqual(gs.last_follower_count, 44)
        self.assertEqual(gs.followers_last_fetched, 0.0)

    def test_game_state_defaults_to_zero_with_no_history(self):
        gs = dashboard.GameState("12345")
        self.assertEqual(gs.cached_followers, 0)

    def test_collector_loads_stored_studio_count(self):
        dashboard.save_follower_count(dashboard.STUDIO_APP_ID, 20)
        c = dashboard.DataCollector()
        self.assertEqual(c.cached_studio_followers, 20)
        self.assertEqual(c.last_studio_followers, 20)
        self.assertEqual(c.studio_followers_last_fetched, 0.0)

    def test_collector_defaults_to_zero_with_no_history(self):
        self.assertEqual(dashboard.DataCollector().cached_studio_followers, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_followers.TestFollowerWarmStart -v`
Expected: 4 FAIL with `AttributeError: 'GameState' object has no attribute 'cached_followers'` and the `DataCollector` equivalent.

- [ ] **Step 3: Add warm-start state to GameState**

In `GameState.__init__`, after the `self.discussions_last_fetched = 0.0` line:

```python
        self.cached_followers = get_latest_follower_count(app_id)
        self.last_follower_count = self.cached_followers
        self.followers_last_fetched = 0.0
```

- [ ] **Step 4: Add warm-start state to DataCollector**

In `DataCollector.__init__`, after the `self.throttled = False` line:

```python
        self.cached_studio_followers = get_latest_follower_count(STUDIO_APP_ID)
        self.last_studio_followers = self.cached_studio_followers
        self.studio_followers_last_fetched = 0.0
```

`main()` calls `init_db()` before constructing the collector, so the table exists by this point.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m unittest tests.test_followers.TestFollowerWarmStart -v`
Expected: 4 passed.

- [ ] **Step 6: Fetch per-game followers in the collect loop**

In `DataCollector.collect()`, immediately after the discussions block closes (currently line 1086) and before the `# Telegram alerts` comment:

```python
            if time.time() - gs.followers_last_fetched > FOLLOWER_FETCH_INTERVAL:
                self.status = f"{game_name}: Fetching followers"
                followers = get_game_followers(app_id)
                self.status = ""
                if record_follower_count(app_id, followers):
                    gs.cached_followers = followers
                    gs.followers_last_fetched = time.time()
                    print(f"  [{game_name}] Followers: {followers}")
                    gs.last_follower_count = followers
                else:
                    print(f"  [{game_name}] Followers: fetch failed, keeping {gs.cached_followers}")
```

On failure the timestamp is deliberately left unchanged so the next cycle retries rather than waiting another 30 minutes.

`gs.last_follower_count` is updated on every successful fetch, including the suppressed first one, so the first alert in Task 6 compares against the day-one reading rather than zero.

- [ ] **Step 7: Fetch studio followers once per cycle**

In `DataCollector.collect()`, after the `for game in games:` loop ends and before `self.is_first_collection = False` (currently line 1151):

```python
        studio_cfg = settings.get('studio', {})
        studio_url = studio_cfg.get('url', '')
        if studio_url and time.time() - self.studio_followers_last_fetched > FOLLOWER_FETCH_INTERVAL:
            self.status = "Fetching studio followers"
            studio_followers = get_studio_followers(studio_url)
            self.status = ""
            if record_follower_count(STUDIO_APP_ID, studio_followers):
                self.cached_studio_followers = studio_followers
                self.studio_followers_last_fetched = time.time()
                print(f"  [studio] Followers: {studio_followers}")
                self.last_studio_followers = studio_followers
            else:
                print(f"  [studio] Followers: fetch failed, keeping {self.cached_studio_followers}")
```

This sits outside the per-game loop so the studio page is fetched once per cycle rather than once per game, and before `is_first_collection` is cleared so Task 6's suppression covers it.

- [ ] **Step 8: Verify live collection**

Run `python dashboard.py` with the studio URL configured and watch the first cycle's output.
Expected: one `Followers: N` line per game plus one `[studio] Followers: N` line. Then confirm the rows landed:

```bash
python -c "import dashboard; print(dashboard.get_follower_history('2587260')); print(dashboard.get_follower_history(dashboard.STUDIO_APP_ID))"
```
Expected: one `(date, count)` tuple each, dated today.

- [ ] **Step 9: Run the full suite**

Run: `python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 10: Commit**

```bash
git add dashboard.py tests/test_followers.py
git commit -m "feat: collect follower counts on a 30 minute throttle

Followers move a few times a week, so scraping them every 300s poll is
wasted traffic. Studio is fetched once per cycle, not once per game.
State warm-starts from the DB so a restart shows real numbers."
```

---

### Task 6: Follower change alerts

**Files:**
- Modify: `dashboard.py` (add two formatters after `send_telegram`, currently ends line 905)
- Modify: `dashboard.py` (per-game alert inside the block added in Task 5 Step 6)
- Modify: `dashboard.py` (studio alert inside the block added in Task 5 Step 7)
- Test: `tests/test_followers.py`

**Interfaces:**
- Consumes: `send_telegram(tg_config, message)` from `dashboard.py:895`.
- Produces:
  - `format_follower_alert(prefix, old, new) -> str`
  - `format_studio_follower_alert(studio_name, old, new) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_followers.py`, before the `if __name__` block:

```python
class TestFollowerAlertText(unittest.TestCase):

    def test_single_gain_is_singular(self):
        msg = dashboard.format_follower_alert("", 44, 45)
        self.assertIn("New follower!", msg)
        self.assertIn("44 → 45", msg)
        self.assertIn("(+1)", msg)

    def test_multiple_gain_is_plural(self):
        msg = dashboard.format_follower_alert("", 44, 47)
        self.assertIn("New followers!", msg)
        self.assertIn("(+3)", msg)

    def test_single_loss_is_singular(self):
        msg = dashboard.format_follower_alert("", 45, 44)
        self.assertIn("Follower lost", msg)
        self.assertIn("(-1)", msg)

    def test_multiple_loss_is_plural(self):
        msg = dashboard.format_follower_alert("", 47, 44)
        self.assertIn("Followers lost", msg)
        self.assertIn("(-3)", msg)

    def test_prefix_included(self):
        msg = dashboard.format_follower_alert("[Chill Seekers] ", 44, 45)
        self.assertIn("[Chill Seekers] ", msg)

    def test_studio_gain_says_up_and_names_studio(self):
        msg = dashboard.format_studio_follower_alert("Lime Blossom Studio", 20, 21)
        self.assertIn("Studio followers up!", msg)
        self.assertIn("Lime Blossom Studio", msg)
        self.assertIn("20 → 21", msg)
        self.assertIn("(+1)", msg)

    def test_studio_loss_says_down(self):
        msg = dashboard.format_studio_follower_alert("Lime Blossom Studio", 21, 20)
        self.assertIn("Studio followers down!", msg)
        self.assertIn("(-1)", msg)

    def test_studio_blank_name_omits_name_line(self):
        msg = dashboard.format_studio_follower_alert("", 20, 21)
        self.assertIn("Studio followers up!", msg)
        self.assertNotIn("\n  \n", msg)

    def test_studio_alert_never_mentions_a_game_total(self):
        msg = dashboard.format_studio_follower_alert("Lime Blossom Studio", 20, 21)
        self.assertNotIn("across", msg)
        self.assertNotIn("total", msg.lower())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_followers.TestFollowerAlertText -v`
Expected: 9 FAIL with `AttributeError: module 'dashboard' has no attribute 'format_follower_alert'`.

- [ ] **Step 3: Write the formatters**

In `dashboard.py`, after `send_telegram` ends (currently line 905) and before `def send_startup_report`:

```python
_ALERT_RULE = "━" * 12


def format_follower_alert(prefix, old, new):
    diff = new - old
    plural = "" if abs(diff) == 1 else "s"
    heading = f"New follower{plural}!" if diff > 0 else f"Follower{plural} lost"
    sign = "+" if diff > 0 else ""
    return (f"\U0001f465 <b>{prefix}{heading}</b>\n"
            f"{_ALERT_RULE}\n"
            f"  {old} → {new}  ({sign}{diff})")


def format_studio_follower_alert(studio_name, old, new):
    """Studio followers are their own metric, never a sum of game followers."""
    diff = new - old
    direction = "up" if diff > 0 else "down"
    name_line = f"  {studio_name}\n" if studio_name else ""
    sign = "+" if diff > 0 else ""
    return (f"\U0001f3e2 <b>Studio followers {direction}!</b>\n"
            f"{_ALERT_RULE}\n"
            f"{name_line}"
            f"  {old} → {new}  ({sign}{diff})")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_followers.TestFollowerAlertText -v`
Expected: 9 passed.

- [ ] **Step 5: Send the per-game alert**

In the per-game follower block added in Task 5 Step 6, replace the single line `gs.last_follower_count = followers` with:

```python
                    if not self.is_first_collection and followers != gs.last_follower_count:
                        send_telegram(tg, format_follower_alert(
                            f"[{game_name}] " if len(games) > 1 else "",
                            gs.last_follower_count, followers))
                    gs.last_follower_count = followers
```

Guarded only by `is_first_collection`. The `last_value > 0` guard used by the wishlist alert is deliberately omitted: warm-starting from the database plus first-cycle suppression already prevents cold-start spam, and that guard would swallow the genuinely interesting 0 to 1 transition on a newly added game.

- [ ] **Step 6: Send the studio alert**

In the studio block added in Task 5 Step 7, replace the single line `self.last_studio_followers = studio_followers` with:

```python
                if not self.is_first_collection and studio_followers != self.last_studio_followers:
                    send_telegram(tg, format_studio_follower_alert(
                        studio_cfg.get('name', ''),
                        self.last_studio_followers, studio_followers))
                self.last_studio_followers = studio_followers
```

`tg` is already bound near the top of `collect()` from `settings.get('telegram', {})`, and this block is inside `collect()`, so it is in scope.

- [ ] **Step 7: Verify suppression on a cold start**

Run `python dashboard.py` with Telegram enabled on a database that already has follower rows.
Expected: follower counts print, and no follower alert is sent on the first cycle.

- [ ] **Step 8: Run the full suite**

Run: `python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add dashboard.py tests/test_followers.py
git commit -m "feat: Telegram alerts on follower changes

Fires on any change in either direction, since at single-digit weekly
growth any threshold above 1 would almost never fire. Suppressed on the
first collection so a restart does not replay offline changes."
```

---

### Task 7: Expose followers over the API

**Files:**
- Modify: `dashboard.py:3307-3308` (`/api/data` payload)
- Modify: `dashboard.py:3362-3368` (`per_game` dict in `/api/data-all`)
- Modify: `dashboard.py:3433-3434` (`/api/data-all` payload)

**Interfaces:**
- Consumes: `get_follower_history` (Task 1); `GameState.cached_followers` and `DataCollector.cached_studio_followers` (Task 5); the `studio` setting (Task 4).
- Produces:
  - `/api/data` gains `followers` (int) and `follower_history` (list of `[date, count]`)
  - `/api/data-all` gains `studio_followers` (int), `studio_name` (str), `studio_configured` (bool), `studio_follower_history` (list), and `follower_history` inside each `per_game` entry

- [ ] **Step 1: Add the per-game keys**

In the `/api/data` payload dict, after the `"discussions": gs.cached_discussions,` line:

```python
                "followers": gs.cached_followers,
                "follower_history": get_follower_history(req_app_id),
```

No studio keys on this endpoint. The studio metric belongs only to the All Games view.

- [ ] **Step 2: Add follower history to each per_game entry**

In the `per_game[app_id] = {...}` dict, after the `"daily_wishlists": get_daily_wishlists(app_id)` line, adding a comma to that line:

```python
                    "follower_history": get_follower_history(app_id)
```

- [ ] **Step 3: Add the studio keys**

In the `/api/data-all` payload dict, after the `"discussions": all_discussions,` line:

```python
                "studio_followers": collector.cached_studio_followers,
                "studio_name": settings.get('studio', {}).get('name', ''),
                "studio_configured": bool(settings.get('studio', {}).get('url', '')),
                "studio_follower_history": get_follower_history(STUDIO_APP_ID),
```

`studio_configured` is what the UI uses to decide whether to show the card, rather than inferring it from a zero count.

- [ ] **Step 4: Verify both endpoints**

Start `python dashboard.py`, then run:

```bash
python -c "import json,urllib.request as u; d=json.load(u.urlopen('http://localhost:8081/api/data?app_id=2587260')); print(d['followers'], len(d['follower_history']), 'studio_followers' in d)"
```
Expected: the current count, `1` (or more), and `False`. The `False` confirms no studio key leaked onto the per-game endpoint.

```bash
python -c "import json,urllib.request as u; d=json.load(u.urlopen('http://localhost:8081/api/data-all')); print(d['studio_followers'], d['studio_name'], d['studio_configured'], len(d['studio_follower_history'])); print([len(g['follower_history']) for g in d['per_game'].values()])"
```
Expected: the studio count, the studio name, `True`, at least 1, and a per-game list with an entry per game.

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add dashboard.py
git commit -m "feat: serve follower counts and history from both data endpoints

Studio keys are confined to /api/data-all, since studio followers are
an All Games metric and not a per-game one."
```

---

### Task 8: Followers metric card

**Files:**
- Modify: `dashboard.py:2119-2122` (`.metrics-grid` columns)
- Modify: `dashboard.py:2325` (add fifth-child animation delays)
- Modify: `dashboard.py:2424` (insert the card after the Wishlists card)
- Modify: `dashboard.py:2949` (populate the card in the update path)

**Interfaces:**
- Consumes: `followers`, `studio_followers`, `studio_name`, `studio_configured` from Task 7.
- Produces: DOM ids `followersCard`, `followersLabel`, `followerCount`, `followerSub`.

- [ ] **Step 1: Let the grid hold five cards**

Replace the `.metrics-grid` rule:

```css
.metrics-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px; margin-bottom: 12px;
}
```

- [ ] **Step 2: Extend the stagger to a fifth card**

After the `.metrics-grid + .metrics-grid .metric-card:nth-child(4)` rule (currently line 2325):

```css
.metrics-grid .metric-card:nth-child(5) { animation-delay: 0.225s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(5) { animation-delay: 0.45s; }
```

- [ ] **Step 3: Insert the card beside Wishlists**

In the second `metrics-grid`, after the Wishlists card's closing `</div>` (currently line 2424) and before the Refund Rate card, so the row reads Reviews, Positive Rate, Wishlists, Followers, Refund Rate:

```html
    <div class="metric-card" id="followersCard">
      <div class="metric-label" id="followersLabel">Followers</div>
      <div class="metric-value loading" id="followerCount">--</div>
      <div class="metric-sub" id="followerSub"></div>
    </div>
```

The card is deliberately **not** marked `sales-only`. Reviews, Positive Rate, and Refund Rate all are, so on an unreleased game the row collapses to exactly Wishlists and Followers, side by side.

- [ ] **Step 4: Populate the card**

In the data update path, immediately before the line that clears the loading class (`document.querySelectorAll('.metric-value.loading')...`, currently line 2949):

```javascript
      // Followers. Studio followers are a separate metric shown only on All
      // Games, never a sum of the per-game counts.
      var fCard = document.getElementById('followersCard');
      if (isPortfolioMode) {
        if (data.studio_configured) {
          fCard.style.display = '';
          document.getElementById('followersLabel').textContent = 'Studio Followers';
          document.getElementById('followerCount').textContent = (data.studio_followers || 0).toLocaleString();
          document.getElementById('followerSub').textContent = data.studio_name || '';
        } else {
          fCard.style.display = 'none';
        }
      } else {
        fCard.style.display = '';
        document.getElementById('followersLabel').textContent = 'Followers';
        document.getElementById('followerCount').textContent = (data.followers || 0).toLocaleString();
        document.getElementById('followerSub').textContent = '';
      }
```

The per-game card shows the count and nothing else. No delta: the existing `playerChange` delta is poll-over-poll, and a second delta on a different window would introduce a competing convention on a card whose whole content is one scalar. Trend lives in the chart from Task 9.

- [ ] **Step 5: Verify in the browser**

Run `python dashboard.py` and open `http://localhost:8081`.
Expected, on a released game: five cards in the second row reading Reviews, Positive Rate, Wishlists, Followers, Refund Rate, with Followers showing a real count and no sub-line.
Expected, on the unreleased game: exactly two cards, Wishlists and Followers, adjacent.
Expected, on All Games with a studio URL set: the card reads `Studio Followers` with the studio name beneath.
Expected, on All Games with the studio URL cleared: the card is absent and the remaining cards stay evenly spaced.

Narrow the window to roughly 700px and confirm the row reflows without overflowing horizontally.

- [ ] **Step 6: Commit**

```bash
git add dashboard.py
git commit -m "feat: followers metric card beside wishlists

Grid switches to auto-fit so five cards flow evenly. Card is not
sales-only, so unreleased games show exactly wishlists and followers."
```

---

### Task 9: Follower Growth chart

**Files:**
- Modify: `dashboard.py:2151-2152` (add `.charts-grid-3`)
- Modify: `dashboard.py:2460` (add the chart card as the third child of `wishlistChartsRow`)
- Modify: `dashboard.py:2492` (declare `followerChart`)
- Modify: `dashboard.py:2568` (destroy it in `rebuildCharts`)
- Modify: `dashboard.py:2734` (create it in `initCharts`)
- Modify: `dashboard.py:2918-2928` (row class swap)
- Modify: `dashboard.py:3015` (per-game data update, after the `wishlistChart.update` call)
- Modify: `dashboard.py:2894` (portfolio data update, after the `wishlistStackedChart.update` call)

**Interfaces:**
- Consumes: `follower_history` and `studio_follower_history` from Task 7; existing `gameColors` (line 2551), `getChartColors()` (line 2533), `legendCfg` (line 2596), `baseOpts`, `baseScaleX`, `baseScaleY`, `baseTooltip`, `isMobile`, `isPortfolioMode`.

**Scope note:** `var cc = getChartColors()` is local to `initCharts()`, so `cc` is **not** in scope in `fetchData()` or `updatePortfolioCharts()`. Steps 6 and 7 declare their own local instead, matching how the existing portfolio code calls `getChartColors().green` inline.
- Produces: chart instance `followerChart`; DOM ids `followerChartCard`, `followerChart`.

- [ ] **Step 1: Add a three-across grid class**

After the `.charts-row` rule (currently line 2152):

```css
.charts-grid-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; margin-bottom: 12px; }
```

Without this, three visible cards in the two-column `.charts-row` would orphan the third.

- [ ] **Step 2: Add the chart card**

Inside `wishlistChartsRow`, after the `wishlistStackedCard` div closes (currently line 2460):

```html
    <div class="chart-card" id="followerChartCard">
      <h3>Follower Growth</h3>
      <canvas id="followerChart" height="180"></canvas>
    </div>
```

- [ ] **Step 3: Declare and destroy the chart**

Add `followerChart` to the chart variable declaration list (currently line 2492):

```javascript
  var playerChart, salesChart, salesTimelineChart, revenueTimelineChart, wishlistChart, wishlistStackedChart, followerChart;
```

In `rebuildCharts`, after the `if (wishlistStackedChart) wishlistStackedChart.destroy();` line:

```javascript
    if (followerChart) followerChart.destroy();
```

- [ ] **Step 4: Create the chart**

In `initCharts`, after the `if (isPortfolioMode) { ... } else { wishlistStackedChart = null; }` block closes (currently line 2734) and before the closing `}` of `initCharts`:

```javascript
    // Follower growth. Single shared y-axis: a second axis for the studio
    // series would obscure that these measure different things.
    followerChart = new Chart(document.getElementById('followerChart'), {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: Object.assign({}, baseOpts, {
        plugins: { legend: isPortfolioMode ? legendCfg : { display: false }, tooltip: baseTooltip },
        scales: {
          x: Object.assign({}, baseScaleX, { ticks: Object.assign({}, baseScaleX.ticks, { maxTicksLimit: 20 }) }),
          y: Object.assign({}, baseScaleY, { beginAtZero: false })
        }
      })
    });
```

`beginAtZero: false` because growth from 44 to 47 is the signal, and a zero-anchored axis would flatten it.

- [ ] **Step 5: Set the row layout per view**

In the view-mode branch, change the two `wishlistChartsRow` class assignments. The portfolio branch (currently line 2921) becomes:

```javascript
        document.getElementById('wishlistChartsRow').className = 'charts-grid-3';
```

The single-game branch (currently line 2928) becomes:

```javascript
        document.getElementById('wishlistChartsRow').className = 'charts-row';
```

In single-game mode `wishlistStackedCard` is `display:none`, so it drops out of grid flow and Wishlist Activity pairs with Follower Growth in the two columns.

- [ ] **Step 6: Draw the single-game series**

In the per-game data update path, after the `wishlistChart.update('none');` line (currently line 3015):

```javascript
      if (followerChart && !isPortfolioMode) {
        var fcc = getChartColors();
        var isMobile = window.innerWidth <= 768;
        var fh = data.follower_history || [];
        followerChart.data.labels = fh.map(function(r) { return r[0]; });
        followerChart.data.datasets = [{
          label: 'Followers',
          data: fh.map(function(r) { return r[1]; }),
          borderColor: fcc.gold, backgroundColor: 'transparent',
          borderWidth: 2, tension: 0.35,
          pointRadius: 0, pointHoverRadius: isMobile ? 2 : 4,
          pointBackgroundColor: fcc.gold, pointBorderColor: 'transparent'
        }];
        followerChart.update('none');
      }
```

`fcc`, not `cc`: see the scope note above. `fcc.gold` is the theme accent colour, despite the key name.

`isMobile` needs a local declaration here for the same reason as `fcc`. It is declared only inside `initCharts` and `updatePortfolioCharts`, neither of which covers `fetchData`, so referencing it bare would be a `ReferenceError` that silently kills the chart update. The value matches both existing declarations exactly.

- [ ] **Step 7: Draw the portfolio series**

In `updatePortfolioCharts`, after the `wishlistStackedChart.update('none');` line and its closing brace (currently line 2895):

```javascript
    // Follower growth: one line per game plus the studio. The studio line is
    // dashed, heavier and legend-first so it cannot read as another game --
    // studio followers are independent of game followers, not a sum of them.
    if (followerChart) {
      var fcc = getChartColors();
      var studioFh = data.studio_follower_history || [];
      var allFDates = {};
      gameIds.forEach(function(id) {
        (perGame[id].follower_history || []).forEach(function(r) { allFDates[r[0]] = true; });
      });
      studioFh.forEach(function(r) { allFDates[r[0]] = true; });
      var sortedFDates = Object.keys(allFDates).sort();

      var fDatasets = [];

      if (data.studio_configured) {
        var studioByDate = {};
        studioFh.forEach(function(r) { studioByDate[r[0]] = r[1]; });
        fDatasets.push({
          label: data.studio_name || 'Studio',
          data: sortedFDates.map(function(d) {
            return studioByDate[d] === undefined ? null : studioByDate[d];
          }),
          borderColor: fcc.gold, backgroundColor: 'transparent',
          borderDash: [6, 3], borderWidth: 3, tension: 0.35,
          pointRadius: 0, pointHoverRadius: isMobile ? 2 : 4,
          pointBackgroundColor: fcc.gold, pointBorderColor: 'transparent',
          spanGaps: true
        });
      }

      gameIds.forEach(function(id, idx) {
        var color = gameColors[idx % gameColors.length];
        var byDate = {};
        (perGame[id].follower_history || []).forEach(function(r) { byDate[r[0]] = r[1]; });
        fDatasets.push({
          label: perGame[id].name,
          data: sortedFDates.map(function(d) {
            return byDate[d] === undefined ? null : byDate[d];
          }),
          borderColor: color.border, backgroundColor: 'transparent',
          borderWidth: 2, tension: 0.35,
          pointRadius: 0, pointHoverRadius: isMobile ? 2 : 4,
          pointBackgroundColor: color.border, pointBorderColor: 'transparent',
          spanGaps: true
        });
      });

      followerChart.data.labels = sortedFDates;
      followerChart.data.datasets = fDatasets;
      followerChart.update('none');
    }
```

`null` plus `spanGaps: true` is used rather than carrying the previous value forward, so a day with no reading is drawn as a straight connection rather than a fabricated plateau.

- [ ] **Step 8: Verify in the browser**

Run `python dashboard.py` and open `http://localhost:8081`.
Expected, single game: two chart cards side by side, Wishlist Activity and Follower Growth, with one line and no legend.
Expected, All Games: three chart cards across on a wide window, with the follower chart showing one line per game plus a dashed heavier studio line listed first in the legend.
Expected, All Games with the studio URL cleared: game lines only, no dashed line.

Switch between games several times and confirm no console errors, since `rebuildCharts` destroys and recreates the chart on every switch.

Narrow the window to roughly 700px and confirm the chart row reflows to a single column with no horizontal page scroll.

- [ ] **Step 9: Run the full suite**

Run: `python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 10: Update the README**

Add followers to the feature list in `README.md`, noting that history begins at first collection and cannot be backfilled because Steam exposes no follower API.

- [ ] **Step 11: Commit**

```bash
git add dashboard.py README.md
git commit -m "feat: Follower Growth chart with per-game and studio lines

Single shared y-axis. Studio line is dashed, heavier and legend-first so
it cannot be mistaken for a game. Missing days are gaps, not plateaus."
```

---

## Notes for the implementer

- **The follower charts will look wrong on day one, and that is correct.** Both sources expose only a current count, so the series starts at first collection and cannot be backfilled. A single point or a flat line for the first week is the expected state, not a bug.
- **If a parser returns `None` against a live page**, the page markup changed. Fix the parser before continuing; do not make the caller write 0.
- **The scrapers hit public pages with no key.** `fetch_html` already sends a browser User-Agent and handles HTTP 429 by returning `None`, so no extra rate-limit handling is needed beyond the 30 minute throttle.
