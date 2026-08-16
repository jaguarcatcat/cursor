#!/usr/bin/env python3
"""Collect a reproducible, RU-localized Google News search snapshot."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_QUERIES = [
    "лобов вадим университет синергия",
    "лобов вадим синергия",
    "лобов вадим",
]
GOOGLE_NEWS = "https://news.google.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
SNIPPET_UNAVAILABLE = "сниппет Google недоступен"


@dataclass
class NewsResult:
    query: str
    position: int
    title: str
    source: str
    url: str
    published_at: str
    snippet: str
    domain: str
    result_type: str
    relevance: str
    grouped_story: bool
    google_news_url: str
    url_resolution_error: str = ""


class GoogleNewsClient:
    def __init__(self, timeout: float = 20.0, delay: float = 0.15):
        self.timeout = timeout
        self.delay = delay

    def _request(self, url: str, data: bytes | None = None) -> bytes:
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
            headers["Referer"] = f"{GOOGLE_NEWS}/"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def fetch(self, query: str, limit: int) -> list[NewsResult]:
        params = urllib.parse.urlencode(
            {"q": query, "hl": "ru", "gl": "RU", "ceid": "RU:ru"}
        )
        feed = self._request(f"{GOOGLE_NEWS}/rss/search?{params}")
        results = parse_rss(feed, query)

        # Resolve enough candidates to still return `limit` unique destination URLs.
        resolved: list[NewsResult] = []
        seen: set[str] = set()
        for item in results:
            destination, error = self.resolve_url(item.google_news_url)
            item.url = destination or item.google_news_url
            item.url_resolution_error = error
            item.domain = domain_from_url(item.url)
            dedupe_key = exact_page_key(item.url)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            item.position = len(resolved) + 1
            resolved.append(item)
            if len(resolved) >= limit:
                break
            if self.delay:
                time.sleep(self.delay)
        return resolved

    def resolve_url(self, google_url: str) -> tuple[str | None, str]:
        article_id = article_id_from_url(google_url)
        if not article_id:
            return None, "идентификатор Google News не найден"
        try:
            page = self._request(f"{GOOGLE_NEWS}/articles/{article_id}").decode(
                "utf-8", errors="replace"
            )
            signature = first_match(page, r'data-n-a-sg="([^"]+)"')
            timestamp = first_match(page, r'data-n-a-ts="(\d+)"')
            if not signature or not timestamp:
                return decode_legacy_article_id(article_id), (
                    "параметры декодирования отсутствуют"
                )

            inner = [
                "garturlreq",
                [
                    [
                        "ru-RU",
                        "RU",
                        ["FINANCE_TOP_INDICES", "WEB_TEST_1_0_0"],
                        None,
                        None,
                        1,
                        1,
                        "RU:ru",
                        None,
                        1,
                        None,
                        None,
                        None,
                        None,
                        None,
                        0,
                        1,
                    ],
                    "ru-RU",
                    "RU",
                    1,
                    [1, 1, 1],
                    1,
                    1,
                    None,
                    0,
                    0,
                    None,
                    0,
                ],
                article_id,
                int(timestamp),
                html.unescape(signature),
            ]
            envelope = [
                [["Fbv4je", json.dumps(inner, separators=(",", ":")), None, "generic"]]
            ]
            data = urllib.parse.urlencode(
                {"f.req": json.dumps(envelope, separators=(",", ":"))}
            ).encode("ascii")
            body = self._request(
                f"{GOOGLE_NEWS}/_/DotsSplashUi/data/batchexecute", data=data
            ).decode("utf-8", errors="replace")
            destination = parse_batchexecute_url(body)
            if destination:
                return destination, ""
            return None, "Google не вернул URL публикации"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return None, f"{type(exc).__name__}: {exc}"


def first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def article_id_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if "/" in path else ""


def decode_legacy_article_id(article_id: str) -> str | None:
    """Decode old IDs that directly embed the publisher URL."""
    import base64

    try:
        raw = base64.urlsafe_b64decode(article_id + "=" * (-len(article_id) % 4))
    except (ValueError, TypeError):
        return None
    match = re.search(rb"https?://[^\x00-\x20]+", raw)
    return match.group(0).decode("utf-8", errors="replace") if match else None


def parse_batchexecute_url(body: str) -> str | None:
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(body):
        try:
            start = min(
                index for index in (body.find("[", cursor), body.find("{", cursor))
                if index >= 0
            )
        except ValueError:
            break
        try:
            value, consumed = decoder.raw_decode(body[start:])
            cursor = start + consumed
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        rows = value if isinstance(value, list) else []
        for row in rows:
            if (
                isinstance(row, list)
                and len(row) >= 3
                and row[0] == "wrb.fr"
                and row[1] == "Fbv4je"
            ):
                payload = json.loads(row[2])
                if (
                    isinstance(payload, list)
                    and len(payload) > 1
                    and payload[0] == "garturlres"
                    and isinstance(payload[1], str)
                ):
                    return payload[1]
    return None


def parse_rss(data: bytes, query: str) -> list[NewsResult]:
    root = ET.fromstring(data)
    parsed: list[NewsResult] = []
    for position, item in enumerate(root.findall("./channel/item"), 1):
        source_node = item.find("source")
        source = clean_text(source_node.text if source_node is not None else "")
        raw_title = clean_text(item.findtext("title", default=""))
        title = strip_source_suffix(raw_title, source)
        google_url = clean_text(item.findtext("link", default=""))
        description = item.findtext("description", default="")
        links = re.findall(r"""href=["']([^"']+)""", html.unescape(description))
        published = parse_rfc822(item.findtext("pubDate", default=""))
        source_url = source_node.attrib.get("url", "") if source_node is not None else ""
        parsed.append(
            NewsResult(
                query=query,
                position=position,
                title=title,
                source=source,
                url=google_url,
                published_at=published,
                # RSS contains a headline/source link, not the Google UI snippet.
                snippet=SNIPPET_UNAVAILABLE,
                domain=domain_from_url(source_url),
                result_type="новостная публикация",
                relevance=relevance_comment(query, title),
                grouped_story=len(set(links)) > 1,
                google_news_url=google_url,
            )
        )
    return parsed


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def strip_source_suffix(title: str, source: str) -> str:
    suffix = f" - {source}"
    return title[: -len(suffix)] if source and title.endswith(suffix) else title


def parse_rfc822(value: str) -> str:
    if not value:
        return ""
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return value


def domain_from_url(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def exact_page_key(url: str) -> str:
    """Normalize transport-only differences, while preserving query parameters."""
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "")
    )


def words(value: str) -> set[str]:
    return set(re.findall(r"[a-zа-яё0-9]+", value.lower()))


def relevance_comment(query: str, title: str) -> str:
    query_words = words(query)
    title_words = words(title)
    matched = query_words & title_words
    if query_words and matched == query_words:
        return "высокая: все слова запроса есть в заголовке"
    if {"лобов", "вадим"} <= title_words:
        return "высокая: Вадим Лобов прямо упомянут в заголовке"
    ratio = len(matched) / len(query_words) if query_words else 0
    if ratio >= 0.5:
        return "средняя: совпадает часть ключевых слов запроса"
    return "низкая: слабое совпадение по заголовку"


NEGATIVE_WORDS = {
    "аферист",
    "скандал",
    "мошенник",
    "мошенничество",
    "шарашкин",
    "уголовный",
    "обман",
    "критика",
}
POSITIVE_WORDS = {
    "помогает",
    "победитель",
    "успех",
    "награда",
    "развитие",
    "поддержка",
    "лидер",
}
THEMES = {
    "образование": {"университет", "синергия", "образование", "студент", "обучение"},
    "бизнес и управление": {"бизнес", "предприниматель", "ректор", "корпорация"},
    "репутация/критика": NEGATIVE_WORDS,
    "СВО и общественная повестка": {"сво", "ветеран", "мобилизация", "уклонист"},
    "спорт": {"спорт", "мма", "бокс", "спортсмен"},
}


def title_sentiment(title: str) -> str:
    tokens = words(title)
    negative = len(tokens & NEGATIVE_WORDS)
    positive = len(tokens & POSITIVE_WORDS)
    if negative > positive:
        return "негативная"
    if positive > negative:
        return "позитивная"
    return "нейтральная/неопределённая"


def build_analytics(results: list[NewsResult]) -> list[str]:
    sources = Counter(item.source for item in results)
    domains = Counter(item.domain for item in results if item.domain)
    sentiments = Counter(title_sentiment(item.title) for item in results)
    themes: Counter[str] = Counter()
    for item in results:
        tokens = words(item.title)
        matched = False
        for theme, vocabulary in THEMES.items():
            if tokens & vocabulary:
                themes[theme] += 1
                matched = True
        if not matched:
            themes["прочее"] += 1

    dominant_sources = format_counts(sources.most_common(8))
    repeated_domains = format_counts(
        [(name, count) for name, count in domains.most_common() if count > 1][:10]
    )
    visible = sorted(
        results,
        key=lambda item: (
            item.position,
            0 if item.relevance.startswith("высокая") else 1,
        ),
    )[:5]
    visible_text = "; ".join(
        f"«{item.title}» ({item.source}, запрос «{item.query}», №{item.position})"
        for item in visible
    )
    return [
        f"Доминирующие СМИ: {dominant_sources or 'нет данных'}.",
        f"Домены с повторами: {repeated_domains or 'нет'}.",
        f"Темы/интенты по заголовкам: {format_counts(themes.most_common())}.",
        (
            "Тональность заголовков (автоматическая словарная оценка, не оценка "
            f"полного текста): {format_counts(sentiments.most_common())}."
        ),
        f"Наиболее заметные позиции: {visible_text or 'нет данных'}.",
        (
            "Информационный фон потенциально формируют источники с наибольшим "
            f"числом результатов: {dominant_sources or 'нет данных'}."
        ),
    ]


def format_counts(values: Iterable[tuple[str, int]]) -> str:
    return ", ".join(f"{name} — {count}" for name, count in values)


def markdown_escape(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", " ")


def write_outputs(
    results: list[NewsResult], output_dir: Path, checked_at: datetime
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    fields = list(NewsResult.__dataclass_fields__)
    with (output_dir / "google_news_results.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(item) for item in results)
    with (output_dir / "google_news_results.json").open(
        "w", encoding="utf-8"
    ) as stream:
        json.dump(
            {
                "checked_at": checked_at.isoformat(),
                "locale": {"hl": "ru", "gl": "RU", "ceid": "RU:ru"},
                "results": [asdict(item) for item in results],
            },
            stream,
            ensure_ascii=False,
            indent=2,
        )

    columns = [
        ("Запрос", "query"),
        ("Позиция", "position"),
        ("Заголовок", "title"),
        ("СМИ", "source"),
        ("Дата", "published_at"),
        ("Сниппет", "snippet"),
        ("URL", "url"),
        ("Домен", "domain"),
    ]
    lines = [
        "# Срез Google Новости",
        "",
        f"Дата проверки (UTC): {checked_at.isoformat()}",
        "",
        "| " + " | ".join(label for label, _ in columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for item in results:
        values = asdict(item)
        lines.append(
            "| "
            + " | ".join(markdown_escape(str(values[key])) for _, key in columns)
            + " |"
        )
    lines.extend(["", "## Краткая аналитика", ""])
    lines.extend(f"- {line}" for line in build_analytics(results))
    lines.extend(
        [
            "",
            "## Методические оговорки",
            "",
            (
                "- Данные получены из RU-локализованной новостной выдачи Google "
                "News RSS (`hl=ru`, `gl=RU`, `ceid=RU:ru`), порядок сохранён."
            ),
            (
                "- RSS Google News не отдаёт текстовый сниппет интерфейса. Поле "
                f"заполнено строго значением «{SNIPPET_UNAVAILABLE}»."
            ),
            (
                "- `grouped_story=true` выставляется только когда один RSS-элемент "
                "содержит несколько разных ссылок; отсутствие отметки не доказывает, "
                "что Google не связывает публикацию с сюжетом в иных интерфейсах."
            ),
            (
                "- Релевантность и тональность — автоматические оценки по заголовку; "
                "исходные поля и ошибки разрешения URL доступны в JSON/CSV."
            ),
        ]
    )
    (output_dir / "google_news_report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Собрать первые результаты RU-выдачи Google Новости."
    )
    parser.add_argument("-q", "--query", action="append", dest="queries")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.limit < 1:
        print("--limit должен быть положительным", file=sys.stderr)
        return 2
    client = GoogleNewsClient(timeout=args.timeout, delay=args.delay)
    results: list[NewsResult] = []
    for query in args.queries or DEFAULT_QUERIES:
        try:
            query_results = client.fetch(query, args.limit)
        except (ET.ParseError, urllib.error.URLError, TimeoutError) as exc:
            print(f"Ошибка запроса «{query}»: {exc}", file=sys.stderr)
            continue
        print(f"«{query}»: получено {len(query_results)} результатов")
        results.extend(query_results)
    if not results:
        print("Google News не вернул ни одного результата", file=sys.stderr)
        return 1
    write_outputs(results, args.output_dir, datetime.now(timezone.utc))
    print(f"Файлы сохранены в {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
