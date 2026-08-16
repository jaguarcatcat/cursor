import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import google_news_parser as parser


RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>First story - Example News</title>
    <link>https://news.google.com/rss/articles/one?oc=5</link>
    <guid>one</guid>
    <pubDate>Sun, 16 Aug 2026 12:00:00 GMT</pubDate>
    <description><![CDATA[
      <a href="https://news.google.com/rss/articles/one">First story</a>
      <font>Example News</font><p>Exact Google snippet.</p>
    ]]></description>
    <source url="https://www.example.ru">Example News</source>
  </item>
  <item>
    <title>Second story - Other</title>
    <link>https://news.google.com/rss/articles/two?oc=5</link>
    <pubDate>bad date</pubDate>
    <description><![CDATA[
      <a href="https://news.google.com/rss/articles/two">Second story</a>
      <font>Other</font>
    ]]></description>
    <source url="https://other.ru">Other</source>
  </item>
  <item>
    <title>Duplicate - Example News</title>
    <link>https://news.google.com/rss/articles/duplicate?oc=5</link>
    <description>Duplicate</description>
    <source url="https://www.example.ru">Example News</source>
  </item>
</channel></rss>"""


class GoogleNewsParserTests(unittest.TestCase):
    def test_feed_url_has_requested_russian_locale(self):
        url = parser.build_feed_url("лобов вадим")
        self.assertIn("hl=ru", url)
        self.assertIn("gl=RU", url)
        self.assertIn("ceid=RU%3Aru", url)
        self.assertIn("%D0%BB%D0%BE%D0%B1%D0%BE%D0%B2", url)

    def test_description_uses_only_actual_extra_text(self):
        snippet, grouped = parser.parse_description(
            '<a href="#">Заголовок</a><font>СМИ</font><p>Точный сниппет.</p>',
            "Заголовок",
            "СМИ",
        )
        self.assertEqual(snippet, "Точный сниппет.")
        self.assertFalse(grouped)

        snippet, _ = parser.parse_description(
            '<a href="#">Заголовок</a><font>СМИ</font>', "Заголовок", "СМИ"
        )
        self.assertEqual(snippet, parser.UNAVAILABLE_SNIPPET)

    @patch("google_news_parser.decode_google_news_url")
    @patch("google_news_parser._request", return_value=RSS)
    def test_collection_resolves_urls_and_deduplicates_exact_pages(
        self, _request, decode
    ):
        decode.side_effect = [
            "https://example.ru/first",
            "https://other.ru/second",
            "https://example.ru/first",
        ]
        rows = parser.collect_query("first story", limit=20)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.position for row in rows], [1, 2])
        self.assertEqual(rows[0].title, "First story")
        self.assertEqual(rows[0].snippet, "Exact Google snippet.")
        self.assertEqual(rows[0].domain, "example.ru")
        self.assertTrue(rows[0].url_resolved)
        self.assertEqual(rows[1].snippet, parser.UNAVAILABLE_SNIPPET)

    def test_decoder_parses_batchexecute_response(self):
        page = b'<article data-n-a-sg="signature" data-n-a-ts="123"></article>'
        payload = json.dumps(["garturlres", "https://example.ru/article", 1])
        response = (")]}'\n" + json.dumps([["wrb.fr", "Fbv4je", payload]])).encode()
        with patch("google_news_parser._request", side_effect=[page, response]):
            result = parser.decode_google_news_url(
                "https://news.google.com/rss/articles/article-id?oc=5"
            )
        self.assertEqual(result, "https://example.ru/article")

    def test_writes_all_output_formats(self):
        row = parser.NewsResult(
            query="query",
            position=1,
            title="Title",
            source="Source",
            url="https://example.ru/a",
            publication_date="2026-08-16T00:00:00+00:00",
            snippet=parser.UNAVAILABLE_SNIPPET,
            domain="example.ru",
            result_type="новость (Google News RSS)",
            relevance="высокая",
            grouped_story=False,
            google_url="https://news.google.com/a",
            url_resolved=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            parser.write_outputs([row], Path(directory), "2026-08-16T00:00:00+00:00")
            self.assertTrue((Path(directory) / "results.csv").exists())
            self.assertIn("Срез Google Новости", (Path(directory) / "report.md").read_text())
            data = json.loads((Path(directory) / "results.json").read_text())
            self.assertEqual(data["results"][0]["domain"], "example.ru")


if __name__ == "__main__":
    unittest.main()
