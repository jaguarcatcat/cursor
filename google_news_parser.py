#!/usr/bin/env python3
"""Collect a reproducible, localized Google News search snapshot via RSS."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

DEFAULT_QUERIES = (
    "лобов вадим университет синергия",
    "лобов вадим синергия",
    "лобов вадим",
)
GOOGLE_NEWS = "https://news.google.com"
UNAVAILABLE_SNIPPET = "сниппет Google недоступен"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TRACKING_QUERY_KEYS = frozenset(
    {"oc", "hl", "gl", "ceid", "utm_source", "utm_medium", "utm_campaign"}
)


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
    relevance: str
    grouped_story: bool
    google_url: str
    url_resolved: bool


class _DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.anchor_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.anchor_count += 1

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


def _request(url: str, *, data: bytes | None = None, timeout: float = 20) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
    if data is not None:
        headers.update(
            {
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Referer": f"{GOOGLE_NEWS}/",
            }
        )
    request = urllib.request.Request(url, data=data, headers=headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
    assert last_error is not None
    raise last_error


def build_feed_url(query: str) -> str:
    params = urllib.parse.urlencode(
        {"q": query, "hl": "ru", "gl": "RU", "ceid": "RU:ru"}
    )
    return f"{GOOGLE_NEWS}/rss/search?{params}"


def decode_google_news_url(url: str, timeout: float = 20) -> str | None:
    """Resolve Google's modern article wrapper using its Fbv4je endpoint."""
    path_parts = urllib.parse.urlsplit(url).path.rstrip("/").split("/")
    if "news.google.com" not in urllib.parse.urlsplit(url).netloc or not path_parts:
        return url
    article_id = path_parts[-1]
    if not article_id:
        return None

    page_url = f"{GOOGLE_NEWS}/articles/{article_id}"
    page = _request(page_url, timeout=timeout).decode("utf-8", errors="replace")
    signature = re.search(r'data-n-a-sg="([^"]+)"', page)
    timestamp = re.search(r'data-n-a-ts="(\d+)"', page)
    if not signature or not timestamp:
        return None

    inner = json.dumps(
        [
            "garturlreq",
            [
                [
                    "ru",
                    "RU",
                    ["FINANCE_TOP_INDICES", "WEB_TEST_1_0_0"],
                    None,
                    None,
                    1,
                    1,
                    "RU:ru",
                    None,
                    180,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    1,
                ],
                "ru",
                "RU",
                1,
                [2, 3, 4, 8],
                1,
                0,
                "655000234",
                0,
                0,
                None,
                0,
            ],
            article_id,
            int(timestamp.group(1)),
            signature.group(1),
        ],
        separators=(",", ":"),
    )
    envelope = json.dumps([[["Fbv4je", inner, None, "generic"]]], separators=(",", ":"))
    response = _request(
        f"{GOOGLE_NEWS}/_/DotsSplashUi/data/batchexecute",
        data=urllib.parse.urlencode({"f.req": envelope}).encode("ascii"),
        timeout=timeout,
    ).decode("utf-8", errors="replace")
    if response.startswith(")]}'"):
        response = response.split("\n", 1)[1]
    response = response.lstrip()
    first_line, separator, remainder = response.partition("\n")
    if separator and first_line.strip().isdigit():
        response = remainder
    for item in json.loads(response):
        if isinstance(item, list) and len(item) >= 3 and item[:2] == ["wrb.fr", "Fbv4je"]:
            payload = json.loads(item[2])
            if (
                isinstance(payload, list)
                and len(payload) >= 2
                and payload[0] == "garturlres"
            ):
                return payload[1]
    return None


def parse_description(description: str, title: str, source: str) -> tuple[str, bool]:
    parser = _DescriptionParser()
    parser.feed(html.unescape(description or ""))
    parts = parser.parts[:]
    for expected in (title, source):
        if parts and _normalized(parts[0]) == _normalized(expected):
            parts.pop(0)
    candidate = " ".join(parts).strip()
    if not candidate or _normalized(candidate) in {
        _normalized(title),
        _normalized(f"{title} {source}"),
    }:
        candidate = UNAVAILABLE_SNIPPET
    return candidate, parser.anchor_count > 1


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-zа-яё0-9]+", value.casefold()))


def relevance_comment(query: str, title: str, snippet: str) -> str:
    terms = set(_normalized(query).split())
    haystack = set(_normalized(f"{title} {snippet}").split())
    matched = terms & haystack
    if terms and matched == terms:
        return "высокая: присутствуют все слова запроса"
    if len(matched) >= max(1, len(terms) // 2):
        missing = ", ".join(sorted(terms - matched))
        return f"средняя: отсутствуют слова: {missing}"
    return "низкая: совпадает только часть слов запроса"


def _publication_date(raw_date: str) -> str:
    try:
        return parsedate_to_datetime(raw_date).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return raw_date or ""


def _domain(url: str, source_url: str) -> str:
    hostname = urllib.parse.urlsplit(url).hostname
    if not hostname or hostname == "news.google.com":
        hostname = urllib.parse.urlsplit(source_url).hostname
    return (hostname or "").removeprefix("www.").casefold()


def collect_query(
    query: str,
    *,
    limit: int = 20,
    resolve_urls: bool = True,
    decode_cache: dict[str, str | None] | None = None,
) -> list[NewsResult]:
    root = ET.fromstring(_request(build_feed_url(query)))
    cache = decode_cache if decode_cache is not None else {}
    results: list[NewsResult] = []
    seen_urls: set[str] = set()

    for item in root.findall("./channel/item"):
        google_url = item.findtext("link", "").strip()
        source_node = item.find("source")
        source = (source_node.text or "").strip() if source_node is not None else ""
        source_url = source_node.get("url", "") if source_node is not None else ""
        title = item.findtext("title", "").strip()
        suffix = f" - {source}"
        if source and title.endswith(suffix):
            title = title[: -len(suffix)].rstrip()

        resolved_url = None
        if resolve_urls:
            if google_url not in cache:
                try:
                    cache[google_url] = decode_google_news_url(google_url)
                except (OSError, ValueError, json.JSONDecodeError):
                    cache[google_url] = None
            resolved_url = cache[google_url]
        url = resolved_url or google_url
        if url in seen_urls:
            continue
        seen_urls.add(url)
        snippet, grouped = parse_description(
            item.findtext("description", ""), title, source
        )
        results.append(
            NewsResult(
                query=query,
                position=len(results) + 1,
                title=title,
                source=source,
                url=url,
                publication_date=_publication_date(item.findtext("pubDate", "")),
                snippet=snippet,
                domain=_domain(url, source_url),
                result_type="новость (Google News RSS)",
                relevance=relevance_comment(query, title, snippet),
                grouped_story=grouped,
                google_url=google_url,
                url_resolved=resolved_url is not None,
            )
        )
        if len(results) >= limit:
            break
    return results


def collect(queries: Iterable[str], limit: int = 20, resolve_urls: bool = True) -> list[NewsResult]:
    cache: dict[str, str | None] = {}
    rows: list[NewsResult] = []
    for query in queries:
        rows.extend(
            collect_query(
                query,
                limit=limit,
                resolve_urls=resolve_urls,
                decode_cache=cache,
            )
        )
    return rows


def sentiment(title: str, snippet: str) -> str:
    text = _normalized(f"{title} {snippet}")
    negative = ("мошен", "скандал", "обман", "проблем", "критик", "шарашк", "негатив")
    positive = ("помога", "поддерж", "развит", "успех", "откры", "запуст", "награ")
    negative_score = sum(token in text for token in negative)
    positive_score = sum(token in text for token in positive)
    if negative_score > positive_score:
        return "негативная"
    if positive_score > negative_score:
        return "позитивная"
    return "нейтральная"


def analytics(rows: list[NewsResult]) -> dict[str, object]:
    domains = Counter(row.domain for row in rows if row.domain)
    sources = Counter(row.source for row in rows if row.source)
    tones = Counter(sentiment(row.title, row.snippet) for row in rows)
    visibility: dict[str, list[NewsResult]] = {}
    for row in rows:
        visibility.setdefault(row.url, []).append(row)
    themes = {
        "образование/университет": ("университет", "образован", "обучен", "студент"),
        "бизнес/управление": ("президент", "корпорац", "бизнес", "предприним"),
        "культура/искусство": ("выстав", "искусств", "арт ", "музе"),
        "миграция/кадры": ("миграц", "кадр", "труд"),
    }
    theme_counts: Counter[str] = Counter()
    for row in rows:
        text = _normalized(row.title)
        matched = False
        for theme, tokens in themes.items():
            if any(token in text for token in tokens):
                theme_counts[theme] += 1
                matched = True
        if not matched:
            theme_counts["прочее"] += 1
    notable = sorted(
        visibility.values(),
        key=lambda matches: (-len(matches), min(row.position for row in matches)),
    )[:5]
    return {
        "dominant_sources": sources.most_common(10),
        "repeated_domains": [(name, count) for name, count in domains.most_common() if count > 1],
        "themes": theme_counts.most_common(),
        "sentiment": tones.most_common(),
        "information_background_sources": domains.most_common(10),
        "notable_publications": [
            {
                "title": matches[0].title,
                "source": matches[0].source,
                "appearances": len(matches),
                "best_position": min(row.position for row in matches),
            }
            for matches in notable
        ],
    }


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_outputs(rows: list[NewsResult], output_dir: Path, checked_at: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    fields = list(NewsResult.__dataclass_fields__)
    with (output_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
    payload = {
        "checked_at": checked_at,
        "locale": {"hl": "ru", "gl": "RU", "ceid": "RU:ru"},
        "results": [asdict(row) for row in rows],
        "analytics": analytics(rows),
    }
    (output_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# Срез Google Новости",
        "",
        f"Дата проверки: {checked_at}. Регион: Россия. Язык: русский.",
        "",
        "| Запрос | Позиция | Заголовок | СМИ | Дата | Сниппет | URL | Домен |",
        "|---|---:|---|---|---|---|---|---|",
    ]
    for row in rows:
        cells = (
            row.query,
            row.position,
            row.title,
            row.source,
            row.publication_date,
            row.snippet,
            row.url,
            row.domain,
        )
        lines.append("| " + " | ".join(_md(cell) for cell in cells) + " |")
    summary = analytics(rows)
    lines.extend(
        [
            "",
            "## Краткая аналитика",
            "",
            "- Доминирующие СМИ: "
            + ", ".join(f"{name} ({count})" for name, count in summary["dominant_sources"]),
            "- Повторяющиеся домены: "
            + (
                ", ".join(f"{name} ({count})" for name, count in summary["repeated_domains"])
                or "нет"
            ),
            "- Темы/интенты: "
            + ", ".join(f"{name} ({count})" for name, count in summary["themes"]),
            "- Тональность заголовков и доступных сниппетов: "
            + ", ".join(f"{name} ({count})" for name, count in summary["sentiment"]),
            "- Наиболее заметны публикации на первых позициях: "
            + "; ".join(
                f"«{item['title']}» — {item['source']} "
                f"(встречается в {item['appearances']} запросах, "
                f"лучшая позиция {item['best_position']})"
                for item in summary["notable_publications"]
            ),
            "- Информационный фон потенциально формируют: "
            + ", ".join(
                f"{name} ({count})"
                for name, count in summary["information_background_sources"]
            ),
            "",
            "Примечание: тональность и темы определены простыми словарными правилами. "
            "Поле grouped_story=true в CSV/JSON означает, что Google объединил "
            "несколько ссылок в описании одного результата.",
        ]
    )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-q", "--query", action="append", dest="queries")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--no-resolve", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit должен быть положительным")
    checked_at = datetime.now(timezone.utc).isoformat()
    rows = collect(args.queries or DEFAULT_QUERIES, args.limit, not args.no_resolve)
    write_outputs(rows, args.output_dir, checked_at)
    counts = Counter(row.query for row in rows)
    print(f"Собрано {len(rows)} результатов: {dict(counts)}")
    print(f"Отчёт: {args.output_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
