#!/usr/bin/env python3
"""Collect a reproducible RU/Russia Google News search snapshot."""

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
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

BASE_URL = "https://news.google.com/"
SEARCH_URL = urllib.parse.urljoin(BASE_URL, "search")
BATCH_URL = urllib.parse.urljoin(BASE_URL, "_/DotsSplashUi/data/batchexecute")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
DEFAULT_QUERIES = [
    "лобов вадим университет синергия",
    "лобов вадим синергия",
    "лобов вадим",
]
SNIPPET_UNAVAILABLE = "сниппет Google недоступен"


@dataclass
class RawResult:
    title: str = ""
    source: str = ""
    google_url: str = ""
    published_at: str = ""
    published_display: str = ""
    grouped_story: bool = False


@dataclass
class Result:
    query: str
    position: int
    title: str
    source: str
    url: str
    google_url: str
    published_at: str
    published_display: str
    snippet: str
    domain: str
    result_type: str
    grouped_story: bool
    relevance_comment: str
    sentiment: str


class GoogleNewsHTMLParser(HTMLParser):
    """Parse result cards from the server-rendered Google News HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[RawResult] = []
        self._card_depth = 0
        self._card: RawResult | None = None
        self._field: str | None = None
        self._field_depth = 0
        self._chunks: list[str] = []

    @staticmethod
    def _classes(attrs: dict[str, str | None]) -> set[str]:
        return set((attrs.get("class") or "").split())

    def handle_starttag(
        self, tag: str, attrs_list: list[tuple[str, str | None]]
    ) -> None:
        attrs = dict(attrs_list)
        classes = self._classes(attrs)

        if tag == "c-wiz" and "PO9Zff" in classes and self._card is None:
            self._card = RawResult()
            self._card_depth = 1
            return
        if self._card is None:
            return

        if tag == "c-wiz":
            self._card_depth += 1

        if self._field:
            self._field_depth += 1
            return

        if tag == "div" and "vr1PYe" in classes:
            self._begin_field("source")
        elif tag == "a" and "JtKRv" in classes:
            self._card.google_url = urllib.parse.urljoin(
                BASE_URL, html.unescape(attrs.get("href") or "")
            )
            self._begin_field("title")
        elif tag == "time" and "hvbAAd" in classes:
            self._card.published_at = attrs.get("datetime") or ""
            self._begin_field("published_display")

        # Google uses this link for a multi-publication story cluster.
        label = (attrs.get("aria-label") or "").lower()
        if "полное освещение" in label or "все материалы" in label:
            self._card.grouped_story = True

    def _begin_field(self, field: str) -> None:
        self._field = field
        self._field_depth = 1
        self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._field:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return

        if self._field:
            self._field_depth -= 1
            if self._field_depth == 0:
                value = " ".join("".join(self._chunks).split())
                setattr(self._card, self._field, value)
                self._field = None
                self._chunks = []

        if tag == "c-wiz":
            self._card_depth -= 1
            if self._card_depth == 0:
                if self._card.title and self._card.google_url:
                    self.results.append(self._card)
                self._card = None


def request(
    url: str, *, data: bytes | None = None, timeout: float = 25
) -> tuple[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        headers["Referer"] = BASE_URL
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace"), response.url


def search_url(query: str) -> str:
    return SEARCH_URL + "?" + urllib.parse.urlencode(
        {"q": query, "hl": "ru", "gl": "RU", "ceid": "RU:ru"}
    )


def parse_search_page(body: str) -> list[RawResult]:
    parser = GoogleNewsHTMLParser()
    parser.feed(body)
    return parser.results


def article_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname != "news.google.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[-2] in {"read", "articles"}:
        return parts[-1]
    if len(parts) >= 3 and parts[-3:-1] == ["rss", "articles"]:
        return parts[-1]
    return None


def _offline_decode(identifier: str) -> str | None:
    """Decode the legacy protobuf wrapper when it embeds a publisher URL."""
    import base64

    try:
        padded = identifier + "=" * (-len(identifier) % 4)
        payload = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return None
    match = re.search(rb"https?://[^\x00-\x20]+", payload)
    return match.group(0).decode("utf-8", errors="replace") if match else None


def decode_google_url(url: str, *, delay: float = 0.0) -> str:
    identifier = article_id(url)
    if not identifier:
        return url
    legacy = _offline_decode(identifier)
    if legacy:
        return legacy

    page = ""
    for prefix in ("articles", "rss/articles", "read"):
        try:
            page, _ = request(urllib.parse.urljoin(BASE_URL, f"{prefix}/{identifier}"))
        except (OSError, urllib.error.HTTPError):
            continue
        if "data-n-a-sg" in page:
            break

    signature = re.search(r'data-n-a-sg="([^"]+)"', page)
    timestamp = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not signature or not timestamp:
        return url

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
        identifier,
        int(timestamp.group(1)),
        signature.group(1),
    ]
    envelope = [[["Fbv4je", json.dumps(inner, separators=(",", ":")), None, "generic"]]]
    data = urllib.parse.urlencode(
        {"f.req": json.dumps(envelope, separators=(",", ":"))}
    ).encode()
    if delay:
        time.sleep(delay)
    try:
        body, _ = request(BATCH_URL + "?rpcids=Fbv4je", data=data)
        if body.startswith(")]}'"):
            body = body.split("\n", 1)[1]
        for row in json.loads(body.lstrip()):
            if len(row) >= 3 and row[0] == "wrb.fr" and row[1] == "Fbv4je":
                decoded = json.loads(row[2])
                if len(decoded) >= 2 and decoded[0] == "garturlres":
                    return decoded[1]
    except (OSError, ValueError, TypeError, urllib.error.HTTPError):
        pass
    return url


def canonical_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    removable = {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
    }
    query = urllib.parse.urlencode(
        [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query) if key not in removable]
    )
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query, "")
    )


def domain_for(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def relevance(query: str, title: str) -> str:
    wanted = set(re.findall(r"[а-яёa-z0-9]+", query.lower()))
    present = set(re.findall(r"[а-яёa-z0-9]+", title.lower()))
    matched = wanted & present
    if wanted and matched == wanted:
        return "Высокая: заголовок содержит все слова запроса"
    if {"лобов", "вадим"} <= present:
        return "Высокая: публикация непосредственно о Вадиме Лобове"
    if matched:
        return "Средняя: совпадает часть терминов запроса"
    return "Низкая: явного совпадения в заголовке нет"


def sentiment(title: str) -> str:
    text = title.lower()
    negative = {
        "мошенник",
        "мошенничество",
        "разводят",
        "скандал",
        "обман",
        "негатив",
        "уклонист",
        "шарашкин",
    }
    positive = {
        "помогает",
        "успех",
        "развитие",
        "открыл",
        "создал",
        "награда",
        "лидер",
    }
    if any(word in text for word in negative):
        return "негативная"
    if any(word in text for word in positive):
        return "позитивная"
    return "нейтральная/неопределимая"


def classify(title: str) -> str:
    lowered = title.lower()
    if any(word in lowered for word in ("досье", "биография", "кто такой")):
        return "Справочный материал / профиль"
    if any(word in lowered for word in ("интервью", "рассказал", "заявил")):
        return "Интервью / заявление"
    return "Новостная публикация"


def collect(queries: Iterable[str], limit: int, delay: float) -> list[Result]:
    all_results: list[Result] = []
    decoded_cache: dict[str, str] = {}
    for query in queries:
        body, _ = request(search_url(query))
        raw_results = parse_search_page(body)
        seen: set[str] = set()
        position = 0
        for raw in raw_results:
            if position >= limit:
                break
            resolved = decoded_cache.get(raw.google_url)
            if resolved is None:
                resolved = decode_google_url(raw.google_url, delay=delay)
                decoded_cache[raw.google_url] = resolved
            exact_page = canonical_url(resolved)
            if exact_page in seen:
                continue
            seen.add(exact_page)
            position += 1
            all_results.append(
                Result(
                    query=query,
                    position=position,
                    title=raw.title,
                    source=raw.source,
                    url=resolved,
                    google_url=raw.google_url,
                    published_at=raw.published_at,
                    published_display=raw.published_display,
                    snippet=SNIPPET_UNAVAILABLE,
                    domain=domain_for(resolved),
                    result_type=classify(raw.title),
                    grouped_story=raw.grouped_story,
                    relevance_comment=relevance(query, raw.title),
                    sentiment=sentiment(raw.title),
                )
            )
    return all_results


def write_csv(path: Path, results: list[Result]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(result) for result in results)


def escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def analytics(results: list[Result]) -> str:
    sources = Counter(result.source for result in results)
    domains = Counter(result.domain for result in results)
    repeated = [(domain, count) for domain, count in domains.most_common() if count > 1]
    tones = Counter(result.sentiment for result in results)
    noticeable = sorted(
        results,
        key=lambda item: (item.position, -domains[item.domain]),
    )[:5]
    grouped_count = sum(result.grouped_story for result in results)
    source_text = ", ".join(f"{name} ({count})" for name, count in sources.most_common(5))
    domain_text = ", ".join(f"{name} ({count})" for name, count in repeated) or "нет"
    tone_text = ", ".join(f"{name} — {count}" for name, count in tones.most_common())
    noticeable_text = "; ".join(
        f"«{item.title}» ({item.source}, позиция {item.position}, запрос «{item.query}»)"
        for item in noticeable
    )
    return "\n".join(
        [
            f"- Доминирующие СМИ: {source_text or 'нет результатов'}.",
            f"- Домены, встречающиеся несколько раз: {domain_text}.",
            "- Основные темы/интенты определяются по заголовкам: биография и деловая "
            "активность Вадима Лобова, деятельность университета «Синергия», а также "
            "репутационные и критические материалы.",
            f"- Тональность заголовков (эвристическая): {tone_text}.",
            f"- Наиболее заметные публикации: {noticeable_text or 'нет результатов'}.",
            f"- Информационный фон прежде всего формируют: {source_text or 'нет результатов'}.",
            f"- Сюжетные группы, явно помеченные Google: {grouped_count}.",
        ]
    )


def write_report(path: Path, results: list[Result], checked_at: str) -> None:
    headers = ["Запрос", "Позиция", "Заголовок", "СМИ", "Дата", "Сниппет", "URL", "Домен"]
    lines = [
        "# Срез выдачи Google News",
        "",
        f"Дата проверки: {checked_at}. Регион: Россия. Язык/интерфейс: русский.",
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for result in results:
        row = [
            result.query,
            result.position,
            result.title,
            result.source,
            result.published_display or result.published_at,
            result.snippet,
            result.url,
            result.domain,
        ]
        lines.append("| " + " | ".join(escape_cell(value) for value in row) + " |")
    lines.extend(["", "## Краткая аналитика", "", analytics(results), ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-q", "--query", action="append", dest="queries")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--csv", type=Path, default=Path("google_news_results.csv"))
    parser.add_argument("--report", type=Path, default=Path("google_news_report.md"))
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")

    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        results = collect(args.queries or DEFAULT_QUERIES, args.limit, args.delay)
    except (OSError, urllib.error.HTTPError) as error:
        print(f"Google News request failed: {error}", file=sys.stderr)
        return 1
    if not results:
        print("Google News returned no parseable results", file=sys.stderr)
        return 2

    write_csv(args.csv, results)
    write_report(args.report, results, checked_at)
    print(
        f"Collected {len(results)} rows; wrote {args.csv} and {args.report}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
