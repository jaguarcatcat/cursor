#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo
echo "Сбор Google News"
echo "Не закрывайте это окно, пока нужна панель в браузере."
echo "Отчёты сохраняются в папку otchet"
echo
if command -v python3 >/dev/null 2>&1; then
  exec python3 server.py
elif command -v python >/dev/null 2>&1; then
  exec python server.py
else
  echo "Не найден Python 3."
  exit 1
fi
