#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Парсер выдачи Google News (Google Новости) по заданным поисковым запросам.

Скрипт открывает реальную страницу https://news.google.com/search?... в headless
Chrome (через Playwright), дожидается клиентского рендеринга результатов
(Google News — это SPA) и извлекает из DOM структурированные данные по каждой
публикации: заголовок, СМИ/источник, дату публикации, реальный URL публикации
(Google в разметке хранит его в base64-кодированном виде внутри атрибута
jslog — сам скрипт декодирует его без дополнительных сетевых запросов),
домен и признак объединения нескольких публикаций в один сюжет.

ВАЖНО про сниппеты:
Веб-интерфейс Google Новости (в отличие от обычной органической выдачи
google.com/search) НЕ показывает текстовый сниппет/описание под заголовком —
только заголовок, источник, время. Поэтому в колонку "Сниппет" скрипт
принципиально не подставляет самостоятельно придуманное описание, а пишет
"сниппет Google недоступен", как и требуется в ТЗ.

Использование:
    python3 parse_google_news.py \
        --query "лобов вадим университет синергия" \
        --query "лобов вадим синергия" \
        --query "лобов вадим" \
        --depth 20 \
        --output-dir output

Результат:
    - output/google_news_results_<timestamp>.csv  — все собранные результаты
    - output/google_news_results_<timestamp>.json — те же данные в JSON
"""

import argparse
import base64
import csv
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    print(
        "Не найден пакет playwright. Установите зависимости командой:\n"
        "    pip install -r requirements.txt\n"
        "    playwright install chromium\n"
        "(или укажите путь к системному Chrome через --chrome-path)",
        file=sys.stderr,
    )
    raise

NEWS_SEARCH_URL = "https://news.google.com/search"

# JS, который выполняется в контексте открытой страницы Google Новости и
# вытаскивает из отрендеренного DOM структурированные карточки результатов.
EXTRACT_JS = r"""
() => {
    function decodeJslogUrl(jslog) {
        if (!jslog) return null;
        const parts = jslog.split(';');
        for (const part of parts) {
            const m = part.trim().match(/^5:(.+)$/);
            if (m) {
                try {
                    const decoded = JSON.parse(atob(m[1]));
                    for (let i = decoded.length - 1; i >= 0; i--) {
                        if (typeof decoded[i] === 'string' && decoded[i].indexOf('http') === 0) {
                            return decoded[i];
                        }
                    }
                } catch (e) {
                    return null;
                }
            }
        }
        return null;
    }

    function extractTitleFromAria(item) {
        const btn = item.querySelector('button[aria-label^="Ещё - "]');
        if (btn) {
            let label = btn.getAttribute('aria-label').replace(/^Ещё - /, '');
            return label;
        }
        return null;
    }

    const cards = Array.from(document.querySelectorAll('c-wiz.PO9Zff'));
    const results = [];
    cards.forEach((card, cardIdx) => {
        let items = Array.from(card.querySelectorAll('.IFHyqb'));
        if (items.length === 0) {
            items = [card];
        }
        items.forEach((item, itemIdx) => {
            const titleA = item.querySelector('a.JtKRv');
            let title = titleA ? titleA.textContent.trim() : null;
            if (!title) {
                title = extractTitleFromAria(item);
            }
            const sourceEl = item.querySelector('.vr1PYe');
            const source = sourceEl ? sourceEl.textContent.trim() : null;
            const timeEl = item.querySelector('time.hvbAAd');
            const datetimeIso = timeEl ? timeEl.getAttribute('datetime') : null;
            const timeText = timeEl ? timeEl.textContent.trim() : null;
            const linkA = item.querySelector('a.WwrzSb');
            const url = linkA ? decodeJslogUrl(linkA.getAttribute('jslog')) : null;
            const googleHref = titleA ? titleA.getAttribute('href') : null;
            const authorEl = item.querySelector('.bInasb, .hMcpwd');
            const author = authorEl ? authorEl.textContent.trim() : null;
            results.push({
                cardIdx: cardIdx,
                itemIdx: itemIdx,
                itemsInCard: items.length,
                title: title,
                source: source,
                author: author,
                datetimeIso: datetimeIso,
                timeText: timeText,
                url: url,
                googleHref: googleHref,
            });
        });
    });
    return results;
}
"""


@dataclass
class NewsResult:
    query: str
    position: int
    sub_position: Optional[str]
    title: str
    source: Optional[str]
    published_at_iso: Optional[str]
    published_at_display: Optional[str]
    snippet: str
    url: Optional[str]
    domain: Optional[str]
    result_type: str
    is_clustered_story: bool
    cluster_size: int
    relevance_comment: str
    checked_at: str
    notes: str = ""


STOPWORDS_TITLE_NOISE = re.compile(r"\s+")


def normalize_domain(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return None
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or None


# Небольшой словарь доменов для эвристической классификации типа результата.
AGGREGATOR_DOMAINS = {"dzen.ru", "vc.ru", "pikabu.ru", "livejournal.com", "yandex.ru"}
VIDEO_DOMAINS = {"youtube.com", "rutube.ru", "youtu.be", "vk.com"}
LEGAL_DOMAINS = {"pravo.ru"}
BUSINESS_MEDIA_DOMAINS = {
    "forbes.ru", "rbc.ru", "kommersant.ru", "vedomosti.ru", "tadviser.ru",
}


def classify_result_type(title: str, source: Optional[str], domain: Optional[str]) -> str:
    t = (title or "").lower()
    src = (source or "").lower()
    dom = (domain or "").lower()

    if any(w in t for w in ["подкаст", "подкаста"]):
        return "Подкаст / аудиоформат"
    if dom in VIDEO_DOMAINS or "тв" == src.strip() or " тв" in src.lower():
        return "Видео / телесюжет"
    if "интервью" in t:
        return "Интервью"
    if dom in LEGAL_DOMAINS:
        return "Отраслевое СМИ (юридическая тематика)"
    if dom in BUSINESS_MEDIA_DOMAINS:
        return "Деловое СМИ — новость/статья"
    if dom in AGGREGATOR_DOMAINS:
        return "Блог-платформа / агрегатор пользовательского контента"
    if any(w in t for w in ["досье", "биография"]):
        return "Досье / биографическая справка"
    if any(w in t for w in ["мошенник", "мошенничество", "развод", "обман"]):
        return "Критический / расследовательский материал"
    return "Новостная статья / заметка СМИ"


NAME_STEM = "вадим"
SURNAME_STEM = "лобов"
SYNERGY_STEM = "синерг"
UNIVERSITY_STEMS = ["универ", "вуз", "институт", "образовательн"]


def assess_relevance(title: str, source: Optional[str]) -> str:
    t = (title or "").lower()
    has_surname = SURNAME_STEM in t
    has_name = NAME_STEM in t
    has_synergy = SYNERGY_STEM in t
    has_university = any(s in t for s in UNIVERSITY_STEMS)

    if has_surname and has_name and has_synergy:
        base = "Высокая: в заголовке явно упомянуты «Вадим Лобов» и «Синергия»."
    elif has_surname and has_synergy and not has_name:
        base = ("Высокая/средняя: упомянуты фамилия «Лобов» и «Синергия», "
                "но имя «Вадим» отсутствует в заголовке (может уточняться в тексте).")
    elif has_surname and has_name and not has_synergy:
        base = ("Средняя: упомянут «Вадим Лобов», но «Синергия» не встречается "
                "в заголовке — стоит проверить текст публикации на предмет контекста.")
    elif has_surname and not has_synergy and not has_name:
        base = ("Средняя/низкая: в заголовке есть только фамилия «Лобов» без имени "
                "и без «Синергии» — велика вероятность однофамильца или другого контекста.")
    elif has_synergy and not has_surname:
        base = ("Низкая (сомнительная) релевантность к персоне: упоминается «Синергия», "
                "но фамилия «Лобов» в заголовке отсутствует — вероятно, публикация "
                "о вузе/компании в целом, а не о Вадиме Лобове лично.")
    else:
        base = ("Низкая: ни фамилия «Лобов», ни «Синергия» не встречаются в заголовке — "
                "требуется ручная проверка полного текста публикации.")

    if has_university:
        base += " В заголовке есть указание на вуз/образовательную тематику."
    return base


def parse_query_datetime(iso_str: Optional[str], display: Optional[str]) -> (Optional[str], Optional[str]):
    if not iso_str:
        return None, display
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d"), display
    except Exception:
        return None, display


def scrape_query(
    page,
    query: str,
    depth: int,
    hl: str,
    gl: str,
    ceid: str,
    checked_at: str,
    settle_ms: int = 1800,
    scroll_attempts: int = 4,
) -> List[NewsResult]:
    params = {"q": query, "hl": hl, "gl": gl, "ceid": ceid}
    url = f"{NEWS_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    print(f"[+] Загружаю выдачу Google Новости: {url}", file=sys.stderr)

    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
    except PlaywrightTimeoutError:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

    page.wait_for_timeout(settle_ms)

    prev_count = -1
    for _ in range(scroll_attempts):
        count = page.evaluate("document.querySelectorAll('c-wiz.PO9Zff').length")
        if count >= depth or count == prev_count:
            break
        prev_count = count
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1200)

    raw_items = page.evaluate(EXTRACT_JS)

    max_card_idx = depth - 1
    filtered = [it for it in raw_items if it["cardIdx"] <= max_card_idx]

    seen_urls = set()
    results: List[NewsResult] = []
    for it in filtered:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        real_url = it.get("url")
        domain = normalize_domain(real_url)
        date_iso, date_display = parse_query_datetime(it.get("datetimeIso"), it.get("timeText"))

        dedup_key = real_url or f"NOURL::{title}"
        if dedup_key in seen_urls:
            # точный дубль той же самой страницы — пропускаем согласно ТЗ
            continue
        seen_urls.add(dedup_key)

        position = it["cardIdx"] + 1
        items_in_card = it.get("itemsInCard", 1)
        is_clustered = items_in_card > 1
        sub_position = None
        if is_clustered:
            sub_letter = chr(ord("a") + it["itemIdx"])
            sub_position = f"{position}{sub_letter}"

        result_type = classify_result_type(title, it.get("source"), domain)
        relevance = assess_relevance(title, it.get("source"))

        results.append(
            NewsResult(
                query=query,
                position=position,
                sub_position=sub_position,
                title=title,
                source=it.get("source"),
                published_at_iso=date_iso,
                published_at_display=date_display,
                snippet="сниппет Google недоступен",
                url=real_url,
                domain=domain,
                result_type=result_type,
                is_clustered_story=is_clustered,
                cluster_size=items_in_card,
                relevance_comment=relevance,
                checked_at=checked_at,
                notes="" if real_url else "Не удалось декодировать реальный URL публикации из разметки Google.",
            )
        )

    total_cards_seen = (raw_items[-1]["cardIdx"] + 1) if raw_items else 0
    print(f"    -> собрано {len(results)} результатов (всего карточек в DOM: {total_cards_seen})", file=sys.stderr)
    return results


def build_browser(playwright, chrome_path: Optional[str], headless: bool):
    launch_kwargs = dict(headless=headless, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    if chrome_path:
        launch_kwargs["executable_path"] = chrome_path
        return playwright.chromium.launch(**launch_kwargs)
    for candidate in ("/usr/local/bin/google-chrome", "/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"):
        if Path(candidate).exists():
            launch_kwargs["executable_path"] = candidate
            return playwright.chromium.launch(**launch_kwargs)
    # fallback на встроенный Chromium Playwright (нужно `playwright install chromium`)
    return playwright.chromium.launch(**launch_kwargs)


def run(
    queries: List[str],
    depth: int,
    hl: str,
    gl: str,
    ceid: str,
    output_dir: Path,
    chrome_path: Optional[str],
    headless: bool,
) -> List[NewsResult]:
    checked_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    all_results: List[NewsResult] = []

    with sync_playwright() as p:
        browser = build_browser(p, chrome_path, headless)
        context = browser.new_context(
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
        )
        page = context.new_page()
        for query in queries:
            try:
                results = scrape_query(page, query, depth, hl, gl, ceid, checked_at)
            except Exception as exc:  # noqa: BLE001
                print(f"[!] Ошибка при обработке запроса '{query}': {exc}", file=sys.stderr)
                results = []
            all_results.extend(results)
        browser.close()

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"google_news_results_{ts}.csv"
    json_path = output_dir / f"google_news_results_{ts}.json"

    fieldnames = [
        "query", "position", "sub_position", "title", "source", "domain",
        "published_at_display", "published_at_iso", "snippet", "url",
        "result_type", "is_clustered_story", "cluster_size",
        "relevance_comment", "checked_at", "notes",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in all_results:
            writer.writerow(asdict(r))

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in all_results], f, ensure_ascii=False, indent=2)

    print(f"\n[✓] Готово. Сохранено {len(all_results)} строк:", file=sys.stderr)
    print(f"    CSV:  {csv_path}", file=sys.stderr)
    print(f"    JSON: {json_path}", file=sys.stderr)
    return all_results


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--query", "-q", action="append", dest="queries",
        help="Поисковый запрос (можно указывать несколько раз). "
             "Если не задан ни один — используются 3 запроса по умолчанию для темы 'Лобов Вадим / Синергия'.",
    )
    parser.add_argument("--depth", type=int, default=20, help="Глубина: сколько первых результатов брать по каждому запросу (по умолчанию 20).")
    parser.add_argument("--hl", default="ru", help="Язык интерфейса Google (по умолчанию ru).")
    parser.add_argument("--gl", default="RU", help="Регион (страна) выдачи Google (по умолчанию RU).")
    parser.add_argument("--ceid", default="RU:ru", help="Google News country:language edition id (по умолчанию RU:ru).")
    parser.add_argument("--output-dir", default="output", help="Каталог для сохранения CSV/JSON (по умолчанию ./output).")
    parser.add_argument("--chrome-path", default=None, help="Путь к бинарнику Chrome/Chromium (иначе автоопределение).")
    parser.add_argument("--headful", action="store_true", help="Запустить браузер в видимом режиме (для отладки).")
    return parser.parse_args(argv)


DEFAULT_QUERIES = [
    "лобов вадим университет синергия",
    "лобов вадим синергия",
    "лобов вадим",
]


def main(argv=None):
    args = parse_args(argv)
    queries = args.queries or DEFAULT_QUERIES
    run(
        queries=queries,
        depth=args.depth,
        hl=args.hl,
        gl=args.gl,
        ceid=args.ceid,
        output_dir=Path(args.output_dir),
        chrome_path=args.chrome_path,
        headless=not args.headful,
    )


if __name__ == "__main__":
    main()
