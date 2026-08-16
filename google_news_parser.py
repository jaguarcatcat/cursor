#!/usr/bin/env python3
"""
Парсер выдачи Google News (Google Новости) для заданных поисковых запросов.
Регион: Россия, язык: русский.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import quote_plus, urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_QUERIES = [
    "лобов вадим университет синергия",
    "лобов вадим синергия",
    "лобов вадим",
]

NEGATIVE_KEYWORDS = [
    "мошенник", "шарашкин", "блок", "долг", "заблокировал", "негатив",
    "разводят", "прокуратура", "колонии", "обман", "жалоб",
]

POSITIVE_KEYWORDS = [
    "запустил", "создани", "достижен", "успех", "развити", "поддержк",
    "открыти", "запуск", "приобрел", "договорил", "организуют",
]


@dataclass
class NewsResult:
    query: str
    position: int
    title: str
    source: str
    url: str
    publication_date: str
    snippet: str
    domain: str
    result_type: str
    relevance_comment: str
    is_cluster: bool = False
    cluster_sources: list[str] = field(default_factory=list)


def build_search_url(query: str) -> str:
    encoded = quote_plus(query)
    return (
        f"https://news.google.com/search"
        f"?q={encoded}&hl=ru&gl=RU&ceid=RU:ru"
    )


def fetch_page(url: str, session: requests.Session) -> str:
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def extract_init_data(html: str) -> list[Any] | None:
    pattern = (
        r"AF_initDataCallback\(\{key: 'ds:2', hash: '[^']+', "
        r"data:(.*?), sideChannel: \{\}\}\);"
    )
    match = re.search(pattern, html, re.DOTALL)
    if not match:
        return None
    return json.loads(match.group(1))


def extract_domain(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    domain = parsed.netloc or ""
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def format_timestamp(ts_list: list | None) -> str:
    if not ts_list or not isinstance(ts_list, list):
        return "дата недоступна"
    ts = ts_list[0]
    if not isinstance(ts, (int, float)):
        return "дата недоступна"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    except (OSError, ValueError, OverflowError):
        return "дата недоступна"


def detect_cluster(token: str) -> tuple[bool, int]:
    """Определяет, объединил ли Google несколько публикаций в один сюжет."""
    if not token:
        return False, 0
    parts = re.findall(r"CBM[a-zA-Z0-9_-]+", token)
    if len(parts) > 1:
        return True, len(parts)
    if len(token) > 200:
        return True, 2
    return False, 0


def extract_snippet(article_data: list) -> str:
    """Извлекает сниппет из данных статьи, если он есть."""
    title = article_data[2] if len(article_data) > 2 else ""

    for idx in range(len(article_data)):
        val = article_data[idx]
        if (
            isinstance(val, str)
            and val != title
            and len(val) > 30
            and not val.startswith("http")
            and not val.startswith("CBM")
            and not val.startswith("Publisher")
            and "Перейти на страницу" not in val
        ):
            return unescape(val)

    return "сниппет Google недоступен"


def determine_result_type(title: str, source: str, is_cluster: bool) -> str:
    if is_cluster:
        return "новостной сюжет (кластер)"
    title_lower = title.lower()
    if any(w in title_lower for w in ["интервью", "рассказал", "сообщил", "объявил"]):
        return "новостная статья / интервью"
    if any(w in title_lower for w in ["открыти", "ярмарка", "выставка", "бал"]):
        return "событийная публикация"
    if any(w in title_lower for w in ["досье", "биография"]):
        return "справочный материал"
    if any(w in title_lower for w in ["мошенник", "блок", "долг", "прокуратура"]):
        return "критический материал"
    if "blog" in source.lower() or "sostav" in source.lower():
        return "блог / авторская публикация"
    return "новостная статья"


def assess_relevance(query: str, title: str, source: str) -> str:
    query_lower = query.lower()
    title_lower = title.lower()
    query_words = set(query_lower.split())

    matched = [w for w in query_words if w in title_lower]
    match_ratio = len(matched) / max(len(query_words), 1)

    has_lobov = "лобов" in title_lower
    has_vadim = "вадим" in title_lower
    has_sinergia = "синерг" in title_lower
    has_university = any(w in title_lower for w in ["университет", "вуз", "образован"])

    if "университет" in query_lower and "синерг" in query_lower:
        if has_lobov and has_sinergia and has_university:
            return "Высокая релевантность: заголовок содержит все ключевые слова запроса"
        if has_lobov and has_sinergia:
            return "Средняя релевантность: упоминаются Лобов и Синергия, но не университет"
        if has_lobov or (has_vadim and has_sinergia):
            return "Частичная релевантность: совпадение по части запроса"
        return "Низкая релевантность: совпадение с запросом минимальное"

    if "синерг" in query_lower:
        if has_lobov and has_sinergia:
            return "Высокая релевантность: заголовок содержит Лобов и Синергия"
        if has_sinergia and not has_lobov:
            return "Средняя релевантность: упоминается Синергия, но не Лобов"
        if has_lobov and not has_sinergia:
            return "Частичная релевантность: упоминается Лобов, но не Синергия"
        return "Низкая релевантность: ключевые слова запроса не найдены в заголовке"

    if has_lobov or has_vadim:
        if match_ratio >= 0.5:
            return "Высокая релевантность: заголовок соответствует запросу"
        return "Средняя релевантность: упоминается Лобов/Вадим"
    return "Низкая релевантность: возможно, другой человек с фамилией Лобов"


def assess_sentiment(title: str) -> str:
    title_lower = title.lower()
    neg = sum(1 for w in NEGATIVE_KEYWORDS if w in title_lower)
    pos = sum(1 for w in POSITIVE_KEYWORDS if w in title_lower)
    if neg > pos:
        return "негативная"
    if pos > neg:
        return "позитивная"
    return "нейтральная"


def parse_article(
    article_wrapper: list,
    query: str,
    position: int,
) -> NewsResult | None:
    if not article_wrapper or not isinstance(article_wrapper, list):
        return None

    article_data = article_wrapper[0]
    if not isinstance(article_data, list) or len(article_data) < 7:
        return None

    title = article_data[2] if isinstance(article_data[2], str) else ""
    if not title:
        return None

    title = unescape(title)
    url = article_data[6] if isinstance(article_data[6], str) else ""
    if not url:
        url = article_data[38] if len(article_data) > 38 and isinstance(article_data[38], str) else ""

    source = ""
    if len(article_data) > 10 and isinstance(article_data[10], list) and len(article_data[10]) > 2:
        source = article_data[10][2] if isinstance(article_data[10][2], str) else ""

    pub_date = format_timestamp(article_data[4] if len(article_data) > 4 else None)
    snippet = extract_snippet(article_data)
    domain = extract_domain(url)

    token = ""
    if len(article_data) > 1 and isinstance(article_data[1], list) and len(article_data[1]) > 1:
        token = article_data[1][1] if isinstance(article_data[1][1], str) else ""

    is_cluster, cluster_count = detect_cluster(token)
    result_type = determine_result_type(title, source, is_cluster)
    relevance = assess_relevance(query, title, source)

    cluster_sources = []
    if is_cluster:
        cluster_sources = [f"кластер из ~{cluster_count} публикаций"]

    return NewsResult(
        query=query,
        position=position,
        title=title,
        source=source,
        url=url,
        publication_date=pub_date,
        snippet=snippet,
        domain=domain,
        result_type=result_type,
        relevance_comment=relevance,
        is_cluster=is_cluster,
        cluster_sources=cluster_sources,
    )


def parse_google_news(
    query: str,
    session: requests.Session,
    max_results: int = 20,
) -> list[NewsResult]:
    url = build_search_url(query)
    html = fetch_page(url, session)
    data = extract_init_data(html)

    if not data or len(data) < 2:
        print(f"  [!] Не удалось извлечь данные для запроса: {query}", file=sys.stderr)
        return []

    articles_raw = data[1]
    if not articles_raw or not isinstance(articles_raw, list):
        return []

    articles_list = articles_raw[0] if isinstance(articles_raw[0], list) else articles_raw

    results: list[NewsResult] = []
    seen_urls: set[str] = set()
    position = 0

    for article_wrapper in articles_list:
        if len(results) >= max_results:
            break

        position += 1
        result = parse_article(article_wrapper, query, position)
        if not result or not result.url:
            continue

        if result.url in seen_urls:
            continue
        seen_urls.add(result.url)

        results.append(result)

    return results


def generate_analytics(all_results: list[NewsResult]) -> dict[str, Any]:
    if not all_results:
        return {"error": "Нет результатов для анализа"}

    source_counter = Counter(r.source for r in all_results if r.source)
    domain_counter = Counter(r.domain for r in all_results if r.domain)

    themes: Counter[str] = Counter()
    for r in all_results:
        tl = r.title.lower()
        if "синерг" in tl or "университет" in tl:
            themes["Образование / Синергия"] += 1
        if "арт россия" in tl or "ярмарк" in tl or "выставк" in tl:
            themes["Искусство / Арт Россия"] += 1
        if any(w in tl for w in NEGATIVE_KEYWORDS):
            themes["Критика / негатив"] += 1
        if "образован" in tl or "edtech" in tl or "колледж" in tl:
            themes["EdTech / образование"] += 1
        if "бизнес" in tl or "предпринимател" in tl:
            themes["Бизнес / предпринимательство"] += 1

    sentiments = Counter(assess_sentiment(r.title) for r in all_results)

    top_by_position = sorted(all_results, key=lambda r: (r.query, r.position))[:5]

    info_background_sources = [
        r.source for r in all_results
        if any(w in r.title.lower() for w in ["мошенник", "шарашкин", "блок", "отзыв", "разводят"])
    ]

    repeated_domains = {d: c for d, c in domain_counter.items() if c > 1}

    return {
        "dominant_sources": source_counter.most_common(10),
        "repeated_domains": repeated_domains,
        "themes": dict(themes.most_common()),
        "sentiment_distribution": dict(sentiments),
        "most_visible": [
            {"position": r.position, "query": r.query, "title": r.title, "source": r.source}
            for r in top_by_position
        ],
        "info_background_sources": list(set(info_background_sources)),
        "total_results": len(all_results),
        "unique_domains": len(domain_counter),
        "unique_sources": len(source_counter),
        "clusters_count": sum(1 for r in all_results if r.is_cluster),
    }


def format_markdown_table(results: list[NewsResult]) -> str:
    lines = [
        "| Запрос | Позиция | Заголовок | СМИ | Дата | Сниппет | URL | Домен |",
        "|--------|---------|-----------|-----|------|---------|-----|-------|",
    ]

    for r in results:
        title = r.title.replace("|", "\\|")[:80]
        snippet = r.snippet.replace("|", "\\|")[:60]
        url_short = r.url[:60] + ("..." if len(r.url) > 60 else "")
        cluster_mark = " [КЛАСТЕР]" if r.is_cluster else ""
        lines.append(
            f"| {r.query[:30]} | {r.position} | {title}{cluster_mark} | {r.source} "
            f"| {r.publication_date} | {snippet} | {url_short} | {r.domain} |"
        )

    return "\n".join(lines)


def format_analytics_md(analytics: dict[str, Any]) -> str:
    lines = ["## Аналитика\n"]

    lines.append("### Доминирующие СМИ")
    for source, count in analytics.get("dominant_sources", [])[:7]:
        lines.append(f"- **{source}** — {count} публикаций")
    lines.append("")

    repeated = analytics.get("repeated_domains", {})
    lines.append("### Домены, встречающиеся несколько раз")
    if repeated:
        for domain, count in sorted(repeated.items(), key=lambda x: -x[1]):
            lines.append(f"- {domain} — {count} раз(а)")
    else:
        lines.append("- Повторяющихся доменов не обнаружено")
    lines.append("")

    lines.append("### Преобладающие темы / интенты")
    for theme, count in analytics.get("themes", {}).items():
        lines.append(f"- {theme}: {count}")
    lines.append("")

    sentiments = analytics.get("sentiment_distribution", {})
    lines.append("### Тональность публикаций")
    for sentiment, count in sentiments.items():
        lines.append(f"- {sentiment.capitalize()}: {count}")
    lines.append("")

    lines.append("### Наиболее заметные публикации (топ позиций)")
    for item in analytics.get("most_visible", []):
        lines.append(
            f"- Поз. {item['position']} [{item['query'][:25]}]: "
            f"{item['title'][:70]}... ({item['source']})"
        )
    lines.append("")

    bg = analytics.get("info_background_sources", [])
    lines.append("### Источники, формирующие информационный фон")
    if bg:
        for s in bg:
            lines.append(f"- {s}")
    else:
        lines.append("- Явных «фоновых» критических источников не выделено")

    clusters = analytics.get("clusters_count", 0)
    lines.append(f"\n### Кластеры Google News: {clusters} сюжетов объединены из нескольких публикаций")

    return "\n".join(lines)


def run_parser(
    queries: list[str] | None = None,
    max_results: int = 20,
    output_json: str | None = None,
    output_md: str | None = None,
) -> list[NewsResult]:
    queries = queries or DEFAULT_QUERIES
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"})

    all_results: list[NewsResult] = []
    check_date = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    print(f"Дата проверки: {check_date}")
    print(f"Регион: Россия (RU), язык: русский\n")

    for query in queries:
        print(f"Парсинг запроса: «{query}»...")
        results = parse_google_news(query, session, max_results=max_results)
        print(f"  Получено результатов: {len(results)}")
        all_results.extend(results)
        time.sleep(1.5)

    analytics = generate_analytics(all_results)

    table = format_markdown_table(all_results)
    analytics_md = format_analytics_md(analytics)

    print("\n" + "=" * 80)
    print("СВОДНАЯ ТАБЛИЦА")
    print("=" * 80)
    print(table)
    print("\n" + analytics_md)

    if output_json:
        output_data = {
            "check_date": check_date,
            "region": "RU",
            "language": "ru",
            "queries": queries,
            "results": [asdict(r) for r in all_results],
            "analytics": analytics,
        }
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"\nJSON сохранён: {output_json}")

    if output_md:
        md_content = (
            f"# Google News — результаты парсинга\n\n"
            f"**Дата проверки:** {check_date}\n"
            f"**Регион:** Россия, **Язык:** русский\n\n"
            f"{table}\n\n{analytics_md}"
        )
        with open(output_md, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"Markdown сохранён: {output_md}")

    return all_results


def main():
    parser = argparse.ArgumentParser(description="Парсер Google News")
    parser.add_argument(
        "--queries", nargs="+", default=DEFAULT_QUERIES,
        help="Поисковые запросы",
    )
    parser.add_argument(
        "--max-results", type=int, default=20,
        help="Максимум результатов на запрос (по умолчанию 20)",
    )
    parser.add_argument("--output-json", default="results.json", help="Путь к JSON-файлу")
    parser.add_argument("--output-md", default="results.md", help="Путь к Markdown-файлу")
    args = parser.parse_args()

    run_parser(
        queries=args.queries,
        max_results=args.max_results,
        output_json=args.output_json,
        output_md=args.output_md,
    )


if __name__ == "__main__":
    main()
