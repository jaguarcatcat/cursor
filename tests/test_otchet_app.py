#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import google_news_parser as gnp
import server as web

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def start_server() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, thread, f"http://{host}:{port}"


def http_json(url: str, data: dict | None = None, headers: dict | None = None, method: str | None = None) -> tuple[int, dict]:
    body = None
    req_headers = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
        method = method or "POST"
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        return exc.code, payload


class OtchetCollectTests(unittest.TestCase):
    def test_collect_writes_html_document_into_otchet_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = gnp.collect(
                queries=["лобов вадим университет синергия"],
                limit=20,
                output_root=tmp,
                from_html=str(FIXTURES / "sample_search.html"),
                query="лобов вадим университет синергия",
                pause=0,
            )
            html_path = Path(result["files"]["html"])
            self.assertTrue(html_path.exists())
            self.assertEqual(html_path.name, "otchet.html")
            self.assertEqual(Path(result["folder"]).parent, Path(tmp))
            text = html_path.read_text(encoding="utf-8")
            for header in ["Запрос", "Позиция", "Заголовок", "СМИ", "Дата", "Сниппет", "URL", "Домен"]:
                self.assertIn(header, text)
            self.assertIn("Example News", text)
            latest = Path(result["latest_html"])
            self.assertTrue(latest.exists())
            self.assertEqual(latest.name, "posledniy.html")
            self.assertTrue((Path(result["folder"]) / "dannye.csv").exists())
            self.assertTrue((Path(result["folder"]) / "otchet.md").exists())
            self.assertTrue((Path(result["folder"]) / "vydacha.xlsx").exists())
            runs = gnp.list_otchet_runs(Path(tmp))
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["id"], result["run_id"])


class WebPanelTests(unittest.TestCase):
    def test_index_and_deploy_files_exist(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("Собрать отчёт", html)
        self.assertIn("Панель работает на сервере в сети", html)
        self.assertTrue((ROOT / "server.py").exists())
        self.assertTrue((ROOT / "install-debian.sh").exists())
        nginx = (ROOT / "deploy/nginx.conf.example").read_text(encoding="utf-8")
        self.assertIn("proxy_pass http://127.0.0.1:8765", nginx)
        unit = (ROOT / "deploy/google-news-otchet.service").read_text(encoding="utf-8")
        self.assertIn("server.py", unit)

    def test_http_collect_saves_to_otchet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_dir = web.OTCHET_DIR
            old_allow = os.environ.get("ALLOW_LOCAL_HTML")
            web.OTCHET_DIR = Path(tmp)
            os.environ["ALLOW_LOCAL_HTML"] = "1"
            httpd, thread, base = start_server()
            try:
                status, data = http_json(
                    f"{base}/api/collect",
                    {
                        "queries": ["лобов вадим университет синергия"],
                        "limit": 20,
                        "from_html": "tests/fixtures/sample_search.html",
                        "pause": 0,
                    },
                )
                self.assertEqual(status, 200)
                self.assertTrue(data["ok"])
                self.assertGreater(data["count"], 0)
                self.assertTrue((Path(tmp) / data["run_id"] / "otchet.html").exists())
                st, status_payload = http_json(f"{base}/api/status", method="GET")
                self.assertEqual(st, 200)
                self.assertEqual(len(status_payload["reports"]), 1)
                report_req = urllib.request.Request(f"{base}{data['report_url']}")
                with urllib.request.urlopen(report_req, timeout=10) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("Срез выдачи Google News", html)
                hz, health = http_json(f"{base}/healthz", method="GET")
                self.assertEqual(hz, 200)
                self.assertTrue(health["ok"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                web.OTCHET_DIR = old_dir
                if old_allow is None:
                    os.environ.pop("ALLOW_LOCAL_HTML", None)
                else:
                    os.environ["ALLOW_LOCAL_HTML"] = old_allow

    def test_network_server_requires_token_and_blocks_local_html(self) -> None:
        old_token = os.environ.get("AUTH_TOKEN")
        old_allow = os.environ.get("ALLOW_LOCAL_HTML")
        os.environ["AUTH_TOKEN"] = "secret-key"
        os.environ.pop("ALLOW_LOCAL_HTML", None)
        httpd, thread, base = start_server()
        try:
            code, payload = http_json(f"{base}/api/status", method="GET")
            self.assertEqual(code, 401)
            self.assertTrue(payload["auth_required"])
            code, payload = http_json(f"{base}/api/status", method="GET", headers={"X-Auth-Token": "secret-key"})
            self.assertEqual(code, 200)
            self.assertTrue(payload["ok"])
            code, payload = http_json(
                f"{base}/api/collect",
                {"from_html": "tests/fixtures/sample_search.html", "pause": 0},
                headers={"X-Auth-Token": "secret-key"},
            )
            self.assertEqual(code, 400)
            self.assertIn("отключён", payload["error"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            if old_token is None:
                os.environ.pop("AUTH_TOKEN", None)
            else:
                os.environ["AUTH_TOKEN"] = old_token
            if old_allow is None:
                os.environ.pop("ALLOW_LOCAL_HTML", None)
            else:
                os.environ["ALLOW_LOCAL_HTML"] = old_allow


if __name__ == "__main__":
    unittest.main()
