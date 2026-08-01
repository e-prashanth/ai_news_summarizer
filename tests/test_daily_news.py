from datetime import date
from zoneinfo import ZoneInfo
import unittest

from app.daily_news import Article, is_retryable_gemini_error, limit_per_source, markdown_to_email_html, parse_feed, parse_recipients, select_for_day, strip_html


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

    def test_limits_articles_per_source(self):
        articles = parse_feed(RSS, "Example") * 3
        self.assertEqual(len(limit_per_source(articles, 2)), 2)

    def test_parses_multiple_recipients_and_renders_html(self):
        self.assertEqual(parse_recipients("one@example.com, two@example.com"), ["one@example.com", "two@example.com"])
        email_html = markdown_to_email_html("# Daily AI News\n\n## Highlights\n\n### Launch\nSource: [Example](https://example.com)")
        self.assertIn("Daily AI Briefing", email_html)
        self.assertIn('href="https://example.com"', email_html)

    def test_only_retries_transient_gemini_errors(self):
        transient = RuntimeError("incomplete chunked read")
        quota = RuntimeError("quota")
        quota.status_code = 429
        self.assertTrue(is_retryable_gemini_error(transient))
        self.assertFalse(is_retryable_gemini_error(quota))


if __name__ == "__main__":
    unittest.main()
