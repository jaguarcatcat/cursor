"""
Unit tests for google_news_parser module.
"""

import os
import tempfile
import xml.etree.ElementTree as ET
import pytest

from google_news_parser import (
    clean_domain,
    clean_title,
    parse_date,
    determine_result_type,
    determine_sentiment,
    determine_relevance_comment,
    GoogleNewsParser,
    export_to_json,
    export_to_csv,
    generate_markdown_table,
    generate_analytics,
)


def test_clean_domain():
    assert clean_domain("https://www.forbes.ru/club/123") == "forbes.ru"
    assert clean_domain("http://sminews.ru/article/456") == "sminews.ru"
    assert clean_domain("https://m.repost.news/page") == "m.repost.news"
    assert clean_domain("", fallback="https://www.sostav.ru") == "sostav.ru"


def test_clean_title():
    assert clean_title("Новость дня - Forbes.ru", "Forbes.ru") == "Новость дня"
    assert clean_title("Заголовок статьи", "Sostav.ru") == "Заголовок статьи"
    assert clean_title("Заголовок - Другое СМИ", "Sostav.ru") == "Заголовок - Другое СМИ"


def test_parse_date():
    iso_d, fmt_d = parse_date("Sun, 12 Jul 2026 21:04:52 GMT")
    assert iso_d == "2026-07-12 21:04:52 UTC"
    assert fmt_d == "12.07.2026 21:04"

    # Fallback for invalid format
    raw = "Invalid date format"
    iso_d2, fmt_d2 = parse_date(raw)
    assert iso_d2 == raw
    assert fmt_d2 == raw


def test_determine_result_type():
    assert determine_result_type("Вадим Лобов - досье, биография", "https://parlament.ua/dossier/1") == "Досье / Биография"
    assert determine_result_type("Интервью с президентом «Синергии»", "https://pravo.ru/story/1") == "Интервью / Прямая речь"
    assert determine_result_type("Мошенничество под прикрытием ВУЗа", "https://repost.news/news/1") == "Расследование / Критический материал"
    assert determine_result_type("Подкаст о карьере", "https://sostav.ru/blogs/1") == "Авторская колонка / Блог"
    assert determine_result_type("Глава компании анонсировал новый проект", "https://ossetia.tv/1") == "Пресс-релиз / Корпоративная новость"


def test_determine_sentiment():
    assert determine_sentiment("Мошенничество в университете", "https://repost.news/1") == "Негативная"
    assert determine_sentiment("Университет помогает ветеранам СВО", "https://zarya.ru/1") == "Позитивная"
    assert determine_sentiment("Статистика рынка онлайн-образования", "https://tadviser.ru/1") == "Нейтральная"


def test_determine_relevance_comment():
    query = "лобов вадим университет синергия"
    c1 = determine_relevance_comment(query, "Вадим Лобов рассказал об университете Синергия", "forbes.ru")
    assert "Высокая прямая релевантность" in c1

    c2 = determine_relevance_comment(query, "Выставка Арт Россия на ВДНХ", "buro247.ru")
    assert "релевантность" in c2.lower()

    c3 = determine_relevance_comment(query, "Новости университета Синергия", "ria.ru")
    assert "Косвенная релевантность" in c3


def test_markdown_table_and_exports():
    sample_results = {
        "лобов вадим": [
            {
                "query": "лобов вадим",
                "position": 1,
                "title": "Интервью Вадима Лобова",
                "source": "Forbes.ru",
                "url": "https://forbes.ru/story/1",
                "google_news_url": "https://news.google.com/articles/1",
                "date_raw": "Sun, 12 Jul 2026 21:04:52 GMT",
                "date_iso": "2026-07-12 21:04:52 UTC",
                "date": "12.07.2026 21:04",
                "snippet": "сниппет Google недоступен",
                "domain": "forbes.ru",
                "result_type": "Интервью / Прямая речь",
                "sentiment": "Позитивная",
                "relevance_comment": "Высокая прямая релевантность",
                "story_cluster": "Одиночная публикация (не объединена в сюжет)",
            }
        ]
    }

    md = generate_markdown_table(sample_results)
    assert "| Запрос | Позиция | Заголовок |" in md
    assert "forbes.ru" in md

    with tempfile.TemporaryDirectory() as tmpdir:
        json_file = os.path.join(tmpdir, "test.json")
        csv_file = os.path.join(tmpdir, "test.csv")

        export_to_json(sample_results, json_file)
        assert os.path.exists(json_file)

        export_to_csv(sample_results, csv_file)
        assert os.path.exists(csv_file)

    analytics = generate_analytics(sample_results)
    assert analytics["total_items"] == 1
    assert analytics["unique_urls"] == 1
    assert analytics["sentiments"]["Позитивная"] == 1


def test_parser_with_mock_xml(monkeypatch):
    mock_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Google News</title>
        <item>
          <title>Test Title - TestSource</title>
          <link>https://news.google.com/articles/CAIiE12345</link>
          <pubDate>Mon, 29 Dec 2025 08:00:00 GMT</pubDate>
          <source url="https://testsource.ru">TestSource</source>
        </item>
      </channel>
    </rss>
    """

    class MockResponse:
        content = mock_xml
        status_code = 200
        def raise_for_status(self):
            pass

    parser = GoogleNewsParser()
    monkeypatch.setattr(parser.session, "get", lambda *args, **kwargs: MockResponse())

    results = parser.parse_query("test query", limit=10)
    assert len(results) == 1
    assert results[0]["title"] == "Test Title"
    assert results[0]["source"] == "TestSource"
    assert results[0]["date"] == "29.12.2025 08:00"
    assert results[0]["snippet"] == "сниппет Google недоступен"

