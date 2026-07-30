import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import tempfile
import shutil
import sqlite3
import threading
import time
import json
import urllib.request
from contextlib import ExitStack
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

    def test_latest_is_none_when_empty(self):
        self.assertIsNone(dashboard.get_latest_follower_count("12345"))

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
        self.assertEqual(gs.followers_next_fetch, 0.0)

    def test_game_state_defaults_to_none_with_no_history(self):
        gs = dashboard.GameState("12345")
        self.assertIsNone(gs.cached_followers)

    def test_collector_loads_stored_studio_count(self):
        dashboard.save_follower_count(dashboard.STUDIO_APP_ID, 20)
        c = dashboard.DataCollector()
        self.assertEqual(c.cached_studio_followers, 20)
        self.assertEqual(c.studio_followers_next_fetch, 0.0)

    def test_collector_defaults_to_none_with_no_history(self):
        self.assertIsNone(dashboard.DataCollector().cached_studio_followers)


class TestFollowerThrottle(unittest.TestCase):
    """The throttle honours its interval and its retry floor."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patcher = patch.object(dashboard, 'DB_PATH', os.path.join(self._tmp, 'test.db'))
        self._patcher.start()
        dashboard.init_db()
        dashboard.save_all_settings({
            'steam_api_key': 'k',
            'steam_financial_key': 'fk',
            'games': [{'app_id': '12345', 'name': 'Test Game', 'launch_date': '2025-01-01'}],
        })

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _patches(self, followers_side_effect):
        """All non-follower collaborators stubbed so collect() only touches the temp DB."""
        stack = ExitStack()
        m = stack.enter_context(patch.object(dashboard, 'get_game_followers', side_effect=followers_side_effect))
        stack.enter_context(patch.object(dashboard, 'refresh_all_sales'))
        stack.enter_context(patch.object(dashboard, 'refresh_all_wishlists'))
        stack.enter_context(patch.object(dashboard, 'get_current_players', return_value=0))
        stack.enter_context(patch.object(dashboard, 'get_reviews', return_value={}))
        stack.enter_context(patch.object(dashboard, 'get_recent_reviews', return_value=[]))
        stack.enter_context(patch.object(dashboard, 'get_app_details', return_value=None))
        stack.enter_context(patch.object(dashboard, 'get_community_discussions', return_value=None))
        stack.enter_context(patch.object(dashboard, 'get_sales_totals', return_value=(0, 0, 0, 0.0)))
        return stack, m

    def test_throttle_skips_immediate_repeat_and_backs_off_on_failure(self):
        # A second immediate collection must not re-fetch: the interval isn't up yet.
        collector = dashboard.DataCollector()
        stack, m = self._patches(lambda app_id: 10)
        with stack:
            collector.collect()
            self.assertEqual(m.call_count, 1)
            collector.collect()
            self.assertEqual(m.call_count, 1)
        success_next = collector.get_state('12345').followers_next_fetch

        # A failed fetch must schedule its retry sooner than a successful one did.
        fail_collector = dashboard.DataCollector()
        stack2, _ = self._patches(lambda app_id: None)
        with stack2:
            fail_collector.collect()
        fail_next = fail_collector.get_state('12345').followers_next_fetch

        now = time.time()
        self.assertLess(fail_next - now, success_next - now)
        self.assertAlmostEqual(fail_next - now, dashboard.FOLLOWER_RETRY_INTERVAL, delta=5)
        self.assertAlmostEqual(success_next - now, dashboard.FOLLOWER_FETCH_INTERVAL, delta=5)


class TestApiDataHasNoStudioKey(unittest.TestCase):
    """Studio followers are an All Games metric only; the per-game payload must not carry them."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patcher = patch.object(dashboard, 'DB_PATH', os.path.join(self._tmp, 'test.db'))
        self._patcher.start()
        dashboard.init_db()
        dashboard.save_all_settings({
            'steam_api_key': 'k',
            'steam_financial_key': 'fk',
            'games': [{'app_id': '12345', 'name': 'Test Game', 'launch_date': '2025-01-01'}],
        })
        dashboard.save_follower_count('12345', 44)

        self._server = dashboard.ReusableHTTPServer(('127.0.0.1', 0), dashboard.DashboardHandler)
        self._server.collector = dashboard.DataCollector()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._port = self._server.server_address[1]

    def tearDown(self):
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()
        self._patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_per_game_payload_omits_studio_keys(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self._port}/api/data?app_id=12345", timeout=5) as resp:
            payload = json.loads(resp.read().decode('utf-8'))

        self.assertIn('followers', payload)
        self.assertIn('follower_history', payload)
        for key in ('studio_followers', 'studio_name', 'studio_configured', 'studio_follower_history'):
            self.assertNotIn(key, payload)


if __name__ == "__main__":
    unittest.main()
