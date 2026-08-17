#!/usr/bin/env python3
"""Сетевая HTML-панель сбора Google News для Linux-сервера и домена."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import google_news_parser as gnp

ROOT = Path(__file__).resolve().parent
OTCHET_DIR = Path(os.environ.get("OTCHET_DIR", ROOT / "otchet"))
INDEX_FILE = ROOT / "index.html"
DEFAULT_PORT = int(os.environ.get("PORT", "8765"))
DEFAULT_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
COOKIE_NAME = "otchet_token"

LOGGER = logging.getLogger("google_news_ui")
STATE_LOCK = threading.Lock()
STATE: dict[str, Any] = {
    "running": False,
    "error": "",
    "log": [],
    "last": None,
}


def auth_token() -> str:
    return os.environ.get("AUTH_TOKEN", "").strip()


def allow_local_html() -> bool:
    return os.environ.get("ALLOW_LOCAL_HTML", "").lower() in {"1", "true", "yes"}


def log_line(message: str) -> None:
    LOGGER.info(message)
    with STATE_LOCK:
        STATE["log"] = (STATE["log"] + [message])[-80:]


def is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def public_otchet_url(path: Path) -> str:
    rel = path.resolve().relative_to(OTCHET_DIR.resolve()).as_posix()
    return "/otchet/" + rel


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": result.get("run_id"),
        "count": result.get("count"),
        "checked_at": result.get("checked_at"),
        "queries": result.get("queries"),
        "report_url": result.get("report_url"),
        "latest_url": result.get("latest_url"),
        "folder": result.get("run_id"),
    }


def run_collect(payload: dict[str, Any]) -> dict[str, Any]:
    queries = payload.get("queries") or gnp.DEFAULT_QUERIES
    if isinstance(queries, str):
        queries = [line.strip() for line in queries.splitlines() if line.strip()]
    queries = [str(item).strip() for item in queries if str(item).strip()]
    limit = int(payload.get("limit") or 20)
    pause = float(payload.get("pause") or 1.0)
    from_html = payload.get("from_html")
    query = payload.get("query")
    if from_html:
        if not allow_local_html():
            raise ValueError("Локальный HTML на сетевом сервере отключён")
        html_path = Path(str(from_html))
        if not html_path.is_absolute():
            html_path = (ROOT / html_path).resolve()
        if not (html_path.exists() and html_path.suffix.lower() in {".html", ".htm"} and is_under(html_path, ROOT)):
            raise ValueError("Недопустимый локальный HTML-файл")
        from_html = str(html_path)
    else:
        from_html = None
    log_line("Старт сбора Google News")
    result = gnp.collect(
        queries=queries,
        limit=limit,
        output_root=OTCHET_DIR,
        from_html=from_html,
        query=query,
        pause=pause,
    )
    result["report_url"] = public_otchet_url(Path(result["files"]["html"]))
    result["latest_url"] = "/otchet/posledniy.html"
    result.pop("report_md", None)
    log_line(f"Готово: {result['run_id']} ({result['count']} карточек)")
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "GoogleNewsOtchet/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - " + fmt, self.address_string(), *args)

    def _cors(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.send_header("Access-Control-Allow-Credentials", "true")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return ""
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else ""

    def _provided_token(self, body: dict[str, Any] | None = None) -> str:
        header = (self.headers.get("X-Auth-Token") or "").strip()
        if header:
            return header
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if body and body.get("token"):
            return str(body.get("token") or "").strip()
        return self._cookie_token()

    def _authorized(self, body: dict[str, Any] | None = None) -> bool:
        expected = auth_token()
        if not expected:
            return True
        return self._provided_token(body) == expected

    def _auth_cookie_header(self, body: dict[str, Any] | None = None) -> str:
        token = self._provided_token(body)
        if not auth_token() or not token:
            return ""
        return f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_file(INDEX_FILE, "text/html; charset=utf-8")
            return
        if parsed.path in {"/healthz", "/api/health"}:
            self._send_json({"ok": True, "status": "ok"})
            return
        if parsed.path == "/api/status":
            if not self._authorized():
                self._send_json({"ok": False, "error": "Нужен ключ доступа", "auth_required": True}, 401)
                return
            with STATE_LOCK:
                last = STATE["last"]
                payload = {
                    "ok": True,
                    "auth_required": bool(auth_token()),
                    "running": STATE["running"],
                    "error": STATE["error"],
                    "log": list(STATE["log"]),
                    "last": public_result(last) if last else None,
                    "queries": gnp.DEFAULT_QUERIES,
                }
            payload["reports"] = [
                {
                    "id": item["id"],
                    "url": public_otchet_url(Path(item["html"])),
                    "modified": item["modified"],
                }
                for item in gnp.list_otchet_runs(OTCHET_DIR)
            ]
            self._send_json(payload, cookie=self._auth_cookie_header())
            return
        if parsed.path.startswith("/otchet/"):
            if not self._authorized():
                self._send_json({"ok": False, "error": "Нужен ключ доступа", "auth_required": True}, 401)
                return
            rel = unquote(parsed.path[len("/otchet/") :])
            target = (OTCHET_DIR / rel).resolve()
            if not is_under(target, OTCHET_DIR) or not target.is_file():
                self._send_json({"ok": False, "error": "not found"}, 404)
                return
            ctype = "text/html; charset=utf-8"
            if target.suffix == ".json":
                ctype = "application/json; charset=utf-8"
            elif target.suffix == ".csv":
                ctype = "text/csv; charset=utf-8"
            elif target.suffix == ".md":
                ctype = "text/markdown; charset=utf-8"
            self._send_file(target, ctype, cookie=self._auth_cookie_header())
            return
        self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/collect":
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "Некорректный JSON"}, 400)
            return
        if not isinstance(payload, dict):
            payload = {}
        if not self._authorized(payload):
            self._send_json({"ok": False, "error": "Нужен ключ доступа", "auth_required": True}, 401)
            return
        with STATE_LOCK:
            if STATE["running"]:
                self._send_json({"ok": False, "error": "Сбор уже выполняется"}, 409)
                return
            STATE["running"] = True
            STATE["error"] = ""
        try:
            result = run_collect(payload)
            with STATE_LOCK:
                STATE["last"] = result
            self._send_json({"ok": True, **public_result(result)}, cookie=self._auth_cookie_header(payload))
        except ValueError as exc:
            with STATE_LOCK:
                STATE["error"] = str(exc)
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Collect failed")
            with STATE_LOCK:
                STATE["error"] = str(exc)
            self._send_json({"ok": False, "error": str(exc)}, 500)
        finally:
            with STATE_LOCK:
                STATE["running"] = False

    def _send_json(self, payload: dict[str, Any], status: int = 200, cookie: str = "") -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, content_type: str, cookie: str = "") -> None:
        if not path.exists():
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self._cors()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def port_in_use(host: str, port: int) -> bool:
    check_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((check_host, port)) == 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Сетевая панель сбора Google News")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Адрес прослушивания, по умолчанию 0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер на сервере")
    parser.add_argument("--browser", action="store_true", help="Открыть браузер (только для локальной отладки)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    OTCHET_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://{args.host}:{args.port}/"
    if port_in_use(args.host, args.port):
        LOGGER.info("Сервер уже запущен: %s", url)
        if args.browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
        return 0
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    LOGGER.info("Панель в сети: %s", url)
    LOGGER.info("Отчёты: %s", OTCHET_DIR)
    if auth_token():
        LOGGER.info("Ключ доступа AUTH_TOKEN включён")
    else:
        LOGGER.warning("AUTH_TOKEN не задан: панель открыта всем, кто знает URL")
    if args.browser and not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Остановка")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
