import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import patch
import dashboard

_TOPIC_LIST_HTML = '''<html><body>
<div class="forum_topic" data-gidforumtopic="111">
  <a class="forum_topic_overlay" href="/app/12345/discussions/0/111/"></a>
  <div class="forum_topic_name">Bug report</div>
  <div class="forum_topic_op">PlayerA</div>
  <div class="forum_topic_reply_count">2</div>
  <div class="forum_topic_lastpost" data-timestamp="1200"></div>
</div>
</body></html>'''

_DETAIL_WITH_REPLIES = '''<html><body>
<div class="forum_op">
  <div class="date commentthread_comment_timestamp" data-timestamp="1000"></div>
  <div class="content">Opening post body text here</div>
</div>
<div id="comment_1">
  <a class="hoverunderline commentthread_author_link"><bdi>PlayerB</bdi></a>
  <div class="commentthread_comment_timestamp" data-timestamp="1100"></div>
  <div class="commentthread_comment_text" id="comment_content_1">First reply</div>
</div>
<div id="comment_2">
  <a class="hoverunderline commentthread_author_link"><bdi>DevC</bdi></a>
  <div class="commentthread_comment_timestamp" data-timestamp="1200"></div>
  <div class="commentthread_comment_text" id="comment_content_2">Latest reply</div>
</div>
</body></html>'''

_DETAIL_NO_REPLIES = '''<html><body>
<div class="forum_op">
  <div class="date commentthread_comment_timestamp" data-timestamp="1000"></div>
  <div class="content">Opening post body text here</div>
</div>
</body></html>'''


class TestGetCommunityDiscussions(unittest.TestCase):

    def _responses(self, *items):
        it = iter(items)
        return lambda *a, **kw: next(it)

    def test_returns_none_when_list_fetch_fails(self):
        with patch.object(dashboard, 'fetch_html', return_value=None):
            result = dashboard.get_community_discussions("12345")
        self.assertIsNone(result)

    def test_returns_empty_when_no_topics(self):
        with patch.object(dashboard, 'fetch_html', return_value='<html><body></body></html>'):
            result = dashboard.get_community_discussions("12345")
        self.assertEqual(result, [])

    def test_thread_fields_populated(self):
        with patch.object(dashboard, 'fetch_html',
                          side_effect=self._responses(_TOPIC_LIST_HTML, _DETAIL_WITH_REPLIES)):
            result = dashboard.get_community_discussions("12345")
        self.assertEqual(len(result), 1)
        t = result[0]
        self.assertEqual(t["id"], "111")
        self.assertEqual(t["title"], "Bug report")
        self.assertEqual(t["reply_count"], 2)
        self.assertEqual(t["author"], "PlayerA")
        self.assertEqual(t["opening_snippet"], "Opening post body text here")

    def test_latest_reply_is_last_comment(self):
        with patch.object(dashboard, 'fetch_html',
                          side_effect=self._responses(_TOPIC_LIST_HTML, _DETAIL_WITH_REPLIES)):
            result = dashboard.get_community_discussions("12345")
        lr = result[0]["latest_reply"]
        self.assertIsNotNone(lr)
        self.assertEqual(lr["snippet"], "Latest reply")
        self.assertEqual(lr["author"], "DevC")
        self.assertEqual(lr["posted_at"], 1200)

    def test_latest_reply_none_when_no_replies(self):
        with patch.object(dashboard, 'fetch_html',
                          side_effect=self._responses(_TOPIC_LIST_HTML, _DETAIL_NO_REPLIES)):
            result = dashboard.get_community_discussions("12345")
        self.assertIsNone(result[0]["latest_reply"])

    def test_skips_thread_when_detail_fails(self):
        with patch.object(dashboard, 'fetch_html',
                          side_effect=self._responses(_TOPIC_LIST_HTML, None)):
            result = dashboard.get_community_discussions("12345")
        self.assertEqual(result, [])

    def test_url_made_absolute(self):
        with patch.object(dashboard, 'fetch_html',
                          side_effect=self._responses(_TOPIC_LIST_HTML, _DETAIL_WITH_REPLIES)):
            result = dashboard.get_community_discussions("12345")
        self.assertTrue(result[0]["url"].startswith("https://"))
        self.assertIn("/discussions/0/111/", result[0]["url"])

    def test_thread_with_no_url_gets_constructed_url(self):
        list_html = '''<html><body>
<div class="forum_topic" data-gidforumtopic="222">
  <div class="forum_topic_name">No URL thread</div>
  <div class="forum_topic_op">SomeUser</div>
  <div class="forum_topic_reply_count">0</div>
  <div class="forum_topic_lastpost" data-timestamp="0"></div>
</div></body></html>'''
        with patch.object(dashboard, 'fetch_html',
                          side_effect=self._responses(list_html, _DETAIL_NO_REPLIES)):
            result = dashboard.get_community_discussions("12345")
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["url"].startswith("https://"))
        self.assertIn("/discussions/0/222/", result[0]["url"])

    def test_opening_snippet_truncated_to_300_chars(self):
        long_body = "A" * 500
        list_html = '''<html><body>
<div class="forum_topic" data-gidforumtopic="1">
  <a class="forum_topic_overlay" href="/app/12345/discussions/0/1/"></a>
  <div class="forum_topic_name">T</div>
  <div class="forum_topic_op">U</div>
  <div class="forum_topic_reply_count">0</div>
  <div class="forum_topic_lastpost" data-timestamp="0"></div>
</div></body></html>'''
        detail_html = f'''<html><body>
<div class="forum_op">
  <div class="date commentthread_comment_timestamp" data-timestamp="0"></div>
  <div class="content">{long_body}</div>
</div></body></html>'''
        with patch.object(dashboard, 'fetch_html',
                          side_effect=self._responses(list_html, detail_html)):
            result = dashboard.get_community_discussions("12345")
        self.assertEqual(len(result[0]["opening_snippet"]), 300)


if __name__ == "__main__":
    unittest.main()
