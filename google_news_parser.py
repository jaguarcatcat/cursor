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

import xlsxwriter


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


def analytics_rows(results: list[NewsResult]) -> list[tuple[str, str]]:
    source_counts: dict[str, int] = {}
    domain_counts: dict[str, int] = {}
    theme_counts: dict[str, int] = {}
    tone_counts = {"негативная": 0, "нейтральная": 0, "позитивная": 0}
    for item in results:
        source = item.source or "Источник не указан"
        domain = item.domain or "Домен не указан"
        source_counts[source] = source_counts.get(source, 0) + 1
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        theme = classify_theme(item.title)
        theme_counts[theme] = theme_counts.get(theme, 0) + 1
        tone_counts[classify_tone(item.title)] += 1
    dominant_sources = ", ".join(f"{name} ({count})" for name, count in sorted(source_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:10])
    repeated_domains = ", ".join(f"{name} ({count})" for name, count in sorted(domain_counts.items()) if count > 1)
    themes = ", ".join(f"{name} ({count})" for name, count in sorted(theme_counts.items(), key=lambda pair: (-pair[1], pair[0])))
    top_results = "; ".join(
        f"«{item.title}» ({item.source}, позиция {item.position}, запрос: {item.query})"
        for item in results if item.position <= 2
    )
    return [
        ("Доминирующие СМИ", dominant_sources or "нет результатов"),
        ("Повторяющиеся домены", repeated_domains or "нет"),
        ("Темы/интенты (эвристика по заголовкам)", themes or "нет результатов"),
        ("Тональность (эвристика по явным словам)", ", ".join(f"{name} — {count}" for name, count in tone_counts.items())),
        ("Наиболее заметные публикации", top_results or "нет"),
        ("Источники информационного фона", dominant_sources or "нет результатов"),
    ]


def write_xlsx(results: list[NewsResult], path: Path, checked_at: datetime) -> None:
    """Write a formatted workbook suitable for filtering and review in Excel."""
    workbook = xlsxwriter.Workbook(path)
    title_format = workbook.add_format({"bold": True, "font_size": 14})
    note_format = workbook.add_format({"text_wrap": True, "valign": "top"})
    date_format = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm", "valign": "top"})
    header_format = workbook.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "#FFFFFF", "valign": "vcenter"})
    wrapped_format = workbook.add_format({"text_wrap": True, "valign": "top"})
    link_format = workbook.add_format({"font_color": "#0563C1", "underline": 1, "text_wrap": True, "valign": "top"})

    summary = workbook.add_worksheet("Сводка")
    summary.set_column("A:A", 37)
    summary.set_column("B:B", 110)
    summary.write("A1", "Срез Google Новости", title_format)
    summary.write("A3", "Проверено")
    summary.write_datetime("B3", checked_at.replace(tzinfo=None), date_format)
    summary.write("A4", "Параметры")
    summary.write("B4", "Google News RSS; hl=ru, gl=RU, ceid=RU:ru; максимум 20 результатов на запрос.", note_format)
    summary.write("A6", "Показатель", header_format)
    summary.write("B6", "Значение", header_format)
    for row, values in enumerate(analytics_rows(results), start=6):
        summary.write(row, 0, values[0], wrapped_format)
        summary.write(row, 1, values[1], note_format)
    summary.write("A14", "Ограничения источника", header_format)
    summary.write("B14", "RSS Google News не передаёт точный UI-сниппет, прямой URL СМИ или надёжный признак объединённого сюжета. Эти данные не подменяются догадками.", note_format)
    summary.set_row(13, 50)

    worksheet = workbook.add_worksheet("Результаты")
    headers = (
        "Поисковый запрос", "Позиция", "Заголовок новости", "СМИ / источник", "Дата публикации",
        "Сниппет", "URL публикации", "Домен", "Тип результата", "Комментарий о релевантности",
        "Новостной сюжет", "URL Google News",
    )
    worksheet.freeze_panes(1, 0)
    worksheet.autofilter(0, 0, len(results), len(headers) - 1)
    worksheet.set_column(0, 0, 29)
    worksheet.set_column(1, 1, 9)
    worksheet.set_column(2, 2, 52)
    worksheet.set_column(3, 3, 26)
    worksheet.set_column(4, 4, 22)
    worksheet.set_column(5, 5, 28)
    worksheet.set_column(6, 6, 55)
    worksheet.set_column(7, 7, 20)
    worksheet.set_column(8, 8, 28)
    worksheet.set_column(9, 10, 47)
    worksheet.set_column(11, 11, 55)
    for column, header in enumerate(headers):
        worksheet.write(0, column, header, header_format)
    for row, item in enumerate(results, start=1):
        values = (
            item.query, item.position, item.title, item.source, item.publication_date, item.snippet,
            item.publication_url, item.domain, item.result_type, item.relevance_comment,
            item.story_cluster, item.google_news_url,
        )
        for column, value in enumerate(values):
            if column in (6, 11) and value.startswith(("http://", "https://")):
                worksheet.write_url(row, column, value, link_format, value)
            else:
                worksheet.write(row, column, value, wrapped_format)
        worksheet.set_row(row, 55)
    workbook.close()


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
    analytics = dict(analytics_rows(results))
    report.extend([
        "",
        "## Краткая аналитика",
        "",
        f"- Доминирующие СМИ: {analytics['Доминирующие СМИ']}.",
        f"- Повторяющиеся домены: {analytics['Повторяющиеся домены']}. Если RSS не раскрыл URL издателя, здесь будет домен Google News, а не домен СМИ.",
        f"- Преобладающие темы/интенты: {analytics['Темы/интенты (эвристика по заголовкам)']}.",
        f"- Тональность заголовков: {analytics['Тональность (эвристика по явным словам)']}.",
        f"- Наиболее заметные публикации (верхние позиции каждого запроса): {analytics['Наиболее заметные публикации']}.",
        f"- Источники, формирующие информационный фон: {analytics['Источники информационного фона']}.",
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
    write_xlsx(results, args.output_dir / "google_news_results.xlsx", checked_at)
    print(f"Wrote {len(results)} rows to {args.output_dir}", file=sys.stderr)
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
