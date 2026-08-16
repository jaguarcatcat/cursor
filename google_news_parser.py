#!/usr/bin/env python3
"""
Парсер выдачи Google News (Google Новости) для заданных поисковых запросов.
Регион: Россия, язык: русский. Финальный вывод — XLSX с кликабельными URL.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from itertools import cycle
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import requests
from googlenewsdecoder import gnewsdecoder
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink

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

XLSX_COLUMNS = [
    ("Запрос", "query", 35),
    ("Позиция", "position", 10),
    ("Заголовок", "title", 55),
    ("СМИ", "source", 25),
    ("Дата публикации", "publication_date", 22),
    ("Сниппет", "snippet", 40),
    ("URL материала", "url", 50),
    ("Домен", "domain", 22),
    ("Тип результата", "result_type", 28),
    ("Кластер Google", "is_cluster", 14),
    ("Источник данных", "data_source", 18),
    ("Комментарий (релевантность)", "relevance_comment", 45),
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
    data_source: str = "html"
    google_url: str = ""


@dataclass
class QueryCoverage:
    query: str
    requested: int
    collected: int
    available: int
    html_count: int
    rss_count: int
    is_complete: bool
    note: str
    proxy_used: str = ""


class ProxyRotator:
    """Ротация прокси с повторными попытками при ошибках."""

    def __init__(self, proxy_urls: list[str]) -> None:
        self._proxies = [p.strip() for p in proxy_urls if p.strip()]
        self._cycle = cycle(self._proxies) if self._proxies else None
        self._failed: set[str] = set()

    @property
    def available(self) -> bool:
        return bool(self._proxies)

    @property
    def count(self) -> int:
        return len(self._proxies)

    def next_proxy(self) -> str | None:
        if not self._cycle:
            return None
        active = [p for p in self._proxies if p not in self._failed]
        if not active:
            self._failed.clear()
            active = self._proxies
        return random.choice(active)

    def mark_failed(self, proxy: str) -> None:
        self._failed.add(proxy)

    def create_session(self, proxy: str | None = None) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
        chosen = proxy or self.next_proxy()
        if chosen:
            session.proxies.update({"http": chosen, "https": chosen})
        return session

    def fetch(self, url: str, max_retries: int = 3) -> str:
        last_error: Exception | None = None
        tried: set[str] = set()

        for _ in range(max_retries):
            proxy = self.next_proxy()
            if proxy and proxy in tried and len(tried) >= len(self._proxies):
                break
            if proxy:
                tried.add(proxy)

            session = self.create_session(proxy)
            try:
                response = session.get(url, timeout=30)
                response.raise_for_status()
                return response.text
            except requests.RequestException as exc:
                last_error = exc
                if proxy:
                    self.mark_failed(proxy)
                time.sleep(0.5)

        if last_error:
            raise last_error
        raise requests.RequestException(f"Не удалось загрузить {url}")


def load_proxy_list(proxy_arg: str | None = None, proxies_file: str | None = None) -> list[str]:
    proxies: list[str] = []

    file_path = proxies_file or os.environ.get("GNEWS_PROXIES_FILE", "proxies.txt")
    if Path(file_path).exists():
        for line in Path(file_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                proxies.append(line)

    if proxy_arg:
        proxies.append(proxy_arg)
    elif os.environ.get("GNEWS_PROXY"):
        proxies.append(os.environ["GNEWS_PROXY"])

    seen: set[str] = set()
    unique: list[str] = []
    for p in proxies:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def build_search_url(query: str) -> str:
    encoded = quote_plus(query)
    return f"https://news.google.com/search?q={encoded}&hl=ru&gl=RU&ceid=RU:ru"


def build_rss_url(query: str) -> str:
    encoded = quote_plus(query)
    return f"https://news.google.com/rss/search?q={encoded}&hl=ru&gl=RU&ceid=RU:ru"


def fetch_page(url: str, rotator: ProxyRotator | None = None, session: requests.Session | None = None) -> str:
    if rotator and rotator.available:
        return rotator.fetch(url)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
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


def extract_articles_list(data: list[Any]) -> list[Any]:
    if not data or len(data) < 2:
        return []
    articles_raw = data[1]
    if not isinstance(articles_raw, list) or not articles_raw:
        return []

    candidates: list[Any] = []
    for block in articles_raw:
        if not isinstance(block, list):
            continue
        for item in block:
            if _looks_like_article_wrapper(item):
                candidates.append(item)

    if candidates:
        return candidates

    first = articles_raw[0]
    if isinstance(first, list):
        return [item for item in first if _looks_like_article_wrapper(item)]
    return []


def _looks_like_article_wrapper(item: Any) -> bool:
    if not isinstance(item, list):
        return False
    article_data = _unwrap_article_data(item)
    return (
        isinstance(article_data, list)
        and len(article_data) > 6
        and isinstance(article_data[2], str)
        and bool(article_data[2].strip())
    )


def _unwrap_article_data(article_wrapper: list) -> list | None:
    if not article_wrapper:
        return None
    if (
        isinstance(article_wrapper[0], int)
        or (
            isinstance(article_wrapper[0], list)
            and article_wrapper[0]
            and isinstance(article_wrapper[0][0], int)
        )
    ):
        if isinstance(article_wrapper[0], list):
            return article_wrapper[0]
        return article_wrapper
    if isinstance(article_wrapper[0], list):
        return article_wrapper[0]
    return None


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


def format_rss_date(pub_date: str | None) -> str:
    if not pub_date:
        return "дата недоступна"
    try:
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    except (TypeError, ValueError):
        return pub_date


def detect_cluster(token: str) -> tuple[bool, int]:
    if not token:
        return False, 0
    parts = re.findall(r"CBM[a-zA-Z0-9_-]+", token)
    if len(parts) > 1:
        return True, len(parts)
    if len(token) > 200:
        return True, 2
    return False, 0


def extract_snippet_from_html(article_data: list) -> str:
    title = article_data[2] if len(article_data) > 2 else ""
    for val in article_data:
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


def extract_snippet_from_rss(description: str | None, title: str) -> str:
    if not description:
        return "сниппет Google недоступен"
    text = re.sub(r"<[^>]+>", " ", description)
    text = unescape(re.sub(r"\s+", " ", text)).strip()
    source_suffix = re.search(r"\s{2,}(.+)$", text)
    if source_suffix:
        text = text[: source_suffix.start()].strip()
    if text == title or len(text) < 10:
        return "сниппет Google недоступен"
    return text


def decode_google_url(google_url: str, session: requests.Session) -> str:
    if not google_url or "news.google.com" not in google_url:
        return google_url
    try:
        result = gnewsdecoder(google_url, interval=0.5)
        if isinstance(result, dict) and result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
    except Exception:
        pass
    return google_url


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


def parse_html_article(
    article_wrapper: list,
    query: str,
    position: int,
) -> NewsResult | None:
    article_data = _unwrap_article_data(article_wrapper)
    if not article_data or len(article_data) < 7:
        return None

    title = article_data[2] if isinstance(article_data[2], str) else ""
    if not title:
        return None

    title = unescape(title)
    url = article_data[6] if isinstance(article_data[6], str) else ""
    if not url and len(article_data) > 38 and isinstance(article_data[38], str):
        url = article_data[38]

    source = ""
    if len(article_data) > 10 and isinstance(article_data[10], list) and len(article_data[10]) > 2:
        source = article_data[10][2] if isinstance(article_data[10][2], str) else ""

    token = ""
    if len(article_data) > 1 and isinstance(article_data[1], list) and len(article_data[1]) > 1:
        token = article_data[1][1] if isinstance(article_data[1][1], str) else ""

    is_cluster, cluster_count = detect_cluster(token)

    return NewsResult(
        query=query,
        position=position,
        title=title,
        source=source,
        url=url,
        publication_date=format_timestamp(article_data[4] if len(article_data) > 4 else None),
        snippet=extract_snippet_from_html(article_data),
        domain=extract_domain(url),
        result_type=determine_result_type(title, source, is_cluster),
        relevance_comment=assess_relevance(query, title, source),
        is_cluster=is_cluster,
        cluster_sources=[f"кластер из ~{cluster_count} публикаций"] if is_cluster else [],
        data_source="html",
        google_url="",
    )


def parse_rss_item(
    item: ET.Element,
    query: str,
    position: int,
    session: requests.Session,
    decode_urls: bool = True,
) -> NewsResult | None:
    ns = {"media": "http://search.yahoo.com/mrss/"}
    title_el = item.find("title")
    link_el = item.find("link")
    pub_el = item.find("pubDate")
    desc_el = item.find("description")
    source_el = item.find("source")
    guid_el = item.find("guid")

    title_raw = title_el.text if title_el is not None and title_el.text else ""
    if not title_raw:
        return None

    title = unescape(title_raw)
    if " - " in title:
        title = title.rsplit(" - ", 1)[0].strip()

    google_url = link_el.text.strip() if link_el is not None and link_el.text else ""
    source = source_el.text.strip() if source_el is not None and source_el.text else ""
    pub_date = format_rss_date(pub_el.text if pub_el is not None else None)
    description = desc_el.text if desc_el is not None else ""
    snippet = extract_snippet_from_rss(description, title)

    guid = guid_el.text if guid_el is not None and guid_el.text else ""
    is_cluster, cluster_count = detect_cluster(guid)

    url = google_url
    if decode_urls and google_url:
        url = decode_google_url(google_url, session)

    return NewsResult(
        query=query,
        position=position,
        title=title,
        source=source,
        url=url,
        publication_date=pub_date,
        snippet=snippet,
        domain=extract_domain(url),
        result_type=determine_result_type(title, source, is_cluster),
        relevance_comment=assess_relevance(query, title, source),
        is_cluster=is_cluster,
        cluster_sources=[f"кластер из ~{cluster_count} публикаций"] if is_cluster else [],
        data_source="rss",
        google_url=google_url,
    )


def normalize_url_key(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return f"{parsed.netloc}{path}".lower()


def fetch_html_results(
    query: str,
    rotator: ProxyRotator | None,
    limit: int = 100,
) -> list[NewsResult]:
    try:
        html = fetch_page(build_search_url(query), rotator=rotator)
        data = extract_init_data(html)
        if not data:
            return []

        results: list[NewsResult] = []
        seen_urls: set[str] = set()

        for article_wrapper in extract_articles_list(data):
            if len(results) >= limit:
                break
            result = parse_html_article(article_wrapper, query, len(results) + 1)
            if not result or not result.url:
                continue
            key = normalize_url_key(result.url)
            if key in seen_urls:
                continue
            seen_urls.add(key)
            results.append(result)
        return results
    except requests.RequestException as exc:
        print(f"  [!] HTML-источник недоступен: {exc}", file=sys.stderr)
        return []


def fetch_rss_results(
    query: str,
    rotator: ProxyRotator | None,
    limit: int = 100,
    decode_urls: bool = True,
) -> list[NewsResult]:
    results: list[NewsResult] = []
    seen_urls: set[str] = set()
    try:
        xml_text = fetch_page(build_rss_url(query), rotator=rotator)
        root = ET.fromstring(xml_text)
        session = rotator.create_session() if rotator and rotator.available else requests.Session()

        for item in root.findall(".//item"):
            if len(results) >= limit:
                break
            result = parse_rss_item(item, query, 0, session, decode_urls=decode_urls)
            if not result:
                continue
            key = normalize_url_key(result.url) or result.google_url
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            results.append(result)
    except (requests.RequestException, ET.ParseError) as exc:
        print(f"  [!] RSS-источник недоступен: {exc}", file=sys.stderr)
    return results


def merge_results(
    html_results: list[NewsResult],
    rss_results: list[NewsResult],
    max_results: int,
) -> tuple[list[NewsResult], int]:
    seen: set[str] = set()
    merged: list[NewsResult] = []

    for result in html_results:
        key = normalize_url_key(result.url)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(result)

    for result in rss_results:
        key = normalize_url_key(result.url) or result.google_url
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(result)

    available = len(merged)
    return reindex_results(merged[:max_results]), available


def reindex_results(results: list[NewsResult]) -> list[NewsResult]:
    for idx, result in enumerate(results, start=1):
        result.position = idx
    return results


def build_coverage_note(
    query: str,
    collected: int,
    requested: int,
    available: int,
    html_count: int,
    rss_count: int,
) -> str:
    if collected >= requested:
        return f"Собрано {collected} из {requested} запрошенных (всего доступно в выдаче: {available})."

    if collected == 0:
        return "Google News не вернул результатов. Проверьте прокси и доступность сервиса."

    if collected >= available:
        return (
            f"Собраны все {collected} доступных результатов по запросу «{query}». "
            f"В выдаче Google News меньше {requested} уникальных публикаций "
            f"(HTML: {html_count}, RSS: {rss_count}, всего уникальных: {available})."
        )

    return (
        f"Собрано {collected} из {available} доступных "
        f"(запрошено до {requested}). HTML: {html_count}, RSS: {rss_count}."
    )


def parse_google_news(
    query: str,
    rotator: ProxyRotator | None,
    max_results: int = 20,
    decode_urls: bool = True,
) -> tuple[list[NewsResult], QueryCoverage]:
    fetch_limit = max(max_results, 100)
    html_results = fetch_html_results(query, rotator, limit=fetch_limit)
    rss_results = fetch_rss_results(query, rotator, limit=fetch_limit, decode_urls=decode_urls)

    results, available = merge_results(html_results, rss_results, max_results)
    collected = len(results)

    is_complete = collected >= max_results or collected >= available
    note = build_coverage_note(
        query, collected, max_results, available,
        len(html_results), len(rss_results),
    )

    proxy_used = ""
    if rotator and rotator.available:
        proxy_used = rotator.next_proxy() or ""

    coverage = QueryCoverage(
        query=query,
        requested=max_results,
        collected=collected,
        available=available,
        html_count=len(html_results),
        rss_count=len(rss_results),
        is_complete=is_complete,
        note=note,
        proxy_used=proxy_used,
    )
    return results, coverage


def generate_analytics(
    all_results: list[NewsResult],
    coverages: list[QueryCoverage],
) -> dict[str, Any]:
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
    repeated_domains = {d: c for d, c in domain_counter.items() if c > 1}

    return {
        "dominant_sources": source_counter.most_common(10),
        "repeated_domains": repeated_domains,
        "themes": dict(themes.most_common()),
        "sentiment_distribution": dict(sentiments),
        "most_visible": [
            {"position": r.position, "query": r.query, "title": r.title, "source": r.source, "url": r.url}
            for r in sorted(all_results, key=lambda x: (x.query, x.position))[:8]
        ],
        "info_background_sources": list({
            r.source for r in all_results
            if any(w in r.title.lower() for w in ["мошенник", "шарашкин", "блок", "отзыв", "разводят"])
        }),
        "total_results": len(all_results),
        "unique_domains": len(domain_counter),
        "unique_sources": len(source_counter),
        "clusters_count": sum(1 for r in all_results if r.is_cluster),
        "coverage": [asdict(c) for c in coverages],
        "incomplete_queries": [c.query for c in coverages if not c.is_complete],
    }


def export_to_xlsx(
    results: list[NewsResult],
    analytics: dict[str, Any],
    coverages: list[QueryCoverage],
    output_path: str,
    check_date: str,
) -> None:
    wb = Workbook()

    ws = wb.active
    ws.title = "Результаты"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E79")
    link_font = Font(color="0563C1", underline="single")

    headers = [col[0] for col in XLSX_COLUMNS]
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    url_col_idx = next(i for i, (_, key, _) in enumerate(XLSX_COLUMNS, start=1) if key == "url")

    for row_idx, result in enumerate(results, start=2):
        row_values = []
        for _, key, _ in XLSX_COLUMNS:
            value = getattr(result, key)
            if key == "is_cluster":
                value = "Да" if value else "Нет"
            row_values.append(value)
        ws.append(row_values)

        url = result.url
        if url:
            url_cell = ws.cell(row=row_idx, column=url_col_idx)
            url_cell.hyperlink = Hyperlink(ref=url_cell.coordinate, target=url)
            url_cell.font = link_font
            url_cell.value = url

    for col_idx, (_, _, width) in enumerate(XLSX_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(results) + 1}"

    ws_meta = wb.create_sheet("Метаданные")
    meta_rows = [
        ("Дата проверки", check_date),
        ("Регион", "Россия (RU)"),
        ("Язык", "русский"),
        ("Всего результатов", len(results)),
        ("Запросов", len(coverages)),
        ("Запросов с полным покрытием", len([c for c in coverages if c.is_complete])),
    ]
    for label, value in meta_rows:
        ws_meta.append([label, value])
    ws_meta.column_dimensions["A"].width = 28
    ws_meta.column_dimensions["B"].width = 50

    ws_cov = wb.create_sheet("Покрытие запросов")
    ws_cov.append([
        "Запрос", "Запрошено", "Собрано", "Доступно в выдаче",
        "HTML", "RSS", "Полное покрытие", "Комментарий",
    ])
    for col_idx in range(1, 9):
        ws_cov.cell(row=1, column=col_idx).font = header_font
        ws_cov.cell(row=1, column=col_idx).fill = header_fill
    for cov in coverages:
        ws_cov.append([
            cov.query, cov.requested, cov.collected, cov.available,
            cov.html_count, cov.rss_count,
            "Да" if cov.is_complete else "Нет",
            cov.note,
        ])
    for col_idx, width in enumerate([40, 12, 12, 16, 10, 10, 16, 80], start=1):
        ws_cov.column_dimensions[get_column_letter(col_idx)].width = width

    ws_an = wb.create_sheet("Аналитика")
    ws_an.append(["Раздел", "Показатель", "Значение"])
    for col_idx in range(1, 4):
        ws_an.cell(row=1, column=col_idx).font = header_font
        ws_an.cell(row=1, column=col_idx).fill = header_fill

    def add_section(title: str, rows: list[tuple[str, Any]]) -> None:
        for key, val in rows:
            ws_an.append([title, key, val])

    add_section("Общее", [
        ("Всего публикаций", analytics.get("total_results", 0)),
        ("Уникальных доменов", analytics.get("unique_domains", 0)),
        ("Уникальных СМИ", analytics.get("unique_sources", 0)),
        ("Кластеров Google", analytics.get("clusters_count", 0)),
    ])

    for source, count in analytics.get("dominant_sources", []):
        add_section("Доминирующие СМИ", [(source, count)])

    for domain, count in analytics.get("repeated_domains", {}).items():
        add_section("Повторяющиеся домены", [(domain, f"{count} раз")])

    for theme, count in analytics.get("themes", {}).items():
        add_section("Темы", [(theme, count)])

    for sentiment, count in analytics.get("sentiment_distribution", {}).items():
        add_section("Тональность", [(sentiment, count)])

    for item in analytics.get("most_visible", []):
        add_section("Заметные публикации", [
            (f"Поз.{item['position']} [{item['query'][:20]}]", item["title"][:80]),
        ])

    for source in analytics.get("info_background_sources", []):
        add_section("Информационный фон", [(source, "критический/фоновый источник")])

    ws_an.column_dimensions["A"].width = 24
    ws_an.column_dimensions["B"].width = 45
    ws_an.column_dimensions["C"].width = 60

    if analytics.get("incomplete_queries"):
        ws_lim = wb.create_sheet("Ограничения")
        ws_lim.append(["Запрос", "Комментарий"])
        ws_lim.cell(row=1, column=1).font = header_font
        ws_lim.cell(row=1, column=2).font = header_font
        ws_lim.cell(row=1, column=1).fill = header_fill
        ws_lim.cell(row=1, column=2).fill = header_fill

        for cov in coverages:
            if not cov.is_complete:
                ws_lim.append([cov.query, cov.note])
        ws_lim.column_dimensions["A"].width = 45
        ws_lim.column_dimensions["B"].width = 90

    wb.save(output_path)


def run_parser(
    queries: list[str] | None = None,
    max_results: int = 20,
    output_xlsx: str = "results.xlsx",
    output_json: str | None = None,
    proxy: str | None = None,
    proxies_file: str | None = None,
    decode_urls: bool = True,
) -> list[NewsResult]:
    queries = queries or DEFAULT_QUERIES
    proxy_list = load_proxy_list(proxy, proxies_file)
    rotator = ProxyRotator(proxy_list) if proxy_list else None

    all_results: list[NewsResult] = []
    coverages: list[QueryCoverage] = []
    check_date = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    print(f"Дата проверки: {check_date}")
    print(f"Регион: Россия (RU), язык: русский")
    if rotator and rotator.available:
        print(f"Прокси: {rotator.count} шт. (ротация включена)")
    else:
        print("Прокси: не используются")
    print()

    for query in queries:
        print(f"Парсинг запроса: «{query}»...")
        results, coverage = parse_google_news(
            query, rotator, max_results=max_results, decode_urls=decode_urls,
        )
        all_results.extend(results)
        coverages.append(coverage)

        if coverage.collected >= coverage.requested:
            status = "OK"
        elif coverage.collected >= coverage.available:
            status = "ВСЕ"
        else:
            status = "ЧАСТИЧНО"

        print(
            f"  [{status}] Собрано: {coverage.collected}/{coverage.requested} "
            f"(доступно: {coverage.available}, HTML: {coverage.html_count}, RSS: {coverage.rss_count})"
        )
        if coverage.collected < coverage.requested:
            print(f"  → {coverage.note}")
        time.sleep(1.0)

    analytics = generate_analytics(all_results, coverages)

    export_to_xlsx(all_results, analytics, coverages, output_xlsx, check_date)
    print(f"\nXLSX сохранён: {output_xlsx} ({len(all_results)} строк)")

    if output_json:
        output_data = {
            "check_date": check_date,
            "region": "RU",
            "language": "ru",
            "proxies_used": rotator.count if rotator else 0,
            "queries": queries,
            "results": [asdict(r) for r in all_results],
            "analytics": analytics,
            "coverage": [asdict(c) for c in coverages],
        }
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"JSON сохранён: {output_json}")

    return all_results


def main():
    parser = argparse.ArgumentParser(description="Парсер Google News → XLSX")
    parser.add_argument("--queries", nargs="+", default=DEFAULT_QUERIES)
    parser.add_argument("--max-results", type=int, default=20)
    parser.add_argument("--output-xlsx", default="results.xlsx", help="Путь к XLSX-файлу")
    parser.add_argument("--output-json", default=None, help="Опциональный JSON-дамп")
    parser.add_argument(
        "--proxy",
        default=None,
        help="Один прокси (http://user:pass@host:port)",
    )
    parser.add_argument(
        "--proxies-file",
        default="proxies.txt",
        help="Файл со списком прокси (по одному на строку)",
    )
    parser.add_argument(
        "--no-decode-urls",
        action="store_true",
        help="Не декодировать Google URL в прямые ссылки издателя",
    )
    args = parser.parse_args()

    run_parser(
        queries=args.queries,
        max_results=args.max_results,
        output_xlsx=args.output_xlsx,
        output_json=args.output_json,
        proxy=args.proxy,
        proxies_file=args.proxies_file,
        decode_urls=not args.no_decode_urls,
    )


if __name__ == "__main__":
    main()
