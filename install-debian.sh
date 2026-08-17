#!/usr/bin/env bash
# Установка панели Google News на Debian / Ubuntu.
# Запуск: sudo DOMAIN=news.example.com ./install-debian.sh
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите от root: sudo DOMAIN=news.example.com $0"
  exit 1
fi

DOMAIN="${DOMAIN:-${1:-}}"
if [[ -z "${DOMAIN}" ]]; then
  echo "Укажите домен: sudo DOMAIN=news.example.com $0"
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/google-news-otchet"
ENV_FILE="/etc/google-news-otchet.env"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 nginx

mkdir -p "${APP_DIR}/otchet"
cp -a "${SRC}/server.py" "${SRC}/google_news_parser.py" "${SRC}/index.html" "${APP_DIR}/"
chown -R www-data:www-data "${APP_DIR}"

if [[ ! -f "${ENV_FILE}" ]]; then
  TOKEN="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
  cat > "${ENV_FILE}" <<EOF
LISTEN_HOST=127.0.0.1
PORT=8765
OTCHET_DIR=${APP_DIR}/otchet
AUTH_TOKEN=${TOKEN}
EOF
  chmod 640 "${ENV_FILE}"
  echo "Ключ доступа записан в ${ENV_FILE}"
else
  echo "Файл ${ENV_FILE} уже есть, не перезаписываю."
fi

cp "${SRC}/deploy/google-news-otchet.service" /etc/systemd/system/google-news-otchet.service
sed "s/news.example.com/${DOMAIN}/g" "${SRC}/deploy/nginx.conf.example" > /etc/nginx/sites-available/google-news-otchet
ln -sfn /etc/nginx/sites-available/google-news-otchet /etc/nginx/sites-enabled/google-news-otchet
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl daemon-reload
systemctl enable --now google-news-otchet
systemctl reload nginx

echo
echo "Готово."
echo "1. Направьте A-запись домена ${DOMAIN} на IP этого сервера."
echo "2. Откройте http://${DOMAIN}/"
echo "3. HTTPS: certbot --nginx -d ${DOMAIN}"
echo "4. Ключ доступа: grep AUTH_TOKEN ${ENV_FILE}"
echo "5. Отчёты на сервере: ${APP_DIR}/otchet"
