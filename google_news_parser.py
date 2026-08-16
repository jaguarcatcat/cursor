#!/usr/bin/env python3
"""Collect a reproducible Russian Google News search snapshot using Google News RSS.

The feed is Google News' own search endpoint, not a site search.  RSS does not
provide the text snippet shown in the Google News UI, therefore this tool
intentionally records the literal marker required for unavailable snippets.
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


DEFAULT_QUERIES = (
    "лобов вадим университет синергия",
    "лобов вадим синергия",
    "лобов вадим",
)
USER_AGENT = "Mozilla/5.0 (compatible; GoogleNewsSnapshot/1.0)"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
UNAVAILABLE_SNIPPET = "сниппет Google недоступен"


@dataclass(frozen=True)
class NewsResult:
    query: str
    position: int
    title: str
    source: str
    publication_date: str
    snippet: str
    publication_url: str
    domain: str
    result_type: str
    relevance_comment: str
    story_cluster: str
    google_news_url: str


def fetch(url: str, timeout: int) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def feed_url(query: str) -> str:
    return f"{GOOGLE_NEWS_RSS}?{urlencode({'q': query, 'hl': 'ru', 'gl': 'RU', 'ceid': 'RU:ru'})}"


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def source_from_description(description: str | None) -> str:
    """Extract only the publisher Google supplied in the RSS item description."""
    match = re.search(r"<font[^>]*>(.*?)</font>", description or "", flags=re.I | re.S)
    return clean_text(re.sub(r"<[^>]+>", "", match.group(1))) if match else ""


def publication_url_from_item(item: ET.Element) -> str:
    """Use a direct RSS URL only when Google supplies one.

    Current Google News RSS commonly exposes an opaque Google redirect rather
    than a publisher URL.  Returning that URL is more auditable than guessing
    a URL from a title, and the report labels it accordingly.
    """
    link = clean_text(item.findtext("link"))
    return link


def classify_relevance(query: str, title: str) -> str:
    terms = [term for term in re.findall(r"\w+", query.casefold()) if len(term) > 2]
    haystack = title.casefold()
    matched = sum(term in haystack for term in terms)
    if matched == len(terms):
        return "Высокая: все значимые слова запроса есть в заголовке."
    if matched >= max(1, len(terms) - 1):
        return "Средняя: большая часть значимых слов запроса есть в заголовке."
    return "Низкая: Google News показал результат, но заголовок слабо совпадает с запросом."


def classify_tone(title: str) -> str:
    negative = ("мошен", "уклонист", "халтур", "скандал", "обвин", "суд", "уголов", "проблем")
    positive = ("помогает", "помощь", "награ", "побед", "успех", "поддерж")
    lowered = title.casefold()
    if any(word in lowered for word in negative):
        return "негативная"
    if any(word in lowered for word in positive):
        return "позитивная"
    return "нейтральная"


def classify_theme(title: str) -> str:
    lowered = title.casefold()
    if any(word in lowered for word in ("мошен", "уклонист", "халтур", "скандал", "обвин", "суд")):
        return "репутационные риски / критика"
    if any(word in lowered for word in ("сво", "ветеран", "професси", "адаптац")):
        return "социальные и образовательные инициативы"
    if "подкаст" in lowered:
        return "медиа / персональный профиль"
    if "синерг" in lowered:
        return "университет и корпорация «Синергия»"
    return "прочее"


def parse_date(value: str | None) -> str:
    value = clean_text(value)
    try:
        return datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return value


def parse_feed(query: str, xml_data: bytes, limit: int) -> list[NewsResult]:
    root = ET.fromstring(xml_data)
    results: list[NewsResult] = []
    seen_urls: set[str] = set()

    for item in root.findall("./channel/item"):
        url = publication_url_from_item(item)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        source = source_from_description(item.findtext("description"))
        title = clean_text(item.findtext("title"))
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].rstrip()
        results.append(
            NewsResult(
                query=query,
                position=len(results) + 1,
                title=title,
                source=source,
                publication_date=parse_date(item.findtext("pubDate")),
                snippet=UNAVAILABLE_SNIPPET,
                publication_url=url,
                domain=urlparse(url).netloc.lower(),
                result_type="Новостная публикация (Google News RSS)",
                relevance_comment=classify_relevance(query, title),
                story_cluster="Не определено: Google News RSS не передаёт признак сюжета.",
                google_news_url=url,
            )
        )
        if len(results) == limit:
            break
    return results


def csv_rows(results: Iterable[NewsResult]) -> list[dict[str, object]]:
    return [asdict(result) for result in results]


def markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_csv(results: list[NewsResult], path: Path) -> None:
    rows = csv_rows(results)
    fields = list(NewsResult.__dataclass_fields__)
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(results: list[NewsResult], path: Path, checked_at: datetime) -> None:
    columns = ("Запрос", "Позиция", "Заголовок", "СМИ", "Дата", "Сниппет", "URL", "Домен")
    report = [
        "# Срез Google Новости",
        "",
        f"Проверено: {checked_at.isoformat()}",
        "Параметры: Google News RSS, `hl=ru`, `gl=RU`, `ceid=RU:ru`; максимум 20 результатов на запрос.",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for item in results:
        row = (
            item.query, item.position, item.title, item.source, item.publication_date,
            item.snippet, item.publication_url, item.domain,
        )
        report.append("| " + " | ".join(markdown_cell(value) for value in row) + " |")
    report.extend([
        "",
        "## Примечания к данным",
        "",
        "- RSS-выдача не передаёт отображаемый Google текстовый сниппет; поэтому для всех таких строк указан точный маркер «сниппет Google недоступен».",
        "- В поле URL находится URL, фактически возвращённый Google News. Если это `news.google.com/rss/articles/...`, Google не раскрыл прямой URL издателя в RSS; скрипт не подменяет его догадкой.",
        "- Признак объединённого сюжета в RSS отсутствует, поэтому кластер нельзя достоверно отметить.",
        "",
        "Полные поля (тип результата, релевантность, кластер, Google URL) находятся в сопутствующем CSV.",
    ])
    source_counts: dict[str, int] = {}
    domain_counts: dict[str, int] = {}
    theme_counts: dict[str, int] = {}
    tone_counts = {"негативная": 0, "нейтральная": 0, "позитивная": 0}
    for item in results:
        source_counts[item.source or "Источник не указан"] = source_counts.get(item.source or "Источник не указан", 0) + 1
        domain_counts[item.domain or "Домен не указан"] = domain_counts.get(item.domain or "Домен не указан", 0) + 1
        theme_counts[classify_theme(item.title)] = theme_counts.get(classify_theme(item.title), 0) + 1
        tone_counts[classify_tone(item.title)] += 1
    dominant_sources = ", ".join(f"{name} ({count})" for name, count in sorted(source_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:10])
    repeated_domains = ", ".join(f"{name} ({count})" for name, count in sorted(domain_counts.items()) if count > 1)
    themes = ", ".join(f"{name} ({count})" for name, count in sorted(theme_counts.items(), key=lambda pair: (-pair[1], pair[0])))
    top_results = "; ".join(f"«{item.title}» ({item.source}, позиция {item.position}, запрос: {item.query})" for item in results if item.position <= 2)
    report.extend([
        "",
        "## Краткая аналитика",
        "",
        f"- Доминирующие СМИ: {dominant_sources or 'нет результатов'}.",
        f"- Повторяющиеся домены: {repeated_domains or 'нет'}. Если RSS не раскрыл URL издателя, здесь будет домен Google News, а не домен СМИ.",
        f"- Преобладающие темы/интенты (эвристика по заголовкам): {themes or 'нет результатов'}.",
        f"- Тональность заголовков (эвристика по явным словам): негативная — {tone_counts['негативная']}, нейтральная — {tone_counts['нейтральная']}, позитивная — {tone_counts['позитивная']}.",
        f"- Наиболее заметные публикации (верхние позиции каждого запроса): {top_results or 'нет'}.",
        f"- Источники, формирующие информационный фон: {dominant_sources or 'нет результатов'}.",
    ])
    path.write_text("\n".join(report) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Google News RU/Russia search snapshot.")
    parser.add_argument("--query", action="append", dest="queries", help="Search query; repeat to add queries.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum results per query (default: 20).")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="Directory for CSV and Markdown.")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds.")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")

    queries = tuple(args.queries or DEFAULT_QUERIES)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    checked_at = datetime.now(UTC)
    results: list[NewsResult] = []
    for query in queries:
        try:
            query_results = parse_feed(query, fetch(feed_url(query), args.timeout), args.limit)
        except Exception as error:  # preserve successful query results in a partial snapshot
            print(f"ERROR: {query}: {error}", file=sys.stderr)
            continue
        results.extend(query_results)
        print(f"{query}: {len(query_results)} results", file=sys.stderr)
        time.sleep(0.5)

    write_csv(results, args.output_dir / "google_news_results.csv")
    write_markdown(results, args.output_dir / "google_news_report.md", checked_at)
    print(f"Wrote {len(results)} rows to {args.output_dir}", file=sys.stderr)
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
