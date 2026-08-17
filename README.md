# Парсер выдачи Google News — сетевая версия

Панель ставится на **Linux-сервер** (Debian / Ubuntu) и открывается в браузере по домену. Со стационарного компьютера ничего запускать не нужно: сбор идёт на сервере, отчёты лежат там же в `otchet`.

## Установка на Debian / Ubuntu

На сервере:

```bash
sudo apt-get update
sudo apt-get install -y git python3 nginx
git clone -b crsr/google-news-parser-6897 https://github.com/jaguarcatcat/cursor.git /opt/google-news-otchet
cd /opt/google-news-otchet
sudo DOMAIN=news.example.com ./install-debian.sh
```

Подставьте свой домен вместо `news.example.com`. Скрипт:

- кладёт приложение в `/opt/google-news-otchet`;
- поднимает службу `google-news-otchet` на `127.0.0.1:8765`;
- настраивает nginx на 80-й порт;
- создаёт ключ доступа в `/etc/google-news-otchet.env`.

Дальше:

1. В DNS направьте A-запись домена на IP сервера.
2. Откройте `http://ваш-домен/`.
3. По желанию HTTPS: `sudo certbot --nginx -d ваш-домен`.
4. Ключ: `sudo grep AUTH_TOKEN /etc/google-news-otchet.env` — вставьте его в поле панели.

## Как пользоваться

С телефона, ноутбука или любого ПК откройте домен → **Собрать отчёт**.

Отчёты:

- в браузере: список на той же странице, либо `https://ваш-домен/otchet/posledniy.html`;
- на диске сервера: `/opt/google-news-otchet/otchet/ГГГГ-ММ-ДД_ЧЧММСС/otchet.html`.

Каждый запуск создаёт новую папку, старые срезы не затираются.

## Ручной запуск без nginx

```bash
AUTH_TOKEN=секрет python3 server.py --host 0.0.0.0 --port 8765 --no-browser
```

Тогда панель доступна как `http://IP-сервера:8765/`. Для домена лучше nginx, как в `install-debian.sh`.

## Служба

```bash
sudo systemctl status google-news-otchet
sudo journalctl -u google-news-otchet -f
sudo systemctl restart google-news-otchet
```
