#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "Сетевая панель Google News на http://0.0.0.0:${PORT:-8765}/"
echo "Отчёты: $(pwd)/otchet"
if command -v python3 >/dev/null 2>&1; then
  exec python3 server.py --host 0.0.0.0 --port "${PORT:-8765}" --no-browser
elif command -v python >/dev/null 2>&1; then
  exec python server.py --host 0.0.0.0 --port "${PORT:-8765}" --no-browser
else
  echo "Не найден Python 3."
  exit 1
fi
