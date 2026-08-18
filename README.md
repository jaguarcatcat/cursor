# Google News parser

`google_news_parser.py` records Google News search results using Google's
Russian RSS search endpoint (`hl=ru`, `gl=RU`, `ceid=RU:ru`). Install the
single export dependency, then run:

```bash
python3 -m pip install -r requirements.txt
python3 google_news_parser.py --output-dir output
```

By default it queries:

1. `лобов вадим университет синергия`
2. `лобов вадим синергия`
3. `лобов вадим`

The command writes:

- `google_news_results.csv` — all collected fields, including type,
  relevance note, cluster note, and the Google News URL;
- `google_news_report.md` — the requested summary table.
- `google_news_results.xlsx` — an Excel workbook with a formatted results
  sheet (filters, frozen header, clickable URLs) and a summary/analytics sheet.

The RSS response is Google News output, but does not expose the exact visible
Google UI snippet or a reliable story-cluster flag. The script does not invent
either: it writes `сниппет Google недоступен` and records that a cluster cannot
be determined. When Google exposes only an opaque `news.google.com` article
redirect instead of a publisher link, that exact returned URL is kept rather
than guessed.

Use `--query "..."` repeatedly to override the default queries and `--limit`
to set the maximum number of entries per query (1–100).
