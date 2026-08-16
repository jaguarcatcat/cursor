# Google News Parser

Парсер выдачи Google News (Google Новости) для мониторинга поисковых запросов.

## Установка

```bash
pip install -r requirements.txt
```

## Запуск

```bash
python google_news_parser.py
```

### Параметры

| Параметр | Описание | По умолчанию |
|----------|----------|--------------|
| `--queries` | Список поисковых запросов | 3 запроса по Лобову/Синергии |
| `--max-results` | Макс. результатов на запрос | 20 |
| `--output-json` | Путь к JSON-файлу | `results.json` |
| `--output-md` | Путь к Markdown-файлу | `results.md` |

### Пример

```bash
python google_news_parser.py \
  --queries "лобов вадим синергия" "лобов вадим" \
  --max-results 20 \
  --output-json output/results.json \
  --output-md output/results.md
```

## Что собирает

- Позиция в выдаче, заголовок, СМИ, URL, дата, домен
- Тип результата и оценка релевантности
- Маркировка кластеров Google News (объединённые сюжеты)
- Сниппет (если Google его отображает; иначе — «сниппет Google недоступен»)

## Параметры поиска

- Регион: Россия (`gl=RU`, `ceid=RU:ru`)
- Язык: русский (`hl=ru`)
- Источник: веб-выдача Google News (не RSS, не органический поиск)
