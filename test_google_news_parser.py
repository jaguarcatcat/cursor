import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from google_news_parser import (
    SNIPPET_UNAVAILABLE,
    build_analytics,
    exact_page_key,
    parse_batchexecute_url,
    parse_rss,
    relevance_comment,
    write_outputs,
)


RSS_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Вадим Лобов и университет &#171;Синергия&#187; - Test Media</title>
    <link>https://news.google.com/rss/articles/abc?oc=5</link>
    <pubDate>Sun, 16 Aug 2026 20:00:00 GMT</pubDate>
    <description>&lt;a href="https://news.google.com/rss/articles/abc"&gt;Вадим Лобов&lt;/a&gt;&amp;nbsp;&amp;nbsp;Test Media</description>
    <source url="https://www.example.ru">Test Media</source>
  </item>
  <item>
    <title>Другой материал - Other</title>
    <link>https://news.google.com/rss/articles/def?oc=5</link>
    <pubDate>Sat, 15 Aug 2026 10:30:00 GMT</pubDate>
    <description>&lt;a href="https://a"&gt;Один&lt;/a&gt;&lt;a href="https://b"&gt;Два&lt;/a&gt;</description>
    <source url="https://other.test">Other</source>
  </item>
</channel></rss>"""


class ParserTests(unittest.TestCase):
    def test_parse_rss_preserves_order_and_marks_missing_snippet(self):
        results = parse_rss(RSS_FIXTURE, "лобов вадим синергия")

        self.assertEqual([item.position for item in results], [1, 2])
        self.assertEqual(
            results[0].title, "Вадим Лобов и университет «Синергия»"
        )
        self.assertEqual(results[0].source, "Test Media")
        self.assertEqual(results[0].domain, "example.ru")
        self.assertEqual(results[0].snippet, SNIPPET_UNAVAILABLE)
        self.assertEqual(results[0].published_at, "2026-08-16T20:00:00+00:00")
        self.assertFalse(results[0].grouped_story)
        self.assertTrue(results[1].grouped_story)

    def test_parse_batchexecute_url(self):
        payload = json.dumps(["garturlres", "https://example.ru/article"])
        body = ")]}'\n" + json.dumps(
            [["wrb.fr", "Fbv4je", payload, None, None, None, "generic"]]
        )

        self.assertEqual(
            parse_batchexecute_url(body), "https://example.ru/article"
        )

    def test_exact_page_key_only_discards_fragment_and_host_case(self):
        self.assertEqual(
            exact_page_key("HTTPS://Example.RU/a?x=1#top"),
            "https://example.ru/a?x=1",
        )
        self.assertNotEqual(
            exact_page_key("https://example.ru/a?x=1"),
            exact_page_key("https://example.ru/a?x=2"),
        )

    def test_relevance_is_based_on_explicit_title_matches(self):
        self.assertTrue(
            relevance_comment(
                "лобов вадим синергия", "Вадим Лобов рассказал о «Синергии»"
            ).startswith("высокая")
        )
        self.assertTrue(
            relevance_comment("лобов вадим синергия", "Новости университета").startswith(
                "низкая"
            )
        )

    def test_writes_all_output_formats(self):
        results = parse_rss(RSS_FIXTURE, "лобов вадим")
        with tempfile.TemporaryDirectory() as directory:
            write_outputs(
                results,
                Path(directory),
                datetime(2026, 8, 16, 23, 0, tzinfo=timezone.utc),
            )
            paths = {path.name for path in Path(directory).iterdir()}
            self.assertEqual(
                paths,
                {
                    "google_news_results.csv",
                    "google_news_results.json",
                    "google_news_report.md",
                },
            )
            report = (Path(directory) / "google_news_report.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("## Краткая аналитика", report)
            self.assertIn(SNIPPET_UNAVAILABLE, report)
            self.assertEqual(len(build_analytics(results)), 6)


if __name__ == "__main__":
    unittest.main()
