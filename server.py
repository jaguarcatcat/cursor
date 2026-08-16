#!/usr/bin/env python3
"""Локальная HTML-панель для регулярного сбора Google News в папку otchet."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import google_news_parser as gnp

ROOT = Path(__file__).resolve().parent
OTCHET_DIR = Path(os.environ.get("OTCHET_DIR", ROOT / "otchet"))
INDEX_FILE = ROOT / "index.html"
DEFAULT_PORT = 8765
HOST = "127.0.0.1"

LOGGER = logging.getLogger("google_news_ui")
STATE_LOCK = threading.Lock()
STATE: dict[str, Any] = {
    "running": False,
    "error": "",
    "log": [],
    "last": None,
}


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
    log_line(f"Готово: {result['folder']} ({result['count']} карточек)")
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "GoogleNewsOtchet/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - " + fmt, self.address_string(), *args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_file(INDEX_FILE, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/status":
            with STATE_LOCK:
                payload = {
                    "running": STATE["running"],
                    "error": STATE["error"],
                    "log": list(STATE["log"]),
                    "last": STATE["last"],
                    "otchet_dir": str(OTCHET_DIR),
                    "queries": gnp.DEFAULT_QUERIES,
                }
            payload["reports"] = [
                {
                    **item,
                    "url": public_otchet_url(Path(item["html"])),
                }
                for item in gnp.list_otchet_runs(OTCHET_DIR)
            ]
            self._send_json(payload)
            return
        if parsed.path.startswith("/otchet/"):
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
            self._send_file(target, ctype)
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
        with STATE_LOCK:
            if STATE["running"]:
                self._send_json({"ok": False, "error": "Сбор уже выполняется"}, 409)
                return
            STATE["running"] = True
            STATE["error"] = ""
        try:
            result = run_collect(payload if isinstance(payload, dict) else {})
            with STATE_LOCK:
                STATE["last"] = result
            self._send_json({"ok": True, **result})
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Collect failed")
            with STATE_LOCK:
                STATE["error"] = str(exc)
            self._send_json({"ok": False, "error": str(exc)}, 500)
        finally:
            with STATE_LOCK:
                STATE["running"] = False

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((HOST, port)) == 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HTML-панель сбора Google News")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    OTCHET_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://{HOST}:{args.port}/"
    if port_in_use(args.port):
        LOGGER.info("Сервер уже запущен: %s", url)
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    LOGGER.info("Панель: %s", url)
    LOGGER.info("Отчёты: %s", OTCHET_DIR)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Остановка")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
