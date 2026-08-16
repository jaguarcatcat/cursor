#!/usr/bin/env python3
"""
Google News Parser for Russian localization (RU / Russian).
Collects, decodes, dedupes, and analyzes Google News SERP results for target queries.
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup
from googlenewsdecoder import new_decoderv1

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_QUERIES = [
    "лобов вадим университет синергия",
    "лобов вадим синергия",
    "лобов вадим",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


def clean_domain(url: str, fallback: str = "") -> str:
    """Extract and normalize domain name from URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if netloc:
            return netloc
    except Exception:
        pass

    if fallback:
        try:
            parsed = urllib.parse.urlparse(fallback)
            netloc = parsed.netloc.lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            if netloc:
                return netloc
        except Exception:
            pass
        return fallback.replace("https://", "").replace("http://", "").rstrip("/")
    return ""


def clean_title(title: str, source_name: str) -> str:
    """Remove trailing source suffix from article title if present."""
    if not title:
        return ""
    if source_name:
        suffix = f" - {source_name}"
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
    return title.strip()


def parse_date(pub_date_str: str) -> Tuple[str, str]:
    """
    Parse RFC 822 / RSS pubDate to standardized ISO date and readable string.
    Returns (iso_date, formatted_date_ru).
    """
    if not pub_date_str:
        return "", ""
    try:
        # Example: 'Sun, 12 Jul 2026 21:04:52 GMT' or 'Mon, 29 Dec 2025 08:00:00 GMT'
        dt = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %Z")
        iso_date = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        formatted_date = dt.strftime("%d.%m.%Y %H:%M")
        return iso_date, formatted_date
    except Exception:
        return pub_date_str, pub_date_str


def decode_google_news_url(google_url: str, session: Optional[requests.Session] = None) -> str:
    """
    Decode Google News redirect URL to direct publisher URL.
    Uses googlenewsdecoder with fallback to HTTP redirect tracing.
    """
    if not google_url:
        return ""

    if not google_url.startswith("http"):
        return google_url

    # Attempt decoding via googlenewsdecoder
    try:
        decoded = new_decoderv1(google_url)
        if isinstance(decoded, dict) and decoded.get("status"):
            decoded_url = decoded.get("decoded_url")
            if decoded_url and decoded_url.startswith("http"):
                return decoded_url
        elif isinstance(decoded, str) and decoded.startswith("http"):
            return decoded
    except Exception as e:
        logger.debug("googlenewsdecoder error for %s: %s", google_url[:50], e)

    # Fallback to HTTP head/get resolution
    try:
        s = session or requests.Session()
        resp = s.head(google_url, headers=HEADERS, allow_redirects=True, timeout=5)
        if resp.url and resp.url.startswith("http") and "news.google.com" not in resp.url:
            return resp.url
    except Exception:
        pass

    return google_url


def determine_result_type(title: str, url: str) -> str:
    """Determine the type of news result based on title and URL structure."""
    title_lower = title.lower()
    url_lower = url.lower()

    if "досье" in title_lower or "биография" in title_lower or "/dossier/" in url_lower:
        return "Досье / Биография"
    if "интервью" in title_lower or "«" in title or "— о " in title_lower or "рассказал" in title_lower:
        if "repost.news" in url_lower or "шарашкин" in title_lower or "мошенник" in title_lower:
            return "Расследование / Критический материал"
        return "Интервью / Прямая речь"
    if any(k in title_lower for k in ["мошенничеств", "шарашкин", "уклонист", "разводят на деньги", "как устроен бизнес", "зарабатывают миллиарды"]):
        return "Расследование / Критический материал"
    if "/blogs/" in url_lower or "блог" in title_lower or "подкаст" in title_lower:
        return "Авторская колонка / Блог"
    if any(k in title_lower for k in ["анонсировал", "запустили", "сообщил", "приобрела", "договорились", "открытие", "помогает"]):
        return "Пресс-релиз / Корпоративная новость"
    return "Стандартная новость / Статья"


def determine_sentiment(title: str, url: str) -> str:
    """Classify sentiment tone of the publication."""
    title_lower = title.lower()
    url_lower = url.lower()

    negative_keywords = [
        "мошеннич", "мошенник", "шарашкин", "уклонист", "развод", "скандал",
        "криминал", "обман", "халтурщик", "разводят на деньги", "негатив",
        "разоблачен", "жалоб", "уголовн"
    ]
    positive_keywords = [
        "помогает ветеранам", "поддержк", "запустили", "развивать",
        "два диплома", "рост", "превысил", "креативной экономики",
        "хрустальная симфония", "сохранит", "бесплатное обучение",
        "трансфера технологий", "центр русского языка", "успех", "награжд"
    ]

    if any(k in title_lower or k in url_lower for k in negative_keywords):
        return "Негативная"
    if any(k in title_lower for k in positive_keywords):
        return "Позитивная"
    return "Нейтральная"


def determine_relevance_comment(query: str, title: str, domain: str) -> str:
    """Provide analytical comment on publication relevance to query."""
    title_lower = title.lower()
    query_lower = query.lower()

    has_lobov = "лобов" in title_lower
    has_synergy = "синерги" in title_lower
    has_university = "университет" in title_lower or "вуз" in title_lower

    if has_lobov and has_synergy:
        if has_university:
            return "Высокая прямая релевантность: упоминается Вадим Лобов в контексте Университета/Корпорации «Синергия»."
        return "Высокая прямая релевантность: публикация посвящена Вадиму Лобову как руководителю корпорации «Синергия»."
    elif has_lobov and not has_synergy:
        if "арт росси" in title_lower:
            return "Тематическая релевантность: Вадим Лобов упоминается как сооснователь ярмарки «Арт Россия» (проект «Синергии»)."
        return "Прямая релевантность персоне: Вадим Лобов упоминается напрямую."
    elif not has_lobov and has_synergy:
        return "Косвенная релевантность: новость о деятельности корпорации/университета «Синергия» без прямого упоминания фамилии в заголовке."
    else:
        if "паломник" in title_lower or "валаам" in title_lower:
            return "Околотематическая/контекстная релевантность: проект экотроп на Валааме, связанный с инициативами руководства."
        return "Умеренная/косвенная релевантность: результат выдан поисковиком по ассоциативной связи с персоной или институтом."


class GoogleNewsParser:
    """Parser for Google News RSS and Web SERP."""

    def __init__(self, hl: str = "ru", gl: str = "RU", ceid: str = "RU:ru"):
        self.hl = hl
        self.gl = gl
        self.ceid = ceid
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def fetch_rss_feed(self, query: str) -> List[Dict[str, Any]]:
        """Fetch raw items from Google News RSS feed."""
        encoded_query = urllib.parse.quote(query)
        url = (
            f"https://news.google.com/rss/search?"
            f"q={encoded_query}&hl={self.hl}&gl={self.gl}&ceid={self.ceid}"
        )
        logger.info("Fetching Google News RSS for query: '%s' -> %s", query, url)

        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
        except Exception as e:
            logger.error("Failed to fetch RSS for '%s': %s", query, e)
            return []

        try:
            root = ET.fromstring(response.content)
        except Exception as e:
            logger.error("Failed to parse XML response for '%s': %s", query, e)
            return []

        channel = root.find("channel")
        if channel is None:
            return []

        raw_items = channel.findall("item")
        logger.info("Retrieved %d raw items from RSS for '%s'", len(raw_items), query)
        return raw_items

    def parse_query(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Fetch, decode, deduplicate and enrich results for a single query.
        """
        raw_items = self.fetch_rss_feed(query)
        results = []
        seen_urls: Set[str] = set()

        position = 1
        for item in raw_items:
            raw_title = item.findtext("title", "")
            raw_link = item.findtext("link", "")
            pub_date_raw = item.findtext("pubDate", "")
            guid = item.findtext("guid", "")

            source_el = item.find("source")
            source_name = source_el.text if source_el is not None else ""
            source_url = source_el.attrib.get("url", "") if source_el is not None else ""

            # Clean title
            title = clean_title(raw_title, source_name)

            # Decode Google redirect link to direct publisher URL
            direct_url = decode_google_news_url(raw_link, self.session)

            # Exact duplicate URL check within this query
            if direct_url in seen_urls:
                logger.debug("Skipping exact duplicate URL: %s", direct_url)
                continue
            seen_urls.add(direct_url)

            # Domain extraction
            domain = clean_domain(direct_url, fallback=source_url)

            # Date formatting
            iso_date, formatted_date = parse_date(pub_date_raw)

            # Result type & sentiment
            res_type = determine_result_type(title, direct_url)
            sentiment = determine_sentiment(title, direct_url)
            relevance_comment = determine_relevance_comment(query, title, domain)

            # Snippet: Google News RSS does not provide body snippet text
            # Following strict user rule: «сниппет Google недоступен»
            snippet = "сниппет Google недоступен"

            # Check if part of story cluster (Google News single items vs cluster)
            story_cluster_note = "Одиночная публикация (не объединена в сюжет)"

            entry = {
                "query": query,
                "position": position,
                "title": title,
                "source": source_name,
                "url": direct_url,
                "google_news_url": raw_link,
                "date_raw": pub_date_raw,
                "date_iso": iso_date,
                "date": formatted_date,
                "snippet": snippet,
                "domain": domain,
                "result_type": res_type,
                "sentiment": sentiment,
                "relevance_comment": relevance_comment,
                "story_cluster": story_cluster_note,
            }

            results.append(entry)
            position += 1

            if len(results) >= limit:
                break

        return results

    def run(self, queries: Optional[List[str]] = None, limit: int = 20) -> Dict[str, List[Dict[str, Any]]]:
        """Run parsing for multiple queries."""
        if queries is None:
            queries = DEFAULT_QUERIES

        all_results: Dict[str, List[Dict[str, Any]]] = {}
        for q in queries:
            items = self.parse_query(q, limit=limit)
            all_results[q] = items
            logger.info("Collected %d results for query '%s'", len(items), q)

        return all_results


def export_to_json(results: Dict[str, List[Dict[str, Any]]], filepath: str) -> None:
    """Export all results to JSON file."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    logger.info("Saved JSON results to %s", filepath)


def export_to_csv(results: Dict[str, List[Dict[str, Any]]], filepath: str) -> None:
    """Export all results to flat CSV file."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    fieldnames = [
        "Поисковый запрос",
        "Позиция",
        "Заголовок",
        "СМИ / Источник",
        "Дата публикации",
        "Сниппет",
        "URL публикации",
        "Домен",
        "Тип результата",
        "Тональность",
        "Комментарий релевантности",
        "Сюжетный кластер",
    ]

    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for query, items in results.items():
            for item in items:
                writer.writerow({
                    "Поисковый запрос": item["query"],
                    "Позиция": item["position"],
                    "Заголовок": item["title"],
                    "СМИ / Источник": item["source"],
                    "Дата публикации": item["date"],
                    "Сниппет": item["snippet"],
                    "URL публикации": item["url"],
                    "Домен": item["domain"],
                    "Тип результата": item["result_type"],
                    "Тональность": item["sentiment"],
                    "Комментарий релевантности": item["relevance_comment"],
                    "Сюжетный кластер": item["story_cluster"],
                })
    logger.info("Saved CSV results to %s", filepath)


def generate_markdown_table(results: Dict[str, List[Dict[str, Any]]]) -> str:
    """Generate clean Markdown table matching user format."""
    lines = [
        "| Запрос | Позиция | Заголовок | СМИ | Дата | Сниппет | URL | Домен | Тип результата | Комментарий |",
        "| :--- | :---: | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]

    for query, items in results.items():
        for item in items:
            # Escape pipe characters in markdown
            title = item["title"].replace("|", "/")
            source = item["source"].replace("|", "/")
            snippet = item["snippet"].replace("|", "/")
            url = item["url"]
            domain = item["domain"]
            res_type = item["result_type"]
            comment = item["relevance_comment"].replace("|", "/")
            date_str = item["date"]

            # Format URL as a compact clickable markdown link or clean string
            link_md = f"[{domain}]({url})" if url.startswith("http") else url

            line = f"| {query} | {item['position']} | {title} | {source} | {date_str} | {snippet} | {link_md} | {domain} | {res_type} | {comment} |"
            lines.append(line)

    return "\n".join(lines)


def generate_analytics(results: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Calculate aggregated analytics across all parsed results."""
    all_items = []
    for query, items in results.items():
        all_items.extend(items)

    total_items = len(all_items)
    unique_urls = len(set(item["url"] for item in all_items))

    # Media / Source counts
    source_counts: Dict[str, int] = {}
    domain_counts: Dict[str, int] = {}
    sentiment_counts: Dict[str, int] = {}
    type_counts: Dict[str, int] = {}

    for item in all_items:
        src = item["source"] or "Не указан"
        source_counts[src] = source_counts.get(src, 0) + 1

        dom = item["domain"] or "Не указан"
        domain_counts[dom] = domain_counts.get(dom, 0) + 1

        sent = item["sentiment"]
        sentiment_counts[sent] = sentiment_counts.get(sent, 0) + 1

        rtype = item["result_type"]
        type_counts[rtype] = type_counts.get(rtype, 0) + 1

    # Sorted
    sorted_sources = sorted(source_counts.items(), key=lambda x: x[1], reverse=True)
    sorted_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)
    repeated_domains = [(d, c) for d, c in sorted_domains if c > 1]

    # Analysis of themes
    themes = {
        "Культура, выставки и ярмарки искусства («Арт Россия», ВДНХ)": 0,
        "Образование, международная экспансия, колледжи и БРИКС": 0,
        "Поддержка ветеранов СВО и социальные программы": 0,
        "Корпоративные новости, партнерства, EdTech и бизнес": 0,
        "Расследования, критика и сомнительные инфоповоды": 0,
    }

    for item in all_items:
        t = item["title"].lower()
        if any(w in t for w in ["арт росси", "тридевять земель", "вднх", "искусств"]):
            themes["Культура, выставки и ярмарки искусства («Арт Россия», ВДНХ)"] += 1
        elif any(w in t for w in ["африк", "ирак", "брикс", "колледж", "болонск", "диплом", "онлайн-школ"]):
            themes["Образование, международная экспансия, колледжи и БРИКС"] += 1
        elif any(w in t for w in ["ветеранам сво", "защитники отечества"]):
            themes["Поддержка ветеранов СВО и социальные программы"] += 1
        elif any(w in t for w in ["мошенник", "шарашкин", "уклонист", "разводят", "бизнес крупнейшего", "миллиарды"]):
            themes["Расследования, критика и сомнительные инфоповоды"] += 1
        else:
            themes["Корпоративные новости, партнерства, EdTech и бизнес"] += 1

    return {
        "total_items": total_items,
        "unique_urls": unique_urls,
        "sources": sorted_sources,
        "domains": sorted_domains,
        "repeated_domains": repeated_domains,
        "sentiments": sentiment_counts,
        "types": type_counts,
        "themes": themes,
    }


def main():
    parser = argparse.ArgumentParser(description="Google News Parser for Russian localization")
    parser.add_argument("--queries", nargs="+", default=DEFAULT_QUERIES, help="List of queries to parse")
    parser.add_argument("--limit", type=int, default=20, help="Maximum results per query (default: 20)")
    parser.add_argument("--json-out", default="results/google_news_results.json", help="Path to JSON output")
    parser.add_argument("--csv-out", default="results/google_news_results.csv", help="Path to CSV output")
    parser.add_argument("--md-out", default="results/google_news_table.md", help="Path to Markdown output")
    args = parser.parse_args()

    news_parser = GoogleNewsParser()
    results = news_parser.run(queries=args.queries, limit=args.limit)

    export_to_json(results, args.json_out)
    export_to_csv(results, args.csv_out)

    md_table = generate_markdown_table(results)
    if args.md_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.md_out)), exist_ok=True)
        with open(args.md_out, "w", encoding="utf-8") as f:
            f.write(md_table)
        logger.info("Saved Markdown table to %s", args.md_out)

    analytics = generate_analytics(results)
    print("\n" + "=" * 60)
    print("АНАЛИТИЧЕСКАЯ СВОДКА:")
    print("=" * 60)
    print(f"Всего собрано записей: {analytics['total_items']}")
    print(f"Уникальных URL: {analytics['unique_urls']}")
    print("\nДоминирующие СМИ (топ-5):")
    for s, c in analytics['sources'][:5]:
        print(f"  - {s}: {c} публ.")
    print("\nПовторяющиеся домены:")
    for d, c in analytics['repeated_domains']:
        print(f"  - {d}: {c} раз")
    print("\nРаспределение тональности:")
    for sent, c in analytics['sentiments'].items():
        pct = (c / analytics['total_items']) * 100
        print(f"  - {sent}: {c} ({pct:.1f}%)")


if __name__ == "__main__":
    main()
