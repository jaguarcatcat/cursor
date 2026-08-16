import unittest

from google_news_parser import (
    SNIPPET_UNAVAILABLE,
    article_id,
    canonical_url,
    domain_for,
    parse_search_page,
    relevance,
)


class ParserTests(unittest.TestCase):
    def test_parses_result_card(self):
        page = """
        <c-wiz class="PO9Zff Ccj79">
          <div class="vr1PYe">Тестовое СМИ</div>
          <a class="JtKRv" href="./read/ARTICLE?hl=ru">Заголовок новости</a>
          <time class="hvbAAd" datetime="2026-08-16T10:00:00Z">16 авг.</time>
        </c-wiz>
        """

        results = parse_search_page(page)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Заголовок новости")
        self.assertEqual(results[0].source, "Тестовое СМИ")
        self.assertEqual(results[0].published_at, "2026-08-16T10:00:00Z")
        self.assertEqual(results[0].published_display, "16 авг.")
        self.assertEqual(
            results[0].google_url,
            "https://news.google.com/read/ARTICLE?hl=ru",
        )

    def test_marks_explicit_story_cluster(self):
        page = """
        <c-wiz class="PO9Zff">
          <a aria-label="Полное освещение сюжета"></a>
          <a class="JtKRv" href="./read/ARTICLE">Заголовок</a>
        </c-wiz>
        """
        self.assertTrue(parse_search_page(page)[0].grouped_story)

    def test_extracts_article_id(self):
        self.assertEqual(
            article_id("https://news.google.com/rss/articles/ABC?hl=ru"), "ABC"
        )
        self.assertEqual(article_id("https://example.com/article"), None)

    def test_canonical_url_removes_only_tracking_and_fragment(self):
        self.assertEqual(
            canonical_url(
                "HTTPS://Example.com/story?id=7&utm_source=news#paragraph"
            ),
            "https://example.com/story?id=7",
        )

    def test_domain_and_relevance(self):
        self.assertEqual(domain_for("https://www.example.ru/story"), "example.ru")
        self.assertTrue(
            relevance("лобов вадим", "Вадим Лобов открыл центр").startswith(
                "Высокая"
            )
        )

    def test_required_unavailable_snippet_text(self):
        self.assertEqual(SNIPPET_UNAVAILABLE, "сниппет Google недоступен")


if __name__ == "__main__":
    unittest.main()
