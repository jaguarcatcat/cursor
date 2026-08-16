#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import google_news_parser as gnp

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class CanonicalUrlTests(unittest.TestCase):
    def test_same_page_with_tracking_and_www(self) -> None:
        left = "http://www.example-news.ru/story/other/?utm_source=google#frag"
        right = "https://example-news.ru/story/other"
        self.assertEqual(gnp.canonical_url(left), gnp.canonical_url(right))

    def test_different_publications_stay_different(self) -> None:
        left = "https://example-news.ru/story/main"
        right = "https://example-news.ru/story/other"
        self.assertNotEqual(gnp.canonical_url(left), gnp.canonical_url(right))


class SnippetTests(unittest.TestCase):
    def test_title_plus_source_is_not_a_snippet(self) -> None:
        snippet = gnp.snippet_or_unavailable(
            '<a>Заголовок</a>&nbsp;&nbsp;<font>Sostav.ru</font>',
            "Заголовок",
            "Sostav.ru",
        )
        self.assertEqual(snippet, gnp.SNIPPET_UNAVAILABLE)

    def test_real_snippet_is_kept(self) -> None:
        text = "Это достаточно длинный сниппет Google, который не совпадает с заголовком и должен сохраниться."
        self.assertEqual(gnp.snippet_or_unavailable(text, "Другой заголовок", "Example"), text)


class HtmlParserTests(unittest.TestCase):
    def setUp(self) -> None:
        html = (FIXTURES / "sample_search.html").read_text(encoding="utf-8")
        self.results = gnp.parse_html_results(html, "fallback-query", "2026-08-16T00:00:00+03:00")

    def test_reads_query_from_payload(self) -> None:
        self.assertTrue(self.results)
        self.assertEqual(self.results[0].query, "лобов вадим университет синергия")

    def test_cluster_is_marked_without_merging_urls(self) -> None:
        cluster = [item for item in self.results if item.clustered]
        self.assertGreaterEqual(len(cluster), 2)
        urls = {item.url for item in cluster}
        self.assertIn("https://www.example-news.ru/story/main?utm_source=google", urls)
        self.assertIn("https://other-media.ru/cluster-story", urls)
        related = [item for item in cluster if "связанная" in str(item.position)]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0].snippet.startswith("Короткий сниппет Google"), True)

    def test_missing_snippet_is_explicit(self) -> None:
        main = next(item for item in self.results if item.google_article_id == "IDMAIN")
        self.assertEqual(main.snippet, gnp.SNIPPET_UNAVAILABLE)

    def test_same_media_keeps_different_urls(self) -> None:
        example_urls = [
            item.url
            for item in self.results
            if item.domain == "example-news.ru" and "связанная" not in str(item.position)
        ]
        self.assertGreaterEqual(len(example_urls), 2)

    def test_dedup_and_limit(self) -> None:
        unique = gnp.dedupe_exact_pages(self.results)
        limited = gnp.limit_serp_positions(unique, limit=2)
        main_positions = [item for item in limited if "связанная" not in str(item.position)]
        self.assertEqual(len(main_positions), 2)
        urls = [gnp.canonical_url(item.url) for item in unique]
        self.assertEqual(len(urls), len(set(urls)))
        dupes = [item for item in unique if item.google_article_id == "IDDUP"]
        self.assertEqual(dupes, [])


class RelevanceTests(unittest.TestCase):
    def test_high_when_person_and_synergy_present(self) -> None:
        comment = gnp.relevance_comment(
            "лобов вадим синергия",
            "Вадим Лобов и корпорация Синергия запустили выставку",
            "SMINEWS",
            "https://sminews.ru/2891",
            "sminews.ru",
        )
        self.assertIn("Высокая релевантность", comment)

    def test_low_when_title_has_no_query_tokens(self) -> None:
        comment = gnp.relevance_comment(
            "лобов вадим университет синергия",
            "Патриарху представили проект экотроп на Валааме",
            "Журнал ФОМА",
            "https://foma.example/valaam",
            "foma.example",
        )
        self.assertIn("Низкая релевантность", comment)


class RssParserTests(unittest.TestCase):
    def test_rss_does_not_invent_google_snippet(self) -> None:
        xml_text = (FIXTURES / "sample_rss.xml").read_text(encoding="utf-8")
        results = gnp.parse_rss(xml_text, "лобов вадим", "2026-08-16T00:00:00+03:00")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].source, "Вести Подмосковья")
        self.assertEqual(results[0].snippet, gnp.SNIPPET_UNAVAILABLE)
        self.assertIn("достаточно длинный сниппет Google", results[1].snippet)


class ReportTests(unittest.TestCase):
    def test_table_and_analytics_include_required_columns(self) -> None:
        html = (FIXTURES / "sample_search.html").read_text(encoding="utf-8")
        results = gnp.dedupe_exact_pages(
            gnp.parse_html_results(html, "q", "2026-08-16T00:00:00+03:00")
        )
        table = gnp.markdown_table(results)
        for header in ["Запрос", "Позиция", "Заголовок", "СМИ", "Дата", "Сниппет", "URL", "Домен"]:
            self.assertIn(header, table)
        analytics = gnp.build_analytics(results)
        self.assertIn("Example News", analytics)
        self.assertIn("сюжет", analytics.lower())


if __name__ == "__main__":
    unittest.main()
