#!/usr/bin/env python3
"""Парсер выдачи Google News / Google Новости (не органического поиска)."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("google_news_parser")

DEFAULT_QUERIES = [
    "лобов вадим университет синергия",
    "лобов вадим синергия",
    "лобов вадим",
]
NEWS_SEARCH_URL = "https://news.google.com/search"
NEWS_RSS_URL = "https://news.google.com/rss/search"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
SNIPPET_UNAVAILABLE = "сниппет Google недоступен"
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "utm_id",
    "fbclid",
    "gclid",
    "yclid",
    "ysclid",
    "oc",
    "ved",
    "usg",
}
NEGATIVE_MARKERS = (
    "мошенник",
    "мошеннич",
    "шарашкин",
    "халтур",
    "уклонист",
    "скандал",
    "обман",
    "жалоб",
    "отзывы студентов",
    "реальные отзывы",
    "обещания и реальн",
    "расследован",
)
POSITIVE_MARKERS = (
    "запустил",
    "помогает",
    "поможет",
    "создал",
    "создание",
    "развивать",
    "кадры",
    "инновац",
    "успех",
    "рост",
    "партнер",
    "партнёр",
)
TOPIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "образование / вуз / EdTech",
        (
            "университет",
            "вуз",
            "студент",
            "образован",
            "колледж",
            "диплом",
            "edtech",
            "школ",
            "кадр",
        ),
    ),
    (
        "культура / выставки / коллекционирование",
        ("выставк", "арт", "искусств", "коллекц", "вднх", "хрустальн", "гала"),
    ),
    (
        "критика / расследования / негатив",
        ("мошенник", "шарашкин", "расследован", "отзывы", "обещания", "озолотится"),
    ),
    (
        "международная экспансия / язык / БРИКС",
        ("ирак", "африk", "африка", "брикс", "центр русского языка", "миграц"),
    ),
    ("интервью / колонки / подкасты", ("интервью", "подкаст", "жзл")),
    ("биография / досье", ("досье", "биография", "предприниматель и создатель")),
]


@dataclass
class NewsResult:
    query: str
    position: str
    title: str
    source: str
    url: str
    published_at: str
    snippet: str
    domain: str
    result_type: str
    relevance_comment: str
    clustered: bool = False
    cluster_role: str = ""
    cluster_size: int = 1
    related_urls: list[str] = field(default_factory=list)
    google_article_id: str = ""
    google_news_url: str = ""
    fetched_at: str = ""

    def table_row(self) -> dict[str, str]:
        return {
            "Запрос": self.query,
            "Позиция": str(self.position),
            "Заголовок": self.title,
            "СМИ": self.source,
            "Дата": self.published_at,
            "Сниппет": self.snippet,
            "URL": self.url,
            "Домен": self.domain,
        }


def moscow_now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Moscow"))


def format_timestamp(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        for fmt in (
            "%a, %d %b %Y %H:%M:%S %Z",
            "%a, %d %b %Y %H:%M:%S GMT",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(raw.replace("GMT", "+0000"), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M %Z")
            except ValueError:
                continue
        return raw
    if isinstance(value, (int, float)) and value > 0:
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
        return dt.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M %Z")
    return ""


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower() if parsed.scheme else "https"
    if scheme == "http":
        scheme = "https"
    netloc = parsed.netloc.lower()
    if netloc.endswith(":80"):
        netloc = netloc[:-3]
    if netloc.endswith(":443"):
        netloc = netloc[:-4]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query_pairs = [
        (key, val)
        for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (scheme, netloc, path, urllib.parse.urlencode(query_pairs), "")
    )


def domain_from_url(url: str) -> str:
    if not url:
        return ""
    netloc = urllib.parse.urlsplit(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def tokenize(text: str) -> set[str]:
    normalized = (text or "").lower().replace("ё", "е")
    return set(re.findall(r"[а-яa-z0-9]+", normalized))


def strip_html(text: str) -> str:
    text = unescape(text or "")
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def looks_like_real_snippet(snippet: str, title: str, source: str) -> bool:
    text = strip_html(snippet)
    if not text:
        return False
    compact = re.sub(r"\s+", " ", text).strip()
    title_c = (title or "").strip()
    source_c = (source or "").strip()
    if compact in {title_c, f"{title_c} {source_c}".strip(), source_c}:
        return False
    remainder = compact
    if title_c:
        remainder = remainder.replace(title_c, " ").strip()
    if source_c:
        remainder = remainder.replace(source_c, " ").strip()
    remainder = re.sub(r"\s+", " ", remainder)
    return len(remainder) >= 40


def snippet_or_unavailable(snippet: str, title: str, source: str) -> str:
    if looks_like_real_snippet(snippet, title, source):
        return strip_html(snippet)
    return SNIPPET_UNAVAILABLE


def classify_result_type(title: str, url: str, source: str) -> str:
    blob = f"{title} {url} {source}".lower()
    if "интервью" in blob:
        return "интервью"
    if "досье" in blob or "биография" in blob:
        return "досье / биография"
    if "/blogs/" in blob or "подкаст" in blob or "колонк" in blob:
        return "блог / колонка / подкаст"
    if "расследован" in blob or "как устроен бизнес" in blob:
        return "расследование"
    if "отзыв" in blob:
        return "обзор / отзывы"
    if any(marker in blob for marker in ("анонсировал", "запустил", "сообщил о")):
        return "новость (анонс)"
    if blob.strip():
        return "новость"
    return "не определено"


def relevance_comment(query: str, title: str, source: str, url: str, domain: str) -> str:
    query_tokens = tokenize(query)
    haystack = tokenize(f"{title} {source} {url} {domain}")
    present = sorted(query_tokens & haystack)
    missing = sorted(query_tokens - haystack)
    has_lobov = "лобов" in haystack
    has_vadim = "вадим" in haystack
    has_synergy = bool({"синергия", "synergy", "sinergiya"} & haystack)
    has_university = bool({"университет", "вуз"} & haystack)
    wants_synergy = "синергия" in query_tokens
    wants_university = "университет" in query_tokens

    if has_lobov and has_vadim and (has_synergy or not wants_synergy):
        if wants_university and not (has_university or has_synergy):
            level = "средняя"
            reason = "имя есть, но университет в заголовке/URL не виден"
        elif wants_synergy and has_synergy:
            level = "высокая"
            reason = "в карточке есть и персона, и «Синергия»"
        else:
            level = "высокая"
            reason = "в карточке явно фигурируют фамилия и имя"
    elif has_lobov and has_synergy:
        level = "высокая"
        reason = "есть фамилия и «Синергия», имя в карточке может быть опущено"
    elif has_lobov or has_vadim:
        level = "средняя"
        reason = "персона частично совпадает, часть запроса в карточке не видна"
    elif has_synergy and wants_synergy:
        level = "средняя"
        reason = "есть «Синергия», но персона в заголовке/URL не видна"
    else:
        level = "низкая"
        reason = "по видимым полям карточки запрос почти не подтверждается"

    present_txt = ", ".join(present) if present else "нет"
    missing_txt = ", ".join(missing) if missing else "нет"
    return (
        f"{level.capitalize()} релевантность: {reason}. "
        f"Совпавшие токены: {present_txt}. Не видны: {missing_txt}."
    )


def classify_topic(title: str, url: str) -> str:
    blob = f"{title} {url}".lower().replace("ё", "е")
    matched = [name for name, keys in TOPIC_RULES if any(key in blob for key in keys)]
    return "; ".join(matched) if matched else "прочее / общий инфоповод"


def classify_tone(title: str, url: str, snippet: str) -> str:
    blob = f"{title} {url} {snippet}".lower().replace("ё", "е")
    neg = any(marker in blob for marker in NEGATIVE_MARKERS)
    pos = any(marker in blob for marker in POSITIVE_MARKERS)
    if neg and not pos:
        return "негативная"
    if pos and not neg:
        return "позитивная"
    if neg and pos:
        return "смешанная"
    return "нейтральная"


def http_get(url: str, timeout: int = 30, retries: int = 3) -> bytes:
    last_error: Exception | None = None
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            LOGGER.warning("GET %s failed (attempt %s/%s): %s", url, attempt, retries, exc)
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"Не удалось загрузить {url}: {last_error}") from last_error


def news_search_url(query: str) -> str:
    params = {"q": query, "hl": "ru", "gl": "RU", "ceid": "RU:ru"}
    return f"{NEWS_SEARCH_URL}?{urllib.parse.urlencode(params)}"


def news_rss_url(query: str) -> str:
    params = {"q": query, "hl": "ru", "gl": "RU", "ceid": "RU:ru"}
    return f"{NEWS_RSS_URL}?{urllib.parse.urlencode(params)}"


def _extract_json_array(text: str) -> Any:
    decoder = json.JSONDecoder()
    obj, _end = decoder.raw_decode(text)
    return obj


def parse_gsrres_from_html(html: str) -> list[Any] | None:
    patterns = [
        r"AF_initDataCallback\(\{key: 'ds:\d+', hash: '[^']*', data:(\[\"gsrres\".*?), sideChannel:",
        r"AF_initDataCallback\(\{key: \"ds:\d+\", hash: \"[^\"]*\", data:(\[\"gsrres\".*?), sideChannel:",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.S)
        if match:
            try:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, list) and parsed and parsed[0] == "gsrres":
                    return parsed
            except json.JSONDecodeError:
                LOGGER.debug("JSON parse failed for gsrres regex candidate")
    marker = 'data:["gsrres"'
    idx = html.find(marker)
    if idx == -1:
        idx = html.find("data:['gsrres'")
    if idx != -1:
        try:
            parsed = _extract_json_array(html[idx + len("data:") :])
            if isinstance(parsed, list) and parsed and parsed[0] == "gsrres":
                return parsed
        except json.JSONDecodeError:
            LOGGER.debug("raw_decode failed for gsrres blob")
    return None


def _article_id(article: list[Any]) -> str:
    try:
        ident = article[1][1]
        return ident if isinstance(ident, str) else ""
    except (IndexError, TypeError):
        return ""


def _article_source(article: list[Any]) -> str:
    try:
        source = article[10][2]
        return source if isinstance(source, str) else ""
    except (IndexError, TypeError):
        return ""


def _article_url(article: list[Any]) -> str:
    for index in (6, 38, 7):
        try:
            value = article[index]
        except IndexError:
            continue
        if isinstance(value, str) and value.startswith("http"):
            return value
    return ""


def _article_title(article: list[Any]) -> str:
    try:
        title = article[2]
        return title.strip() if isinstance(title, str) else ""
    except IndexError:
        return ""


def _article_snippet(article: list[Any]) -> str:
    for index in (3, 8, 9, 11):
        try:
            value = article[index]
        except IndexError:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _article_timestamp(article: list[Any]) -> Any:
    try:
        return article[4]
    except IndexError:
        return None


def is_article_object(obj: Any) -> bool:
    if not isinstance(obj, list) or len(obj) < 7:
        return False
    title = _article_title(obj)
    url = _article_url(obj)
    if not title or not url:
        return False
    if obj[0] == 13:
        return True
    return url.startswith("http") and len(title) > 5


def collect_articles(obj: Any, acc: list[list[Any]] | None = None) -> list[list[Any]]:
    if acc is None:
        acc = []
    if is_article_object(obj):
        acc.append(obj)
        return acc
    if isinstance(obj, list):
        for item in obj:
            collect_articles(item, acc)
    return acc


def google_news_article_url(article_id: str) -> str:
    if not article_id:
        return ""
    return (
        "https://news.google.com/rss/articles/"
        f"{urllib.parse.quote(article_id)}?hl=ru&gl=RU&ceid=RU:ru"
    )


def results_from_articles(
    query: str,
    articles: list[list[Any]],
    position: int,
    fetched_at: str,
    clustered: bool,
) -> list[NewsResult]:
    rows: list[NewsResult] = []
    seen_urls: set[str] = set()
    unique_articles: list[list[Any]] = []
    for article in articles:
        url = _article_url(article)
        key = canonical_url(url)
        if not key or key in seen_urls:
            continue
        seen_urls.add(key)
        unique_articles.append(article)

    cluster_size = len(unique_articles)
    related_urls = [_article_url(item) for item in unique_articles]
    for index, article in enumerate(unique_articles):
        title = _article_title(article)
        url = _article_url(article)
        source = _article_source(article) or domain_from_url(url)
        snippet = snippet_or_unavailable(_article_snippet(article), title, source)
        article_id = _article_id(article)
        role = ""
        pos: str | int = position
        if clustered and cluster_size > 1:
            role = "главная карточка сюжета" if index == 0 else "связанная публикация в сюжете"
            pos = str(position) if index == 0 else f"{position} (связанная)"
        rows.append(
            NewsResult(
                query=query,
                position=str(pos),
                title=title,
                source=source,
                url=url,
                published_at=format_timestamp(_article_timestamp(article)),
                snippet=snippet,
                domain=domain_from_url(url),
                result_type=classify_result_type(title, url, source),
                relevance_comment=relevance_comment(query, title, source, url, domain_from_url(url)),
                clustered=clustered and cluster_size > 1,
                cluster_role=role,
                cluster_size=cluster_size if clustered else 1,
                related_urls=[item for item in related_urls if item != url],
                google_article_id=article_id,
                google_news_url=google_news_article_url(article_id),
                fetched_at=fetched_at,
            )
        )
    return rows


def parse_search_payload(payload: list[Any], query: str, fetched_at: str) -> list[NewsResult]:
    if not payload or payload[0] != "gsrres":
        return []
    try:
        cards = payload[1][0]
    except (IndexError, TypeError):
        return []
    if not isinstance(cards, list):
        return []

    results: list[NewsResult] = []
    for offset, card in enumerate(cards, start=1):
        articles = collect_articles(card)
        if not articles:
            continue
        clustered = len({canonical_url(_article_url(item)) for item in articles if _article_url(item)}) > 1
        results.extend(
            results_from_articles(
                query=query,
                articles=articles,
                position=offset,
                fetched_at=fetched_at,
                clustered=clustered,
            )
        )
    return results


def parse_rss(xml_text: str, query: str, fetched_at: str) -> list[NewsResult]:
    root = ET.fromstring(xml_text)
    items = root.findall("./channel/item")
    results: list[NewsResult] = []
    for offset, item in enumerate(items, start=1):
        raw_title = (item.findtext("title") or "").strip()
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        title = raw_title
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        google_url = (item.findtext("link") or "").strip()
        description = item.findtext("description") or ""
        snippet = snippet_or_unavailable(description, title, source)
        results.append(
            NewsResult(
                query=query,
                position=str(offset),
                title=title,
                source=source or "не указано",
                url=google_url,
                published_at=format_timestamp(item.findtext("pubDate") or ""),
                snippet=snippet,
                domain=domain_from_url(google_url) or "news.google.com",
                result_type=classify_result_type(title, google_url, source),
                relevance_comment=relevance_comment(
                    query, title, source, google_url, domain_from_url(google_url)
                ),
                google_news_url=google_url,
                fetched_at=fetched_at,
            )
        )
    return results


def dedupe_exact_pages(results: list[NewsResult]) -> list[NewsResult]:
    seen: set[str] = set()
    unique: list[NewsResult] = []
    for item in results:
        key = canonical_url(item.url) or f"{item.title}|{item.source}|{item.published_at}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def limit_serp_positions(results: list[NewsResult], limit: int) -> list[NewsResult]:
    kept: list[NewsResult] = []
    main_positions = 0
    for item in results:
        is_related = "связанная" in str(item.position) or item.cluster_role.startswith("связанная")
        if not is_related:
            if main_positions >= limit:
                continue
            main_positions += 1
            kept.append(item)
        elif kept:
            # Related URLs belong to an already accepted cluster card.
            kept.append(item)
    return kept


def parse_html_results(html: str, query: str, fetched_at: str) -> list[NewsResult]:
    payload = parse_gsrres_from_html(html)
    if not payload:
        return []
    parsed_query = query
    if len(payload) > 2 and isinstance(payload[2], str) and payload[2].strip():
        parsed_query = payload[2].strip()
    return parse_search_payload(payload, parsed_query, fetched_at)


def fetch_query_results(query: str, limit: int, pause_sec: float = 1.0) -> tuple[list[NewsResult], dict[str, Any]]:
    fetched_at = moscow_now().isoformat(timespec="seconds")
    meta: dict[str, Any] = {
        "query": query,
        "search_url": news_search_url(query),
        "rss_url": news_rss_url(query),
        "source_used": "",
        "fetched_at": fetched_at,
        "locale": {"hl": "ru", "gl": "RU", "ceid": "RU:ru"},
    }
    html = http_get(meta["search_url"]).decode("utf-8", errors="replace")
    results = parse_html_results(html, query, fetched_at)
    if results:
        meta["source_used"] = "google_news_html"
        meta["raw_count"] = len(results)
    else:
        LOGGER.warning("HTML parse empty for %r, falling back to RSS", query)
        rss = http_get(meta["rss_url"]).decode("utf-8", errors="replace")
        results = parse_rss(rss, query, fetched_at)
        meta["source_used"] = "google_news_rss_fallback"
        meta["raw_count"] = len(results)
    results = dedupe_exact_pages(results)
    results = limit_serp_positions(results, limit)
    meta["returned_count"] = len(results)
    if pause_sec:
        time.sleep(pause_sec)
    return results, meta


def markdown_table(results: list[NewsResult]) -> str:
    headers = ["Запрос", "Позиция", "Заголовок", "СМИ", "Дата", "Сниппет", "URL", "Домен"]
    lines = ["|" + "|".join(headers) + "|", "|" + "|".join(["---"] * len(headers)) + "|"]

    def cell(value: str) -> str:
        return (value or "").replace("\n", " ").replace("|", "\\|").replace("\r", " ")

    for item in results:
        row = item.table_row()
        lines.append("|" + "|".join(cell(row[name]) for name in headers) + "|")
    return "\n".join(lines)


def build_analytics(results: list[NewsResult]) -> str:
    if not results:
        return "Результатов нет, аналитика недоступна."

    source_counts = Counter(item.source for item in results)
    domain_counts = Counter(item.domain for item in results)
    topic_counts = Counter(classify_topic(item.title, item.url) for item in results)
    tone_counts = Counter(classify_tone(item.title, item.url, item.snippet) for item in results)
    type_counts = Counter(item.result_type for item in results)
    clustered = [item for item in results if item.clustered]
    multi_query_urls: dict[str, set[str]] = defaultdict(set)
    for item in results:
        multi_query_urls[canonical_url(item.url)].add(item.query)

    dominant = ", ".join(f"{name} ({count})" for name, count in source_counts.most_common(8)) or "нет данных"
    repeated_domains = [
        f"{domain} ({count})" for domain, count in domain_counts.most_common() if count > 1
    ]
    top_visible = [
        f"[{item.query} / #{item.position}] {item.title} — {item.source}"
        for item in results
        if str(item.position).isdigit() and int(item.position) <= 5
    ]
    background_sources = [
        f"{name} ({count})"
        for name, count in source_counts.most_common()
        if count >= 2 or name in {item.source for item in results if str(item.position) in {"1", "2", "3"}}
    ]
    recurring = [
        url
        for url, queries in multi_query_urls.items()
        if len(queries) > 1
    ]
    cluster_card_ids = {item.position.split()[0] for item in clustered}
    if clustered:
        cluster_note = (
            f"Обнаружено карточек-кластеров: {len(cluster_card_ids)}. "
            f"Связанных URL внутри сюжетов: {len(clustered)}."
        )
    else:
        cluster_note = (
            "В текущем срезе Google News не показал объединение нескольких публикаций "
            "в один новостной сюжет: каждая карточка — отдельный результат."
        )
    recurring_note = (
        "\n".join(f"- {url}" for url in recurring[:20])
        if recurring
        else "Точных совпадений URL между разными запросами не найдено."
    )
    repeated_note = (
        ", ".join(repeated_domains)
        if repeated_domains
        else "Повторяющихся доменов в пределах собранного среза нет"
    )
    background_note = (
        ", ".join(background_sources[:12]) if background_sources else "недостаточно повторов"
    )

    lines = [
        "### Какие СМИ доминируют",
        dominant + ".",
        "",
        "### Какие домены встречаются несколько раз",
        repeated_note + ".",
        "",
        "### Какие темы/интенты преобладают",
        ", ".join(f"{name} ({count})" for name, count in topic_counts.most_common()) + ".",
        f"Типы карточек: {', '.join(f'{name} ({count})' for name, count in type_counts.most_common())}.",
        "",
        "### Тональность",
        ", ".join(f"{name} ({count})" for name, count in tone_counts.most_common()) + ".",
        "Оценка тональности сделана только по видимым полям карточки (заголовок/URL), без дочитывания полного текста статьи.",
        "",
        "### Какие публикации наиболее заметны",
    ]
    lines.extend([f"- {line}" for line in top_visible[:15]] or ["- нет данных"])
    lines.extend(
        [
            "",
            "### Источники, которые потенциально формируют информационный фон",
            background_note + ".",
            "",
            "### Сюжеты, которые Google мог объединить",
            cluster_note,
            "",
            "### Публикации, которые повторяются между запросами",
            recurring_note,
        ]
    )
    return "\n".join(lines)


def build_report(
    results: list[NewsResult],
    metas: list[dict[str, Any]],
    checked_at: str,
) -> str:
    clusters = [item for item in results if item.clustered]
    lines = [
        "# Срез выдачи Google News",
        "",
        f"- Дата проверки: {checked_at}",
        "- Поисковая система: Google News / Google Новости (`news.google.com/search`)",
        "- Регион: Россия (`gl=RU`)",
        "- Язык и интерфейс: русский (`hl=ru`, `ceid=RU:ru`)",
        "- Глубина: первые 20 результатов по каждому запросу",
        "- Органическая выдача Google Search не использовалась",
        "- Точные дубли одной страницы удалялись после нормализации URL; разные публикации одного СМИ сохранялись",
        "- Сниппет брался только из полей самой выдачи Google News; при отсутствии писалось «сниппет Google недоступен»",
        f"- Источники запросов: {', '.join(sorted({meta.get('source_used') or '?'} for meta in metas))}",
        "",
        "## Сводная таблица",
        "",
        markdown_table(results),
        "",
        "## Кластеры / связанные публикации",
        "",
    ]
    if clusters:
        for item in clusters:
            lines.append(
                f"- Запрос «{item.query}», позиция {item.position}: {item.cluster_role}; "
                f"размер сюжета {item.cluster_size}. {item.title} — {item.url}"
            )
    else:
        lines.append("Объединённых сюжетов в выдаче не обнаружено.")
    lines.extend(
        [
            "",
            "## Комментарии по релевантности и типу результата",
            "",
        ]
    )
    for item in results:
        cluster_note = f" Кластер: {item.cluster_role}." if item.clustered else ""
        lines.append(
            f"- [{item.query} / #{item.position}] тип: {item.result_type}. "
            f"{item.relevance_comment}{cluster_note}"
        )
    lines.extend(["", "## Краткая аналитика", "", build_analytics(results), ""])
    lines.extend(["## Технические метаданные запросов", ""])
    for meta in metas:
        lines.append(
            f"- «{meta['query']}»: источник `{meta.get('source_used')}`, "
            f"сырых карточек {meta.get('raw_count')}, после фильтра {meta.get('returned_count')}, "
            f"[выдача]({meta.get('search_url')})"
        )
    lines.append("")
    return "\n".join(lines)


def write_outputs(results: list[NewsResult], report: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "google_news_results.json"
    csv_path = output_dir / "google_news_results.csv"
    md_path = output_dir / "google_news_report.md"
    json_path.write_text(
        json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Запрос", "Позиция", "Заголовок", "СМИ", "Дата", "Сниппет", "URL", "Домен"],
        )
        writer.writeheader()
        for item in results:
            writer.writerow(item.table_row())
    md_path.write_text(report, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Парсер выдачи Google News (RU).")
    parser.add_argument("--queries", nargs="+", default=DEFAULT_QUERIES, help="Поисковые запросы")
    parser.add_argument("--limit", type=int, default=20, help="Глубина выдачи по каждому запросу")
    parser.add_argument("--output-dir", default="output", help="Каталог для JSON/CSV/Markdown")
    parser.add_argument("--from-html", help="Разобрать сохранённый HTML вместо живого запроса")
    parser.add_argument("--query", help="Запрос для режима --from-html")
    parser.add_argument("--pause", type=float, default=1.0, help="Пауза между живыми запросами")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    checked_at = moscow_now().strftime("%Y-%m-%d %H:%M %Z")
    all_results: list[NewsResult] = []
    metas: list[dict[str, Any]] = []

    if args.from_html:
        html = Path(args.from_html).read_text(encoding="utf-8")
        query = args.query or (args.queries[0] if args.queries else "")
        fetched_at = moscow_now().isoformat(timespec="seconds")
        parsed = parse_html_results(html, query, fetched_at)
        parsed = dedupe_exact_pages(parsed)
        parsed = limit_serp_positions(parsed, args.limit)
        all_results.extend(parsed)
        metas.append(
            {
                "query": query,
                "search_url": news_search_url(query),
                "rss_url": news_rss_url(query),
                "source_used": "local_html",
                "raw_count": len(parsed),
                "returned_count": len(parsed),
                "fetched_at": fetched_at,
            }
        )
    else:
        for query in args.queries:
            LOGGER.info("Fetching Google News for %r", query)
            results, meta = fetch_query_results(query, limit=args.limit, pause_sec=args.pause)
            all_results.extend(results)
            metas.append(meta)

    report = build_report(all_results, metas, checked_at)
    write_outputs(all_results, report, Path(args.output_dir))
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
