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


def _creator_json(followers=20, name="Lime Blossom Studio", success=1):
    return {"success": success, "creator_clan_id": 44681599, "name": name,
            "followers": followers, "vanity": "limeblossom"}


class TestGetStudioFollowers(unittest.TestCase):
    """The HTML path is the fallback; every test pins which path it exercises.

    fetch_json must be patched in all of these. Left unpatched it reaches the
    live store endpoint, which made these tests pass by accident over the
    network rather than against the fixture.
    """

    def setUp(self):
        # Module-level cache; leaking a clan id between tests would let one
        # test's URL satisfy another's lookup.
        dashboard._studio_clan_ids.clear()

    def test_parses_follower_count(self):
        with patch.object(dashboard, 'fetch_html', return_value=_STUDIO_HTML), \
             patch.object(dashboard, 'fetch_json', return_value=None):
            self.assertEqual(
                dashboard.get_studio_followers("https://store.steampowered.com/developer/LimeBlossom"), 20)

    def test_parses_comma_separated_count(self):
        with patch.object(dashboard, 'fetch_html', return_value=_STUDIO_HTML_COMMAS), \
             patch.object(dashboard, 'fetch_json', return_value=None):
            self.assertEqual(dashboard.get_studio_followers("https://x/"), 12345)

    def test_ignores_num_followers_text_label(self):
        html = '<html><body><div class="num_followers_text">Followers</div></body></html>'
        with patch.object(dashboard, 'fetch_html', return_value=html), \
             patch.object(dashboard, 'fetch_json', return_value=None):
            self.assertIsNone(dashboard.get_studio_followers("https://x/"))

    def test_returns_none_when_fetch_fails(self):
        with patch.object(dashboard, 'fetch_html', return_value=None), \
             patch.object(dashboard, 'fetch_json', return_value=None):
            self.assertIsNone(dashboard.get_studio_followers("https://x/"))

    def test_returns_none_when_markup_absent(self):
        with patch.object(dashboard, 'fetch_html', return_value='<html><body></body></html>'), \
             patch.object(dashboard, 'fetch_json', return_value=None):
            self.assertIsNone(dashboard.get_studio_followers("https://x/"))

    def test_blank_url_touches_no_network(self):
        with patch.object(dashboard, 'fetch_html') as mh, \
             patch.object(dashboard, 'fetch_json') as mj:
            self.assertIsNone(dashboard.get_studio_followers(""))
        mh.assert_not_called()
        mj.assert_not_called()

    def test_developer_url_fetches_the_page_to_find_the_clan_id(self):
        target = "https://store.steampowered.com/developer/LimeBlossom"
        with patch.object(dashboard, 'fetch_html', return_value=_STUDIO_HTML) as mh, \
             patch.object(dashboard, 'fetch_json', return_value=_creator_json()):
            self.assertEqual(dashboard.get_studio_followers(target), 20)
        self.assertEqual(mh.call_args[0][0], target)

    def test_curator_url_skips_the_page_fetch_entirely(self):
        """The id is in the URL, so no page needs reading."""
        with patch.object(dashboard, 'fetch_html') as mh, \
             patch.object(dashboard, 'fetch_json', return_value=_creator_json(31)) as mj:
            self.assertEqual(
                dashboard.get_studio_followers("https://store.steampowered.com/curator/44681599"), 31)
        mh.assert_not_called()
        self.assertIn("44681599", mj.call_args[0][0])

    def test_json_preferred_over_markup_when_both_available(self):
        with patch.object(dashboard, 'fetch_html', return_value=_STUDIO_HTML), \
             patch.object(dashboard, 'fetch_json', return_value=_creator_json(99)):
            self.assertEqual(dashboard.get_studio_followers("https://x/curator/44681599"), 99)

    def test_null_name_is_none_not_a_fabricated_zero(self):
        """A nonexistent clan id answers success:1, followers:0, name:null.

        Trusting that would store a zero for a mistyped URL, permanently.
        """
        with patch.object(dashboard, 'fetch_html', return_value=None), \
             patch.object(dashboard, 'fetch_json',
                          return_value=_creator_json(followers=0, name=None)):
            self.assertIsNone(
                dashboard.get_studio_followers("https://x/curator/999999999"))

    def test_invalid_clan_does_not_fall_back_to_a_placeholder_page(self):
        """Regression: the markup fallback used to defeat the JSON guard.

        A nonexistent curator's store page still renders a num_followers div
        showing 0, so falling back after an 'invalid' JSON answer stored a
        fabricated zero. Verified live before this guard existed.
        """
        placeholder = ('<html><body><div class="num_followers" '
                       'id="CuratorNumFollowers_999999999">0</div></body></html>')
        with patch.object(dashboard, 'fetch_html', return_value=placeholder) as mh, \
             patch.object(dashboard, 'fetch_json',
                          return_value=_creator_json(followers=0, name=None)):
            self.assertIsNone(
                dashboard.get_studio_followers("https://x/curator/999999999"))
        mh.assert_not_called()

    def test_invalid_clan_is_not_cached(self):
        """A bad id must not be remembered as though it resolved."""
        with patch.object(dashboard, 'fetch_html', return_value=None), \
             patch.object(dashboard, 'fetch_json',
                          return_value=_creator_json(followers=0, name=None)):
            dashboard.get_studio_followers("https://x/curator/999999999")
        self.assertEqual(dashboard._studio_clan_ids, {})

    def test_unsuccessful_json_falls_back_to_markup(self):
        with patch.object(dashboard, 'fetch_html', return_value=_STUDIO_HTML), \
             patch.object(dashboard, 'fetch_json', return_value=_creator_json(success=0)):
            self.assertEqual(dashboard.get_studio_followers("https://x/curator/44681599"), 20)

    def test_genuine_zero_from_json_is_kept(self):
        with patch.object(dashboard, 'fetch_html', return_value=None), \
             patch.object(dashboard, 'fetch_json', return_value=_creator_json(followers=0)):
            self.assertEqual(
                dashboard.get_studio_followers("https://x/curator/44681599"), 0)

    def test_clan_id_cached_so_the_page_is_read_once(self):
        target = "https://store.steampowered.com/developer/LimeBlossom"
        with patch.object(dashboard, 'fetch_html', return_value=_STUDIO_HTML) as mh, \
             patch.object(dashboard, 'fetch_json', return_value=_creator_json()):
            dashboard.get_studio_followers(target)
            dashboard.get_studio_followers(target)
        self.assertEqual(mh.call_count, 1)


class TestClanIdExtraction(unittest.TestCase):

    def test_from_curator_url(self):
        self.assertEqual(
            dashboard._clan_id_from_url("https://store.steampowered.com/curator/44681599"), "44681599")
        self.assertEqual(
            dashboard._clan_id_from_url("https://store.steampowered.com/curator/44681599/"), "44681599")
        self.assertEqual(
            dashboard._clan_id_from_url("https://store.steampowered.com/curator/44681599?l=english"), "44681599")

    def test_none_from_developer_url(self):
        self.assertIsNone(
            dashboard._clan_id_from_url("https://store.steampowered.com/developer/LimeBlossom"))

    def test_from_follower_div_id(self):
        self.assertEqual(dashboard._clan_id_from_html(_STUDIO_HTML), "44681599")

    def test_from_curator_clanid_query_param(self):
        html = '<a href="/app/1?curator_clanid=44681599&snr=x">x</a>'
        self.assertEqual(dashboard._clan_id_from_html(html), "44681599")

    def test_none_when_absent(self):
        self.assertIsNone(dashboard._clan_id_from_html("<html><body></body></html>"))

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


_STEAMDB_CSV = '\ufeff"DateTime","Followers"\n' \
               '"2023-09-17 00:00:00",2\n' \
               '"2023-09-18 00:00:00",3\n' \
               '"2023-09-19 00:00:00",3\n'

_PLAIN_CSV = "date,count\n2026-01-01,10\n2026-01-02,11\n"


class TestParseFollowerCsv(unittest.TestCase):

    def test_parses_steamdb_export_with_bom_and_time(self):
        rows, rejected = dashboard.parse_follower_csv(_STEAMDB_CSV)
        self.assertEqual(rows, [("2023-09-17", 2), ("2023-09-18", 3), ("2023-09-19", 3)])
        self.assertEqual(rejected, [])

    def test_parses_plain_date_count(self):
        rows, rejected = dashboard.parse_follower_csv(_PLAIN_CSV)
        self.assertEqual(rows, [("2026-01-01", 10), ("2026-01-02", 11)])
        self.assertEqual(rejected, [])

    def test_rows_sorted_ascending_regardless_of_file_order(self):
        rows, _ = dashboard.parse_follower_csv(
            "date,count\n2026-01-03,3\n2026-01-01,1\n2026-01-02,2\n")
        self.assertEqual([d for d, _ in rows], ["2026-01-01", "2026-01-02", "2026-01-03"])

    def test_unknown_headers_raise(self):
        with self.assertRaises(ValueError):
            dashboard.parse_follower_csv("foo,bar\n1,2\n")

    def test_bad_rows_rejected_not_silently_dropped(self):
        rows, rejected = dashboard.parse_follower_csv(
            "date,count\n2026-01-01,10\nnot-a-date,5\n2026-01-02,oops\n2026-01-03,-4\n")
        self.assertEqual(rows, [("2026-01-01", 10)])
        self.assertEqual(len(rejected), 3)

    def test_duplicate_date_rejected(self):
        rows, rejected = dashboard.parse_follower_csv(
            "date,count\n2026-01-01,10\n2026-01-01,11\n")
        self.assertEqual(rows, [("2026-01-01", 10)])
        self.assertEqual(len(rejected), 1)

    def test_genuine_zero_is_kept(self):
        rows, rejected = dashboard.parse_follower_csv("date,count\n2026-01-01,0\n")
        self.assertEqual(rows, [("2026-01-01", 0)])
        self.assertEqual(rejected, [])

    def test_app_id_derived_from_steamdb_filename(self):
        self.assertEqual(dashboard.app_id_from_csv_name("steamdb_chart_2587260.csv"), "2587260")
        self.assertEqual(
            dashboard.app_id_from_csv_name(r"C:\x\steamdb_chart_4627290.csv"), "4627290")
        self.assertIsNone(dashboard.app_id_from_csv_name("followers.csv"))


class TestImportFollowerHistory(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patcher = patch.object(dashboard, 'DB_PATH', os.path.join(self._tmp, 'test.db'))
        self._patcher.start()
        dashboard.init_db()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_inserts_all_rows(self):
        inserted, skipped = dashboard.import_follower_history(
            "1", [("2026-01-01", 10), ("2026-01-02", 11)])
        self.assertEqual((inserted, skipped), (2, 0))
        self.assertEqual(dashboard.get_follower_history("1"),
                         [("2026-01-01", 10), ("2026-01-02", 11)])

    def test_never_overwrites_a_scraped_row(self):
        """A reading this dashboard took itself must outrank an imported one."""
        dashboard.save_follower_count("1", 44)          # today, scraped
        today = dashboard.datetime.now().strftime("%Y-%m-%d")
        inserted, skipped = dashboard.import_follower_history("1", [(today, 99)])
        self.assertEqual((inserted, skipped), (0, 1))
        self.assertEqual(dashboard.get_latest_follower_count("1"), 44)

    def test_import_is_scoped_to_its_app(self):
        dashboard.import_follower_history("1", [("2026-01-01", 10)])
        self.assertEqual(dashboard.get_follower_history("2"), [])

    def test_anchor_zero_writes_a_real_zero(self):
        dashboard.import_follower_history("1", [("2026-01-01", 0)])
        self.assertEqual(dashboard.get_follower_history("1"), [("2026-01-01", 0)])
        self.assertEqual(dashboard.get_latest_follower_count("1"), 0)


if __name__ == "__main__":
    unittest.main()
