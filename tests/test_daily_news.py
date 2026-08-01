from datetime import date
from zoneinfo import ZoneInfo
import unittest

from app.daily_news import Article, parse_feed, select_for_day, strip_html


RSS = b'''<?xml version="1.0"?><rss><channel><item><title>Model release</title><link>https://example.com/model</link><pubDate>Thu, 31 Jul 2026 23:30:00 +0000</pubDate><description>&lt;p&gt;A &lt;b&gt;new&lt;/b&gt; model&lt;/p&gt;</description></item></channel></rss>'''
ATOM = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Research post</title><link href="https://example.com/research"/><updated>2026-07-31T12:00:00Z</updated><summary>Results</summary></entry></feed>'''


class DailyNewsTests(unittest.TestCase):
    def test_parses_rss_and_html_description(self):
        article = parse_feed(RSS, "Example")[0]
        self.assertEqual(article.title, "Model release")
        self.assertEqual(article.excerpt, "A new model")

    def test_parses_atom_entry(self):
        article = parse_feed(ATOM, "Example")[0]
        self.assertEqual(article.url, "https://example.com/research")

    def test_selects_target_calendar_day_in_timezone_and_deduplicates(self):
        article = parse_feed(RSS, "Example")[0]
        duplicate = Article("Other", article.title, article.url, article.published_at, "")
        selected = select_for_day([article, duplicate], date(2026, 7, 31), ZoneInfo("UTC"))
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(select_for_day([article], date(2026, 8, 1), ZoneInfo("Asia/Kolkata"))), 1)

    def test_strip_html(self):
        self.assertEqual(strip_html("<p>Hello&nbsp;<em>world</em></p>"), "Hello world")


if __name__ == "__main__":
    unittest.main()
