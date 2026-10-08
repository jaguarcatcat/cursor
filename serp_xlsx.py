#!/usr/bin/env python3
"""Сравнение двух срезов Google News и выгрузка в XLSX."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

import google_news_parser as gnp

HEADER_FILL = PatternFill("solid", fgColor="1C1814")
HEADER_FONT = Font(color="F6F1E8", bold=True, name="Calibri", size=11)
NEW_FILL = PatternFill("solid", fgColor="D9EAD3")
GONE_FILL = PatternFill("solid", fgColor="F4CCCC")
UP_FILL = PatternFill("solid", fgColor="C9DAF8")
DOWN_FILL = PatternFill("solid", fgColor="FFF2CC")
SAME_FILL = PatternFill("solid", fgColor="F3F3F3")
THIN = Border(
    left=Side(style="thin", color="D7CBB8"),
    right=Side(style="thin", color="D7CBB8"),
    top=Side(style="thin", color="D7CBB8"),
    bottom=Side(style="thin", color="D7CBB8"),
)
WRAP = Alignment(wrap_text=True, vertical="top")


def load_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Ожидался список карточек в {path}")
    return [dict(item) for item in data]


def position_number(value: Any) -> int | None:
    try:
        return int(str(value).split()[0])
    except (TypeError, ValueError):
        return None


def row_key(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item.get("query") or ""), gnp.canonical_url(str(item.get("url") or "")))


def article_id_from_item(item: dict[str, Any]) -> str:
    ident = str(item.get("google_article_id") or "").strip()
    if ident:
        return ident
    for field in ("url", "google_news_url"):
        match = re.search(r"/articles/([A-Za-z0-9_-]+)", str(item.get(field) or ""))
        if match:
            return match.group(1)
    return ""


def norm_title(item: dict[str, Any]) -> str:
    title = (item.get("title") or "").lower().replace("ё", "е")
    title = re.sub(r"[^а-яa-z0-9]+", " ", title)
    return " ".join(title.split())


def match_keys(item: dict[str, Any]) -> list[tuple[str, str, str]]:
    query = str(item.get("query") or "")
    keys: list[tuple[str, str, str]] = []
    url = gnp.canonical_url(str(item.get("url") or ""))
    if url and "news.google.com" not in url:
        keys.append(("url", query, url))
    article_id = article_id_from_item(item)
    if article_id:
        keys.append(("id", query, article_id))
    title = norm_title(item)
    if title:
        keys.append(("title", query, title))
    return keys


def pair_rows(
    previous: list[dict[str, Any]], current: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    used_prev: set[int] = set()
    used_curr: set[int] = set()
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for kind in ("url", "id", "title"):
        index: dict[tuple[str, str, str], int] = {}
        for i, item in enumerate(previous):
            if i in used_prev:
                continue
            for key in match_keys(item):
                if key[0] == kind:
                    index[key] = i
        for j, item in enumerate(current):
            if j in used_curr:
                continue
            for key in match_keys(item):
                if key[0] != kind or key not in index:
                    continue
                i = index[key]
                if i in used_prev:
                    continue
                used_prev.add(i)
                used_curr.add(j)
                pairs.append((previous[i], current[j]))
                break
    new_rows = [item for j, item in enumerate(current) if j not in used_curr]
    gone_rows = [item for i, item in enumerate(previous) if i not in used_prev]
    return pairs, new_rows, gone_rows


def enrich_publisher_urls(current: list[dict[str, Any]], *known: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_title: dict[tuple[str, str], dict[str, Any]] = {}
    for rows in known:
        for item in rows:
            url = str(item.get("url") or "")
            if not url or "news.google.com" in url:
                continue
            ident = article_id_from_item(item)
            if ident:
                by_id[ident] = item
            title = norm_title(item)
            if title:
                by_title[(str(item.get("query") or ""), title)] = item
    enriched: list[dict[str, Any]] = []
    for item in current:
        copy = dict(item)
        if "news.google.com" in str(copy.get("url") or ""):
            known_item = by_id.get(article_id_from_item(copy)) or by_title.get(
                (str(copy.get("query") or ""), norm_title(copy))
            )
            if known_item:
                copy["url"] = known_item.get("url") or copy.get("url")
                copy["domain"] = known_item.get("domain") or gnp.domain_from_url(str(copy["url"]))
        enriched.append(copy)
    return enriched


def fetched_label(rows: list[dict[str, Any]], fallback: str = "") -> str:
    for item in rows:
        if item.get("fetched_at"):
            return str(item["fetched_at"])
    return fallback


def compare_rows(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    pairs, new_rows, gone_rows = pair_rows(previous, current)
    stayed: list[dict[str, Any]] = []
    rose = fell = unchanged = 0
    for prev_item, curr_item in pairs:
        prev_pos = position_number(prev_item.get("position"))
        curr_pos = position_number(curr_item.get("position"))
        delta = None
        if prev_pos is not None and curr_pos is not None:
            delta = curr_pos - prev_pos
            if delta < 0:
                rose += 1
                change = "поднялась"
            elif delta > 0:
                fell += 1
                change = "опустилась"
            else:
                unchanged += 1
                change = "без изменений"
        else:
            change = "без изменений"
            unchanged += 1
        stayed.append(
            {
                "query": curr_item.get("query") or prev_item.get("query"),
                "url": curr_item.get("url") or prev_item.get("url"),
                "canonical_url": gnp.canonical_url(str(curr_item.get("url") or prev_item.get("url") or "")),
                "title": curr_item.get("title"),
                "source": curr_item.get("source"),
                "domain": curr_item.get("domain"),
                "position_prev": prev_item.get("position"),
                "position_curr": curr_item.get("position"),
                "delta": delta,
                "change": change,
                "published_at": curr_item.get("published_at"),
                "title_changed": (prev_item.get("title") or "") != (curr_item.get("title") or ""),
                "title_prev": prev_item.get("title"),
            }
        )

    by_query: list[dict[str, Any]] = []
    queries = sorted({item.get("query") for item in previous + current if item.get("query")})
    for query in queries:
        prev_q = [item for item in previous if item.get("query") == query]
        curr_q = [item for item in current if item.get("query") == query]
        overlap = sum(1 for prev_item, curr_item in pairs if (curr_item.get("query") or prev_item.get("query")) == query)
        top_prev = next((item.get("title") for item in prev_q if str(item.get("position")) == "1"), "")
        top_curr = next((item.get("title") for item in curr_q if str(item.get("position")) == "1"), "")
        by_query.append(
            {
                "query": query,
                "prev_count": len(prev_q),
                "curr_count": len(curr_q),
                "overlap": overlap,
                "new": len(curr_q) - overlap,
                "gone": len(prev_q) - overlap,
                "top1_same": bool(top_prev and top_prev == top_curr),
                "top1_prev": top_prev,
                "top1_curr": top_curr,
            }
        )

    prev_domains = Counter(item.get("domain") or "" for item in previous)
    curr_domains = Counter(item.get("domain") or "" for item in current)
    domains = sorted(set(prev_domains) | set(curr_domains))
    domain_rows = [
        {
            "domain": domain,
            "prev": prev_domains.get(domain, 0),
            "curr": curr_domains.get(domain, 0),
            "delta": curr_domains.get(domain, 0) - prev_domains.get(domain, 0),
        }
        for domain in domains
        if domain
    ]
    domain_rows.sort(key=lambda item: (-abs(item["delta"]), -item["curr"], item["domain"]))

    return {
        "previous_at": fetched_label(previous),
        "current_at": fetched_label(current),
        "prev_count": len(previous),
        "curr_count": len(current),
        "new_count": len(new_rows),
        "gone_count": len(gone_rows),
        "stable_count": len(pairs),
        "rose": rose,
        "fell": fell,
        "unchanged": unchanged,
        "stayed": stayed,
        "new_rows": new_rows,
        "gone_rows": gone_rows,
        "by_query": by_query,
        "domains": domain_rows,
    }


def _style_header(sheet: Worksheet, width: int) -> None:
    for col in range(1, width + 1):
        cell = sheet.cell(1, col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.border = THIN
    sheet.auto_filter.ref = sheet.dimensions
    sheet.freeze_panes = "A2"
    sheet.row_dimensions[1].height = 24


def _write_rows(sheet: Worksheet, headers: list[str], rows: list[list[Any]], fills: list[PatternFill | None] | None = None) -> None:
    sheet.append(headers)
    for index, row in enumerate(rows):
        sheet.append(row)
        fill = fills[index] if fills else None
        for col in range(1, len(headers) + 1):
            cell = sheet.cell(index + 2, col)
            cell.alignment = WRAP
            cell.border = THIN
            if fill is not None:
                cell.fill = fill
    _style_header(sheet, len(headers))
    for col, header in enumerate(headers, start=1):
        letter = get_column_letter(col)
        sample = max([len(str(header))] + [len(str(row[col - 1])) for row in rows[:40]], default=10)
        sheet.column_dimensions[letter].width = min(max(sample + 2, 12), 60)


def _card_row(item: dict[str, Any]) -> list[Any]:
    return [
        item.get("query"),
        item.get("position"),
        item.get("title"),
        item.get("source"),
        item.get("published_at"),
        item.get("snippet"),
        item.get("url"),
        item.get("domain"),
        item.get("result_type"),
        item.get("relevance_comment"),
    ]


CARD_HEADERS = ["Запрос", "Позиция", "Заголовок", "СМИ", "Дата", "Сниппет", "URL", "Домен", "Тип", "Релевантность"]


def stable_identity(item: dict[str, Any]) -> tuple[str, str, str] | None:
    query = str(item.get("query") or "")
    article_id = article_id_from_item(item)
    if article_id:
        return (query, "id", article_id)
    title = norm_title(item)
    if title:
        return (query, "title", title)
    url = gnp.canonical_url(str(item.get("url") or ""))
    if url:
        return (query, "url", url)
    return None


def snapshot_map(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    mapping: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in rows:
        key = stable_identity(item)
        if key:
            mapping[key] = item
    return mapping


def trajectory_label(positions: list[int | None]) -> str:
    present = [pos for pos in positions if pos is not None]
    if not present:
        return "нет данных"
    if positions[-1] is None:
        return "выпала в последнем срезе"
    if positions[0] is None:
        return "появилась позже первого среза"
    if all(pos == present[0] for pos in present):
        return "стабильна"
    first, last = present[0], present[-1]
    if last < first:
        return "в итоге выше"
    if last > first:
        return "в итоге ниже"
    return "колебалась"


def build_trend(
    snapshots: list[tuple[str, list[dict[str, Any]]]],
) -> tuple[list[str], list[list[Any]]]:
    maps = [(label, snapshot_map(rows)) for label, rows in snapshots]
    keys: set[tuple[str, str]] = set()
    for _label, mapping in maps:
        keys |= set(mapping)
    headers = (
        ["Запрос", "Заголовок", "СМИ", "Домен", "URL"]
        + [f"Позиция: {label}" for label, _rows in snapshots]
        + ["Траектория"]
    )
    rows: list[list[Any]] = []
    for key in sorted(keys):
        latest = None
        positions: list[int | None] = []
        pos_cells: list[Any] = []
        for _label, mapping in maps:
            item = mapping.get(key)
            if item:
                latest = item
                number = position_number(item.get("position"))
                positions.append(number)
                pos_cells.append(item.get("position"))
            else:
                positions.append(None)
                pos_cells.append("—")
        if latest is None:
            continue
        rows.append(
            [
                key[0],
                latest.get("title"),
                latest.get("source"),
                latest.get("domain"),
                latest.get("url") or key[1],
                *pos_cells,
                trajectory_label(positions),
            ]
        )
    return headers, rows


def build_analytics_lines(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    cmp: dict[str, Any],
) -> list[list[Any]]:
    tone_prev = Counter(gnp.classify_tone(str(item.get("title") or ""), str(item.get("url") or ""), str(item.get("snippet") or "")) for item in previous)
    tone_curr = Counter(gnp.classify_tone(str(item.get("title") or ""), str(item.get("url") or ""), str(item.get("snippet") or "")) for item in current)
    topic_curr = Counter(gnp.classify_topic(str(item.get("title") or ""), str(item.get("url") or "")) for item in current)
    source_curr = Counter(str(item.get("source") or "") for item in current)
    lines = [
        ["Блок", "Вывод"],
        ["Период сравнения", f"{cmp.get('previous_at')} → {cmp.get('current_at')}"],
        ["Объём выдачи", f"{cmp['prev_count']} → {cmp['curr_count']} карточек"],
        ["Стабильность URL", f"сохранились {cmp['stable_count']}, новых {cmp['new_count']}, выпало {cmp['gone_count']}"],
        ["Движение позиций", f"поднялись {cmp['rose']}, опустились {cmp['fell']}, без сдвига {cmp['unchanged']}"],
        [
            "Тональность было / стало",
            "; ".join(
                f"{name}: {tone_prev.get(name, 0)} → {tone_curr.get(name, 0)}"
                for name in sorted(set(tone_prev) | set(tone_curr))
            ),
        ],
        ["Темы сейчас", "; ".join(f"{name} ({count})" for name, count in topic_curr.most_common(8))],
        ["Доминирующие СМИ", "; ".join(f"{name} ({count})" for name, count in source_curr.most_common(8))],
    ]
    for item in cmp["by_query"]:
        top_note = "топ-1 тот же" if item["top1_same"] else "сменился топ-1"
        lines.append(
            [
                f"Запрос «{item['query']}»",
                f"{item['prev_count']} → {item['curr_count']}; пересечение {item['overlap']}; новых {item['new']}; выпало {item['gone']}; {top_note}.",
            ]
        )
        if not item["top1_same"]:
            lines.append(["  топ-1 было", item["top1_prev"]])
            lines.append(["  топ-1 стало", item["top1_curr"]])
    visible_new = [
        f"[{item.get('query')} / #{item.get('position')}] {item.get('title')} — {item.get('source')}"
        for item in cmp["new_rows"]
        if position_number(item.get("position")) is not None and position_number(item.get("position")) <= 5
    ]
    if visible_new:
        lines.append(["Новые в топ-5", " | ".join(visible_new[:8])])
    return lines


def build_summary_table(cmp: dict[str, Any]) -> tuple[list[list[Any]], list[PatternFill | None]]:
    rows: list[list[Any]] = []
    fills: list[PatternFill | None] = []

    def sort_key(query: Any, position: Any) -> tuple[str, int]:
        number = position_number(position)
        return (str(query or ""), number if number is not None else 999)

    for item in sorted(cmp["stayed"], key=lambda row: sort_key(row.get("query"), row.get("position_curr"))):
        fill = SAME_FILL
        if item["change"] == "поднялась":
            fill = UP_FILL
        elif item["change"] == "опустилась":
            fill = DOWN_FILL
        rows.append(
            [
                item.get("query"),
                item.get("title"),
                item.get("source"),
                item.get("domain"),
                item.get("position_prev"),
                item.get("position_curr"),
                item.get("delta"),
                item.get("change"),
                item.get("url"),
            ]
        )
        fills.append(fill)
    for item in sorted(cmp["new_rows"], key=lambda row: sort_key(row.get("query"), row.get("position"))):
        rows.append(
            [
                item.get("query"),
                item.get("title"),
                item.get("source"),
                item.get("domain"),
                "—",
                item.get("position"),
                None,
                "новая",
                item.get("url"),
            ]
        )
        fills.append(NEW_FILL)
    for item in sorted(cmp["gone_rows"], key=lambda row: sort_key(row.get("query"), row.get("position"))):
        rows.append(
            [
                item.get("query"),
                item.get("title"),
                item.get("source"),
                item.get("domain"),
                item.get("position"),
                "—",
                None,
                "выпала",
                item.get("url"),
            ]
        )
        fills.append(GONE_FILL)
    return rows, fills


def write_comparison_xlsx(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    path: Path,
    previous_label: str = "предыдущий скрининг",
    current_label: str = "актуальный срез",
    baseline: list[dict[str, Any]] | None = None,
    baseline_label: str = "первый скрининг",
) -> Path:
    current = enrich_publisher_urls(current, previous, *( [baseline] if baseline else [] ))
    previous = enrich_publisher_urls(previous, *( [baseline] if baseline else [] ))
    cmp = compare_rows(previous, current)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()

    summary = book.active
    summary.title = "Сводка"
    summary_rows = [
        ["Показатель", "Значение"],
        ["Предыдущий скрининг", cmp["previous_at"] or previous_label],
        ["Актуальный срез", cmp["current_at"] or current_label],
        ["Карточек было", cmp["prev_count"]],
        ["Карточек стало", cmp["curr_count"]],
        ["Сохранились (тот же запрос + URL)", cmp["stable_count"]],
        ["Новые публикации", cmp["new_count"]],
        ["Выпали из выдачи", cmp["gone_count"]],
        ["Поднялись в выдаче", cmp["rose"]],
        ["Опустились в выдаче", cmp["fell"]],
        ["Позиция не изменилась", cmp["unchanged"]],
    ]
    for row in summary_rows:
        summary.append(row)
    _style_header(summary, 2)
    summary.column_dimensions["A"].width = 42
    summary.column_dimensions["B"].width = 56
    summary["A13"] = "Как читать"
    summary["A14"] = (
        "Сравнение идёт по паре «запрос + канонический URL». "
        "Отрицательная дельта позиции значит, что карточка поднялась (например, с 5 на 2 = −3). "
        "Разные публикации одного СМИ не сливаются."
    )
    summary.merge_cells("A14:B16")
    summary["A14"].alignment = WRAP

    table_sheet = book.create_sheet("Сводная таблица")
    table_rows, table_fills = build_summary_table(cmp)
    _write_rows(
        table_sheet,
        [
            "Запрос",
            "Заголовок",
            "СМИ",
            "Домен",
            f"Позиция: {previous_label}",
            f"Позиция: {current_label}",
            "Дельта",
            "Статус",
            "URL",
        ],
        table_rows,
        table_fills,
    )
    table_sheet.column_dimensions["A"].width = 36
    table_sheet.column_dimensions["B"].width = 60
    table_sheet.column_dimensions["H"].width = 16

    stayed_rows = [
        [
            item["query"],
            item["position_prev"],
            item["position_curr"],
            item["delta"],
            item["change"],
            item["title"],
            item["source"],
            item["domain"],
            item["url"],
            "да" if item["title_changed"] else "нет",
            item["published_at"],
        ]
        for item in cmp["stayed"]
    ]
    stayed_fills = []
    for item in cmp["stayed"]:
        if item["change"] == "поднялась":
            stayed_fills.append(UP_FILL)
        elif item["change"] == "опустилась":
            stayed_fills.append(DOWN_FILL)
        else:
            stayed_fills.append(SAME_FILL)
    stayed_sheet = book.create_sheet("Сравнение позиций")
    _write_rows(
        stayed_sheet,
        [
            "Запрос",
            "Позиция было",
            "Позиция стало",
            "Дельта",
            "Изменение",
            "Заголовок",
            "СМИ",
            "Домен",
            "URL",
            "Заголовок изменился",
            "Дата публикации",
        ],
        stayed_rows,
        stayed_fills,
    )

    new_sheet = book.create_sheet("Новые")
    _write_rows(new_sheet, CARD_HEADERS, [_card_row(item) for item in cmp["new_rows"]], [NEW_FILL] * len(cmp["new_rows"]))

    gone_sheet = book.create_sheet("Выпавшие")
    _write_rows(gone_sheet, CARD_HEADERS, [_card_row(item) for item in cmp["gone_rows"]], [GONE_FILL] * len(cmp["gone_rows"]))

    current_sheet = book.create_sheet("Актуальный срез")
    _write_rows(current_sheet, CARD_HEADERS, [_card_row(item) for item in current])

    previous_sheet = book.create_sheet("Предыдущий скрининг")
    _write_rows(previous_sheet, CARD_HEADERS, [_card_row(item) for item in previous])

    query_sheet = book.create_sheet("По запросам")
    _write_rows(
        query_sheet,
        [
            "Запрос",
            "Было",
            "Стало",
            "Пересечение URL",
            "Новых",
            "Выпало",
            "Топ-1 тот же",
            "Топ-1 было",
            "Топ-1 стало",
        ],
        [
            [
                item["query"],
                item["prev_count"],
                item["curr_count"],
                item["overlap"],
                item["new"],
                item["gone"],
                "да" if item["top1_same"] else "нет",
                item["top1_prev"],
                item["top1_curr"],
            ]
            for item in cmp["by_query"]
        ],
    )

    domain_sheet = book.create_sheet("СМИ и домены")
    _write_rows(
        domain_sheet,
        ["Домен", "Было карточек", "Стало карточек", "Дельта"],
        [[item["domain"], item["prev"], item["curr"], item["delta"]] for item in cmp["domains"]],
    )

    analytics_sheet = book.create_sheet("Аналитика")
    _write_rows(analytics_sheet, ["Блок", "Вывод"], build_analytics_lines(previous, current, cmp)[1:])

    if baseline:
        trend_headers, trend_rows = build_trend(
            [
                (baseline_label, baseline),
                (previous_label, previous),
                (current_label, current),
            ]
        )
        trend_sheet = book.create_sheet("Динамика 3 срезов")
        _write_rows(trend_sheet, trend_headers, trend_rows)

    book.save(path)
    return path


def write_current_xlsx(rows: list[dict[str, Any]], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = "Выдача"
    _write_rows(sheet, CARD_HEADERS, [_card_row(item) for item in rows])
    book.save(path)
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Сравнение срезов Google News в XLSX")
    parser.add_argument("--previous", required=True, help="JSON предыдущего скрининга")
    parser.add_argument("--current", required=True, help="JSON актуального среза")
    parser.add_argument("--output", required=True, help="Путь к XLSX")
    parser.add_argument("--baseline", help="JSON первого скрининга для листа динамики")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    previous = load_rows(Path(args.previous))
    current = load_rows(Path(args.current))
    baseline = load_rows(Path(args.baseline)) if args.baseline else None
    path = write_comparison_xlsx(
        previous,
        current,
        Path(args.output),
        baseline=baseline,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
